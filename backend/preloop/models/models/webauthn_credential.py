"""WebAuthn passkey credential model."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import BigInteger, Boolean, DateTime, String, Text

from .base import Base

if TYPE_CHECKING:
    from .user import User


class WebAuthnCredential(Base):
    """A registered WebAuthn passkey credential.

    One row per registered authenticator. Scope is intentionally tight
    (single-device passkeys with discoverable credentials): no admin
    management, no attestation trust chains, no backup-state tracking beyond
    what the authenticator reports.

    Attributes:
        user_id: Owner of the credential.
        credential_id: Base64url-encoded credential ID from the authenticator.
        public_key: Base64url-encoded COSE public key.
        sign_count: Signature counter for clone detection.
        transports: JSON-encoded list of transports (e.g. ["internal"]).
        name: User-friendly label shown in settings.
        aaguid: Authenticator AAGUID (informational).
        is_active: Soft-delete flag.
        last_used_at: Last successful authentication with this credential.
    """

    __tablename__ = "webauthn_credential"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The user this passkey belongs to",
    )
    credential_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True, index=True
    )
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    sign_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    transports: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Passkey")
    aaguid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:
        """Return a string representation of the credential."""
        return f"<WebAuthnCredential {self.name} for user {self.user_id}>"
