"""API key model for storing API access keys."""

import uuid
from datetime import datetime, timezone

# Use TYPE_CHECKING to avoid circular imports
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Boolean, DateTime, Integer, String

from sqlalchemy.dialects.postgresql import UUID

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .managed_agent_credential import ManagedAgentCredential
    from .user import User


class ApiKey(Base):
    """API key model for authenticated API access.

    Attributes:
        id: The unique identifier for the key.
        name: A user-friendly name for the key.
        key: The plaintext key value for legacy/user-managed keys.
        key_hash: Deterministic hash used for validating runtime-only keys.
        key_prefix: Non-sensitive prefix used to narrow hashed lookups.
        created_at: When the key was created.
        expires_at: When the key expires (optional).
        last_used_at: When the key was last used (optional).
        user_id: The ID of the user who created the key.
        scopes: The list of scopes/permissions the key has.
        is_active: Whether the key is active.
    """

    __tablename__ = "api_key"
    __table_args__ = (
        UniqueConstraint("account_id", "name", name="uix_api_key_account_id_name"),
    )

    # Key details
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, unique=True, index=True
    )
    key_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    key_prefix: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True, index=True
    )

    # Timestamp fields
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Security fields
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The account this API key belongs to",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        comment="The user who created this API key",
    )
    scopes: Mapped[List] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Additional context data for context-specific restrictions
    # For flow executions: {"flow_execution_id": "...", "allowed_mcp_tools": [...]}
    context_data: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Explicit mode marker. Null grants or malformed versions must never make
    # this credential look like a legacy account key.
    credential_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy", server_default="legacy"
    )
    credential_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ci_principal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ci_principal.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    ci_actions: Mapped[Optional[List[str]]] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )

    @property
    def requires_machine_authorization(self) -> bool:
        """Explicit machine markers cannot fall back to owner authentication."""
        return (
            self.credential_type not in (None, "legacy")
            or self.credential_version is not None
            or self.ci_principal_id is not None
            or self.ci_actions is not None
        )

    # Relationships
    account: Mapped["Account"] = relationship("Account")
    creator: Mapped["User"] = relationship("User", back_populates="api_keys")
    managed_agent_credential: Mapped[Optional["ManagedAgentCredential"]] = relationship(
        "ManagedAgentCredential", back_populates="api_key", uselist=False
    )

    def __repr__(self) -> str:
        """Return a string representation of the key.

        Returns:
            String representation of the key.
        """
        return f"<ApiKey {self.name} created by user {self.user_id}>"

    @property
    def is_expired(self) -> bool:
        """Check if the key is expired.

        Returns:
            True if the key has an expiration date and it's in the past.
        """
        if not self.expires_at:
            return False

        # Assuming expires_at is a naive datetime stored in UTC, make it aware for comparison.
        return self.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)

    def is_valid(self) -> bool:
        """Check if the key is valid.

        Returns:
            True if the key is active and not expired.
        """
        return self.is_active and not self.is_expired

    def update_last_used(self) -> None:
        """Update the last_used_at timestamp to now."""
        # Ensure last_used_at is also timezone-aware UTC
        self.last_used_at = datetime.now(timezone.utc)
