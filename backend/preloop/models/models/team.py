"""Team and TeamMembership models for permissions and approval workflows."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .user import User
    from .permission import TeamRole


class Team(Base):
    """Team model for grouping users (permissions + approval workflows).

    Teams serve dual purposes:
    1. Permission grouping: Teams can be assigned roles, and all members
       inherit those permissions
    2. Approval workflows: Teams can be required approvers in multi-stage
       or consensus approval workflows

    Examples:
    - Engineering team (has editor role, required for prod deployments)
    - Security team (has analyst role, required for security-sensitive changes)
    - Management team (consensus approval for high-value transactions)

    Attributes:
        id: Unique identifier for the team.
        account_id: The account this team belongs to.
        name: Human-readable name for the team.
        description: Optional description of the team's purpose.
        created_at: When the team was created.
        updated_at: When the team was last modified.
    """

    __tablename__ = "team"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The account this team belongs to",
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Human-readable name for the team",
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Optional description of the team's purpose",
    )

    # Relationships
    account: Mapped["Account"] = relationship("Account", back_populates="teams")
    memberships: Mapped[list["TeamMembership"]] = relationship(
        "TeamMembership",
        back_populates="team",
        cascade="all, delete-orphan",
    )
    team_roles: Mapped[list["TeamRole"]] = relationship(
        "TeamRole",
        back_populates="team",
        cascade="all, delete-orphan",
    )

    # Unique constraint: one team name per account
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "name",
            name="uq_account_team_name",
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<Team(name={self.name}, account_id={self.account_id})>"


class TeamMembership(Base):
    """Many-to-many relationship between Users and Teams.

    This junction table allows users to be members of multiple teams
    and teams to have multiple members.

    Attributes:
        id: Unique identifier for the membership.
        team_id: Reference to the team.
        user_id: Reference to the user.
        role: Optional role within the team (e.g., 'member', 'lead').
        added_by: User who added this member (optional).
        added_at: When the user was added to the team.
    """

    __tablename__ = "team_membership"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("team.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the team",
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the user",
    )

    manually_granted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Optional role within the team
    role: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Optional role within the team (e.g., 'member', 'lead')",
    )

    # Who added this member
    added_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who added this member",
    )

    # Timestamp
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the user was added to the team",
    )

    # Relationships
    team: Mapped["Team"] = relationship("Team", back_populates="memberships")
    user: Mapped["User"] = relationship(
        "User", foreign_keys=[user_id], back_populates="team_memberships"
    )
    adder: Mapped[Optional["User"]] = relationship("User", foreign_keys=[added_by])

    # Unique constraint: one user per team
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "user_id",
            name="uq_team_user",
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<TeamMembership(team_id={self.team_id}, user_id={self.user_id})>"
