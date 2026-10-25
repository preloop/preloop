"""Delivery Report runs for the console (collection and report page)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_or_404
from preloop.models.crud import crud_flow_execution
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.schemas.delivery_report import (
    DeliveryReportDetail,
    DeliveryReportList,
)
from preloop.services import delivery_report_views
from preloop.utils.permissions import require_permission

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
    current_user: User = Depends(get_current_active_user),
) -> DeliveryReportList:
    """Runs of the account's Delivery Report flows that stored a report."""
    account = get_account_or_404(db, current_user)
    rows, total = crud_flow_execution.list_delivery_reports(
        db, account_id=str(account.id), skip=offset, limit=limit, since=since
    )
    return DeliveryReportList(
        items=[delivery_report_views.report_row(row) for row in rows], total=total
    )


@router.get("/{execution_id}", response_model=DeliveryReportDetail)
@require_permission("view_flows")
def get_delivery_report(
    execution_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
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
