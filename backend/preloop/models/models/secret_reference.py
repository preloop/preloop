"""Secret reference model for provider-agnostic secret storage."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .ai_model import AIModel

#: ``meta_data`` key holding the provider's last rejection of a stored API key.
#: Written by the model gateway on an upstream 401, removed when the provider
#: next accepts the key and whenever the key is replaced or rotated.
UPSTREAM_AUTH_REJECTION_KEY = "upstream_auth_rejection"
UPSTREAM_BILLING_REJECTION_KEY = "upstream_billing_rejection"


class SecretReference(Base):
    """Reference to a secret managed by a pluggable backend."""

    __tablename__ = "secret_reference"

    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    backend_type: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    external_ref: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    #: Incremented each time the stored material (or external reference) is
    #: replaced, so operators and audit rows can tell rotations apart.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    #: When the material was last replaced; None means never rotated.
    rotated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    meta_data: Mapped[Optional[Dict]] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    account: Mapped[Optional["Account"]] = relationship(
        "Account", back_populates="secret_references"
    )
    ai_models: Mapped[List["AIModel"]] = relationship(
        "AIModel", back_populates="credentials_secret"
    )

    @property
    def upstream_auth_rejection(self) -> Optional[Dict[str, Any]]:
        """The provider's standing rejection of this stored API key, if any."""
        if not isinstance(self.meta_data, dict):
            return None
        rejection = self.meta_data.get(UPSTREAM_AUTH_REJECTION_KEY)
        if not isinstance(rejection, dict) or not rejection.get("last_rejected_at"):
            return None
        return rejection

    @property
    def upstream_credential_rejection(self) -> Optional[Dict[str, Any]]:
        """Return the standing authentication or billing rejection."""
        if self.upstream_auth_rejection is not None:
            return self.upstream_auth_rejection
        if not isinstance(self.meta_data, dict):
            return None
        rejection = self.meta_data.get(UPSTREAM_BILLING_REJECTION_KEY)
        if not isinstance(rejection, dict) or not rejection.get("last_rejected_at"):
            return None
        return rejection

    def __repr__(self) -> str:
        return (
            f"<SecretReference(id={self.id}, backend_type='{self.backend_type}', "
            f"secret_kind='{self.secret_kind}')>"
        )
