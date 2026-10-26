"""Delivery Report runs for the console (collection and report page)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_or_404
from preloop.models.crud import crud_flow_execution
from preloop.models.db.session import get_db_session
from preloop.models import models
from preloop.schemas.delivery_report import (
    DeliveryReportAttention,
    DeliveryReportDetail,
    DeliveryReportList,
)
from preloop.services import delivery_report_actions, delivery_report_views
from preloop.services.delivery_report_actions import (
    ActionContext,
    ReportActionError,
)
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter(prefix="/delivery-reports", tags=["Delivery Reports"])


@router.get("", response_model=DeliveryReportList)
@require_permission("view_flows")
def list_delivery_reports(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    since: Optional[datetime] = Query(
        None, description="Runs that started at or after this time."
    ),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> DeliveryReportList:
    """Runs of the account's Delivery Report flows that stored a report."""
    account = get_account_or_404(db, current_user)
    rows, total = crud_flow_execution.list_delivery_reports(
        db, account_id=str(account.id), skip=offset, limit=limit, since=since
    )
    return DeliveryReportList(
        items=[delivery_report_views.report_row(row) for row in rows], total=total
    )


@router.get("/attention", response_model=list[DeliveryReportAttention])
@require_permission("view_flows")
def delivery_report_attention(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> list[DeliveryReportAttention]:
    """Pending decisions from up to 100 report runs, independent of collection paging."""
    account = get_account_or_404(db, current_user)
    items = []
    for execution in crud_flow_execution.delivery_reports_with_proposals(
        db, account_id=str(account.id)
    ):
        item = delivery_report_views.report_attention(
            db, account_id=account.id, execution=execution
        )
        if item is not None:
            items.append(item)
    return items


@router.get("/{execution_id}", response_model=DeliveryReportDetail)
@require_permission("view_flows")
def get_delivery_report(
    execution_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> DeliveryReportDetail:
    """One run's full report, with reopen requests that went stale marked."""
    account = get_account_or_404(db, current_user)
    execution = crud_flow_execution.get_delivery_report(
        db, execution_id=execution_id, account_id=str(account.id)
    )
    if execution is None or not isinstance(execution.result, dict):
        raise HTTPException(status_code=404, detail="Delivery report not found")
    return delivery_report_views.report_detail(
        db, account_id=account.id, execution=execution
    )


class DeliveryReportActionRequest(BaseModel):
    """One click on a proposal in the report page.

    ``open_issue`` names the follow-up by ``index``; ``reopen`` and ``close``
    name the issue by ``ref``; ``dismiss`` names the proposal by ``key``
    (``follow_up:<index>`` or ``reopen:<ref>``) and needs a ``reason``.
    """

    kind: Literal["open_issue", "reopen", "dismiss", "close"]
    index: Optional[int] = Field(None, ge=0)
    ref: Optional[str] = None
    key: Optional[str] = None
    reason: Optional[str] = Field(None, max_length=2000)


class DeliveryReportActionResult(BaseModel):
    """The decision stored on the run for the proposal."""

    key: str
    decision: dict


#: The permission each action needs on top of reading the report.
_ACTION_PERMISSIONS = {
    "open_issue": "create_issues",
    "reopen": "edit_issues",
    "dismiss": "edit_issues",
    "close": "edit_issues",
}


@router.post(
    "/{execution_id}/actions",
    response_model=DeliveryReportActionResult,
    responses={
        403: {},
        404: {},
        409: {},
        # Python 3.11 and 3.13 name HTTP 422 differently. Keep the public
        # schema stable across development and CI interpreters.
        422: {"description": "Unprocessable Entity"},
        502: {},
    },
)
@require_permission("view_flows")
def act_on_delivery_report(
    execution_id: UUID,
    body: DeliveryReportActionRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> DeliveryReportActionResult | JSONResponse:
    """Open a proposed follow-up, reopen an issue, or dismiss a proposal.

    The click is the approval: Open issue and Reopen write to the tracker
    whatever the account's ``tracker_writes`` rules say for the run itself.
    A stale reopen request (new commits on the closing pull request, or an
    issue edit after the assessment) is refused with 409 ``stale``. Close
    is always refused with 403 ``close_denied``, and the attempt is logged.
    """

    @require_permission(_ACTION_PERMISSIONS[body.kind])
    def authorize_action(*, db: Session, current_user: models.User) -> None:
        ensure_permission_in_oss(db, current_user, _ACTION_PERMISSIONS[body.kind])

    authorize_action(db=db, current_user=current_user)
    account = get_account_or_404(db, current_user)
    execution = crud_flow_execution.get_delivery_report(
        db, execution_id=execution_id, account_id=str(account.id)
    )
    if execution is None or not isinstance(execution.result, dict):
        raise HTTPException(status_code=404, detail="Delivery report not found")
    ctx = ActionContext(
        db=db, account_id=account.id, user=current_user, execution=execution
    )
    try:
        if body.kind == "close":
            raise delivery_report_actions.refuse_close(ctx, body.ref)
        if body.kind == "open_issue":
            if body.index is None:
                raise ReportActionError("invalid", "open_issue needs index.", 422)
            key = delivery_report_actions.follow_up_key(body.index)
            decision = asyncio.run(
                delivery_report_actions.open_follow_up(ctx, body.index)
            )
        elif body.kind == "reopen":
            if not body.ref:
                raise ReportActionError("invalid", "reopen needs ref.", 422)
            key = delivery_report_actions.reopen_key(body.ref)
            decision = asyncio.run(delivery_report_actions.reopen_issue(ctx, body.ref))
        else:
            reason = (body.reason or "").strip()
            if not body.key or not reason:
                raise ReportActionError("invalid", "dismiss needs key and reason.", 422)
            key = body.key
            decision = delivery_report_actions.dismiss(ctx, body.key, reason)
    except ReportActionError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )
    return DeliveryReportActionResult(key=key, decision=decision)
