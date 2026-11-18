"""CRUD operations for Permission and Role models."""

import uuid
from typing import List, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from preloop.models import models

from .base import CRUDBase

LAST_OWNER_MESSAGE = (
    "This change would leave the account without an owner. "
    "Make another member an owner first."
)


class LastOwnerError(Exception):
    """A role or membership change would leave an account with no owner.

    The API maps it to HTTP 409 with code ``last_owner``.
    """

    code = "last_owner"

    def __init__(self) -> None:
        """Carry the fixed, caller-safe message."""
        super().__init__(LAST_OWNER_MESSAGE)


def _owner_role_ids(db: Session) -> set[uuid.UUID]:
    """The global system ``owner`` role ids."""
    return set(
        db.scalars(
            select(models.Role.id).where(
                models.Role.name == "owner",
                models.Role.account_id.is_(None),
                models.Role.is_system_role.is_(True),
            )
        )
    )


def ensure_owner_remains(
    db: Session,
    *,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: Optional[uuid.UUID] = None,
) -> None:
    """Refuse a change that would leave ``account_id`` with no active owner.

    The account row is locked first, so two concurrent removals in the same
    account are serialized and the second one counts after the first commits.

    Args:
        db: Session the change is staged in; the lock lasts until it ends.
        account_id: Account the membership belongs to.
        user_id: Membership losing the role, or being deactivated or deleted.
        role_id: The role being removed; None when the whole membership goes.

    Raises:
        LastOwnerError: When ``user_id`` is the only active owner left.

    Core has no owner protection: without the RBAC plugin every member has
    full access and roles carry no rights, so the guard applies only while
    role-based access is enforced.
    """
    from preloop.utils import permissions

    if permissions.members_have_full_access() or not (
        permissions._rbac_checks_enabled()
    ):
        return
    db.scalar(
        select(models.Account.id)
        .where(models.Account.id == account_id)
        .with_for_update()
    )
    owners = _owner_role_ids(db)
    if not owners or (role_id is not None and role_id not in owners):
        return
    holds = db.scalar(
        select(func.count())
        .select_from(models.UserRole)
        .where(models.UserRole.user_id == user_id, models.UserRole.role_id.in_(owners))
    )
    if not holds:
        return
    others = db.scalar(
        select(func.count(func.distinct(models.User.id)))
        .select_from(models.UserRole)
        .join(models.User, models.User.id == models.UserRole.user_id)
        .where(
            models.User.account_id == account_id,
            models.User.is_active.is_(True),
            models.User.id != user_id,
            models.UserRole.role_id.in_(owners),
        )
    )
    if not others:
        raise LastOwnerError()


