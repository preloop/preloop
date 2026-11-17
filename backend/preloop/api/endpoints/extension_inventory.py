"""Installed extensions and account-wide name approvals."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models import models
from preloop.models.crud import crud_discovered_extension, crud_extension_approval
from preloop.models.db.session import get_db_session
from preloop.schemas.agent_discovery import AGENT_KIND_PATTERN, HEX_SHA256_PATTERN
from preloop.schemas.discovered_extension import (
    IDENTIFIER_PATTERN,
    ApprovalState,
    DiscoveredExtensionList,
    ExtensionApprovalList,
    ExtensionApprovalSummary,
    ExtensionApprovalUpdate,
    ExtensionKind,
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
    state: ApprovalState | None = None,
    source_kind: SourceKind | None = None,
    group: Literal["extension"] | None = None,
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
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc
    return DiscoveredExtensionList.model_validate(
        {
            "items": items,
            "next_cursor": next_cursor,
            "completeness": crud_discovered_extension.completeness(
                db, account_id=account.id, fingerprint=fingerprint
            ),
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
        items=[ExtensionApprovalSummary.model_validate(row) for row in rows],
        next_cursor=next_cursor,
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
    return ExtensionApprovalSummary.model_validate(row)


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
    row = crud_extension_approval.set_state(
        db,
        account_id=account.id,
        agent_kind=agent_kind,
        extension_kind=extension_kind,
        extension_id=extension_id,
        decided_by_user_id=current_user.id,
        payload=payload,
    )
    result = ExtensionApprovalSummary.model_validate(row)
    db.commit()
    evaluate_fleet_findings(db, account_id=account.id)
    return result
