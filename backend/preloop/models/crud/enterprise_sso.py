"""CRUD for enterprise SSO configuration.

Every read that an admin request can reach takes the account id and filters
on it, so an id from another account behaves exactly like an unknown id.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from ..models.enterprise_sso import (
    AccountSsoConnection,
    AccountSsoPolicy,
    AccountVerifiedDomain,
    ScimToken,
    SsoGroupMapping,
    SsoReplayGuard,
)
from ..models.secret_reference import SecretReference
from ..models.team import Team
from .base import CRUDBase, ModelType

#: Connection fields an update may set directly.
CONNECTION_UPDATABLE_FIELDS = frozenset(
    {
        "name",
        "enabled",
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret_ref_id",
        "oidc_scopes",
        "groups_claim",
        "saml_idp_entity_id",
        "saml_idp_sso_url",
        "saml_idp_metadata_url",
        "saml_idp_certs",
        "saml_sp_signing_key_ref_id",
        "saml_want_assertions_signed",
        "default_role",
        "jit_enabled",
        "attribute_mapping",
    }
)


class CrossAccountReferenceError(ValueError):
    """A referenced row (connection, team, secret) belongs to another account."""


class DomainVerifiedElsewhereError(Exception):
    """The domain is already verified by a different account."""


def utc_now() -> datetime:
    """Naive UTC now, matching the ``DateTime`` columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_scim_token(token: str) -> str:
    """Hex SHA-256 of a SCIM token; the only form that is stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _AccountScopedCRUD(CRUDBase[ModelType]):
    """Writes use validated named helpers; deletion is scoped to an account.

    The inherited ``delete`` would remove another account's row given its
    id; here ``account_id`` is required and filters through
    ``_scope_to_account``.
    """

    def create(
        self, db: Session, *, obj_in: dict[str, Any], commit: bool = True
    ) -> ModelType:
        """Require named write helpers, which validate account references."""
        raise TypeError(f"{type(self).__name__}.create requires a named write helper")

    def update(
        self,
        db: Session,
        *,
        db_obj: ModelType,
        obj_in: dict[str, Any],
        commit: bool = True,
    ) -> ModelType:
        """Require named write helpers, which validate account references."""
        raise TypeError(f"{type(self).__name__}.update requires a named write helper")

    def delete(  # type: ignore[override]
        self, db: Session, *, id: Any, account_id: Any = None
    ) -> Optional[ModelType]:
        """Delete one row of ``account_id`` by id; commits.

        Raises:
            TypeError: ``account_id`` was not given.
        """
        if account_id is None:
            raise TypeError(f"{type(self).__name__}.delete requires account_id")
        query = self._scope_to_account(
            db.query(self.model).filter(self.model.id == id), account_id
        )
        obj = query.first()
        if obj is not None:
            db.delete(obj)
            db.commit()
        return obj


class _ThroughConnection:
    """Scope rows that reach their account through ``connection_id``."""

    model: Any

    def _scope_to_account(self, query: Query, account_id: Any) -> Query:
        """Join the owning connection and filter on its account."""
        return query.join(
            AccountSsoConnection,
            AccountSsoConnection.id == self.model.connection_id,
        ).filter(AccountSsoConnection.account_id == account_id)


def _check_secret_refs(db: Session, account_id: Any, fields: dict[str, Any]) -> None:
    """Secret references set on a connection must belong to its account.

    Raises:
        CrossAccountReferenceError: A reference points at another account.
    """
    for key in ("oidc_client_secret_ref_id", "saml_sp_signing_key_ref_id"):
        secret_id = fields.get(key)
        if secret_id is None:
            continue
        secret = db.get(SecretReference, secret_id)
        if secret is None or str(secret.account_id) != str(account_id):
            raise CrossAccountReferenceError(key)


class CRUDAccountSsoConnection(_AccountScopedCRUD[AccountSsoConnection]):
    """Account-scoped connection reads and writes."""

    def delete(  # type: ignore[override]
        self, db: Session, *, id: Any, account_id: Any = None, commit: bool = True
    ) -> Optional[AccountSsoConnection]:
        """Revoke this connection's managed access before deleting it."""
        from .sso_group_grants import revoke_connection_grants

        if account_id is None:
            raise TypeError("CRUDAccountSsoConnection.delete requires account_id")
        row = self.get_in_account(db, connection_id=id, account_id=account_id)
        if row is not None:
            revoke_connection_grants(
                db, connection_id=row.id, account_id=row.account_id
            )
            db.delete(row)
            db.flush()
            if commit:
                db.commit()
        return row

    def list_for_account(
        self, db: Session, *, account_id: Any
    ) -> list[AccountSsoConnection]:
        """List an account's connections by name."""
        return list(
            db.execute(
                select(AccountSsoConnection)
                .where(AccountSsoConnection.account_id == account_id)
                .order_by(AccountSsoConnection.name)
            ).scalars()
        )

    def get_in_account(
        self, db: Session, *, connection_id: Any, account_id: Any
    ) -> Optional[AccountSsoConnection]:
        """Return one connection when it belongs to ``account_id``."""
        return db.execute(
            select(AccountSsoConnection).where(
                AccountSsoConnection.id == connection_id,
                AccountSsoConnection.account_id == account_id,
            )
        ).scalar_one_or_none()

    def create_for_account(
        self,
        db: Session,
        *,
        account_id: Any,
        protocol: str,
        fields: dict[str, Any],
        user_id: Any = None,
    ) -> AccountSsoConnection:
        """Add a connection; the caller commits.

        Raises:
            IntegrityError: Name taken in the account, or a constraint failed.
            CrossAccountReferenceError: A secret reference is not the account's.
        """
        _check_secret_refs(db, account_id, fields)
        row = AccountSsoConnection(
            account_id=account_id,
            protocol=protocol,
            created_by_user_id=user_id,
            updated_by_user_id=user_id,
            **{k: v for k, v in fields.items() if k in CONNECTION_UPDATABLE_FIELDS},
        )
        db.add(row)
        db.flush()
        return row

    def apply_update(
        self,
        db: Session,
        *,
        connection: AccountSsoConnection,
        fields: dict[str, Any],
        user_id: Any = None,
    ) -> AccountSsoConnection:
        """Set allowed fields; the caller commits.

        Raises:
            CrossAccountReferenceError: A secret reference is not the account's.
        """
        _check_secret_refs(db, connection.account_id, fields)
        for key, value in fields.items():
            if key in CONNECTION_UPDATABLE_FIELDS:
                setattr(connection, key, value)
        connection.updated_by_user_id = user_id
        # Secret rotation may retain the same reference id. Invalidate protocol
        # flows and admin test proofs even when all other fields are unchanged.
        connection.updated_at = utc_now()
        db.flush()
        return connection


