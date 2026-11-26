"""Installed extensions, account-wide approvals and their content pins."""

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models import models
from preloop.models.crud import crud_discovered_extension, crud_extension_approval
from preloop.models.crud.extension_approval import PinConflictError, PinStateError
from preloop.models.db.session import get_db_session
from preloop.schemas.agent_discovery import AGENT_KIND_PATTERN, HEX_SHA256_PATTERN
from preloop.schemas.discovered_extension import (
    IDENTIFIER_PATTERN,
    DiscoveredExtensionList,
    EffectiveState,
    ExtensionApprovalList,
    ExtensionApprovalSummary,
    ExtensionApprovalUpdate,
    ExtensionKind,
    ExtensionPinCreate,
    ExtensionPinSummary,
    ExtensionPolicyEntry,
    ExtensionPolicyFile,
    SourceKind,
)
from preloop.services.fleet_findings import evaluate_fleet_findings
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter()


@router.get("/agents/discovery-extensions", response_model=DiscoveredExtensionList)
@require_permission("view_agents")
def list_extensions(
    account: Annotated[models.Account, Depends(get_account_for_user)],
    fingerprint: str | None = Query(default=None, pattern=HEX_SHA256_PATTERN),
    agent_kind: str | None = Query(default=None, pattern=AGENT_KIND_PATTERN),
    state: EffectiveState | None = None,
    source_kind: SourceKind | None = None,
    group: Literal["extension"] | None = None,
    q: str | None = Query(default=None, max_length=300),
    extension_kind: ExtensionKind | None = None,
    extension_id: str | None = Query(default=None, pattern=IDENTIFIER_PATTERN),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=1024),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DiscoveredExtensionList:
    """List present extensions with their account decisions, unknown by default."""
    ensure_permission_in_oss(db, current_user, "view_agents")
    try:
        items, next_cursor = crud_discovered_extension.list_for_account(
            db,
            account_id=account.id,
            fingerprint=fingerprint,
            agent_kind=agent_kind,
            state=state,
            source_kind=source_kind,
            group=group,
            q=q,
            extension_kind=extension_kind,
            extension_id=extension_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc
    coverage = crud_discovered_extension.coverage(
        db, account_id=account.id, fingerprint=fingerprint
    )
    return DiscoveredExtensionList.model_validate(
        {
            "items": items,
            "next_cursor": next_cursor,
            "completeness": crud_discovered_extension.coverage_completeness(coverage),
            "coverage": coverage,
        }
    )


@router.get("/agents/extension-approvals", response_model=ExtensionApprovalList)
@require_permission("view_agents")
def list_approvals(
    account: Annotated[models.Account, Depends(get_account_for_user)],
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=1024),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ExtensionApprovalList:
    """List durable decisions, including extensions no longer installed."""
    ensure_permission_in_oss(db, current_user, "view_agents")
    try:
        rows, next_cursor = crud_extension_approval.list(
            db, account_id=account.id, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc
    return ExtensionApprovalList(
        items=[
            ExtensionApprovalSummary.model_validate(item)
            for item in crud_extension_approval.summary(db, rows)
        ],
        next_cursor=next_cursor,
    )


def extension_policy_account_ref(account_id: UUID) -> str:
    """Hash the canonical account id; the file keeps only the first 16 hex digits."""
    return hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:16]


@router.get(
    "/agents/extension-approvals/export",
    response_model=ExtensionPolicyFile,
    response_model_by_alias=True,
)
@require_permission("view_agents")
def export_approvals(
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ExtensionPolicyFile:
    """Download every account decision as an offline policy file.

    ``content_hashes`` lists that approval's pinned hashes, oldest first.
    An approval with no pins exports an empty list. The file has no notes,
    user ids, credentials, or device data.
    """
    ensure_permission_in_oss(db, current_user, "view_agents")
    rows = crud_extension_approval.list_all(db, account_id=account.id)
    pins = crud_extension_approval.pins_for(db, approval_ids=[row.id for row in rows])
    return ExtensionPolicyFile(
        account_ref=extension_policy_account_ref(account.id),
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        entries=[
            ExtensionPolicyEntry.model_validate(
                {
                    "agent_kind": row.agent_kind,
                    "extension_kind": row.extension_kind,
                    "extension_id": row.extension_id,
                    "approval_state": row.approval_state,
                    "version_constraint": row.version_constraint,
                    "content_hashes": [
                        pin["content_hash"] for pin in pins.get(row.id, [])
                    ],
                }
            )
            for row in rows
        ],
    )


@router.get(
    "/agents/extension-approvals/{agent_kind}/{extension_kind}/{extension_id}",
    response_model=ExtensionApprovalSummary,
    responses={404: {"description": "Extension approval not found"}},
)
@require_permission("view_agents")
def get_approval(
    account: Annotated[models.Account, Depends(get_account_for_user)],
    agent_kind: str = Path(pattern=AGENT_KIND_PATTERN),
    extension_kind: ExtensionKind = Path(),
    extension_id: str = Path(pattern=IDENTIFIER_PATTERN),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ExtensionApprovalSummary:
    """Read one account decision without traversing unrelated approvals."""
    ensure_permission_in_oss(db, current_user, "view_agents")
    if len(extension_id.encode()) > 128:
        raise HTTPException(
            status_code=422, detail="Extension identifier exceeds 128 bytes"
        )
    row = crud_extension_approval.get(
        db,
        account_id=account.id,
        agent_kind=agent_kind,
        extension_kind=extension_kind,
        extension_id=extension_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Extension approval not found")
    return ExtensionApprovalSummary.model_validate(
        crud_extension_approval.summary(db, [row])[0]
    )


@router.put(
    "/agents/extension-approvals/{agent_kind}/{extension_kind}/{extension_id}",
    response_model=ExtensionApprovalSummary,
)
@require_permission("manage_agents")
def set_approval(
    payload: ExtensionApprovalUpdate,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    agent_kind: str = Path(pattern=AGENT_KIND_PATTERN),
    extension_kind: ExtensionKind = Path(),
    extension_id: str = Path(pattern=IDENTIFIER_PATTERN),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ExtensionApprovalSummary:
    """Replace an account decision without modifying anything on the device."""
    ensure_permission_in_oss(db, current_user, "manage_agents")
    if len(extension_id.encode()) > 128:
        raise HTTPException(
            status_code=422, detail="Extension identifier exceeds 128 bytes"
        )
    try:
        row = crud_extension_approval.set_state(
            db,
            account_id=account.id,
            agent_kind=agent_kind,
            extension_kind=extension_kind,
            extension_id=extension_id,
            decided_by_user_id=current_user.id,
            payload=payload,
        )
    except PinConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = ExtensionApprovalSummary.model_validate(
        crud_extension_approval.summary(db, [row])[0]
    )
    db.commit()
    evaluate_fleet_findings(db, account_id=account.id)
    return result


def _approval_or_404(
    db: Session,
    account: models.Account,
    agent_kind: str,
    extension_kind: str,
    extension_id: str,
) -> models.ExtensionApproval:
    """Resolve an approval inside the caller's account or raise 404."""
    if len(extension_id.encode()) > 128:
        raise HTTPException(
            status_code=422, detail="Extension identifier exceeds 128 bytes"
        )
    row = crud_extension_approval.get(
        db,
        account_id=account.id,
        agent_kind=agent_kind,
        extension_kind=extension_kind,
        extension_id=extension_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Extension approval not found")
    return row


@router.post(
    "/agents/extension-approvals/{agent_kind}/{extension_kind}/{extension_id}/pins",
    response_model=ExtensionPinSummary,
    status_code=201,
    responses={
        404: {"description": "Extension approval not found"},
        409: {"description": "Hash already pinned or pin limit reached"},
        422: {"description": "Approval is not approved or hash is malformed"},
    },
)
@require_permission("manage_agents")
def add_pin(
    payload: ExtensionPinCreate,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    agent_kind: str = Path(pattern=AGENT_KIND_PATTERN),
    extension_kind: ExtensionKind = Path(),
    extension_id: str = Path(pattern=IDENTIFIER_PATTERN),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ExtensionPinSummary:
    """Approve one exact content hash under an approved extension."""
    ensure_permission_in_oss(db, current_user, "manage_agents")
    approval = _approval_or_404(db, account, agent_kind, extension_kind, extension_id)
    try:
        pin = crud_extension_approval.add_pin(
            db, approval=approval, pin=payload, pinned_by_user_id=current_user.id
        )
    except PinStateError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PinConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    pins = crud_extension_approval.pins_for(db, approval_ids=[approval.id])
    result = ExtensionPinSummary.model_validate(
        next(item for item in pins[approval.id] if item["id"] == pin.id)
    )
    db.commit()
    evaluate_fleet_findings(db, account_id=account.id)
    return result


@router.delete(
    "/agents/extension-approvals/{agent_kind}/{extension_kind}/{extension_id}"
    "/pins/{pin_id}",
    status_code=204,
    responses={404: {"description": "Pin not found under this approval"}},
)
@require_permission("manage_agents")
def delete_pin(
    pin_id: UUID,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    agent_kind: str = Path(pattern=AGENT_KIND_PATTERN),
    extension_kind: ExtensionKind = Path(),
    extension_id: str = Path(pattern=IDENTIFIER_PATTERN),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> None:
    """Unpin a hash; deleting the last pin returns to a name approval."""
    ensure_permission_in_oss(db, current_user, "manage_agents")
    approval = _approval_or_404(db, account, agent_kind, extension_kind, extension_id)
    if not crud_extension_approval.delete_pin(db, approval=approval, pin_id=pin_id):
        raise HTTPException(status_code=404, detail="Pin not found")
    db.commit()
    evaluate_fleet_findings(db, account_id=account.id)
