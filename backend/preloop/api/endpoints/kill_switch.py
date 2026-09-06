"""API endpoints for the account kill switch (org-level emergency halt).

The kill switch is the big red button (#157): one account-scoped control
that blocks new gateway requests, MCP calls and flow starts. Activation and staged re-enable
are role-restricted and fully audited; the status read is deliberately
available to every authenticated user of the account — knowing that agent
activity is halted is not privileged information, and hiding it would
defeat the point of the console banner.
"""

import logging
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_account_halt,
    crud_user_role,
    crud_role,
    crud_team,
    crud_team_role,
)
from preloop.models.db.session import get_db_session
from preloop.schemas.kill_switch import (
    KillSwitchActivateRequest,
    KillSwitchDeactivateRequest,
    KillSwitchScopeState,
    KillSwitchStatus,
)
from preloop.services.kill_switch import invalidate_kill_switch_cache
from preloop.utils.permissions import require_permission, _rbac_checks_enabled

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/account/kill-switch",
    tags=["Kill Switch"],
)

#: Permission required to toggle the kill switch. In EE builds the RBAC
#: plugin enforces it via the decorator below; in OSS builds the explicit
#: role check in :func:`_ensure_toggle_authorized` enforces the equivalent.
MANAGE_KILL_SWITCH_PERMISSION = "manage_kill_switch"

# Mirrors the plugin detection in preloop.utils.permissions: when the
# proprietary RBAC plugin is installed, the decorator already enforced the
# permission and the OSS role check below must not double-gate EE users.
try:  # pragma: no cover - depends on the proprietary plugin being installed
    from preloop.plugins.proprietary.rbac.permissions import (
        require_permission as _plugin_require_permission,
    )
except ImportError:  # pragma: no cover - OSS build
    _plugin_require_permission = None


def _user_has_kill_switch_permission(db: Session, current_user: models.User) -> bool:
    """Whether one of the user's roles grants the kill-switch permission.

    Data-driven on the seeded role/permission matrix (owner and admin hold
    ``manage_kill_switch``; viewer, editor, executor do not), with the
    ``owner`` system role additionally treated as all-powerful, matching
    the implicit-owner convention of the RBAC layer.
    """
    roles = crud_user_role.get_user_roles(db, user_id=current_user.id)
    offset = 0
    while True:
        teams = crud_team.get_user_teams(
            db, user_id=current_user.id, skip=offset, limit=100
        )
        for team in teams:
            if team.account_id == current_user.account_id:
                roles.extend(crud_team_role.get_team_roles(db, team_id=team.id))
        if len(teams) < 100:
            break
        offset += len(teams)
    for role in roles:
        if role.account_id is not None and role.account_id != current_user.account_id:
            continue
        if role.name == "owner" and role.is_system_role:
            return True
        if any(
            permission.name == MANAGE_KILL_SWITCH_PERMISSION
            for permission in crud_role.get_permissions(db, role_id=role.id)
        ):
            return True
    return False


def _ensure_toggle_authorized(db: Session, current_user: models.User) -> None:
    """Enforce role restriction for kill-switch toggles in OSS builds.

    The kill switch stays role-restricted even with RBAC disabled: it is
    the one control that halts the whole account, so a viewer or editor
    must not be able to flip it, while the account owner, an
    ``owner``/``admin``-role user, or a superuser can always reach it in an
    emergency.

    Args:
        db: Database session.
        current_user: The authenticated user attempting the toggle.

    Raises:
        HTTPException: 403 when the user may not toggle the kill switch.
    """
    if _plugin_require_permission is not None and _rbac_checks_enabled():
        # EE build: the decorator already enforced the permission.
        return

    if current_user.is_superuser:
        return
    account = crud_account.get(db, id=current_user.account_id)
    if account is not None and str(account.primary_user_id) == str(current_user.id):
        return
    if _user_has_kill_switch_permission(db, current_user):
        return
    raise HTTPException(
        status_code=403,
        detail=(f"Insufficient permissions. Required: {MANAGE_KILL_SWITCH_PERMISSION}"),
    )


def _status_response(db: Session, account_id: UUID) -> KillSwitchStatus:
    """Build the banner-facing status payload for an account."""
    from preloop.models.crud import crud_user

    snapshot = crud_account_halt.snapshot_for_account(db, account_id=account_id)
    scopes = []
    for scope, entry in snapshot["scopes"].items():
        username = None
        user_id = entry.get("activated_by_user_id")
        if user_id is not None:
            user = crud_user.get(db, id=user_id)
            username = getattr(user, "username", None)
        scopes.append(
            KillSwitchScopeState(
                scope=scope,
                activated_by_user_id=user_id,
                activated_by_username=username,
                activated_at=entry.get("activated_at"),
                reason=entry.get("reason"),
            )
        )
    return KillSwitchStatus(active=bool(scopes), scopes=scopes)


@router.get("/status", response_model=KillSwitchStatus)
def get_kill_switch_status(
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> KillSwitchStatus:
    """Report the account's current kill-switch state.

    Readable by any authenticated user of the account: the console banner
    polls this, and a halted account must be visible to everyone governing
    it, not only to whoever flipped the switch.
    """
    return _status_response(db, current_user.account_id)


@router.post("/activate", response_model=KillSwitchStatus)
@require_permission("manage_kill_switch")
def activate_kill_switch(
    payload: KillSwitchActivateRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> KillSwitchStatus:
    """Activate the kill switch for one or more traffic scopes.

    Defaults to a full halt (gateway, tools, flows). Activation is
    idempotent per scope and audited: the actor, timestamp, and reason are
    recorded on the halt row and in the audit log, and every process
    enforces the halt within seconds (the serving process immediately).
    """
    _ensure_toggle_authorized(db, current_user)
    scopes: List[str] = list(dict.fromkeys(payload.scopes))
    crud_account_halt.transition_scopes(
        db,
        account_id=current_user.account_id,
        scopes=scopes,
        active=True,
        user_id=current_user.id,
        reason=payload.reason,
    )
    invalidate_kill_switch_cache(current_user.account_id)

    logger.warning(
        "KILL SWITCH ACTIVATED: account=%s scopes=%s by=%s reason=%r",
        current_user.account_id,
        scopes,
        current_user.id,
        payload.reason,
    )
    return _status_response(db, current_user.account_id)


@router.post("/deactivate", response_model=KillSwitchStatus)
@require_permission("manage_kill_switch")
def deactivate_kill_switch(
    payload: KillSwitchDeactivateRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> KillSwitchStatus:
    """Re-enable one or more traffic scopes (staged recovery).

    Each scope lifts independently, so an operator can restore the gateway
    first, verify behavior, then tools, then flows — instead of one
    all-at-once un-halt. Deactivating a scope that is not halted is a
    no-op. Like activation, every transition is audited.
    """
    _ensure_toggle_authorized(db, current_user)
    scopes: List[str] = list(dict.fromkeys(payload.scopes))
    crud_account_halt.transition_scopes(
        db,
        account_id=current_user.account_id,
        scopes=scopes,
        active=False,
        user_id=current_user.id,
        reason=payload.reason,
    )
    invalidate_kill_switch_cache(current_user.account_id)

    logger.warning(
        "KILL SWITCH DEACTIVATED: account=%s scopes=%s by=%s",
        current_user.account_id,
        scopes,
        current_user.id,
    )
    return _status_response(db, current_user.account_id)