class CRUDAccountVerifiedDomain(_AccountScopedCRUD[AccountVerifiedDomain]):
    """Domain claims; a verified domain belongs to exactly one account."""

    def list_for_account(
        self, db: Session, *, account_id: Any
    ) -> list[AccountVerifiedDomain]:
        """List an account's domains alphabetically."""
        return list(
            db.execute(
                select(AccountVerifiedDomain)
                .where(AccountVerifiedDomain.account_id == account_id)
                .order_by(AccountVerifiedDomain.domain)
            ).scalars()
        )

    def get_in_account(
        self, db: Session, *, domain_id: Any, account_id: Any
    ) -> Optional[AccountVerifiedDomain]:
        """Return one domain claim when it belongs to ``account_id``."""
        return db.execute(
            select(AccountVerifiedDomain).where(
                AccountVerifiedDomain.id == domain_id,
                AccountVerifiedDomain.account_id == account_id,
            )
        ).scalar_one_or_none()

    def get_by_domain(
        self, db: Session, *, account_id: Any, domain: str
    ) -> Optional[AccountVerifiedDomain]:
        """Return the account's claim on ``domain``, if any."""
        return db.execute(
            select(AccountVerifiedDomain).where(
                AccountVerifiedDomain.account_id == account_id,
                AccountVerifiedDomain.domain == domain.lower(),
            )
        ).scalar_one_or_none()

    def get_verified(
        self, db: Session, *, domain: str
    ) -> Optional[AccountVerifiedDomain]:
        """Return the single verified claim on ``domain`` across accounts."""
        return db.execute(
            select(AccountVerifiedDomain).where(
                AccountVerifiedDomain.domain == domain.lower(),
                AccountVerifiedDomain.verified_at.is_not(None),
            )
        ).scalar_one_or_none()

    def create_claim(
        self, db: Session, *, account_id: Any, domain: str, challenge_token: str
    ) -> AccountVerifiedDomain:
        """Add a pending claim; the caller commits.

        Raises:
            IntegrityError: The account already claims the domain.
        """
        row = AccountVerifiedDomain(
            account_id=account_id,
            domain=domain.lower(),
            challenge_token=challenge_token,
        )
        db.add(row)
        db.flush()
        return row

    def record_check(
        self, db: Session, *, claim: AccountVerifiedDomain, verified: bool
    ) -> AccountVerifiedDomain:
        """Record a verification attempt; on success set ``verified_at``.

        Raises:
            DomainVerifiedElsewhereError: Another account verified it first.
        """
        now = utc_now()
        claim.last_checked_at = now
        # Record the attempt outside the savepoint, whatever the outcome.
        db.flush()
        if not verified:
            return claim
        holder = self.get_verified(db, domain=claim.domain)
        if holder is not None and holder.account_id != claim.account_id:
            raise DomainVerifiedElsewhereError(claim.domain)
        try:
            # Assign inside the savepoint so the UPDATE that can violate the
            # unique index is flushed after SAVEPOINT, not before it.
            with db.begin_nested():
                claim.verified_at = now
                db.flush()
        except IntegrityError as exc:
            # A concurrent verification by another account won the unique index.
            claim.verified_at = None
            raise DomainVerifiedElsewhereError(claim.domain) from exc
        return claim


