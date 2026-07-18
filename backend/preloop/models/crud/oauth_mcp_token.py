"""CRUD operations for OAuth MCP token models (auth codes, access tokens, refresh tokens)."""

import hashlib
import secrets
import time
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from ..models.oauth_mcp_token import (
    OAuthMCPAccessToken,
    OAuthMCPAuthorizationCode,
    OAuthMCPRefreshToken,
)


def _hash_token(token: str) -> str:
    """Return a one-way fingerprint for opaque OAuth token lookup/storage.

    OAuth access/refresh tokens are high-entropy secrets; this is not password
    hashing and must stay fast for token introspection.
    """
    # codeql[py/weak-sensitive-data-hashing] Opaque token fingerprint, not password storage
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token(nbytes: int = 32) -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_urlsafe(nbytes)


def generate_authorization_code() -> str:
    """Generate an authorization code with >= 160 bits of entropy (RFC 6749 §10.10)."""
    return secrets.token_urlsafe(24)  # 192 bits


# --- Authorization Code CRUD ---


class CRUDOAuthMCPAuthorizationCode:
    """CRUD for OAuth MCP authorization codes."""

    def create(
        self,
        db: Session,
        *,
        code: str,
        client_id: str,
        user_id: UUID,
        account_id: UUID,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        code_challenge: str,
        scopes: list[str],
        expires_at: float,
        resource: Optional[str] = None,
    ) -> OAuthMCPAuthorizationCode:
        """Store an authorization code (hashed)."""
        obj = OAuthMCPAuthorizationCode(
            code_hash=_hash_token(code),
            client_id=client_id,
            user_id=user_id,
            account_id=account_id,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
            code_challenge=code_challenge,
            scopes=scopes,
            expires_at=expires_at,
            resource=resource,
            is_used=False,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    def get_by_code(
        self, db: Session, *, code: str, client_id: str
    ) -> Optional[OAuthMCPAuthorizationCode]:
        """Look up an authorization code by its hash and client_id."""
        return (
            db.query(OAuthMCPAuthorizationCode)
            .filter(
                OAuthMCPAuthorizationCode.code_hash == _hash_token(code),
                OAuthMCPAuthorizationCode.client_id == client_id,
            )
            .first()
        )

    def get_by_code_hash(
        self, db: Session, *, code: str
    ) -> Optional[OAuthMCPAuthorizationCode]:
        """Look up an authorization code by its hash only (no client_id filter).

        Used when the token request omits client_id (e.g. Codex CLI).
        """
        return (
            db.query(OAuthMCPAuthorizationCode)
            .filter(
                OAuthMCPAuthorizationCode.code_hash == _hash_token(code),
            )
            .first()
        )

    def mark_used(self, db: Session, *, obj: OAuthMCPAuthorizationCode) -> None:
        """Mark an authorization code as consumed."""
        obj.is_used = True
        db.add(obj)
        db.commit()

    def delete_expired(self, db: Session) -> int:
        """Delete expired or used authorization codes. Returns count deleted."""
        now = time.time()
        expired = (
            db.query(OAuthMCPAuthorizationCode)
            .filter(
                (OAuthMCPAuthorizationCode.expires_at < now)
                | (OAuthMCPAuthorizationCode.is_used == True)  # noqa: E712
            )
            .all()
        )
        count = len(expired)
        for obj in expired:
            db.delete(obj)
        if count:
            db.commit()
        return count


# --- Access Token CRUD ---


class CRUDOAuthMCPAccessToken:
    """CRUD for OAuth MCP access tokens."""

    def create(
        self,
        db: Session,
        *,
        token: str,
        client_id: str,
        user_id: UUID,
        account_id: UUID,
        scopes: list[str],
        expires_at: Optional[int] = None,
        resource: Optional[str] = None,
    ) -> OAuthMCPAccessToken:
        """Store an access token (hashed)."""
        obj = OAuthMCPAccessToken(
            token_hash=_hash_token(token),
            client_id=client_id,
            user_id=user_id,
            account_id=account_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=resource,
            is_revoked=False,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    def get_by_token(self, db: Session, *, token: str) -> Optional[OAuthMCPAccessToken]:
        """Look up an access token by its hash."""
        return (
            db.query(OAuthMCPAccessToken)
            .filter(OAuthMCPAccessToken.token_hash == _hash_token(token))
            .first()
        )

    def revoke(self, db: Session, *, obj: OAuthMCPAccessToken) -> None:
        """Revoke an access token."""
        obj.is_revoked = True
        db.add(obj)
        db.commit()

    def revoke_by_user_and_client(
        self, db: Session, *, user_id: UUID, client_id: str
    ) -> int:
        """Revoke all access tokens for a user+client pair. Returns count."""
        tokens = (
            db.query(OAuthMCPAccessToken)
            .filter(
                OAuthMCPAccessToken.user_id == user_id,
                OAuthMCPAccessToken.client_id == client_id,
                OAuthMCPAccessToken.is_revoked == False,  # noqa: E712
            )
            .all()
        )
        for t in tokens:
            t.is_revoked = True
            db.add(t)
        if tokens:
            db.commit()
        return len(tokens)

    def delete_expired_and_revoked(self, db: Session) -> int:
        """Delete expired or revoked access tokens. Returns count deleted."""
        now = int(time.time())
        stale = (
            db.query(OAuthMCPAccessToken)
            .filter(
                (OAuthMCPAccessToken.is_revoked == True)  # noqa: E712
                | (
                    OAuthMCPAccessToken.expires_at.isnot(None)
                    & (OAuthMCPAccessToken.expires_at < now)
                )
            )
            .all()
        )
        count = len(stale)
        for obj in stale:
            db.delete(obj)
        if count:
            db.commit()
        return count


# --- Refresh Token CRUD ---


class CRUDOAuthMCPRefreshToken:
    """CRUD for OAuth MCP refresh tokens."""

    def create(
        self,
        db: Session,
        *,
        token: str,
        client_id: str,
        user_id: UUID,
        account_id: UUID,
        scopes: list[str],
        expires_at: Optional[int] = None,
    ) -> OAuthMCPRefreshToken:
        """Store a refresh token (hashed)."""
        obj = OAuthMCPRefreshToken(
            token_hash=_hash_token(token),
            client_id=client_id,
            user_id=user_id,
            account_id=account_id,
            scopes=scopes,
            expires_at=expires_at,
            is_revoked=False,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    def get_by_token(
        self, db: Session, *, token: str
    ) -> Optional[OAuthMCPRefreshToken]:
        """Look up a refresh token by its hash."""
        return (
            db.query(OAuthMCPRefreshToken)
            .filter(OAuthMCPRefreshToken.token_hash == _hash_token(token))
            .first()
        )

    def revoke(self, db: Session, *, obj: OAuthMCPRefreshToken) -> None:
        """Revoke a refresh token."""
        obj.is_revoked = True
        db.add(obj)
        db.commit()

    def revoke_by_user_and_client(
        self, db: Session, *, user_id: UUID, client_id: str
    ) -> int:
        """Revoke all refresh tokens for a user+client pair. Returns count."""
        tokens = (
            db.query(OAuthMCPRefreshToken)
            .filter(
                OAuthMCPRefreshToken.user_id == user_id,
                OAuthMCPRefreshToken.client_id == client_id,
                OAuthMCPRefreshToken.is_revoked == False,  # noqa: E712
            )
            .all()
        )
        for t in tokens:
            t.is_revoked = True
            db.add(t)
        if tokens:
            db.commit()
        return len(tokens)

    def delete_expired_and_revoked(self, db: Session) -> int:
        """Delete expired or revoked refresh tokens. Returns count deleted."""
        now = int(time.time())
        stale = (
            db.query(OAuthMCPRefreshToken)
            .filter(
                (OAuthMCPRefreshToken.is_revoked == True)  # noqa: E712
                | (
                    OAuthMCPRefreshToken.expires_at.isnot(None)
                    & (OAuthMCPRefreshToken.expires_at < now)
                )
            )
            .all()
        )
        count = len(stale)
        for obj in stale:
            db.delete(obj)
        if count:
            db.commit()
        return count


crud_oauth_mcp_auth_code = CRUDOAuthMCPAuthorizationCode()
crud_oauth_mcp_access_token = CRUDOAuthMCPAccessToken()
crud_oauth_mcp_refresh_token = CRUDOAuthMCPRefreshToken()
