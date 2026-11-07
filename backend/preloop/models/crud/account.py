"""CRUD operations for Account model."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.future import select

from ..models.account import Account
from .base import CRUDBase


class CRUDAccount(CRUDBase[Account]):
    """CRUD operations for Account model."""

    def create(
        self, db: Session, *, obj_in: Dict[str, Any], commit: bool = True
    ) -> Account:
        """Create new account with initialized timestamp fields.

        Args:
            db: Database session.
            obj_in: Column values for the new account.
            commit: When False, flush only so callers can batch several
                writes into one atomic transaction and commit themselves.

        Returns:
            The created account.
        """
        obj_data = dict(obj_in)

        # Initialize timestamp fields
        current_time = datetime.now(timezone.utc)
        obj_data.setdefault("created", current_time)
        obj_data.setdefault("last_updated", current_time)

        from preloop.plugins import get_plugin_manager

        account = super().create(db=db, obj_in=obj_data, commit=False)
        meter = get_plugin_manager().get_service("hosted_spend")
        if meter is not None:
            meter.initialize_new_account(db, account_id=account.id)
        if commit:
            db.commit()
            db.refresh(account)
        return account

    def update(self, db: Session, *, db_obj: Account, obj_in: Any) -> Account:
        """Update account and its last_updated timestamp."""
        if isinstance(obj_in, dict):
            update_data = obj_in
        else:
            update_data = obj_in.model_dump(exclude_unset=True)

        # Update last_updated field
        update_data["last_updated"] = datetime.now(timezone.utc)

        return super().update(db=db, db_obj=db_obj, obj_in=update_data)

    def get_active(
        self, db: Session, *, skip: int = 0, limit: int = 100
    ) -> List[Account]:
        """Get active accounts."""
        return (
            db.query(Account)
            .filter(Account.is_active.is_(True))
            .offset(skip)
            .limit(limit)
            .all()
        )

    def add_to_organization(
        self,
        db: Session,
        *,
        account_id: str,
        organization_id: str,
        role: str = "member",
    ) -> None:
        """Add account to organization with role."""
        # Import here to avoid circular imports
        from sqlalchemy import text

        # Use direct SQL to avoid issues with SQLAlchemy ORM and composite keys
        db.execute(
            text(
                """
                INSERT INTO accountorganization (account_id, organization_id, role, created_at, updated_at)
                VALUES (:account_id, :organization_id, :role, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
            ),
            {
                "account_id": account_id,
                "organization_id": organization_id,
                "role": role,
            },
        )
        db.commit()

    def remove_from_organization(
        self, db: Session, *, account_id: str, organization_id: str
    ) -> None:
        """Remove account from organization."""
        # Import here to avoid circular imports
        from sqlalchemy import text

        # Use direct SQL to avoid issues with SQLAlchemy ORM and composite keys
        db.execute(
            text(
                """
                DELETE FROM accountorganization
                WHERE account_id = :account_id AND organization_id = :organization_id
            """
            ),
            {"account_id": account_id, "organization_id": organization_id},
        )
        db.commit()

    def update_organization_role(
        self, db: Session, *, account_id: str, organization_id: str, role: str
    ) -> None:
        """Update account's role in organization."""
        # Import here to avoid circular imports
        from sqlalchemy import text

        # Use direct SQL to avoid issues with SQLAlchemy ORM and composite keys
        db.execute(
            text(
                """
                UPDATE accountorganization
                SET role = :role, updated_at = CURRENT_TIMESTAMP
                WHERE account_id = :account_id AND organization_id = :organization_id
            """
            ),
            {
                "account_id": account_id,
                "organization_id": organization_id,
                "role": role,
            },
        )
        db.commit()

    def purge(
        self, db: Session, *, account_id: str, commit: bool = True
    ) -> Optional[Account]:
        """Hard-delete an account and everything it owns.

        Deletes each user via ``crud_user.hard_delete`` so per-user SSO
        artifacts (``oauth_token``, ``identity_link``) are removed explicitly,
        then deletes the account row; remaining children are removed by the
        ORM relationship cascades and DB-level ``ON DELETE CASCADE``
        constraints.

        This method does NOT touch external systems (e.g. Stripe customers or
        subscriptions) — callers own that cleanup before purging.

        Args:
            db: Database session.
            account_id: Account ID to purge.
            commit: When False, flush only so callers can batch the purge into
                a larger atomic transaction and commit themselves.

        Returns:
            The deleted account if found, None otherwise.
        """
        from ..models.user import User
        from .user import crud_user

        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            return None

        user_ids = [
            row[0]
            for row in db.query(User.id).filter(User.account_id == account.id).all()
        ]
        for user_id in user_ids:
            crud_user.hard_delete(db, user_id=user_id, commit=False)

        db.delete(account)
        if commit:
            db.commit()
        else:
            db.flush()
        return account

    COST_REVISION_KEY = "cost_cache_revision"

    def bump_cost_revision(
        self, db: Session, *, account_id: Any, commit: bool = False
    ) -> None:
        """Advance the account's cost revision in one atomic UPDATE.

        Cost read caches key on the account's ``meta_data``, so a write that
        changes past usage (reprice, ledger backfill, usage import) bumps this
        counter and every worker stops serving older aggregates. The UPDATE
        touches only this key, so concurrent metadata writers are not lost.

        Args:
            db: Database session.
            account_id: Account whose cached cost reads are now stale.
            commit: Commit immediately; otherwise the caller's commit applies it.
        """
        db.execute(
            text(
                "UPDATE account SET meta_data = jsonb_set("
                "COALESCE(meta_data::jsonb, '{}'::jsonb), :path, "
                "to_jsonb(COALESCE((meta_data::jsonb ->> :key)::bigint, 0) + 1)"
                ")::json WHERE id = :account_id"
            ),
            {
                "path": "{" + self.COST_REVISION_KEY + "}",
                "key": self.COST_REVISION_KEY,
                "account_id": str(account_id),
            },
        )
        if commit:
            db.commit()

    def get_organizations(self, db: Session, *, account_id: str) -> Dict[str, str]:
        """Get all organizations and roles for an account."""
        account = self.get(db, id=account_id)
        if not account:
            return {}
        return {obj.organization_id: obj.role for obj in account.organizations}


async def get_meta_data_async(db: Session, account_id: str) -> dict:
    """Async: Get meta_data for an account by ID."""
    result = await db.execute(select(Account.meta_data).where(Account.id == account_id))
    return result.scalar_one_or_none() or {}