class CRUDAccountSsoPolicy(_AccountScopedCRUD[AccountSsoPolicy]):
    """One policy row per account, created on first write."""

    def get_for_account(
        self, db: Session, *, account_id: Any
    ) -> Optional[AccountSsoPolicy]:
        """Return the account's policy row, if one was ever written."""
        return db.execute(
            select(AccountSsoPolicy).where(AccountSsoPolicy.account_id == account_id)
        ).scalar_one_or_none()

    def upsert(
        self,
        db: Session,
        *,
        account_id: Any,
        fields: dict[str, Any],
        user_id: Any = None,
    ) -> AccountSsoPolicy:
        """Create or update the policy atomically; the caller commits.

        A single ``INSERT ... ON CONFLICT (account_id) DO UPDATE`` so two
        concurrent first writes cannot both insert.
        """
        values: dict[str, Any] = {
            key: fields[key]
            for key in ("enforce", "break_glass_owner_ids", "session_max_age_seconds")
            if key in fields
        }
        values["updated_by_user_id"] = user_id
        stmt = insert(AccountSsoPolicy).values(
            id=uuid.uuid4(), account_id=account_id, **values
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AccountSsoPolicy.account_id],
            set_={**values, "updated_at": func.now()},
        )
        db.execute(stmt)
        row = self.get_for_account(db, account_id=account_id)
        if row is None:  # pragma: no cover - the upsert just wrote it
            raise RuntimeError("SSO policy upsert returned no row")
        db.refresh(row)
        return row


class CRUDSsoGroupMapping(_ThroughConnection, _AccountScopedCRUD[SsoGroupMapping]):
    """Group mappings, always reached through a connection of the account."""

    def list_for_connection(
        self, db: Session, *, connection_id: Any, account_id: Any
    ) -> list[SsoGroupMapping]:
        """List a connection's mappings by group; empty for another account."""
        return list(
            db.execute(
                select(SsoGroupMapping)
                .join(
                    AccountSsoConnection,
                    AccountSsoConnection.id == SsoGroupMapping.connection_id,
                )
                .where(
                    SsoGroupMapping.connection_id == connection_id,
                    AccountSsoConnection.account_id == account_id,
                )
                .order_by(SsoGroupMapping.idp_group, SsoGroupMapping.created_at)
            ).scalars()
        )

    def get_in_connection(
        self, db: Session, *, mapping_id: Any, connection_id: Any, account_id: Any
    ) -> Optional[SsoGroupMapping]:
        """Return one mapping of the account's connection ``connection_id``."""
        return db.execute(
            select(SsoGroupMapping)
            .join(
                AccountSsoConnection,
                AccountSsoConnection.id == SsoGroupMapping.connection_id,
            )
            .where(
                SsoGroupMapping.id == mapping_id,
                SsoGroupMapping.connection_id == connection_id,
                AccountSsoConnection.account_id == account_id,
            )
        ).scalar_one_or_none()

    def create_mapping(
        self,
        db: Session,
        *,
        account_id: Any,
        connection_id: Any,
        idp_group: str,
        team_id: Any = None,
        role: Optional[str] = None,
    ) -> SsoGroupMapping:
        """Add a mapping; the caller commits.

        Raises:
            CrossAccountReferenceError: The connection or team is not the
                account's.
        """
        connection = db.get(AccountSsoConnection, connection_id)
        if connection is None or str(connection.account_id) != str(account_id):
            raise CrossAccountReferenceError("connection_id")
        if team_id is not None:
            team = db.get(Team, team_id)
            if team is None or str(team.account_id) != str(account_id):
                raise CrossAccountReferenceError("team_id")
        if team_id is not None:
            from .sso_group_grants import validate_team

            validate_team(db, account_id=connection.account_id, team_id=team.id)
        row = SsoGroupMapping(
            connection_id=connection_id, idp_group=idp_group, team_id=team_id, role=role
        )
        db.add(row)
        db.flush()
        return row


