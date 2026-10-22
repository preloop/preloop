"""Tenant-bound upstream MCP OAuth lifecycle, separate from inbound grants."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UpstreamMCPOAuthConnection(Base):
    """Versioned credential reference and a bounded cross-worker refresh lease."""

    __tablename__ = "upstream_mcp_oauth_connection"
    __table_args__ = (
        UniqueConstraint("server_id", "account_id", name="uq_upstream_mcp_oauth_owner"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_server.id", ondelete="CASCADE"), unique=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    config_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="disconnected")
    secret_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("secret_reference.id", ondelete="SET NULL")
    )
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> dict[str, Any]:
        """Only connection state is public; no fingerprints or secret handles."""
        return {
            "server_id": str(self.server_id),
            "state": self.state,
            "version": self.version,
        }


class UpstreamMCPOAuthTransaction(Base):
    """Ten-minute single-use browser transaction; state and browser nonce hashed."""

    __tablename__ = "upstream_mcp_oauth_transaction"
    __table_args__ = (
        ForeignKeyConstraint(
            ["server_id", "account_id"],
            [
                "upstream_mcp_oauth_connection.server_id",
                "upstream_mcp_oauth_connection.account_id",
            ],
            ondelete="CASCADE",
            name="fk_upstream_mcp_oauth_transaction_owner",
        ),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE")
    )
    auth_generation: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    issuer: Mapped[str] = mapped_column(String(2048))
    redirect_uri: Mapped[str] = mapped_column(String(2048))
    context_encrypted: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    def to_dict(self) -> dict[str, Any]:
        """Never serialize state, browser identity or PKCE context."""
        return {"expires_at": self.expires_at.isoformat()}
