"""Runner owner grants: allow one flow on one runner until revoked."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.crud import (
    crud_approval_request,
    crud_audit_log,
    crud_flow,
    crud_flow_execution,
)
from preloop.models.crud import owner_consent as crud_owner_consent
from preloop.models.crud import runner_flow_grant as crud_grant
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.db.session import get_db_session as get_db
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter()


class RunnerFlowGrantCreate(BaseModel):
    """Request body for a new grant."""

    flow_id: Optional[UUID] = None
    approval_request_id: Optional[UUID] = None


class RunnerFlowGrantResponse(BaseModel):
    """An active grant as shown on the runner page and the approval list."""

    id: UUID
    runner_id: UUID
    runner_name: Optional[str] = None
    flow_id: UUID
    flow_name: Optional[str] = None
    approval_request_id: Optional[UUID] = None
    created_at: datetime


def _to_responses(
    db: Session, rows: List[models.RunnerFlowGrant]
) -> List[RunnerFlowGrantResponse]:
    """Render grants with runner and flow names, two queries in total.

    Args:
        db: Database session.
        rows: Grant rows.

    Returns:
        Response models in the same order.
    """
    runner_names, flow_names = crud_grant.names_for(db, rows)
    return [
        RunnerFlowGrantResponse(
            id=row.id,
            runner_id=row.runner_id,
            runner_name=runner_names.get(row.runner_id),
            flow_id=row.flow_id,
            flow_name=flow_names.get(row.flow_id),
            approval_request_id=row.approval_request_id,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _to_response(db: Session, row: models.RunnerFlowGrant) -> RunnerFlowGrantResponse:
    """Render one grant.

    Args:
        db: Database session.
        row: Grant row.

    Returns:
        The response model.
    """
    return _to_responses(db, [row])[0]


def _owned_runner(db: Session, runner_id: UUID, user: models.User) -> models.FlowRunner:
    """Return the runner when ``user`` registered it, else raise.

    Args:
        db: Database session.
        runner_id: Runner id.
        user: Current user.

    Returns:
        The runner.

    Raises:
        HTTPException: 404 when missing, 403 when not the owner.
    """
    runner = crud_flow_runner.get(db, id=runner_id, account_id=str(user.account_id))
    if runner is None:
        raise HTTPException(status_code=404, detail="Runner not found")
    if runner.registered_by_user_id != user.id:
        raise HTTPException(
            status_code=403, detail="Only the runner owner can manage flow grants"
        )
    return runner


def _flow_from_approval(
    db: Session, approval_id: UUID, runner_id: UUID, user: models.User
) -> UUID:
    """Resolve the flow of an owner consent approval bound to this owner.

    Args:
        db: Database session.
        approval_id: The ``owner_consent`` approval request.
        runner_id: Runner the grant is for.
        user: Current user, who must be the bound owner.

    Returns:
        The flow id of the approval's execution.

    Raises:
        HTTPException: 404 when the approval is not this owner's runner flow
            request for this runner.
    """
    if not crud_owner_consent.belongs_to_owner(
        db,
        approval_id=approval_id,
        account_id=user.account_id,
        owner_user_id=user.id,
    ):
        raise HTTPException(status_code=404, detail="Approval not found")
    approval = crud_approval_request.get(db, id=approval_id)
    args = (approval.tool_args or {}) if approval is not None else {}
    if approval is None or str(args.get("runner_id")) != str(runner_id):
        raise HTTPException(status_code=404, detail="Approval not found")
    execution = crud_flow_execution.get(db, id=args.get("execution_id"))
    if execution is None or execution.flow_id is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return UUID(str(execution.flow_id))


@router.get(
    "/runners/{runner_id}/flow-grants",
    response_model=List[RunnerFlowGrantResponse],
)
@require_permission("execute_flows")
def list_runner_flow_grants(
    runner_id: UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> List[RunnerFlowGrantResponse]:
    """List flows the owner always allows on this runner."""
    ensure_permission_in_oss(db, current_user, "execute_flows")
    _owned_runner(db, runner_id, current_user)
    rows = crud_grant.list_active(
        db, owner_user_id=current_user.id, runner_id=runner_id
    )
    return _to_responses(db, rows)


@router.get("/runner-flow-grants", response_model=List[RunnerFlowGrantResponse])
@require_permission("execute_flows")
def list_my_runner_flow_grants(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> List[RunnerFlowGrantResponse]:
    """List every active grant the current user gave on their runners."""
    ensure_permission_in_oss(db, current_user, "execute_flows")
    rows = crud_grant.list_active(db, owner_user_id=current_user.id)
    return _to_responses(db, rows)


@router.post(
    "/runners/{runner_id}/flow-grants",
    response_model=RunnerFlowGrantResponse,
)
@require_permission("execute_flows")
def create_runner_flow_grant(
    runner_id: UUID,
    body: RunnerFlowGrantCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> RunnerFlowGrantResponse:
    """Always allow one flow on this runner until the owner revokes it.

    Pass ``approval_request_id`` to grant from an owner consent request (the
    flow may then belong to another account that routes to this runner), or
    ``flow_id`` for a flow in the owner's own account.
    """
    ensure_permission_in_oss(db, current_user, "execute_flows")
    runner = _owned_runner(db, runner_id, current_user)
    if body.approval_request_id is not None:
        flow_id = _flow_from_approval(
            db, body.approval_request_id, runner_id, current_user
        )
        if body.flow_id is not None and body.flow_id != flow_id:
            raise HTTPException(
                status_code=400, detail="flow_id does not match the approval"
            )
    elif body.flow_id is not None:
        if (
            crud_flow.get(db, id=body.flow_id, account_id=current_user.account_id)
            is None
        ):
            raise HTTPException(status_code=404, detail="Flow not found")
        flow_id = body.flow_id
    else:
        raise HTTPException(
            status_code=400, detail="flow_id or approval_request_id is required"
        )
    row, created = crud_grant.grant(
        db,
        account_id=runner.account_id,
        owner_user_id=current_user.id,
        runner_id=runner.id,
        flow_id=flow_id,
        approval_request_id=body.approval_request_id,
    )
    if created:
        crud_audit_log.log_action(
            db,
            account_id=current_user.account_id,
            user_id=current_user.id,
            action="runner_flow_grant_created",
            resource_type="flow_runner",
            resource_id=str(runner.id),
            status="success",
            details={
                "grant_id": str(row.id),
                "flow_id": str(flow_id),
                "approval_request_id": (
                    str(body.approval_request_id) if body.approval_request_id else None
                ),
            },
        )
    return _to_response(db, row)


@router.delete("/runner-flow-grants/{grant_id}", response_model=RunnerFlowGrantResponse)
@require_permission("execute_flows")
def revoke_runner_flow_grant(
    grant_id: UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> RunnerFlowGrantResponse:
    """Revoke a grant; the next run of the flow asks the owner again."""
    ensure_permission_in_oss(db, current_user, "execute_flows")
    row = crud_grant.get_owned(db, grant_id=grant_id, owner_user_id=current_user.id)
    if row is None or row.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Grant not found")
    crud_grant.revoke(db, row, revoked_by_user_id=current_user.id)
    crud_audit_log.log_action(
        db,
        account_id=current_user.account_id,
        user_id=current_user.id,
        action="runner_flow_grant_revoked",
        resource_type="flow_runner",
        resource_id=str(row.runner_id),
        status="success",
        details={"grant_id": str(row.id), "flow_id": str(row.flow_id)},
    )
    return _to_response(db, row)
