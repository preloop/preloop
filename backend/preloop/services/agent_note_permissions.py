"""Human ownership and console-equivalent control facts for agent notes."""

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models.crud import crud_managed_agent, crud_runtime_session, crud_user
from preloop.utils.permissions import (
    _plugin_require_permission,
    _rbac_checks_enabled,
    ensure_permission_in_oss,
)


def human_note_facts(
    db: Session,
    *,
    account_id: str,
    author_agent_id: Any,
    target_agent_id: Any,
    target_session_id: Any,
) -> dict[str, Any]:
    """Resolve owners inside the account and apply the console's control ceiling."""
    author = crud_managed_agent.get_for_account(
        db, account_id=account_id, agent_id=str(author_agent_id)
    )
    target = None
    if target_agent_id:
        target = crud_managed_agent.get_for_account(
            db, account_id=account_id, agent_id=str(target_agent_id)
        )
    elif target_session_id:
        session = crud_runtime_session.get_account_session(
            db, account_id=account_id, runtime_session_id=str(target_session_id)
        )
        if session is not None:
            target = crud_managed_agent.get_for_note_session(
                db, account_id=account_id, runtime_session=session
            )
    owner_id = getattr(author, "owner_user_id", None)
    user = crud_user.get(db, id=owner_id) if owner_id else None
    can_steer = False
    if user is not None and user.is_active and str(user.account_id) == str(account_id):
        if _plugin_require_permission is not None and _rbac_checks_enabled():
            from preloop.plugins.proprietary.rbac.permissions import (  # type: ignore[import-untyped]
                has_permission,
            )

            can_steer = has_permission(user, "control_managed_agent", db)
        else:
            try:
                ensure_permission_in_oss(db, user, "control_managed_agent")
                can_steer = True
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
    return {
        "same_owner": bool(
            owner_id
            and target is not None
            and str(target.owner_user_id) == str(owner_id)
        ),
        "caller_user_can_steer_target": can_steer,
        "note_caller_user_id": str(user.id) if can_steer and user is not None else None,
    }
