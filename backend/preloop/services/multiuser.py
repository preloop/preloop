"""Core user management extension points for commercial account controls.

Core owns basic controllers. Installed plugins supply optional services through
PluginManager, so disabling proprietary plugins leaves no dependency on them.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_role, crud_user, crud_user_role
from preloop.plugins.base import get_plugin_manager

BASIC_ROLE_NAMES = frozenset({"admin", "member"})


def membership_module() -> Any:
    """Return the optional identity membership overlay."""
    return get_plugin_manager().get_service("multiuser_membership")


def email_exists(db: Session, *, email: str, account_id: UUID) -> bool:
    """Check email uniqueness within the destination account."""
    return crud_user.email_exists(db, email=email, account_id=account_id)


def enforce_seat_available(
    db: Session,
    account_id: UUID,
    *,
    additional_seats: int = 1,
    include_details: bool = True,
) -> None:
    """Apply a registered hosted seat policy before adding a colleague."""
    gate = get_plugin_manager().get_service("multiuser_seat_gate")
    if gate is not None:
        gate(
            db,
            account_id,
            additional_seats=additional_seats,
            include_details=include_details,
        )


def get_billing_service(db: Session) -> Any:
    """Return optional subscription housekeeping supplied by a plugin."""
    factory = get_plugin_manager().get_service("multiuser_billing_service")
    return factory(db) if factory is not None else None


def sync_subscription_quantity(db: Session, account_id: str) -> None:
    """Update an installed billing service after membership changes."""
    billing = get_billing_service(db)
    if billing is not None:
        billing.update_subscription_quantity(account_id)


def advanced_roles_enabled() -> bool:
    """Whether an installed overlay supports advanced role assignments."""
    return get_plugin_manager().get_service("multiuser_advanced_roles") is not None


def is_basic_role(role: models.Role) -> bool:
    """Recognize real basic system roles, including compatible account ownership."""
    return (
        role.account_id is None
        and role.is_system_role
        and role.name in BASIC_ROLE_NAMES | {"owner"}
    )


def ensure_invitation_roles_available(
    db: Session, invitation: models.UserInvitation
) -> None:
    """Reject stored advanced invitations before creating an OSS membership."""
    if advanced_roles_enabled():
        return
    for role_id in (invitation.role_ids or "").split(","):
        if not role_id.strip():
            continue
        role = crud_role.get(db, id=UUID(role_id.strip()))
        if (
            role is not None
            and role.account_id in (None, invitation.account_id)
            and not is_basic_role(role)
        ):
            raise HTTPException(
                402, detail={"code": "upgrade_required", "feature": "rbac"}
            )


def ensure_role_available(
    db: Session, current_user: models.User, role: models.Role
) -> None:
    """Allow basic system roles or delegate advanced grants to the overlay.

    Account scoping is checked by the caller before this edition boundary.
    The existing owner role remains available for ownership compatibility;
    the controller separately requires an existing owner to grant or revoke it.
    """
    if is_basic_role(role):
        return
    validator = get_plugin_manager().get_service("multiuser_advanced_roles")
    if validator is not None:
        validator(db, current_user, role)
        return
    raise HTTPException(
        status_code=402,
        detail={"code": "upgrade_required", "feature": "rbac"},
    )


def assign_default_member_role(
    db: Session, user: models.User, *, granted_by: UUID
) -> None:
    """Give an OSS colleague the member role when no role was supplied."""
    if advanced_roles_enabled() or crud_user_role.get_by_user(db, user_id=user.id):
        return
    role = crud_role.get_by_name(db, name="member")
    if role is None:
        raise HTTPException(503, "Basic roles are not initialized")
    crud_user_role.assign_role(
        db, user_id=user.id, role_id=role.id, granted_by=granted_by
    )
