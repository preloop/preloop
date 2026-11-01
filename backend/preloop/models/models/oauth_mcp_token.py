"""OAuth MCP token models for authorization codes, access tokens, and refresh tokens."""

import uuid
from typing import Any, Optional

from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .base import Base


class OAuthMCPAuthorizationCode(Base):
    """Stores OAuth authorization codes (short-lived, consumed on token exchange).

    Authorization codes are generated during the /authorize flow and exchanged
    for access + refresh tokens via POST /token.

    Attributes:
        code_hash: SHA-256 hash of the authorization code.
        client_id: The OAuth client that requested the code.
        user_id: The Preloop user who authorized.
        account_id: The Preloop account of the user.
        redirect_uri: The redirect URI for this code.
        redirect_uri_provided_explicitly: Whether redirect_uri was explicitly provided.
        code_challenge: PKCE code challenge (S256).
        scopes: JSON array of requested scopes.
        expires_at: Unix timestamp when the code expires.
        resource: RFC 8707 resource indicator.
        is_used: Whether the code has been consumed.
    """

    __tablename__ = "oauth_mcp_authorization_code"

    code_hash: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    redirect_uri_provided_explicitly: Mapped[bool] = mapped_column(
        nullable=False, default=True
    )
    code_challenge: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_context: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    auth_generation: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    resource: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    is_used: Mapped[bool] = mapped_column(nullable=False, default=False)


class OAuthMCPAccessToken(Base):
    """Stores issued OAuth access tokens.

    Access tokens are looked up by their SHA-256 hash for verification.
    They are linked to the Preloop user/account for context extraction.

    Attributes:
        token_hash: SHA-256 hash of the access token.
        client_id: The OAuth client this token was issued to.
        user_id: The Preloop user this token represents.
        account_id: The Preloop account of the user.
        scopes: JSON array of granted scopes.
        expires_at: Unix timestamp when the token expires (None = no expiry).
        resource: RFC 8707 resource indicator.
        is_revoked: Whether the token has been revoked.
    """

    __tablename__ = "oauth_mcp_access_token"

    token_hash: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    auth_context: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    auth_generation: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[Optional[int]] = mapped_column(nullable=True)
    resource: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    is_revoked: Mapped[bool] = mapped_column(nullable=False, default=False)


class OAuthMCPRefreshToken(Base):
    """Stores issued OAuth refresh tokens.

    Refresh tokens are used to obtain new access tokens without re-authorization.
    They are rotated on each use (old token invalidated, new one issued).

    Attributes:
        token_hash: SHA-256 hash of the refresh token.
        client_id: The OAuth client this token was issued to.
        user_id: The Preloop user this token represents.
        account_id: The Preloop account of the user.
        scopes: JSON array of granted scopes.
        expires_at: Unix timestamp when the token expires (None = no expiry).
        is_revoked: Whether the token has been revoked.
    """

    __tablename__ = "oauth_mcp_refresh_token"

    token_hash: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    client_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    auth_context: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    auth_generation: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0"
    )
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[Optional[int]] = mapped_column(nullable=True)
    is_revoked: Mapped[bool] = mapped_column(nullable=False, default=False)
