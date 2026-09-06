"""Authenticated lifecycle refinement, readiness and audit recovery endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution, crud_issue_lifecycle
from preloop.models.db.session import get_db_session
from preloop.schemas.issue_lifecycle import ReadinessContract
from preloop.services.issue_lifecycle_runtime import build_lifecycle_service
from preloop.utils.permissions import require_permission

router = APIRouter(prefix="/issues/{issue_id}/lifecycle")


class ReadyRequest(BaseModel):
    """The reviewer approves one exact issue revision."""

    issue_revision: str


@router.get("")
@require_permission("view_issues")
async def lifecycle_status(
    issue_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Read live scope plus durable readiness, pickup and audit history."""
    try:
        service = await build_lifecycle_service(
            db,
            issue_id=issue_id,
            account_id=current_user.account_id,
            current_user=current_user,
        )
        snapshot = await service.provider.issue(service.number)
        rows = crud_issue_lifecycle.list_for_issue(
            db, account_id=current_user.account_id, issue_id=issue_id
        )
        return {
            "issue_revision": snapshot.revision,
            "issue_url": snapshot.url,
            "history": [
                {
                    "kind": row.kind,
                    "revision": row.revision,
                    "state": row.state,
                    "execution_id": str(row.execution_id) if row.execution_id else None,
                    "data": row.data,
                }
                for row in rows
            ],
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/refine")
@require_permission("edit_issues")
async def refine_issue(
    issue_id: UUID,
    contract: ReadinessContract,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Store a reviewable proposal; this operation cannot launch implementation."""
    try:
        service = await build_lifecycle_service(
            db,
            issue_id=issue_id,
            account_id=current_user.account_id,
            current_user=current_user,
        )
        return await service.refine(contract)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/ready")
@require_permission("edit_issues")
async def ready_issue(
    issue_id: UUID,
    request: ReadyRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Apply the configured readiness policy once to an approved scope."""
    try:
        service = await build_lifecycle_service(
            db,
            issue_id=issue_id,
            account_id=current_user.account_id,
            current_user=current_user,
        )
        return await service.ready(request.issue_revision)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/audit/reconcile")
@require_permission("execute_flows")
async def reconcile_audit(
    issue_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Recover dispatch/publication after a transient provider or worker failure."""
    from preloop.services.flow_trigger_service import FlowTriggerService

    try:
        service = await build_lifecycle_service(
            db,
            issue_id=issue_id,
            account_id=current_user.account_id,
            current_user=current_user,
        )
        flow_id = service.policy.get("audit_flow_id")
        flow = (
            crud_flow.get(db, id=UUID(flow_id), account_id=current_user.account_id)
            if flow_id
            else None
        )
        if not flow or not flow.is_enabled:
            raise ValueError("audit_flow_not_configured")
        trigger = FlowTriggerService(db)

        async def dispatch(execution: models.FlowExecution) -> None:
            await trigger._start_flow_execution(
                flow,
                execution.trigger_event_details,
                None,
                precreated_execution=execution,
            )

        execution = await service.schedule_audit(flow, dispatch)
        if execution and execution.status in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
            "ABORTED",
        }:
            # Reload after any concurrent worker update, through the CRUD layer.
            execution = crud_flow_execution.get(db, id=execution.id)
            return await service.finish_audit(execution)
        return {
            "execution_id": str(execution.id) if execution else None,
            "state": execution.status if execution else "no_pending_merge_audit",
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class DeploymentVerificationRequest(BaseModel):
    """An authenticated deployment record, never inferred from issue closure."""

    merge_sha: str
    target: str
    deployed_revision: str
    deployment_evidence: str


@router.post("/deployment/verify")
@require_permission("execute_flows")
async def verify_deployment(
    issue_id: UUID,
    request: DeploymentVerificationRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Trigger a separately approved target/scope after recording deployment."""
    import re

    from preloop.services.flow_trigger_service import FlowTriggerService

    if (
        not re.fullmatch(r"[0-9a-f]{40}", request.deployed_revision)
        or not re.fullmatch(r"[0-9a-f]{40}", request.merge_sha)
        or not request.deployment_evidence
    ):
        raise HTTPException(
            422, "Exact merge/deployed revisions and deployment evidence are required"
        )
    try:
        service = await build_lifecycle_service(
            db,
            issue_id=issue_id,
            account_id=current_user.account_id,
            current_user=current_user,
        )
        target = (service.policy.get("deployment_targets") or {}).get(
            request.target
        ) or {}
        flow = (
            crud_flow.get(
                db, id=UUID(target["flow_id"]), account_id=current_user.account_id
            )
            if target.get("flow_id")
            else None
        )
        if flow is None:
            raise ValueError("deployment_scope_not_authorized")
        trigger = FlowTriggerService(db)

        async def dispatch(execution: models.FlowExecution) -> None:
            await trigger._start_flow_execution(
                flow,
                execution.trigger_event_details,
                None,
                precreated_execution=execution,
            )

        execution = await service.schedule_deployment_audit(
            flow=flow, dispatch=dispatch, **request.model_dump()
        )
        if execution.status in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "TIMED_OUT",
            "ABORTED",
        }:
            return await service.finish_audit(execution)
        return {"execution_id": str(execution.id), "state": execution.status}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
