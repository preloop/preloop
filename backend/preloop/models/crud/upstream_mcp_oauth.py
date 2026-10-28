"""Atomic upstream OAuth state consumption, credential rotation and invalidation.

Lock order is always MCP server, connection, transaction/secret. Network I/O
never holds a database lock. A lost refresh lease requires reauthorization,
not replay of a possibly consumed token. Completion compares version and lease.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.utils.encryption import decrypt_value, encrypt_value


class OAuthContextError(ValueError):
    """Expired, stale or mismatched upstream authorization context."""


@dataclass(frozen=True)
class OAuthSnapshot:
    """Internal credential snapshot; never serialize or log its config."""

    server_id: UUID
    account_id: UUID
    version: int
    url: str
    transport: str
    config: dict[str, Any] = field(repr=False)
    lease_id: UUID | None = None
    verifier: str | None = field(default=None, repr=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def server_digest(server: models.MCPServer) -> str:
    """Detect replacement even by policy rollback or a writer outside this CRUD."""
    return digest(
        json.dumps(
            [
                server.url,
                server.transport,
                server.auth_type,
                server.auth_config,
                server.status == "disabled",
            ],
            sort_keys=True,
            default=str,
        )
    )


def public_auth_config(value: Any) -> Any:
    """Strip nested secret fields before any JSONB write or autoflush."""
    from preloop.models.schemas.mcp_server import _is_secret_auth_key

    if isinstance(value, dict):
        return {
            key: public_auth_config(item)
            for key, item in value.items()
            if not _is_secret_auth_key(key)
        }
    if isinstance(value, list):
        return [public_auth_config(item) for item in value]
    return value


class CRUDUpstreamMCPOAuth:
    """All upstream OAuth persistence, including encrypted reference ownership."""

    def _server(
        self, db: Session, server_id: UUID, account_id: UUID | None = None
    ) -> models.MCPServer:
        query = db.query(models.MCPServer).filter(models.MCPServer.id == server_id)
        if account_id is not None:
            query = query.filter(models.MCPServer.account_id == account_id)
        server = query.populate_existing().with_for_update().one_or_none()
        if server is None or server.auth_type != "oauth" or server.status == "disabled":
            raise OAuthContextError(
                "OAuth server is unavailable. Check its configuration."
            )
        return server

    def _connection(
        self, db: Session, server: models.MCPServer
    ) -> models.UpstreamMCPOAuthConnection:
        row = (
            db.query(models.UpstreamMCPOAuthConnection)
            .filter_by(server_id=server.id, account_id=server.account_id)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            row = models.UpstreamMCPOAuthConnection(
                server_id=server.id,
                account_id=server.account_id,
                version=1,
                config_digest=server_digest(server),
                state="connected"
                if (server.auth_config or {}).get("access_token")
                else "disconnected",
            )
            db.add(row)
            db.flush()
        elif row.config_digest != server_digest(server):
            self.invalidate(db, server.id)
            row.config_digest = server_digest(server)
            row.state = (
                "connected"
                if (server.auth_config or {}).get("access_token")
                else "disconnected"
            )
        return row

    def _config(
        self,
        db: Session,
        server: models.MCPServer,
        row: models.UpstreamMCPOAuthConnection,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if row.secret_id:
            secret = (
                db.query(models.SecretReference)
                .filter_by(
                    id=row.secret_id,
                    account_id=server.account_id,
                    secret_kind="upstream_mcp_oauth",
                )
                .one_or_none()
            )
            if secret is None:
                raise OAuthContextError(
                    "OAuth credentials are unavailable. Reconnect the account."
                )
            from preloop.services.secret_service import get_secret_service

            config = json.loads(
                get_secret_service().resolve_secret_reference(secret).value
            )

        def overlay(stored: dict[str, Any], public: dict[str, Any]) -> dict[str, Any]:
            merged = dict(stored)
            for key, value in public.items():
                merged[key] = (
                    overlay(merged[key], value)
                    if isinstance(value, dict) and isinstance(merged.get(key), dict)
                    else value
                )
            return merged

        return overlay(config, server.auth_config or {})

    def _store(
        self,
        db: Session,
        server: models.MCPServer,
        row: models.UpstreamMCPOAuthConnection,
        config: dict[str, Any],
    ) -> None:
        secret = None
        if row.secret_id:
            secret = (
                db.query(models.SecretReference)
                .filter_by(
                    id=row.secret_id,
                    account_id=server.account_id,
                    secret_kind="upstream_mcp_oauth",
                )
                .with_for_update()
                .one_or_none()
            )
            if secret is None:
                raise OAuthContextError("OAuth credential ownership mismatch.")
        if secret is None:
            secret = models.SecretReference(
                account_id=server.account_id,
                name="Upstream MCP OAuth",
                backend_type="local_encrypted",
                secret_kind="upstream_mcp_oauth",
                status="active",
            )
            db.add(secret)
        secret.encrypted_value = encrypt_value(json.dumps(config))
        db.flush()
        row.secret_id = secret.id
        # Keep the API's non-secret setup editable. Tokens and client secrets
        # live only in SecretReference; no ciphertext in config or snapshots.
        server.auth_config = public_auth_config(config)
        row.config_digest = server_digest(server)

    def setup(self, db: Session, server_id: UUID, account_id: UUID) -> OAuthSnapshot:
        server = self._server(db, server_id, account_id)
        row = self._connection(db, server)
        snapshot = OAuthSnapshot(
            server.id,
            server.account_id,
            row.version,
            server.url,
            server.transport,
            self._config(db, server, row),
        )
        db.commit()
        return snapshot

    def cleanup(self, db: Session) -> int:
        """Remove expired encrypted handshakes; called on setup and callback."""
        return (
            db.query(models.UpstreamMCPOAuthTransaction)
            .filter(
                models.UpstreamMCPOAuthTransaction.expires_at
                <= datetime.now(timezone.utc)
            )
            .delete(synchronize_session=False)
        )

    def begin(
        self,
        db: Session,
        snapshot: OAuthSnapshot,
        *,
        user_id: UUID,
        state: str,
        browser_nonce: str,
        verifier: str,
        config: dict[str, Any],
    ) -> None:
        server = self._server(db, snapshot.server_id, snapshot.account_id)
        row = self._connection(db, server)
        if row.version != snapshot.version:
            raise OAuthContextError(
                "OAuth configuration changed. Start authorization again."
            )
        user = (
            db.query(models.User)
            .filter_by(id=user_id, account_id=server.account_id, is_active=True)
            .one_or_none()
        )
        if user is None:
            raise OAuthContextError("OAuth initiating principal is unavailable.")
        self.cleanup(db)
        db.query(models.UpstreamMCPOAuthTransaction).filter_by(
            server_id=server.id
        ).delete(synchronize_session=False)
        row.version += 1
        row.state = "connecting"
        row.lease_id = row.lease_until = row.retry_at = None
        # Exclude any old grant from this new authorization transaction.
        context = {
            key: value
            for key, value in config.items()
            if key not in {"access_token", "refresh_token", "expires_at"}
        }
        db.add(
            models.UpstreamMCPOAuthTransaction(
                server_id=server.id,
                account_id=server.account_id,
                user_id=user.id,
                auth_generation=user.auth_generation or 0,
                version=row.version,
                state_hash=digest(state),
                browser_hash=digest(browser_nonce),
                issuer=config["issuer"],
                redirect_uri=config["redirect_uri"],
                context_encrypted=encrypt_value(
                    json.dumps({"config": context, "verifier": verifier})
                ),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        db.commit()

    def consume(
        self,
        db: Session,
        *,
        state: str,
        browser_nonce: str,
        redirect_uri: str,
        issuer: str | None = None,
        account_id: UUID | None = None,
        server_id: UUID | None = None,
    ) -> OAuthSnapshot:
        """Consume once across workers before token exchange, checking every binding."""
        transaction = (
            db.query(models.UpstreamMCPOAuthTransaction)
            .filter_by(state_hash=digest(state))
            .one_or_none()
        )
        if transaction is None:
            raise OAuthContextError(
                "Invalid or expired OAuth state. Start authorization again."
            )
        server = self._server(db, transaction.server_id, transaction.account_id)
        row = self._connection(db, server)
        transaction = (
            db.query(models.UpstreamMCPOAuthTransaction)
            .filter_by(state_hash=digest(state))
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if transaction is None:
            raise OAuthContextError("OAuth state has already been consumed.")
        user = (
            db.query(models.User)
            .filter_by(
                id=transaction.user_id,
                account_id=transaction.account_id,
                is_active=True,
            )
            .one_or_none()
        )
        context = json.loads(decrypt_value(transaction.context_encrypted))
        if (
            (
                context["config"].get("authorization_response_iss_parameter_supported")
                is True
                and issuer is None
            )
            or transaction.expires_at <= datetime.now(timezone.utc)
            or not hmac.compare_digest(transaction.browser_hash, digest(browser_nonce))
            or transaction.redirect_uri != redirect_uri
            or transaction.version != row.version
            or (issuer is not None and issuer != transaction.issuer)
            or (account_id is not None and transaction.account_id != account_id)
            or (server_id is not None and transaction.server_id != server_id)
            or user is None
            or (user.auth_generation or 0) != transaction.auth_generation
        ):
            raise OAuthContextError(
                "OAuth authorization context is expired or mismatched."
            )
        snapshot = OAuthSnapshot(
            server.id,
            server.account_id,
            row.version,
            server.url,
            server.transport,
            context["config"],
            verifier=context["verifier"],
        )
        db.delete(transaction)
        self.cleanup(db)
        db.commit()
        return snapshot

    def complete(
        self, db: Session, snapshot: OAuthSnapshot, config: dict[str, Any]
    ) -> bool:
        """CAS both tokens and expiry in the same commit; stale results lose."""
        server = self._server(db, snapshot.server_id, snapshot.account_id)
        row = self._connection(db, server)
        if row.version != snapshot.version or row.lease_id != snapshot.lease_id:
            db.commit()
            return False
        server.status = "active"
        server.last_error = None
        self._store(db, server, row, config)
        row.version += 1
        row.state = "connected"
        row.lease_id = row.lease_until = row.retry_at = None
        db.commit()
        return True

    def prepare(
        self, db: Session, server_id: UUID, *, force_version: int | None = None
    ) -> OAuthSnapshot | None:
        """Read current credentials or claim one 30s lease. None means busy."""
        server = self._server(db, server_id)
        row = self._connection(db, server)
        now = datetime.now(timezone.utc)
        config = self._config(db, server, row)
        if row.lease_id:
            if row.lease_until and row.lease_until > now:
                db.commit()
                return None
            # The worker may have died after rotating upstream. Never reuse it.
            row.lease_id = row.lease_until = None
            row.state = "reconnect_required"
        if row.state in {
            "connecting",
            "reconnect_required",
            "disconnected",
        } or not config.get("access_token"):
            db.commit()
            raise OAuthContextError(
                "OAuth account must be connected or reconnected in Tools setup."
            )
        if row.retry_at and row.retry_at > now:
            db.commit()
            raise OAuthContextError(
                "OAuth provider rate limited refresh. Try again later."
            )
        expires = config.get("expires_at")
        try:
            needs_refresh = (
                expires is not None and float(expires) <= time.time() + 60
            ) or force_version == row.version
        except (ValueError, TypeError):
            needs_refresh = True
        if needs_refresh:
            if not all(
                config.get(key)
                for key in ("refresh_token", "token_endpoint", "client_id")
            ):
                row.state = "reconnect_required"
                db.commit()
                raise OAuthContextError(
                    "OAuth token expired without refresh credentials. Reconnect the account."
                )
            row.lease_id = uuid4()
            row.lease_until = now + timedelta(seconds=30)
            config.setdefault("resource", server.url)
            config.setdefault(
                "token_endpoint_auth_method",
                "client_secret_post" if config.get("client_secret") else "none",
            )
        snapshot = OAuthSnapshot(
            server.id,
            server.account_id,
            row.version,
            server.url,
            server.transport,
            config,
            lease_id=row.lease_id,
        )
        db.commit()
        return snapshot

    def fail(
        self, db: Session, snapshot: OAuthSnapshot, *, retry_after: float | None = None
    ) -> None:
        """Release a lease without making an ambiguous token reusable."""
        server = self._server(db, snapshot.server_id, snapshot.account_id)
        row = self._connection(db, server)
        if row.version == snapshot.version and row.lease_id == snapshot.lease_id:
            row.lease_id = row.lease_until = None
            row.state = (
                "connected"
                if retry_after is not None and snapshot.lease_id
                else "reconnect_required"
            )
            row.retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=retry_after)
                if retry_after is not None
                else None
            )
        db.commit()

    def invalidate(self, db: Session, server_id: UUID) -> None:
        """Invalidate under the caller's server lock, without committing it."""
        row = (
            db.query(models.UpstreamMCPOAuthConnection)
            .filter_by(server_id=server_id)
            .with_for_update()
            .one_or_none()
        )
        if row is not None:
            row.version += 1
            row.state = "disconnected"
            row.lease_id = row.lease_until = row.retry_at = None
            if row.secret_id:
                secret = (
                    db.query(models.SecretReference)
                    .filter_by(
                        id=row.secret_id,
                        account_id=row.account_id,
                        secret_kind="upstream_mcp_oauth",
                    )
                    .one_or_none()
                )
                row.secret_id = None
                if secret:
                    db.delete(secret)
            db.query(models.UpstreamMCPOAuthTransaction).filter_by(
                server_id=server_id
            ).delete(synchronize_session=False)

    def disconnect(self, db: Session, server_id: UUID, account_id: UUID) -> None:
        """Clear local grants and transactions; retain encrypted client registration."""
        server = self._server(db, server_id, account_id)
        row = self._connection(db, server)
        config = self._config(db, server, row)
        for key in ("access_token", "refresh_token", "expires_at", "token_type"):
            config.pop(key, None)
        self.invalidate(db, server_id)
        self._store(db, server, row, config)
        db.commit()

    def config_for_edit(self, db: Session, server: models.MCPServer) -> dict[str, Any]:
        """Internal write-only merge input; response serialization redacts it."""
        row = (
            db.query(models.UpstreamMCPOAuthConnection)
            .filter_by(server_id=server.id, account_id=server.account_id)
            .one_or_none()
        )
        if row is None or row.config_digest != server_digest(server):
            return dict(server.auth_config or {})
        return self._config(db, server, row)

    def protect_config(
        self, db: Session, server: models.MCPServer, config: dict[str, Any]
    ) -> None:
        """Persist newly configured OAuth secrets without plaintext JSON storage."""
        row = self._connection(db, server)
        self._store(db, server, row, config)
        row.state = "connected" if config.get("access_token") else "disconnected"

    def state(self, db: Session, server: models.MCPServer) -> str | None:
        """Public connection state; expired handshakes never remain connecting."""
        return self.states_for_servers(db, [server])[server.id]

    def states_for_servers(
        self, db: Session, servers: list[models.MCPServer]
    ) -> dict[UUID, str | None]:
        """Read public states in one query without loading encrypted credentials."""
        oauth_servers = {s.id: s for s in servers if s.auth_type == "oauth"}
        states = {
            s.id: (
                (
                    "connected"
                    if (s.auth_config or {}).get("access_token")
                    else "disconnected"
                )
                if s.auth_type == "oauth"
                else None
            )
            for s in servers
        }
        if not oauth_servers:
            return states
        connection = models.UpstreamMCPOAuthConnection
        transaction = models.UpstreamMCPOAuthTransaction
        pending = (
            select(transaction.id)
            .where(
                transaction.server_id == connection.server_id,
                transaction.account_id == connection.account_id,
                transaction.version == connection.version,
                transaction.expires_at > datetime.now(timezone.utc),
            )
            .correlate(connection)
            .exists()
        )
        rows = db.query(connection, pending).filter(
            tuple_(connection.server_id, connection.account_id).in_(
                [(s.id, s.account_id) for s in oauth_servers.values()]
            )
        )
        for row, has_pending in rows:
            if row.config_digest != server_digest(oauth_servers[row.server_id]):
                continue
            states[row.server_id] = (
                "reconnect_required"
                if row.state == "connecting" and not has_pending
                else row.state
            )
        return states


crud_upstream_mcp_oauth = CRUDUpstreamMCPOAuth()
