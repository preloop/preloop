"""Core user management extension points for commercial account controls.

Core owns the basic controllers for users, invitations and teams. Core applies
no access control between users of the same account: every active member can
do everything in it. Installed plugins supply optional services through
PluginManager, so disabling proprietary plugins leaves no dependency on them:

- ``multiuser_seat_gate``: hosted seat policy before a membership is added.
- ``multiuser_billing_service``: subscription housekeeping after changes.
- ``multiuser_membership``: canonical identity membership linking.
- ``multiuser_access_control``: roles, owner protection and other access
  rules, implementing :class:`AccessControl`.
"""

from typing import Any, Protocol
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_user
from preloop.plugins.base import get_plugin_manager


class AccessControl(Protocol):
    """Access rules a commercial overlay layers over the core controllers."""

    def validate_invitation_roles(
        self, db: Session, current_user: models.User, role_ids: list[UUID]
    ) -> list[UUID]:
        """Validate roles an invitation will grant and return the accepted ids."""
        ...

    def apply_invitation_roles(
        self, db: Session, user: models.User, invitation: models.UserInvitation
    ) -> None:
        """Grant the stored invitation roles to the membership it created."""
        ...

    def ensure_can_remove_member(
        self, db: Session, current_user: models.User, user: models.User
    ) -> None:
        """Raise when ``current_user`` may not deactivate or delete ``user``."""
        ...


def membership_module() -> Any:
    """Return the optional identity membership overlay."""
    return get_plugin_manager().get_service("multiuser_membership")


def access_control() -> AccessControl | None:
    """Return the optional access control overlay."""
    return get_plugin_manager().get_service("multiuser_access_control")


def email_exists(db: Session, *, email: str, account_id: UUID) -> bool:
    """Check email uniqueness within the destination account."""
    return crud_user.email_exists(db, email=email, account_id=account_id)


def enforce_seat_available(
    db: Session,
    account_id: UUID,
    *,
    additional_seats: int = 1,
    include_details: bool = True,
    invitation_email: str | None = None,
) -> None:
    """Apply a registered hosted seat policy before adding a colleague."""
    gate = get_plugin_manager().get_service("multiuser_seat_gate")
    if gate is not None:
        invitation_context = (
            {"invitation_email": invitation_email}
            if invitation_email is not None
            else {}
        )
        gate(
            db,
            account_id,
            additional_seats=additional_seats,
            include_details=include_details,
            **invitation_context,
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


def validate_invitation_roles(
    db: Session, current_user: models.User, role_ids: list[UUID] | None
) -> list[UUID]:
    """Accept invitation roles only when an access control overlay is installed.

    Raises:
        HTTPException: 402 ``upgrade_required`` when roles are requested
            without an overlay, since roles are not part of the core edition.
    """
    if not role_ids:
        return []
    overlay = access_control()
    if overlay is None:
        raise HTTPException(
            status_code=402,
            detail={"code": "upgrade_required", "feature": "rbac"},
        )
    return overlay.validate_invitation_roles(db, current_user, list(role_ids))


def apply_invitation_roles(
    db: Session, user: models.User, invitation: models.UserInvitation
) -> None:
    """Let the overlay grant stored invitation roles; core grants none."""
    overlay = access_control()
    if overlay is not None and invitation.role_ids:
        overlay.apply_invitation_roles(db, user, invitation)


def ensure_can_remove_member(
    db: Session, current_user: models.User, user: models.User
) -> None:
    """Apply overlay removal rules; core lets any member remove another."""
    overlay = access_control()
    if overlay is not None:
        overlay.ensure_can_remove_member(db, current_user, user)
