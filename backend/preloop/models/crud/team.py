"""CRUD operations for Team and TeamMembership models."""

import uuid
from typing import List, Optional

from sqlalchemy.orm import Session

from ..models.team import Team, TeamMembership
from ..models.user import User
from .base import CRUDBase


class CRUDTeam(CRUDBase[Team]):
    """CRUD operations for Team model."""

    def get_by_name(self, db: Session, *, name: str, account_id: str) -> Optional[Team]:
        """Get team by name within an account.

        Args:
            db: Database session.
            name: Team name.
            account_id: Account ID.

        Returns:
            Team if found, None otherwise.
        """
        return (
            db.query(Team)
            .filter(Team.name == name, Team.account_id == account_id)
            .first()
        )

    def get_by_account(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[Team]:
        """Get all teams for an account.

        Args:
            db: Database session.
            account_id: Account ID.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of teams.
        """
        return (
            db.query(Team)
            .filter(Team.account_id == account_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_members(
        self, db: Session, *, team_id: uuid.UUID, skip: int = 0, limit: int = 100
    ) -> List[User]:
        """Get all members of a team.

        Args:
            db: Database session.
            team_id: Team ID.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of users.
        """
        return (
            db.query(User)
            .join(TeamMembership, User.id == TeamMembership.user_id)
            .filter(TeamMembership.team_id == team_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_membership(
        self, db: Session, *, team_id: uuid.UUID, user_id: uuid.UUID
    ) -> Optional[TeamMembership]:
        """Get team membership for a user.

        Args:
            db: Database session.
            team_id: Team ID.
            user_id: User ID.

        Returns:
            TeamMembership if found, None otherwise.
        """
        return (
            db.query(TeamMembership)
            .filter(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
            )
            .first()
        )

    def add_member(
        self,
        db: Session,
        *,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        role: Optional[str] = None,
        added_by: Optional[uuid.UUID] = None,
    ) -> TeamMembership:
        """Add a member to a team.

        Args:
            db: Database session.
            team_id: Team ID.
            user_id: User ID.
            role: Optional role within the team (e.g., 'member', 'lead').
            added_by: User who added this member (optional).

        Returns:
            Created TeamMembership.
        """
        membership = TeamMembership(
            id=uuid.uuid4(),
            team_id=team_id,
            user_id=user_id,
            role=role,
            added_by=added_by,
        )
        db.add(membership)
        db.commit()
        db.refresh(membership)
        return membership

    def remove_member(
        self, db: Session, *, team_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Remove a member from a team.

        Args:
            db: Database session.
            team_id: Team ID.
            user_id: User ID.

        Returns:
            True if removed, False if not found.
        """
        membership = (
            db.query(TeamMembership)
            .filter(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
            )
            .first()
        )
        if membership:
            db.delete(membership)
            db.commit()
            return True
        return False

    def get_user_teams(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        account_id: Optional[uuid.UUID] = None,
        skip: int = 0,
        limit: Optional[int] = 100,
    ) -> List[Team]:
        """Get all teams a user is a member of.

        Args:
            db: Database session.
            user_id: User ID.
            account_id: When set, only teams belonging to this account.
            skip: Number of records to skip.
            limit: Maximum number of records to return. ``None`` means no cap.

        Returns:
            Teams ordered by name.
        """
        query = (
            db.query(Team)
            .join(TeamMembership, Team.id == TeamMembership.team_id)
            .filter(TeamMembership.user_id == user_id)
        )
        if account_id is not None:
            query = query.filter(Team.account_id == account_id)
        query = query.order_by(Team.name).offset(skip)
        if limit is not None:
            query = query.limit(limit)
        return query.all()


# Create instance
crud_team = CRUDTeam(Team)
