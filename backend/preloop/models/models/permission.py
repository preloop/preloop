"""Permission and role models for RBAC system."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Boolean

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .user import User
    from .team import Team


class Permission(Base):
    """Permission definition for fine-grained access control.

    Permissions are the atomic units of access control. Examples:
    - manage_billing
    - manage_users
    - create_flows
    - execute_flows
    - manage_trackers

    Attributes:
        id: Unique identifier for the permission.
        name: Unique permission name (e.g., 'manage_billing').
        description: Human-readable description of what this permission allows.
        category: Category for grouping (e.g., 'billing', 'flows', 'trackers').
        is_active: Whether this permission is currently active.
        created_at: When the permission was created.
    """

    __tablename__ = "permission"

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        comment="Unique permission name",
    )

    description: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Description of what this permission allows"
    )

    category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Category for grouping permissions",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="Whether permission is active"
    )

    # Relationships
    role_permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="permission", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<Permission(name={self.name}, category={self.category})>"


class Role(Base):
    """Role definition containing a set of permissions.

    Roles can be system-defined (predefined) or custom (account-specific).
    System roles are available to all accounts. Custom roles are created
    by account admins.

    System Roles:
    - owner: Full access including billing, user management, account closure
    - admin: Full access except billing and account closure
    - editor: Create/edit flows, tools, trackers, projects
    - executor: Execute flows, trigger tools
    - tracker_manager: Add/edit trackers, sync data
    - analyst: Read-only + compliance/dependency/duplicate detection
    - viewer: Read-only access

    Attributes:
        id: Unique identifier for the role.
        account_id: Account this role belongs to (null for system roles).
        name: Role name (unique within account).
        description: Human-readable description.
        is_system_role: Whether this is a system-defined role.
        created_at: When the role was created.
        updated_at: When the role was last updated.
    """

    __tablename__ = "role"

    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Account this role belongs to (null for system roles)",
    )

    name: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True, comment="Role name"
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="Description of what this role allows"
    )

    is_system_role: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Whether this is a system-defined role",
    )

    # Relationships
    account: Mapped[Optional["Account"]] = relationship("Account")
    role_permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="role", cascade="all, delete-orphan"
    )
    user_roles: Mapped[list["UserRole"]] = relationship(
        "UserRole", back_populates="role", cascade="all, delete-orphan"
    )
    team_roles: Mapped[list["TeamRole"]] = relationship(
        "TeamRole", back_populates="role", cascade="all, delete-orphan"
    )

    # Unique constraint: one role name per account (or per system)
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "name",
            name="uq_account_role_name",
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        role_type = "system" if self.is_system_role else "custom"
        return f"<Role(name={self.name}, type={role_type})>"


class RolePermission(Base):
    """Many-to-many mapping between Roles and Permissions.

    Attributes:
        id: Unique identifier.
        role_id: Reference to the role.
        permission_id: Reference to the permission.
        granted_at: When this permission was granted to the role.
    """

    __tablename__ = "role_permission"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("role.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the role",
    )

    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("permission.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the permission",
    )

    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this permission was granted",
    )

    # Relationships
    role: Mapped["Role"] = relationship("Role", back_populates="role_permissions")
    permission: Mapped["Permission"] = relationship(
        "Permission", back_populates="role_permissions"
    )

    # Unique constraint: one permission per role
    __table_args__ = (
        UniqueConstraint(
            "role_id",
            "permission_id",
            name="uq_role_permission",
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<RolePermission(role_id={self.role_id}, permission_id={self.permission_id})>"


class UserRole(Base):
    """User role assignments.

    Attributes:
        id: Unique identifier.
        user_id: Reference to the user.
        role_id: Reference to the role.
        granted_by: User who granted this role (optional).
        granted_at: When the role was granted.
        access_grant_id: The account access grant that created this role, so
            revoking the grant removes exactly the roles it created.
    """

    __tablename__ = "user_role"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the user",
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("role.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the role",
    )

    granted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who granted this role",
    )

    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the role was granted",
    )

    access_grant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "account_access_grant.id",
            ondelete="CASCADE",
            name="fk_user_role_access_grant",
        ),
        nullable=True,
        index=True,
        comment="Grant that created this role; NULL for a role assigned directly",
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User", foreign_keys=[user_id], back_populates="roles"
    )
    role: Mapped["Role"] = relationship("Role", back_populates="user_roles")
    granter: Mapped[Optional["User"]] = relationship("User", foreign_keys=[granted_by])

    # Unique constraint: one role per user
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "role_id",
            name="uq_user_role",
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<UserRole(user_id={self.user_id}, role_id={self.role_id})>"


class TeamRole(Base):
    """Team role assignments.

    When a role is assigned to a team, all team members inherit that role's
    permissions.

    Attributes:
        id: Unique identifier.
        team_id: Reference to the team.
        role_id: Reference to the role.
        granted_by: User who granted this role (optional).
        granted_at: When the role was granted.
    """

    __tablename__ = "team_role"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("team.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the team",
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("role.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the role",
    )

    granted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who granted this role",
    )

    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the role was granted",
    )

    # Relationships
    team: Mapped["Team"] = relationship("Team", back_populates="team_roles")
    role: Mapped["Role"] = relationship("Role", back_populates="team_roles")
    granter: Mapped[Optional["User"]] = relationship("User")

    # Unique constraint: one role per team
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "role_id",
            name="uq_team_role",
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<TeamRole(team_id={self.team_id}, role_id={self.role_id})>"
