"""Account-scoped membership and notification facts for approval readiness."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.utils.permissions import members_have_full_access


def members(db: Session, *, account_id: UUID) -> list[models.User]:
    """Read active members who can sign in to this account."""
    from preloop.api.auth.email_verification import verification_required

    users = db.scalars(
        select(models.User).where(
            models.User.account_id == account_id, models.User.is_active.is_(True)
        )
    ).all()
    return [user for user in users if not verification_required(user, db)]


def deciders(db: Session, users: list[models.User]) -> list[models.User]:
    """Batch role grants, then retain the account authorizer's per-user gate."""
    from preloop.plugins.account_hooks import AuthorizationContext, authorize

    if not users:
        return []
    account_id = users[0].account_id
    permissions = ("view_approvals", "decide_approvals")
    candidates = users
    if not members_have_full_access():
        conditions = []
        for permission in permissions:
            roles = select(models.Role.id).where(
                or_(
                    models.Role.account_id.is_(None),
                    models.Role.account_id == account_id,
                ),
                or_(
                    (models.Role.name == "owner")
                    & models.Role.is_system_role.is_(True),
                    models.Role.id.in_(
                        select(models.RolePermission.role_id)
                        .join(
                            models.Permission,
                            models.Permission.id == models.RolePermission.permission_id,
                        )
                        .where(models.Permission.name == permission)
                    ),
                ),
            )
            direct = select(models.UserRole.user_id).where(
                models.UserRole.role_id.in_(roles)
            )
            teams = (
                select(models.TeamMembership.user_id)
                .join(models.Team, models.Team.id == models.TeamMembership.team_id)
                .join(models.TeamRole, models.TeamRole.team_id == models.Team.id)
                .where(
                    models.Team.account_id == account_id,
                    models.TeamRole.role_id.in_(roles),
                )
            )
            conditions.append(
                or_(
                    models.User.id.in_(direct),
                    models.User.id.in_(teams),
                    models.User.is_superuser.is_(True),
                )
            )
        eligible = set(
            db.scalars(
                select(models.User.id).where(
                    models.User.account_id == account_id,
                    models.User.id.in_([user.id for user in users]),
                    *conditions,
                )
            )
        )
        candidates = [user for user in users if user.id in eligible]
    return [
        user
        for user in candidates
        if all(
            authorize(
                AuthorizationContext(account_id=account_id, db=db, user=user),
                permission,
                None,
            ).allowed
            for permission in permissions
        )
    ]


def preferences(
    db: Session, *, account_id: UUID, user_ids: set[UUID]
) -> dict[UUID, models.NotificationPreferences]:
    """Read recipient preferences in one account-scoped query."""
    rows = db.scalars(
        select(models.NotificationPreferences)
        .join(models.User)
        .where(
            models.User.account_id == account_id,
            models.NotificationPreferences.user_id.in_(user_ids),
        )
    )
    return {row.user_id: row for row in rows}


def muted_user_ids(
    db: Session, *, account_id: UUID, user_ids: set[UUID], agent_id: UUID | None
) -> set[UUID]:
    """Both active bypass modes mute notifications, regardless of strength."""
    return set(
        db.scalars(
            select(models.ApprovalBypass.user_id).where(
                models.ApprovalBypass.account_id == account_id,
                models.ApprovalBypass.user_id.in_(user_ids),
                models.ApprovalBypass.revoked_at.is_(None),
                models.ApprovalBypass.expires_at > datetime.utcnow(),
                or_(
                    models.ApprovalBypass.managed_agent_id.is_(None),
                    models.ApprovalBypass.managed_agent_id == agent_id,
                ),
            )
        )
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