class CRUDScimToken(_AccountScopedCRUD[ScimToken]):
    """SCIM tokens; plaintext is never stored."""

    def list_for_account(self, db: Session, *, account_id: Any) -> list[ScimToken]:
        """List an account's tokens, newest first."""
        return list(
            db.execute(
                select(ScimToken)
                .where(ScimToken.account_id == account_id)
                .order_by(ScimToken.created_at.desc())
            ).scalars()
        )

    def get_in_account(
        self, db: Session, *, token_id: Any, account_id: Any
    ) -> Optional[ScimToken]:
        """Return one token when it belongs to ``account_id``."""
        return db.execute(
            select(ScimToken).where(
                ScimToken.id == token_id, ScimToken.account_id == account_id
            )
        ).scalar_one_or_none()

    def create_token(
        self,
        db: Session,
        *,
        account_id: Any,
        name: str,
        token: str,
        prefix: str,
        user_id: Any = None,
    ) -> ScimToken:
        """Store the hash of ``token``; the caller commits."""
        row = ScimToken(
            account_id=account_id,
            name=name,
            token_hash=hash_scim_token(token),
            prefix=prefix,
            created_by_user_id=user_id,
        )
        db.add(row)
        db.flush()
        return row

    def get_active_by_token(self, db: Session, *, token: str) -> Optional[ScimToken]:
        """Return the unrevoked token row matching ``token``."""
        return db.execute(
            select(ScimToken).where(
                ScimToken.token_hash == hash_scim_token(token),
                ScimToken.revoked_at.is_(None),
            )
        ).scalar_one_or_none()

    def revoke(self, db: Session, *, token: ScimToken) -> ScimToken:
        """Revoke ``token`` (idempotent); the caller commits."""
        if token.revoked_at is None:
            token.revoked_at = utc_now()
            db.flush()
        return token


class CRUDSsoReplayGuard(_ThroughConnection, _AccountScopedCRUD[SsoReplayGuard]):
    """Single-use protocol ids."""

    def claim(
        self,
        db: Session,
        *,
        connection_id: Any,
        guard_id: str,
        expires_at: datetime,
    ) -> bool:
        """Record ``guard_id`` as used; False when it was used before.

        The insert is atomic, so two concurrent callbacks carrying the same id
        cannot both succeed.
        """
        result = db.execute(
            insert(SsoReplayGuard)
            .values(
                connection_id=connection_id,
                guard_id=guard_id,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(constraint="uq_sso_replay_guard_connection_id")
            .returning(SsoReplayGuard.id)
        )
        return result.first() is not None

    def consume_pending(
        self,
        db: Session,
        *,
        connection_id: Any,
        guard_id: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """Atomically remove one unexpired, server-stored pending request."""
        if not guard_id.startswith("pending:saml:"):
            raise ValueError("Only pending SAML requests may be consumed")
        result = db.execute(
            delete(SsoReplayGuard)
            .where(
                SsoReplayGuard.connection_id == connection_id,
                SsoReplayGuard.guard_id == guard_id,
                SsoReplayGuard.expires_at > (now or utc_now()),
            )
            .returning(SsoReplayGuard.id)
        )
        return result.first() is not None

    def purge_expired(self, db: Session, *, now: Optional[datetime] = None) -> int:
        """Delete expired ids and return how many were removed; caller commits."""
        result = db.execute(
            delete(SsoReplayGuard).where(SsoReplayGuard.expires_at < (now or utc_now()))
        )
        return int(getattr(result, "rowcount", 0) or 0)
