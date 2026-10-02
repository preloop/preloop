"""Managed OAuth storage contract, independent of provider services.

Every mutation owns a fresh session. Lock order is configuration, transaction,
tracker, grant. Configuration locks are shared for ordinary operations and
exclusive for replacement; independent grants can refresh concurrently.
"""

import hashlib
import math
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.db.session import get_engine
from preloop.utils.encryption import decrypt_value, encrypt_value


class OAuthConflictError(ValueError):
    """Missing, stale, expired or unauthorized OAuth state (intentionally opaque)."""


@dataclass(frozen=True)
class TokenPair:
    """Provider result; plaintext only lives in this short-lived non-ORM value."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None
    scope: str | None = None


@dataclass(frozen=True)
class CallbackSecrets:
    """Explicit internal-only provider input, never returned as model metadata."""

    client_secret: str | None = field(repr=False)
    pkce_verifier: str | None = field(repr=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("OAuth expiry must be timezone-aware")
    return value.astimezone(timezone.utc) if value is not None else None


def canonical_instance(value: str) -> str:
    """Normalize an HTTPS origin plus optional case-sensitive DC context path."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "\\" in value
    ):
        raise ValueError("Expected an HTTPS instance URL without credentials or query")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port not in (None, 443) else ""
    return f"https://{host}{port}{parsed.path.rstrip('/')}"


