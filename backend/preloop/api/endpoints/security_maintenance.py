"""Authenticated inventory, scan ingest, approval, and resume endpoints."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.schemas.security_maintenance import (
    ApprovalDecisionRequest,
    BaselineAcceptRequest,
    RebuiltInputsRequest,
    ResumeRequest,
    ScanIngestRequest,
    SupportedReleaseCreate,
    SupportedReleaseUpdate,
)
from preloop.services.issue_lifecycle_worker import run_lifecycle_endpoint
from preloop.services.security_maintenance import SecurityMaintenanceService
from preloop.services.security_maintenance_refs import (
    CrossAccountError,
    InvalidTransitionError,
    MissingEvidenceError,
    SourceOutageError,
    StaleCompletionError,
    UnsupportedReleaseError,
)
from preloop.utils.permissions import require_permission

router = APIRouter(prefix="/security-maintenance")


def _http_status(exc: Exception) -> int:
    if isinstance(exc, CrossAccountError):
        return 404
    if isinstance(exc, UnsupportedReleaseError):
        return 404
    if isinstance(exc, MissingEvidenceError):
        return 409
    if isinstance(exc, SourceOutageError):
        return 503
    if isinstance(exc, (InvalidTransitionError, StaleCompletionError)):
        return 409
    return 409


def _service(db: Session, user: models.User) -> SecurityMaintenanceService:
    return SecurityMaintenanceService(db, account_id=user.account_id)


def _reject_managed_credentials(current_user: models.User) -> None:
    """Deny managed execution/agent credentials for human maintenance decisions."""
    api_key = getattr(current_user, "_auth_api_key", None)
    if api_key is None:
        return
    context = api_key.context_data if isinstance(api_key.context_data, dict) else {}
    if context.get("flow_execution_id") or context.get("managed_agent_id"):
        raise HTTPException(403, "managed_credential_cannot_decide")


@router.get("/releases")
@require_permission("view_flows")
def list_releases(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> list[dict[str, Any]]:
    """List this tenant's opted-in supported releases."""
    return _service(db, current_user).list_releases()


@router.post("/releases")
@require_permission("edit_flows")
def create_release(
    body: SupportedReleaseCreate,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Create an explicit opt-in inventory row."""

    async def operation() -> dict[str, Any]:
        try:
            return await _service(db, current_user).create_release(body)
        except (
            CrossAccountError,
            InvalidTransitionError,
            UnsupportedReleaseError,
        ) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)


@router.get("/releases/{release_id}")
@require_permission("view_flows")
def get_release(
    release_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Return one inventory row."""
    try:
        return _service(db, current_user).get_release(release_id)
    except CrossAccountError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch("/releases/{release_id}")
@require_permission("edit_flows")
def update_release(
    release_id: UUID,
    body: SupportedReleaseUpdate,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Update mutable inventory fields. Identity stays fixed."""

    async def operation() -> dict[str, Any]:
        try:
            return await _service(db, current_user).update_release(release_id, body)
        except (CrossAccountError, InvalidTransitionError) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)


@router.post("/releases/{release_id}/baseline")
@require_permission("edit_flows")
def accept_baseline(
    release_id: UUID,
    body: BaselineAcceptRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Accept a baseline from a bound audit execution with stored evidence."""

    async def operation() -> dict[str, Any]:
        try:
            return await _service(db, current_user).accept_baseline(release_id, body)
        except (
            CrossAccountError,
            InvalidTransitionError,
            MissingEvidenceError,
        ) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)


@router.post("/scans")
@require_permission("edit_flows")
def ingest_scan(
    body: ScanIngestRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Ingest a trusted scan. Unsupported product/release names fail closed."""

    async def operation() -> dict[str, Any]:
        try:
            return await _service(db, current_user).ingest_scan(body)
        except (
            CrossAccountError,
            InvalidTransitionError,
            SourceOutageError,
            UnsupportedReleaseError,
        ) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)


@router.get("/items")
@require_permission("view_flows")
def list_items(
    release_id: UUID | None = None,
    state: str | None = None,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> list[dict[str, Any]]:
    """List durable remediation items."""
    return _service(db, current_user).list_items(release_id=release_id, state=state)


@router.get("/items/{item_id}")
@require_permission("view_flows")
def get_item(
    item_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Return one work item. Reconciliation runs in the background, not here."""
    try:
        return _service(db, current_user).get_item(item_id)
    except CrossAccountError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/items/{item_id}/decisions")
@require_permission("view_flows")
def list_decisions(
    item_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> list[dict[str, Any]]:
    """Return append-only decision history."""
    try:
        return _service(db, current_user).list_decisions(item_id)
    except CrossAccountError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/items/{item_id}/approve")
@require_permission("decide_approvals")
def approve_item(
    item_id: UUID,
    body: ApprovalDecisionRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Record a human approval through the platform approval workflow."""

    async def operation() -> dict[str, Any]:
        try:
            _reject_managed_credentials(current_user)
            return await _service(db, current_user).decide_approval(
                item_id,
                body,
                actor_user_id=current_user.id,
                approved=True,
            )
        except (
            CrossAccountError,
            InvalidTransitionError,
            SourceOutageError,
        ) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)


@router.post("/items/{item_id}/deny")
@require_permission("decide_approvals")
def deny_item(
    item_id: UUID,
    body: ApprovalDecisionRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Hold the item after a human denial. History is preserved."""

    async def operation() -> dict[str, Any]:
        try:
            _reject_managed_credentials(current_user)
            return await _service(db, current_user).decide_approval(
                item_id,
                body,
                actor_user_id=current_user.id,
                approved=False,
            )
        except (CrossAccountError, InvalidTransitionError) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)


@router.post("/items/{item_id}/escalate")
@require_permission("decide_approvals")
def escalate_item(
    item_id: UUID,
    body: ApprovalDecisionRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Escalate a held or expired approval. Never auto-releases."""

    async def operation() -> dict[str, Any]:
        try:
            _reject_managed_credentials(current_user)
            return await _service(db, current_user).escalate_item(
                item_id, body, actor_user_id=current_user.id
            )
        except (CrossAccountError, InvalidTransitionError) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)


@router.post("/items/{item_id}/resume")
@require_permission("edit_flows")
def resume_item(
    item_id: UUID,
    body: ResumeRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Human retry. Prior decisions stay append-only."""

    async def operation() -> dict[str, Any]:
        try:
            _reject_managed_credentials(current_user)
            return await _service(db, current_user).resume(
                item_id, body, actor_user_id=current_user.id
            )
        except (
            CrossAccountError,
            InvalidTransitionError,
            SourceOutageError,
        ) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)


@router.post("/items/{item_id}/build")
@require_permission("edit_flows")
def submit_rebuilt_inputs(
    item_id: UUID,
    body: RebuiltInputsRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Submit a rebuilt SBOM for the published commit. The controller does not rebuild."""

    async def operation() -> dict[str, Any]:
        try:
            _reject_managed_credentials(current_user)
            return await _service(db, current_user).submit_rebuilt_inputs(
                item_id, body, actor_user_id=current_user.id
            )
        except (
            CrossAccountError,
            InvalidTransitionError,
            SourceOutageError,
        ) as exc:
            raise HTTPException(_http_status(exc), str(exc)) from exc

    return run_lifecycle_endpoint(operation)
