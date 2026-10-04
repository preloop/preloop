"""OAuth Token model.

Generic model for OAuth tokens that can be used across different
providers (GitHub, GitLab, etc.).
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    CheckConstraint,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime, Text

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .github_app_installation import OAuthAppInstallation
    from .user import User


class OAuthToken(Base):
    """OAuth Token model.

    Stores encrypted OAuth tokens for user authentication across providers.
    These tokens are used for user-attributed actions (creating issues as the user).
    This generic model supports multiple providers (GitHub, GitLab, etc.).
    """

    __tablename__ = "oauth_token"

    __table_args__ = (
        UniqueConstraint("id", "account_id", name="uq_oauth_token_tenant"),
        UniqueConstraint("tracker_id", name="uq_oauth_token_tracker"),
        ForeignKeyConstraint(
            ["tracker_id", "account_id"],
            ["tracker.id", "tracker.account_id"],
            name="fk_oauth_grant_tracker_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["configuration_id", "account_id"],
            [
                "oauth_provider_configuration.id",
                "oauth_provider_configuration.account_id",
            ],
            name="fk_oauth_grant_configuration_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["connection_transaction_id", "account_id"],
            [
                "oauth_connection_transaction.id",
                "oauth_connection_transaction.account_id",
            ],
            name="fk_oauth_grant_transaction_tenant",
            ondelete="CASCADE",
            use_alter=True,
        ),
        CheckConstraint("rotation_version >= 0", name="ck_oauth_rotation_version"),
        CheckConstraint(
            "auth_mode IS NULL OR (auth_mode = 'managed_oauth' AND "
            "configuration_id IS NOT NULL AND configuration_version IS NOT NULL AND "
            "provider_subject IS NOT NULL AND canonical_instance IS NOT NULL AND "
            "installation_id IS NULL AND status IS NOT NULL AND configuration_version > 0 AND status IN ('pending', 'active', 'disconnected', 'invalidated') AND "
            "((status = 'pending' AND connection_transaction_id IS NOT NULL AND tracker_id IS NULL) OR "
            "(status = 'active' AND tracker_id IS NOT NULL) OR status IN ('disconnected', 'invalidated')))",
            name="ck_oauth_managed_grant",
        ),
    )

    auth_mode: Mapped[Optional[str]] = mapped_column(String(50))
    tracker_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    configuration_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    configuration_version: Mapped[Optional[int]] = mapped_column(Integer)
    connection_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True)
    )
    provider_subject: Mapped[Optional[str]] = mapped_column(String(1000))
    canonical_instance: Mapped[Optional[str]] = mapped_column(String(1000))
    status: Mapped[Optional[str]] = mapped_column(String(30))
    rotation_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )

    def to_dict(self) -> dict:
        """Return metadata only; never serialize encrypted credentials."""
        return {
            key: value
            for key, value in super().to_dict().items()
            if key not in {"access_token_encrypted", "refresh_token_encrypted"}
        }

    # Provider identification
    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="OAuth provider: 'github', 'gitlab', etc.",
    )

    # Token details (encrypted)
    access_token_encrypted: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Encrypted access token",
    )
    refresh_token_encrypted: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Encrypted refresh token",
    )

    # Token metadata
    token_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="bearer",
        comment="Token type (usually 'bearer')",
    )
    scope: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="OAuth scopes granted to this token",
    )

    # Token expiration
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the access token expires",
    )
    refresh_token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the refresh token expires",
    )

    # Foreign keys
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    installation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_app_installation.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="oauth_tokens")
    account: Mapped["Account"] = relationship("Account", back_populates="oauth_tokens")
    installation: Mapped[Optional["OAuthAppInstallation"]] = relationship(
        "OAuthAppInstallation", back_populates="oauth_tokens"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<OAuthToken(id={self.id}, provider={self.provider}, user_id={self.user_id})>"

    @property
    def is_expired(self) -> bool:
        """Check if the access token is expired."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def can_refresh(self) -> bool:
        """Check if the token can be refreshed."""
        if self.refresh_token_encrypted is None:
            return False
        if self.refresh_token_expires_at is None:
            return True
        return datetime.now(timezone.utc) < self.refresh_token_expires_at


# Backward compatibility alias
GitHubOAuthToken = OAuthToken
