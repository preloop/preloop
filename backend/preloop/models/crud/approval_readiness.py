"""Account-scoped membership and notification facts for approval readiness."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.utils.permissions import user_holds_permission


def members(db: Session, *, account_id: UUID) -> list[models.User]:
    """Read active members who can sign in to this account."""
    from preloop.api.auth.email_verification import verification_required

    users = db.scalars(
        select(models.User).where(
            models.User.account_id == account_id, models.User.is_active.is_(True)
        )
    ).all()
    return [user for user in users if not verification_required(user, db)]


def can_decide(db: Session, user: models.User) -> bool:
    """Use the console's role resolver, including direct and team roles."""
    from preloop.plugins.account_hooks import AuthorizationContext, authorize

    permissions = ("view_approvals", "decide_approvals")
    context = AuthorizationContext(account_id=user.account_id, db=db, user=user)
    return all(
        (user.is_superuser or user_holds_permission(db, user, permission))
        and authorize(context, permission, None).allowed
        for permission in permissions
    )


def recipients(db: Session, workflow: models.ApprovalWorkflow) -> set[UUID]:
    """Expand direct and team recipients within the workflow's account."""
    ids = {UUID(str(id)) for id in workflow.approver_user_ids or []}
    if workflow.approver_team_ids:
        ids.update(
            db.scalars(
                select(models.TeamMembership.user_id)
                .join(models.Team, models.Team.id == models.TeamMembership.team_id)
                .where(
                    models.Team.account_id == workflow.account_id,
                    models.Team.id.in_(workflow.approver_team_ids),
                )
            ).all()
        )
    return ids


def chat_destinations(
    db: Session, *, account_id: UUID, user_ids: set[UUID]
) -> list[str]:
    """Read enabled private-chat bindings, excluding credentials and addresses."""
    return list(
        db.scalars(
            select(models.ChatConnection.provider)
            .join(
                models.ChatIdentity,
                models.ChatIdentity.connection_id == models.ChatConnection.id,
            )
            .where(
                models.ChatConnection.account_id == account_id,
                models.ChatIdentity.account_id == account_id,
                models.ChatIdentity.user_id.in_(user_ids),
                models.ChatConnection.enabled.is_(True),
            )
        ).unique()
    )
