"""CRUD operations for User model."""

import uuid
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models.user import User
from .base import CRUDBase

# Constant advisory-lock key that serializes first-user (bootstrap)
# registration. Arbitrary but stable app-unique value ("PRLB" in ASCII).
REGISTRATION_BOOTSTRAP_LOCK_KEY = 0x5052_4C42


class CRUDUser(CRUDBase[User]):
    """CRUD operations for User model."""

    def acquire_registration_bootstrap_lock(self, db: Session) -> None:
        """Serialize concurrent first-user registrations.

        Takes a Postgres transaction-scoped advisory lock
        (``pg_advisory_xact_lock``) on a constant key so two concurrent
        signups cannot both observe the zero-users state. The lock is
        released automatically when the surrounding transaction commits or
        rolls back. No-op on non-Postgres dialects.

        Args:
            db: Database session (the lock binds to its transaction).
        """
        bind = db.get_bind()
        if bind.dialect.name != "postgresql":
            return
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": REGISTRATION_BOOTSTRAP_LOCK_KEY},
        )

    def get_by_username(self, db: Session, *, username: str) -> Optional[User]:
        """Get user by username.

        Args:
            db: Database session.
            username: Username to search for.

        Returns:
            User if found, None otherwise.
        """
        return db.query(User).filter(User.username == username).first()

    def get_by_email(
        self, db: Session, *, email: str, account_id: Optional[str] = None
    ) -> Optional[User]:
        """Get user by email.

        Args:
            db: Database session.
            email: Email to search for.
            account_id: Optional account ID for scoping.

        Returns:
            User if found, None otherwise.
        """
        query = db.query(User).filter(User.email == email)
        if account_id:
            query = query.filter(User.account_id == account_id)
        return query.first()

    def get_by_external_id(
        self, db: Session, *, external_id: str, user_source: str
    ) -> Optional[User]:
        """Get user by external ID and source.

        Args:
            db: Database session.
            external_id: External system's user ID.
            user_source: Source of authentication (ldap, ad, saml, oauth).

        Returns:
            User if found, None otherwise.
        """
        return (
            db.query(User)
            .filter(User.external_id == external_id, User.user_source == user_source)
            .first()
        )

    def get_by_account(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[User]:
        """Get all users for an account.

        Args:
            db: Database session.
            account_id: Account ID.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of users.
        """
        return (
            db.query(User)
            .filter(User.account_id == account_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_active_by_account(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[User]:
        """Get all active users for an account.

        Args:
            db: Database session.
            account_id: Account ID.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of active users.
        """
        return (
            db.query(User)
            .filter(User.account_id == account_id, User.is_active)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_by_account(self, db: Session, *, account_id: str) -> int:
        """Count all users belonging to an account.

        Args:
            db: Database session.
            account_id: Account ID.

        Returns:
            Number of users in the account.
        """
        return db.query(User.id).filter(User.account_id == account_id).count()

    def hard_delete(
        self, db: Session, *, user_id: uuid.UUID, commit: bool = True
    ) -> Optional[User]:
        """Permanently delete a user, including their SSO/OAuth identity records.

        The SSO identity fields (``user_source``, ``oauth_provider``,
        ``oauth_id``, ``external_id``) live on the user row itself and are
        removed with it. Provider token rows (``oauth_token``) and identity
        graph edges (``identity_link``) are removed explicitly rather than
        relying on DB-level ``ON DELETE CASCADE`` so behavior is identical on
        backends/test setups where FK cascades are not enforced.

        Audit logs and events referencing the user are preserved with
        ``user_id`` set to NULL (see the relationship configuration on
        :class:`~preloop.models.models.user.User`).

        Args:
            db: Database session.
            user_id: User ID to delete.
            commit: When False, flush only so callers can batch several
                deletions into one atomic transaction and commit themselves.

        Returns:
            The deleted user if found, None otherwise.
        """
        from ..models.github_oauth_token import OAuthToken
        from ..models.identity_link import IdentityLink

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return None

        # Explicit SSO artifact cleanup (also covered by DB FK cascades).
        # ORM-level deletes (not bulk .delete()) so the session stays
        # consistent with the delete-orphan cascade on User.oauth_tokens.
        for token in db.query(OAuthToken).filter(OAuthToken.user_id == user_id).all():
            db.delete(token)
        for link in (
            db.query(IdentityLink).filter(IdentityLink.user_id == user_id).all()
        ):
            db.delete(link)

        db.delete(user)
        if commit:
            db.commit()
        else:
            db.flush()
        return user

    def deactivate(self, db: Session, *, user_id: uuid.UUID) -> Optional[User]:
        """Deactivate a user (soft delete).

        Args:
            db: Database session.
            user_id: User ID to deactivate.

        Returns:
            Deactivated user if found, None otherwise.
        """
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.is_active = False
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    def activate(self, db: Session, *, user_id: uuid.UUID) -> Optional[User]:
        """Activate a user.

        Args:
            db: Database session.
            user_id: User ID to activate.

        Returns:
            Activated user if found, None otherwise.
        """
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.is_active = True
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    def has_any_users(self, db: Session) -> bool:
        """Return whether at least one user exists on this instance.

        Args:
            db: Database session.

        Returns:
            True when any user row exists, False on a fresh instance.
        """
        return db.query(User.id).first() is not None

    def set_avatar_from_sso(
        self,
        db: Session,
        *,
        user: User,
        avatar_url: str,
        commit: bool = True,
    ) -> User:
        """Set the avatar from an SSO provider, respecting precedence.

        Manual uploads take precedence: if the user's current avatar_source
        is ``"manual"``, the SSO URL is silently ignored.

        Args:
            db: Database session.
            user: User ORM instance to update.
            avatar_url: Provider-supplied avatar URL.
            commit: Commit the transaction when True.

        Returns:
            The (possibly updated) user.
        """
        if user.avatar_source == "manual":
            return user
        user.avatar_url = avatar_url
        user.avatar_source = "sso"
        db.add(user)
        if commit:
            db.commit()
            db.refresh(user)
        else:
            db.flush()
        return user


# Create instance
crud_user = CRUDUser(User)
