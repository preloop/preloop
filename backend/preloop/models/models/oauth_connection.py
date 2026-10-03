"""Tenant-owned provider configuration and single-use OAuth handshakes."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OAuthProviderConfiguration(Base):
    """One versioned consumer; replacement requires the managed OAuth CRUD lock."""

    __tablename__ = "oauth_provider_configuration"
    __table_args__ = (
        UniqueConstraint("id", "account_id", name="uq_oauth_configuration_tenant"),
        CheckConstraint("version > 0", name="ck_oauth_configuration_version"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50))
    canonical_instance: Mapped[str] = mapped_column(String(1000))
    context: Mapped[str] = mapped_column(String(1000), default="")
    client_id: Mapped[str] = mapped_column(String(1000))
    client_secret_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="SET NULL"),
        unique=True,
    )
    callback_uri: Mapped[str] = mapped_column(String(2000))
    selected_permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def to_dict(self) -> dict:
        """Expose configuration metadata, excluding the encrypted secret."""
        return {k: v for k, v in super().to_dict().items() if k != "client_secret_id"}


class OAuthConnectionTransaction(Base):
    """An expiring, owner-bound callback, with hashed state and session identity."""

    __tablename__ = "oauth_connection_transaction"
    __table_args__ = (
        UniqueConstraint("id", "account_id", name="uq_oauth_transaction_tenant"),
        ForeignKeyConstraint(
            ["configuration_id", "account_id"],
            [
                "oauth_provider_configuration.id",
                "oauth_provider_configuration.account_id",
            ],
            name="fk_oauth_transaction_configuration_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["pending_grant_id", "account_id"],
            ["oauth_token.id", "oauth_token.account_id"],
            name="fk_oauth_transaction_grant_tenant",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["tracker_id", "account_id"],
            ["tracker.id", "tracker.account_id"],
            name="fk_oauth_transaction_tracker_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'completed', 'invalidated')",
            name="ck_oauth_transaction_status",
        ),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE")
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    session_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(50))
    configuration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    configuration_version: Mapped[int] = mapped_column(Integer)
    callback_uri: Mapped[str] = mapped_column(String(2000))
    return_path: Mapped[str] = mapped_column(String(2000))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    pending_grant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    tracker_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    pkce_verifier_encrypted: Mapped[Optional[str]] = mapped_column(Text)

    def to_dict(self) -> dict:
        """Return public metadata without callback credentials or their hashes."""
        hidden = {"pkce_verifier_encrypted", "state_hash", "session_hash"}
        return {k: v for k, v in super().to_dict().items() if k not in hidden}