class CRUDManagedOAuth:
    """Account-scoped storage operations; accepts an engine, never a caller session."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    def _session(self) -> Session:
        return Session(self._engine or get_engine(), expire_on_commit=False)

    def _config(
        self,
        db: Session,
        account_id: UUID,
        configuration_id: UUID,
        *,
        exclusive: bool = False,
    ) -> models.OAuthProviderConfiguration:
        row = db.scalar(
            select(models.OAuthProviderConfiguration)
            .where(
                models.OAuthProviderConfiguration.id == configuration_id,
                models.OAuthProviderConfiguration.account_id == account_id,
            )
            .with_for_update(read=not exclusive)
        )
        if row is None:
            raise OAuthConflictError("OAuth state unavailable")
        return row

    def _owner(self, db: Session, account_id: UUID, user_id: UUID) -> None:
        if (
            db.scalar(
                select(models.User.id).where(
                    models.User.id == user_id,
                    models.User.account_id == account_id,
                    models.User.is_active.is_(True),
                )
            )
            is None
        ):
            raise OAuthConflictError("OAuth state unavailable")

    @staticmethod
    def _secret(db: Session, config: models.OAuthProviderConfiguration) -> str | None:
        if config.client_secret_id is None:
            return None
        secret = db.scalar(
            select(models.SecretReference).where(
                models.SecretReference.id == config.client_secret_id,
                models.SecretReference.account_id == config.account_id,
                models.SecretReference.secret_kind == "oauth_client_secret",
                models.SecretReference.status == "active",
            )
        )
        if secret is None or not secret.encrypted_value:
            raise OAuthConflictError("OAuth state unavailable")
        return decrypt_value(secret.encrypted_value)

    @staticmethod
    def _set_secret(
        db: Session, config: models.OAuthProviderConfiguration, value: str | None
    ) -> None:
        old_id = config.client_secret_id
        if value:
            secret = models.SecretReference(
                account_id=config.account_id,
                name="Managed OAuth consumer",
                backend_type="local_encrypted",
                secret_kind="oauth_client_secret",
                encrypted_value=encrypt_value(value),
            )
            db.add(secret)
            db.flush()
            config.client_secret_id = secret.id
        else:
            config.client_secret_id = None
        db.flush()
        if old_id is not None:
            old = db.scalar(
                select(models.SecretReference).where(
                    models.SecretReference.id == old_id,
                    models.SecretReference.account_id == config.account_id,
                )
            )
            if old is not None:
                db.delete(old)

    def get_configuration(
        self, *, account_id: UUID, configuration_id: UUID
    ) -> dict | None:
        """Read sanitized metadata only within the requested account."""
        with self._session() as db:
            row = db.scalar(
                select(models.OAuthProviderConfiguration).where(
                    models.OAuthProviderConfiguration.id == configuration_id,
                    models.OAuthProviderConfiguration.account_id == account_id,
                )
            )
            return row.to_dict() if row else None

    def create_configuration(
        self,
        *,
        account_id: UUID,
        provider: str,
        instance: str,
        client_id: str,
        client_secret: str | None,
        callback_uri: str,
        selected_permissions: list[str],
        context: str = "",
    ) -> dict:
        """Create a tenant-owned consumer with a dedicated encrypted secret reference."""
        canonical = canonical_instance(instance)
        canonical_instance(callback_uri)
        with self._session() as db, db.begin():
            row = models.OAuthProviderConfiguration(
                account_id=account_id,
                provider=provider,
                canonical_instance=canonical,
                context=context,
                client_id=client_id,
                callback_uri=callback_uri,
                selected_permissions=selected_permissions,
            )
            db.add(row)
            db.flush()
            self._set_secret(db, row, client_secret)
            return row.to_dict()

    def replace_configuration(
        self,
        *,
        account_id: UUID,
        configuration_id: UUID,
        expected_version: int,
        client_id: str,
        client_secret: str | None,
        callback_uri: str,
        selected_permissions: list[str],
        enabled: bool = True,
    ) -> dict:
        """Replace consumer credentials and invalidate all old-version handshakes/grants.

        Provider, instance and context are immutable identities. Create a new
        configuration to change them. Existing trackers require reconnect.
        """
        canonical_instance(callback_uri)
        with self._session() as db, db.begin():
            row = self._config(db, account_id, configuration_id, exclusive=True)
            if row.version != expected_version:
                raise OAuthConflictError("OAuth state unavailable")
            row.version += 1
            row.client_id = client_id
            self._set_secret(db, row, client_secret)
            row.callback_uri = callback_uri
            row.selected_permissions = selected_permissions
            row.enabled = enabled
            transactions = db.scalars(
                select(models.OAuthConnectionTransaction)
                .where(
                    models.OAuthConnectionTransaction.configuration_id == row.id,
                    models.OAuthConnectionTransaction.status.in_(
                        ("pending", "claimed")
                    ),
                )
                .with_for_update()
            ).all()
            for transaction in transactions:
                transaction.status = "invalidated"
                transaction.pkce_verifier_encrypted = None
                transaction.pending_grant_id = None
            db.flush()
            grants = db.scalars(
                select(models.OAuthToken)
                .where(models.OAuthToken.configuration_id == row.id)
                .with_for_update()
            ).all()
            for grant in grants:
                self._erase(grant, "invalidated")
            db.flush()
            return row.to_dict()

    def begin_connection(
        self,
        *,
        account_id: UUID,
        user_id: UUID,
        session_id: str,
        configuration_id: UUID,
        return_path: str,
        pkce_verifier: str | None = None,
    ) -> tuple[dict, str]:
        """Issue random state once; store its hash with a ten-minute owner binding."""
        if (
            not session_id
            or not return_path.startswith("/")
            or return_path.startswith("//")
            or "\\" in return_path
            or any(ord(c) < 32 for c in return_path)
        ):
            raise ValueError("A session and local return path are required")
        state = secrets.token_urlsafe(32)
        with self._session() as db, db.begin():
            config = self._config(db, account_id, configuration_id)
            self._owner(db, account_id, user_id)
            if not config.enabled:
                raise OAuthConflictError("OAuth state unavailable")
            row = models.OAuthConnectionTransaction(
                account_id=account_id,
                user_id=user_id,
                session_hash=_hash(session_id),
                state_hash=_hash(state),
                provider=config.provider,
                configuration_id=config.id,
                configuration_version=config.version,
                callback_uri=config.callback_uri,
                return_path=return_path,
                expires_at=_now() + timedelta(minutes=10),
                pkce_verifier_encrypted=encrypt_value(pkce_verifier)
                if pkce_verifier
                else None,
            )
            db.add(row)
            db.flush()
            return row.to_dict(), state

    def _transaction(
        self,
        db: Session,
        *,
        account_id: UUID,
        user_id: UUID,
        session_id: str,
        state: str,
        callback_uri: str,
    ) -> tuple[models.OAuthConnectionTransaction, models.OAuthProviderConfiguration]:
        self._owner(db, account_id, user_id)
        query = select(models.OAuthConnectionTransaction).where(
            models.OAuthConnectionTransaction.account_id == account_id,
            models.OAuthConnectionTransaction.user_id == user_id,
            models.OAuthConnectionTransaction.session_hash == _hash(session_id),
            models.OAuthConnectionTransaction.state_hash == _hash(state),
            models.OAuthConnectionTransaction.callback_uri == callback_uri,
        )
        # Read just the identity before the config lock, then reread under lock.
        configuration_id = db.scalar(
            query.with_only_columns(models.OAuthConnectionTransaction.configuration_id)
        )
        if configuration_id is None:
            raise OAuthConflictError("OAuth state unavailable")
        config = self._config(db, account_id, configuration_id)
        row = db.scalar(query.with_for_update())
        if row is None or row.status == "invalidated":
            raise OAuthConflictError("OAuth state unavailable")
        if row.status != "completed" and (
            row.expires_at <= _now()
            or not config.enabled
            or row.configuration_version != config.version
        ):
            raise OAuthConflictError("OAuth state unavailable")
        return row, config

    def claim_callback(
        self,
        *,
        account_id: UUID,
        user_id: UUID,
        session_id: str,
        state: str,
        callback_uri: str,
    ) -> tuple[dict, CallbackSecrets]:
        """Consume callback state exactly once and explicitly release provider secrets."""
        with self._session() as db, db.begin():
            row, config = self._transaction(
                db,
                account_id=account_id,
                user_id=user_id,
                session_id=session_id,
                state=state,
                callback_uri=callback_uri,
            )
            if row.status != "pending":
                raise OAuthConflictError("OAuth state unavailable")
            row.status = "claimed"
            result = CallbackSecrets(
                self._secret(db, config),
                decrypt_value(row.pkce_verifier_encrypted)
                if row.pkce_verifier_encrypted
                else None,
            )
            row.pkce_verifier_encrypted = None
            db.flush()
            return row.to_dict(), result

    def store_pending_grant(
        self,
        *,
        account_id: UUID,
        user_id: UUID,
        session_id: str,
        state: str,
        callback_uri: str,
        provider_subject: str,
        tokens: TokenPair,
    ) -> dict:
        """Persist an exchanged pair under its initiating claimed transaction."""
        if not provider_subject:
            raise ValueError("Provider subject is required")
        with self._session() as db, db.begin():
            row, config = self._transaction(
                db,
                account_id=account_id,
                user_id=user_id,
                session_id=session_id,
                state=state,
                callback_uri=callback_uri,
            )
            if row.status != "claimed" or row.pending_grant_id is not None:
                raise OAuthConflictError("OAuth state unavailable")
            grant = models.OAuthToken(
                account_id=account_id,
                user_id=user_id,
                provider=config.provider,
                auth_mode="managed_oauth",
                status="pending",
                configuration_id=config.id,
                configuration_version=config.version,
                canonical_instance=config.canonical_instance,
                provider_subject=provider_subject,
                connection_transaction_id=row.id,
            )
            self._write_pair(grant, tokens)
            db.add(grant)
            db.flush()
            row.pending_grant_id = grant.id
            db.flush()
            return grant.to_dict()

    def complete_connection(
        self,
        *,
        account_id: UUID,
        user_id: UUID,
        session_id: str,
        state: str,
        callback_uri: str,
        tracker_name: str,
        tracker_id: UUID | None = None,
        expected_rotation_version: int | None = None,
    ) -> UUID:
        """Bind once, or replace a grant on reconnect under the same grant lock.

        Same-owner retries return the original tracker even after transaction expiry.
        Reconnect to an attached grant requires its current rotation version.
        """
        with self._session() as db, db.begin():
            row, config = self._transaction(
                db,
                account_id=account_id,
                user_id=user_id,
                session_id=session_id,
                state=state,
                callback_uri=callback_uri,
            )
            if row.status == "completed":
                return row.tracker_id
            if row.status != "claimed" or row.pending_grant_id is None:
                raise OAuthConflictError("OAuth state unavailable")
            if tracker_id is None:
                tracker = models.Tracker(
                    account_id=account_id,
                    name=tracker_name,
                    tracker_type=config.provider,
                    url=config.canonical_instance,
                    auth_type="managed_oauth",
                    api_key=None,
                )
                db.add(tracker)
                db.flush()
            else:
                tracker = db.scalar(
                    select(models.Tracker)
                    .where(
                        models.Tracker.id == tracker_id,
                        models.Tracker.account_id == account_id,
                    )
                    .with_for_update()
                )
                if (
                    tracker is None
                    or tracker.is_deleted
                    or tracker.tracker_type != config.provider
                    or tracker.url != config.canonical_instance
                    or tracker.auth_type != "managed_oauth"
                    or tracker.api_key
                    or tracker.credentials_secret_id
                ):
                    raise OAuthConflictError("OAuth state unavailable")
            old = db.scalar(
                select(models.OAuthToken)
                .where(
                    models.OAuthToken.tracker_id == tracker.id,
                    models.OAuthToken.account_id == account_id,
                )
                .with_for_update()
            )
            pending = db.scalar(
                select(models.OAuthToken)
                .where(
                    models.OAuthToken.id == row.pending_grant_id,
                    models.OAuthToken.account_id == account_id,
                )
                .with_for_update()
            )
            if (
                pending is None
                or pending.status != "pending"
                or pending.connection_transaction_id != row.id
            ):
                raise OAuthConflictError("OAuth state unavailable")
            if old is not None:
                if (
                    old.configuration_id != config.id
                    or old.rotation_version != expected_rotation_version
                ):
                    raise OAuthConflictError("OAuth state unavailable")
                old.access_token_encrypted = pending.access_token_encrypted
                old.refresh_token_encrypted = pending.refresh_token_encrypted
                old.expires_at = pending.expires_at
                old.refresh_token_expires_at = pending.refresh_token_expires_at
                old.scope = pending.scope
                old.user_id = user_id
                old.provider_subject = pending.provider_subject
                old.configuration_version = config.version
                old.status = "active"
                old.rotation_version += 1
                row.pending_grant_id = None
                db.flush()
                db.delete(pending)
            else:
                if expected_rotation_version is not None:
                    raise OAuthConflictError("OAuth state unavailable")
                pending.tracker_id = tracker.id
                pending.status = "active"
                pending.connection_transaction_id = None
                row.pending_grant_id = None
            row.status = "completed"
            row.tracker_id = tracker.id
            row.pkce_verifier_encrypted = None
            tracker.is_active = True
            db.flush()
            return tracker.id

    def get_grant(
        self,
        *,
        account_id: UUID,
        grant_id: UUID | None = None,
        tracker_id: UUID | None = None,
    ) -> dict | None:
        """Look up sanitized managed metadata by grant or tracker within a tenant."""
        if (grant_id is None) == (tracker_id is None):
            raise ValueError("Specify exactly one grant or tracker")
        with self._session() as db:
            query = select(models.OAuthToken).where(
                models.OAuthToken.account_id == account_id,
                models.OAuthToken.auth_mode == "managed_oauth",
            )
            query = (
                query.where(models.OAuthToken.id == grant_id)
                if grant_id
                else query.where(models.OAuthToken.tracker_id == tracker_id)
            )
            row = db.scalar(query)
            return row.to_dict() if row else None

    def _grant(
        self, db: Session, account_id: UUID, grant_id: UUID
    ) -> tuple[models.OAuthToken, models.OAuthProviderConfiguration]:
        configuration_id = db.scalar(
            select(models.OAuthToken.configuration_id).where(
                models.OAuthToken.id == grant_id,
                models.OAuthToken.account_id == account_id,
                models.OAuthToken.auth_mode == "managed_oauth",
            )
        )
        if configuration_id is None:
            raise OAuthConflictError("OAuth state unavailable")
        config = self._config(db, account_id, configuration_id)
        grant = db.scalar(
            select(models.OAuthToken)
            .where(
                models.OAuthToken.id == grant_id,
                models.OAuthToken.account_id == account_id,
            )
            .with_for_update()
        )
        if grant is None:
            raise OAuthConflictError("OAuth state unavailable")
        return grant, config

    @staticmethod
    def _write_pair(grant: models.OAuthToken, pair: TokenPair) -> None:
        if not pair.access_token:
            raise ValueError("Access token is required")
        grant.expires_at = _utc(pair.expires_at)
        grant.refresh_token_expires_at = _utc(pair.refresh_token_expires_at)
        grant.access_token_encrypted = encrypt_value(pair.access_token)
        grant.refresh_token_encrypted = (
            encrypt_value(pair.refresh_token) if pair.refresh_token else None
        )
        grant.scope = pair.scope

    @staticmethod
    def _erase(grant: models.OAuthToken, status: str) -> None:
        grant.access_token_encrypted = ""
        grant.refresh_token_encrypted = None
        grant.expires_at = None
        grant.refresh_token_expires_at = None
        grant.status = status
        grant.rotation_version += 1

    def rotate(
        self,
        *,
        account_id: UUID,
        grant_id: UUID,
        expected_version: int,
        refresh: Callable[[TokenPair, CallbackSecrets, float], TokenPair],
        timeout: float = 10,
    ) -> dict:
        """Reread and rotate a pair atomically in a dedicated locked transaction.

        Provider receives only detached secret values and a transport timeout.
        The hard wait is also bounded: a late result cannot write to storage.
        Providers must honor the supplied transport timeout to release I/O resources.
        Invoke this synchronous API from a worker thread in async services.
        """
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Refresh timeout must be between 0 and 60 seconds")
        with self._session() as db, db.begin():
            grant, config = self._grant(db, account_id, grant_id)
            if (
                grant.status != "active"
                or grant.rotation_version != expected_version
                or not config.enabled
                or grant.configuration_version != config.version
            ):
                raise OAuthConflictError("OAuth state unavailable")
            pair = TokenPair(
                decrypt_value(grant.access_token_encrypted),
                decrypt_value(grant.refresh_token_encrypted)
                if grant.refresh_token_encrypted
                else None,
                grant.expires_at,
                grant.refresh_token_expires_at,
                grant.scope,
            )
            credentials = CallbackSecrets(
                self._secret(db, config),
                None,
            )
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                result = executor.submit(refresh, pair, credentials, timeout).result(
                    timeout=timeout
                )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            self._write_pair(grant, result)
            grant.rotation_version += 1
            db.flush()
            return grant.to_dict()

    def disconnect(
        self, *, account_id: UUID, grant_id: UUID, expected_version: int
    ) -> None:
        """Erase secrets and detach the tracker, retaining a stale-write tombstone."""
        with self._session() as db, db.begin():
            # Lock tracker before grant, matching completion's order.
            configuration_id = db.scalar(
                select(models.OAuthToken.configuration_id).where(
                    models.OAuthToken.id == grant_id,
                    models.OAuthToken.account_id == account_id,
                )
            )
            if configuration_id is None:
                raise OAuthConflictError("OAuth state unavailable")
            self._config(db, account_id, configuration_id)
            tracker_id = db.scalar(
                select(models.OAuthToken.tracker_id).where(
                    models.OAuthToken.id == grant_id
                )
            )
            tracker = (
                db.scalar(
                    select(models.Tracker)
                    .where(models.Tracker.id == tracker_id)
                    .with_for_update()
                )
                if tracker_id
                else None
            )
            grant, _ = self._grant(db, account_id, grant_id)
            if grant.rotation_version != expected_version or grant.status == "pending":
                raise OAuthConflictError("OAuth state unavailable")
            self._erase(grant, "disconnected")
            grant.tracker_id = None
            if tracker is not None:
                tracker.is_active = False

    def cleanup_expired(self, *, account_id: UUID) -> int:
        """Delete expired unfinished handshakes and their encrypted pending grants."""
        with self._session() as db, db.begin():
            ids = db.execute(
                select(
                    models.OAuthConnectionTransaction.id,
                    models.OAuthConnectionTransaction.configuration_id,
                ).where(
                    models.OAuthConnectionTransaction.account_id == account_id,
                    models.OAuthConnectionTransaction.status != "completed",
                    models.OAuthConnectionTransaction.expires_at <= _now(),
                )
            ).all()
        count = 0
        for transaction_id, configuration_id in ids:
            with self._session() as db, db.begin():
                self._config(db, account_id, configuration_id)
                row = db.scalar(
                    select(models.OAuthConnectionTransaction)
                    .where(models.OAuthConnectionTransaction.id == transaction_id)
                    .with_for_update()
                )
                if row is None or row.status == "completed" or row.expires_at > _now():
                    continue
                row.pending_grant_id = None
                db.flush()
                db.delete(row)  # FK cascade removes its pending grant.
                count += 1
        return count


crud_managed_oauth = CRUDManagedOAuth()
