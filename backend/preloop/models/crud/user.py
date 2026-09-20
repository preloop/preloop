"""CRUD operations for models.User model."""

import uuid
from typing import Any, List, Optional

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from preloop.models import models
from .base import CRUDBase

# Constant advisory-lock key that serializes first-user (bootstrap)
# registration. Arbitrary but stable app-unique value ("PRLB" in ASCII).
REGISTRATION_BOOTSTRAP_LOCK_KEY = 0x5052_4C42


class CRUDUser(CRUDBase[models.User]):
    """CRUD operations for models.User model."""

    def create(
        self, db: Session, *, obj_in: dict[str, Any], commit: bool = True
    ) -> models.User:
        """Serialize growth before persisting an active account member."""
        from .capacity import authorize_capacity, record_capacity_change

        if obj_in.get("is_active", True) and obj_in.get("account_id"):
            authorize_capacity(db, str(obj_in["account_id"]), "users")
            record_capacity_change(db, str(obj_in["account_id"]))
        return super().create(db, obj_in=obj_in, commit=commit)

    def update(
        self, db: Session, *, db_obj: models.User, obj_in: dict[str, Any]
    ) -> models.User:
        """A reactivation claims the same capacity as a new active member."""
        from .capacity import authorize_capacity, record_capacity_change

        if "is_active" in obj_in:
            from .billing import billing

            with db.no_autoflush:
                billing.lock_account(db, str(db_obj.account_id))
                db_obj = (
                    db.query(models.User)
                    .filter(models.User.id == db_obj.id)
                    .with_for_update()
                    .populate_existing()
                    .one()
                )
        if obj_in.get("is_active") and not db_obj.is_active:
            authorize_capacity(db, str(db_obj.account_id), "users")
        if "is_active" in obj_in and obj_in["is_active"] != db_obj.is_active:
            record_capacity_change(db, str(db_obj.account_id))
        return super().update(db, db_obj=db_obj, obj_in=obj_in)

    def delete(self, db: Session, *, id: Any) -> Optional[models.User]:
        """Deleting an active seat queues the same durable billing repair."""
        from .capacity import record_capacity_change

        user = self.get(db, id=id)
        if user is not None and user.is_active:
            record_capacity_change(db, str(user.account_id))
        return super().delete(db, id=id)

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

    def get_by_username(self, db: Session, *, username: str) -> Optional[models.User]:
        """Get user by username.

        Args:
            db: Database session.
            username: Username to search for.

        Returns:
            models.User if found, None otherwise.
        """
        return db.query(models.User).filter(models.User.username == username).first()

    def get_by_email(
        self, db: Session, *, email: str, account_id: Optional[str] = None
    ) -> Optional[models.User]:
        """Get user by email.

        Args:
            db: Database session.
            email: Email to search for.
            account_id: Optional account ID for scoping.

        Returns:
            models.User if found, None otherwise.
        """
        query = db.query(models.User).filter(models.User.email == email)
        if account_id:
            query = query.filter(models.User.account_id == account_id)
        return query.first()

    def get_by_external_id(
        self, db: Session, *, external_id: str, user_source: str
    ) -> Optional[models.User]:
        """Get user by external ID and source.

        Args:
            db: Database session.
            external_id: External system's user ID.
            user_source: Source of authentication (ldap, ad, saml, oauth).

        Returns:
            models.User if found, None otherwise.
        """
        return (
            db.query(models.User)
            .filter(
                models.User.external_id == external_id,
                models.User.user_source == user_source,
            )
            .first()
        )

    def get_by_account(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[models.User]:
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
            db.query(models.User)
            .filter(models.User.account_id == account_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_active_by_account(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[models.User]:
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
            db.query(models.User)
            .filter(models.User.account_id == account_id, models.User.is_active)
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
        return (
            db.query(models.User.id)
            .filter(models.User.account_id == account_id)
            .count()
        )

    def hard_delete(
        self, db: Session, *, user_id: uuid.UUID, commit: bool = True
    ) -> Optional[models.User]:
        """Permanently delete a user, including their SSO/OAuth identity records.

        The SSO identity fields (``user_source``, ``oauth_provider``,
        ``oauth_id``, ``external_id``) live on the user row itself and are
        removed with it. Provider token rows (``oauth_token``) and identity
        graph edges (``identity_link``) are removed explicitly rather than
        relying on DB-level ``ON DELETE CASCADE`` so behavior is identical on
        backends/test setups where FK cascades are not enforced.

        Audit logs and events referencing the user are preserved with
        ``user_id`` set to NULL (see the relationship configuration on
        :class:`~preloop.models.models.user.models.User`).

        Args:
            db: Database session.
            user_id: models.User ID to delete.
            commit: When False, flush only so callers can batch several
                deletions into one atomic transaction and commit themselves.

        Returns:
            The deleted user if found, None otherwise.
        """
        from ..models.github_oauth_token import OAuthToken
        from ..models.identity_link import IdentityLink

        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            return None

        # Explicit SSO artifact cleanup (also covered by DB FK cascades).
        # ORM-level deletes (not bulk .delete()) so the session stays
        # consistent with the delete-orphan cascade on models.User.oauth_tokens.
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

    def deactivate(self, db: Session, *, user_id: uuid.UUID) -> Optional[models.User]:
        """Deactivate a user (soft delete).

        Args:
            db: Database session.
            user_id: models.User ID to deactivate.

        Returns:
            Deactivated user if found, None otherwise.
        """
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user:
            return self.update(db, db_obj=user, obj_in={"is_active": False})
        return user

    def activate(self, db: Session, *, user_id: uuid.UUID) -> Optional[models.User]:
        """Activate a user.

        Args:
            db: Database session.
            user_id: models.User ID to activate.

        Returns:
            Activated user if found, None otherwise.
        """
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user:
            return self.update(db, db_obj=user, obj_in={"is_active": True})
        return user

    def has_any_users(self, db: Session) -> bool:
        """Return whether at least one user exists on this instance.

        Args:
            db: Database session.

        Returns:
            True when any user row exists, False on a fresh instance.
        """
        return db.query(models.User.id).first() is not None

    def set_avatar_from_sso(
        self,
        db: Session,
        *,
        user: models.User,
        avatar_url: str,
        commit: bool = True,
    ) -> models.User:
        """Set the avatar from an SSO provider, respecting precedence.

        Manual uploads take precedence: if the user's current avatar_source
        is ``"manual"``, the SSO URL is silently ignored.

        Args:
            db: Database session.
            user: models.User ORM instance to update.
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

    def bump_auth_generation(self, db: Session, user_id: Any) -> int:
        """Atomically increment the user's JWT generation and return it.

        Args:
            db: Database session.
            user_id: User whose outstanding JWT sessions should be revoked.

        Returns:
            The new ``auth_generation`` value.

        Raises:
            ValueError: If no user exists with ``user_id``.
        """
        stmt = (
            update(models.User)
            .where(models.User.id == user_id)
            .values(auth_generation=models.User.auth_generation + 1)
            .returning(models.User.auth_generation)
        )
        result = db.execute(stmt)
        row = result.first()
        if row is None:
            raise ValueError(f"User {user_id} not found")
        db.commit()
        return int(row[0])


# Create instance
crud_user = CRUDUser(models.User)
