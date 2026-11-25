"""Account policy editing and preview; exporting does not install policy."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models import models
from preloop.models.crud import crud_bundle_revision, crud_extension_approval
from preloop.models.crud import extension_enforcement_policy as policies
from preloop.models.db.session import get_db_session
from preloop.schemas.extension_policy import (
    EnforcementPolicy,
    Platform,
    PolicyUpdate,
    RenderedOverlay,
)
from preloop.services.extension_policy_render import RenderRefusalError, render_overlay
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter(prefix="/agents/extension-policy", tags=["Extension enforcement"])


@router.get("/{agent_kind}", response_model=EnforcementPolicy)
@require_permission("view_agents")
def get_policy(
    agent_kind: Literal["claude_code"],
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> EnforcementPolicy:
    """Return revision-zero defaults before the first explicit save."""
    ensure_permission_in_oss(db, current_user, "view_agents")
    row = policies.get(db, account_id=account.id, agent_kind=agent_kind)
    return EnforcementPolicy.model_validate(row) if row else EnforcementPolicy()


@router.put(
    "/{agent_kind}",
    response_model=EnforcementPolicy,
    responses={409: {"description": "Stale revision"}},
)
@require_permission("manage_agents")
def put_policy(
    agent_kind: Literal["claude_code"],
    payload: PolicyUpdate,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    if_match: Annotated[str, Header(pattern=r"^(0|[1-9][0-9]{0,9})$")],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> EnforcementPolicy:
    """Replace the whole document only if its revision still matches."""
    ensure_permission_in_oss(db, current_user, "manage_agents")
    try:
        row = policies.upsert(
            db,
            account_id=account.id,
            agent_kind=agent_kind,
            payload=payload,
            expected_revision=int(if_match),
            user_id=current_user.id,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    result: EnforcementPolicy = EnforcementPolicy.model_validate(row)
    db.commit()
    return result


@router.get(
    "/{agent_kind}/managed-settings",
    response_model=RenderedOverlay,
    responses={
        409: {"description": "Nothing to enforce or hook prerequisite missing"},
        422: {"description": "Unsafe marketplace lockout"},
    },
)
@require_permission("manage_agents")
def preview_policy(
    agent_kind: Literal["claude_code"],
    account: Annotated[models.Account, Depends(get_account_for_user)],
    platform: Platform = "darwin",
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> RenderedOverlay:
    """Render a reviewable drop-in; vendor-console delivery remains unverified."""
    ensure_permission_in_oss(db, current_user, "manage_agents")
    row = policies.get(db, account_id=account.id, agent_kind=agent_kind)
    policy = EnforcementPolicy.model_validate(row) if row else EnforcementPolicy()
    approvals = crud_extension_approval.list_all(db, account_id=account.id)
    hook_registered = any(
        b.app_id == "claude-code" and b.platform == platform
        for b in crud_bundle_revision.list_active(db, account_id=account.id)
    )
    try:
        return render_overlay(
            policy, approvals, platform=platform, hook_registered=hook_registered
        )
    except RenderRefusalError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
