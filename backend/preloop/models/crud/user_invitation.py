"""CRUD operations for UserInvitation model."""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from ..models.user_invitation import UserInvitation, UserInvitationStatus
from .base import CRUDBase


class CRUDUserInvitation(CRUDBase[UserInvitation]):
    """CRUD operations for UserInvitation model."""

    def get_by_token(self, db: Session, *, token: str) -> Optional[UserInvitation]:
        """Get invitation by token.

        Args:
            db: Database session.
            token: Invitation token.

        Returns:
            UserInvitation if found, None otherwise.
        """
        return db.query(UserInvitation).filter(UserInvitation.token == token).first()

    def get_by_email(
        self, db: Session, *, email: str, account_id: str
    ) -> Optional[UserInvitation]:
        """Get pending invitation by email for an account.

        Args:
            db: Database session.
            email: Email address.
            account_id: Account ID.

        Returns:
            Pending UserInvitation if found, None otherwise.
        """
        return (
            db.query(UserInvitation)
            .filter(
                UserInvitation.email == email,
                UserInvitation.account_id == account_id,
                UserInvitation.status == UserInvitationStatus.PENDING,
            )
            .first()
        )

    def get_by_account(
        self,
        db: Session,
        *,
        account_id: str,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[UserInvitation]:
        """Get invitations for an account.

        Args:
            db: Database session.
            account_id: Account ID.
            status: Optional status filter.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of invitations.
        """
        query = db.query(UserInvitation).filter(UserInvitation.account_id == account_id)
        if status:
            query = query.filter(UserInvitation.status == status)
        return query.offset(skip).limit(limit).all()

    def count_by_account(
        self, db: Session, *, account_id: uuid.UUID, status: Optional[str] = None
    ) -> int:
        """Count invitations in an account with the same filter as the list."""
        query = db.query(UserInvitation).filter(UserInvitation.account_id == account_id)
        if status:
            query = query.filter(UserInvitation.status == status)
        return query.count()

    def get_pending(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[UserInvitation]:
        """Get pending invitations for an account.

        Args:
            db: Database session.
            account_id: Account ID.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of pending invitations.
        """
        return (
            db.query(UserInvitation)
            .filter(
                UserInvitation.account_id == account_id,
                UserInvitation.status == UserInvitationStatus.PENDING,
            )
            .offset(skip)
            .limit(limit)
            .all()
        )

    def accept(
        self, db: Session, *, invitation_id: uuid.UUID, user_id: uuid.UUID
    ) -> Optional[UserInvitation]:
        """Mark an invitation as accepted.

        Args:
            db: Database session.
            invitation_id: Invitation ID.
            user_id: ID of the user created from this invitation.

        Returns:
            Updated invitation if found, None otherwise.
        """
        invitation = (
            db.query(UserInvitation).filter(UserInvitation.id == invitation_id).first()
        )
        if invitation:
            invitation.status = UserInvitationStatus.ACCEPTED
            invitation.accepted_at = datetime.now(timezone.utc)
            invitation.accepted_by = user_id
            db.add(invitation)
            db.commit()
            db.refresh(invitation)
        return invitation

    def cancel(
        self, db: Session, *, invitation_id: uuid.UUID
    ) -> Optional[UserInvitation]:
        """Cancel an invitation.

        Args:
            db: Database session.
            invitation_id: Invitation ID.

        Returns:
            Updated invitation if found, None otherwise.
        """
        invitation = (
            db.query(UserInvitation).filter(UserInvitation.id == invitation_id).first()
        )
        if invitation:
            invitation.status = UserInvitationStatus.CANCELLED
            db.add(invitation)
            db.commit()
            db.refresh(invitation)
        return invitation

    def expire_old_invitations(self, db: Session) -> int:
        """Mark expired invitations as expired.

        Args:
            db: Database session.

        Returns:
            Number of invitations marked as expired.
        """
        now = datetime.now(timezone.utc)
        expired = (
            db.query(UserInvitation)
            .filter(
                UserInvitation.status == UserInvitationStatus.PENDING,
                UserInvitation.expires_at < now,
            )
            .all()
        )

        count = 0
        for invitation in expired:
            invitation.status = UserInvitationStatus.EXPIRED
            db.add(invitation)
            count += 1

        if count > 0:
            db.commit()

        return count

    def cleanup_expired(self, db: Session, *, days_old: int = 30) -> int:
        """Delete old expired invitations.

        Args:
            db: Database session.
            days_old: Delete invitations expired for this many days.

        Returns:
            Number of invitations deleted.
        """
        from datetime import timedelta

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_old)
        expired = (
            db.query(UserInvitation)
            .filter(
                UserInvitation.status.in_(
                    [UserInvitationStatus.EXPIRED, UserInvitationStatus.CANCELLED]
                ),
                UserInvitation.expires_at < cutoff_date,
            )
            .all()
        )

        count = 0
        for invitation in expired:
            db.delete(invitation)
            count += 1

        if count > 0:
            db.commit()

        return count


# Create instance
crud_user_invitation = CRUDUserInvitation(UserInvitation)