class CRUDPermission(CRUDBase[models.Permission]):
    """CRUD operations for Permission model."""

    def get_by_name(self, db: Session, *, name: str) -> Optional[models.Permission]:
        """Get permission by name.

        Args:
            db: Database session.
            name: Permission name.

        Returns:
            Permission if found, None otherwise.
        """
        return (
            db.query(models.Permission).filter(models.Permission.name == name).first()
        )

    def get_by_category(
        self, db: Session, *, category: str, skip: int = 0, limit: int = 100
    ) -> List[models.Permission]:
        """Get permissions by category.

        Args:
            db: Database session.
            category: Permission category.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of permissions.
        """
        return (
            db.query(models.Permission)
            .filter(models.Permission.category == category, models.Permission.is_active)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_active(
        self, db: Session, *, skip: int = 0, limit: int = 1000
    ) -> List[models.Permission]:
        """Get all active permissions.

        Args:
            db: Database session.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of active permissions.
        """
        return (
            db.query(models.Permission)
            .filter(models.Permission.is_active)
            .offset(skip)
            .limit(limit)
            .all()
        )


class CRUDRole(CRUDBase[models.Role]):
    """CRUD operations for Role model."""

    def get_effective_permissions(
        self, db: Session, *, user_id: uuid.UUID, account_id: uuid.UUID
    ) -> set[str]:
        """Read current active local role permissions, without identity-map caches.

        System roles require null account ownership and the genuine system
        marker. Custom roles and team membership must belong to this account.
        Role names never confer implicit permissions.
        """
        direct = db.query(models.UserRole.role_id).filter(
            models.UserRole.user_id == user_id
        )
        team = (
            db.query(models.TeamRole.role_id)
            .join(models.Team, models.Team.id == models.TeamRole.team_id)
            .join(
                models.TeamMembership,
                models.TeamMembership.team_id == models.Team.id,
            )
            .filter(
                models.Team.account_id == account_id,
                models.TeamMembership.user_id == user_id,
            )
        )
        permissions = (
            db.query(models.Permission.name)
            .join(
                models.RolePermission,
                models.RolePermission.permission_id == models.Permission.id,
            )
            .join(models.Role, models.Role.id == models.RolePermission.role_id)
            .join(models.User, models.User.id == user_id)
            .join(models.Account, models.Account.id == models.User.account_id)
            .filter(
                models.User.account_id == account_id,
                models.User.is_active.is_(True),
                models.Account.is_active.is_(True),
                models.Permission.is_active.is_(True),
                or_(
                    and_(
                        models.Role.account_id == account_id,
                        models.Role.is_system_role.is_(False),
                    ),
                    and_(
                        models.Role.account_id.is_(None),
                        models.Role.is_system_role.is_(True),
                    ),
                ),
                or_(models.Role.id.in_(direct), models.Role.id.in_(team)),
            )
            .distinct()
            .all()
        )
        return {name for (name,) in permissions}

    def get_by_name(
        self, db: Session, *, name: str, account_id: Optional[str] = None
    ) -> Optional[models.Role]:
        """Get role by name.

        Args:
            db: Database session.
            name: Role name.
            account_id: Account ID for custom roles (None for system roles).

        Returns:
            Role if found, None otherwise.
        """
        query = db.query(models.Role).filter(models.Role.name == name)
        if account_id is not None:
            query = query.filter(models.Role.account_id == account_id)
        else:
            query = query.filter(models.Role.account_id.is_(None))
        return query.first()

    def get_system_roles(
        self, db: Session, *, skip: int = 0, limit: int = 100
    ) -> List[models.Role]:
        """Get all system roles.

        Args:
            db: Database session.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of system roles.
        """
        return (
            db.query(models.Role)
            .filter(models.Role.is_system_role)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_custom_roles(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[models.Role]:
        """Get custom roles for an account.

        Args:
            db: Database session.
            account_id: Account ID.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of custom roles.
        """
        return (
            db.query(models.Role)
            .filter(
                models.Role.account_id == account_id,
                models.Role.is_system_role.is_(False),
            )
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_all_for_account(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int | None = 100
    ) -> List[models.Role]:
        """Get all roles available to an account (system + custom).

        Args:
            db: Database session.
            account_id: Account ID.
            skip: Number of records to skip.
            limit: Maximum number of records, or None for the complete catalog.

        Returns:
            List of roles (system + account's custom roles).
        """
        return (
            db.query(models.Role)
            .filter(
                (
                    models.Role.is_system_role.is_(True)
                    & models.Role.account_id.is_(None)
                )
                | (models.Role.account_id == account_id)
            )
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_permissions(
        self, db: Session, *, role_id: uuid.UUID
    ) -> List[models.Permission]:
        """Get all permissions for a role.

        Args:
            db: Database session.
            role_id: Role ID.

        Returns:
            List of permissions.
        """
        return (
            db.query(models.Permission)
            .join(models.RolePermission)
            .filter(models.RolePermission.role_id == role_id)
            .all()
        )

    def assign_permission(
        self, db: Session, *, role_id: uuid.UUID, permission_id: uuid.UUID
    ) -> models.RolePermission:
        """Assign a permission to a role.

        Args:
            db: Database session.
            role_id: Role ID.
            permission_id: Permission ID.

        Returns:
            Created RolePermission.
        """
        role_perm = models.RolePermission(
            id=uuid.uuid4(), role_id=role_id, permission_id=permission_id
        )
        db.add(role_perm)
        db.commit()
        db.refresh(role_perm)
        return role_perm

    def remove_permission(
        self, db: Session, *, role_id: uuid.UUID, permission_id: uuid.UUID
    ) -> bool:
        """Remove a permission from a role.

        Args:
            db: Database session.
            role_id: Role ID.
            permission_id: Permission ID.

        Returns:
            True if removed, False if not found.
        """
        role_perm = (
            db.query(models.RolePermission)
            .filter(
                models.RolePermission.role_id == role_id,
                models.RolePermission.permission_id == permission_id,
            )
            .first()
        )
        if role_perm:
            db.delete(role_perm)
            db.commit()
            return True
        return False


class CRUDUserRole(CRUDBase[models.UserRole]):
    """CRUD operations for UserRole model."""

    def get_by_user(self, db: Session, *, user_id: uuid.UUID) -> List[models.UserRole]:
        """Get all user role assignments for a user.

        Args:
            db: Database session.
            user_id: User ID.

        Returns:
            List of UserRole objects.
        """
        return (
            db.query(models.UserRole).filter(models.UserRole.user_id == user_id).all()
        )

    def get_user_roles(self, db: Session, *, user_id: uuid.UUID) -> List[models.Role]:
        """Get all roles for a user.

        Args:
            db: Database session.
            user_id: User ID.

        Returns:
            List of roles.
        """
        return (
            db.query(models.Role)
            .join(models.UserRole)
            .filter(models.UserRole.user_id == user_id)
            .all()
        )

    def assign_role(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        granted_by: Optional[uuid.UUID] = None,
    ) -> models.UserRole:
        """Assign a role to a user.

        Args:
            db: Database session.
            user_id: User ID.
            role_id: Role ID.
            granted_by: User who granted the role (optional).

        Returns:
            Created UserRole.
        """
        user_role = models.UserRole(
            id=uuid.uuid4(), user_id=user_id, role_id=role_id, granted_by=granted_by
        )
        db.add(user_role)
        db.commit()
        db.refresh(user_role)
        return user_role

    def remove_role(
        self, db: Session, *, user_id: uuid.UUID, role_id: uuid.UUID
    ) -> bool:
        """Remove a role from a user.

        Args:
            db: Database session.
            user_id: User ID.
            role_id: Role ID.

        Returns:
            True if removed, False if not found.

        Raises:
            LastOwnerError: When this would remove the account's last owner.
        """
        user = db.get(models.User, user_id)
        if user is not None:
            ensure_owner_remains(
                db, account_id=user.account_id, user_id=user_id, role_id=role_id
            )
        user_role = (
            db.query(models.UserRole)
            .filter(
                models.UserRole.user_id == user_id, models.UserRole.role_id == role_id
            )
            .first()
        )
        if user_role:
            db.delete(user_role)
            db.commit()
            return True
        return False


class CRUDTeamRole(CRUDBase[models.TeamRole]):
    """CRUD operations for TeamRole model."""

    def get_team_roles(self, db: Session, *, team_id: uuid.UUID) -> List[models.Role]:
        """Get all roles for a team.

        Args:
            db: Database session.
            team_id: Team ID.

        Returns:
            List of roles.
        """
        return (
            db.query(models.Role)
            .join(models.TeamRole)
            .filter(models.TeamRole.team_id == team_id)
            .all()
        )

    def get_roles_for_teams(
        self, db: Session, *, account_id: uuid.UUID, team_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[models.Role]]:
        """Batch role projections for teams belonging to the requesting account."""
        result: dict[uuid.UUID, list[models.Role]] = {}
        if not team_ids:
            return result
        rows = (
            db.query(models.TeamRole.team_id, models.Role)
            .join(models.Role, models.Role.id == models.TeamRole.role_id)
            .join(models.Team, models.Team.id == models.TeamRole.team_id)
            .filter(
                models.Team.account_id == account_id,
                models.Team.id.in_(team_ids),
                (models.Role.account_id == account_id)
                | (
                    models.Role.account_id.is_(None)
                    & models.Role.is_system_role.is_(True)
                ),
            )
            .all()
        )
        for team_id, role in rows:
            result.setdefault(team_id, []).append(role)
        return result

    def assign_role(
        self,
        db: Session,
        *,
        team_id: uuid.UUID,
        role_id: uuid.UUID,
        granted_by: Optional[uuid.UUID] = None,
    ) -> models.TeamRole:
        """Assign a role to a team.

        Args:
            db: Database session.
            team_id: Team ID.
            role_id: Role ID.
            granted_by: User who granted the role (optional).

        Returns:
            Created TeamRole.
        """
        team_role = models.TeamRole(
            id=uuid.uuid4(), team_id=team_id, role_id=role_id, granted_by=granted_by
        )
        db.add(team_role)
        db.commit()
        db.refresh(team_role)
        return team_role

    def remove_role(
        self, db: Session, *, team_id: uuid.UUID, role_id: uuid.UUID
    ) -> bool:
        """Remove a role from a team.

        Args:
            db: Database session.
            team_id: Team ID.
            role_id: Role ID.

        Returns:
            True if removed, False if not found.
        """
        team_role = (
            db.query(models.TeamRole)
            .filter(
                models.TeamRole.team_id == team_id, models.TeamRole.role_id == role_id
            )
            .first()
        )
        if team_role:
            db.delete(team_role)
            db.commit()
            return True
        return False


# Create instances
crud_permission = CRUDPermission(models.Permission)
crud_role = CRUDRole(models.Role)
crud_user_role = CRUDUserRole(models.UserRole)
crud_team_role = CRUDTeamRole(models.TeamRole)
