"""CRUD operations for Account model."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, text
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

    #: Rows registration itself writes for a new account, as
    #: ``(table, column)`` foreign keys. Any other row that points at a
    #: candidate account, user or person means the account was used.
    SIGNUP_DEFAULT_REFERENCES = frozenset(
        {
            ('"user"', "account_id"),
            ('"user"', "person_id"),
            ("user_role", "user_id"),
            ("account", "primary_user_id"),
            ("account", "root_account_id"),
            ("person", "primary_user_id"),
            ("person", "last_active_user_id"),
            ("approval_workflow", "account_id"),
            ("tool_configuration", "account_id"),
            ("tool_access_rules", "account_id"),
        }
    )

    def list_unused_unverified_signups(
        self,
        db: Session,
        *,
        created_before: datetime,
        limit: int,
        after_id: Optional[Any] = None,
        exclude_ids: tuple[str, ...] = (),
        only_ids: Optional[List[Any]] = None,
        lock: bool = False,
    ) -> tuple[List[Any], Optional[Any]]:
        """Find self-registered accounts nobody ever used.

        An account qualifies when it was created before ``created_before``,
        is a top-level account without a billing customer, has exactly one
        member whose identity belongs to no other account, whose address was
        never verified and who never signed in, and nothing else in the
        database refers to the account, the member or the identity except the
        defaults registration writes (``SIGNUP_DEFAULT_REFERENCES``) and
        failed-outcome audit rows (for example refused sign-ins). Foreign keys
        are read from the live catalog, so tables added by plugins count.

        Args:
            db: Database session.
            created_before: Only accounts created before this instant.
            limit: Accounts examined per call (one batch).
            after_id: Resume after this account id (keyset pagination).
            exclude_ids: Account ids never to return (operator accounts).
            only_ids: Re-check just these accounts.
            lock: Lock the account, member and identity rows (``FOR UPDATE``)
                so a verification or sign-in cannot commit between this check
                and a delete in the same transaction.

        Returns:
            ``(qualifying account ids, last examined id)``; the second is None
            when no account was examined, meaning the scan is complete.
        """
        from ..models.person import Person
        from ..models.user import User

        members = (
            select(User.account_id, func.count(User.id).label("n"))
            .group_by(User.account_id)
            .subquery()
        )
        memberships = (
            select(User.person_id, func.count(User.id).label("n"))
            .group_by(User.person_id)
            .subquery()
        )
        query = (
            select(Account.id, User.id, Person.id)
            .join(User, User.account_id == Account.id)
            .join(Person, Person.id == User.person_id)
            .join(members, members.c.account_id == Account.id)
            .join(memberships, memberships.c.person_id == Person.id)
            .where(
                Account.created < created_before,
                Account.parent_account_id.is_(None),
                Account.stripe_customer_id.is_(None),
                Account.is_superuser.is_(False),
                members.c.n == 1,
                memberships.c.n == 1,
                User.last_login.is_(None),
                User.is_superuser.is_(False),
                Person.email_verified_at.is_(None),
            )
            .order_by(Account.id)
            .limit(limit)
        )
        if after_id is not None:
            query = query.where(Account.id > after_id)
        if exclude_ids:
            query = query.where(Account.id.not_in(list(exclude_ids)))
        if only_ids is not None:
            query = query.where(Account.id.in_(list(only_ids)))
        if lock:
            query = query.with_for_update(of=(Account, User, Person))
        rows = db.execute(query).all()
        if not rows:
            return [], None
        last_id = rows[-1][0]
        by_target = {
            "account": [row[0] for row in rows],
            '"user"': [row[1] for row in rows],
            "person": [row[2] for row in rows],
        }
        owner_of: Dict[Any, Any] = {}
        for account_id, user_id, person_id in rows:
            owner_of[account_id] = account_id
            owner_of[user_id] = account_id
            owner_of[person_id] = account_id
        used = {owner_of[i] for i in self._referenced_ids(db, by_target)}
        return [row[0] for row in rows if row[0] not in used], last_id

    def _referenced_ids(self, db: Session, by_target: Dict[str, List[Any]]) -> set:
        """Ids among ``by_target`` that any non-default row refers to."""
        references = db.execute(
            text(
                """
                SELECT c.conrelid::regclass::text AS tbl,
                       a.attname AS col,
                       c.confrelid::regclass::text AS target
                FROM pg_constraint c
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
                WHERE c.contype = 'f'
                  AND array_length(c.conkey, 1) = 1
                  AND c.confrelid::regclass::text = ANY(:targets)
                """
            ),
            {"targets": list(by_target)},
        ).all()
        used: set = set()
        for tbl, col, target in references:
            if (tbl, col) in self.SIGNUP_DEFAULT_REFERENCES:
                continue
            ids = by_target[target]
            column = '"' + col.replace('"', '""') + '"'
            where = f"{column} = ANY(:ids)"
            if tbl == target:
                # A row's reference to itself (none today) is not use.
                where += " AND id <> ALL(:ids)"
            if tbl == "audit_log":
                where += " AND status <> 'failure'"
            # Identifiers come from the catalog, never from input.
            found = db.execute(
                text(f"SELECT DISTINCT {column} FROM {tbl} WHERE {where}"),  # nosec B608
                {"ids": ids},
            ).scalars()
            used.update(found)
        return used

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
