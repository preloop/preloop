"""The per-account Ed25519 key Preloop signs records with.

One active key per account, enforced by a partial unique index rather than by
a service check, for the same reason #559 put the one-active-hold rule in an
index: concurrent callers race past service checks.

Retired keys are kept, public key and all. Rotation that made every signature
ever issued unverifiable would not be rotation, it would be revocation of the
customer's own evidence. The key id travels with every signature so a verifier
knows which public key to ask for.

The private key is stored the way every other secret in this product is
stored: Fernet-encrypted through :mod:`preloop.utils.encryption`, under
``SECURITY__ENCRYPTION_KEY``. That is worth being precise about, because it
bounds what a signature proves. The signing key sits in the same database as
the records it signs, so an attacker who owns the platform owns the key too.
What the signature adds is that evidence which left the platform can be
checked later by someone who never trusted the platform's copy of it.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base

#: The only algorithm this table holds today. Stored rather than assumed so a
#: future key type does not need a migration to be told apart from this one.
SIGNING_ALGORITHM_ED25519 = "ed25519"

#: Prefix on every key id. Signatures carry the id, so it shows up in exported
#: bundles and in support conversations; a prefix makes it obvious what it is.
KEY_ID_PREFIX = "psk_"


class AccountSigningKey(Base):
    """One signing key: public part in the clear, private part encrypted."""

    __tablename__ = "account_signing_key"
    __table_args__ = (
        # One active key per account.
        Index(
            "uq_account_signing_key_active",
            "account_id",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
        Index("ix_account_signing_key_key_id", "key_id", unique=True),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Public identifier carried by every signature this key makes",
    )
    algorithm: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SIGNING_ALGORITHM_ED25519,
        server_default=SIGNING_ALGORITHM_ED25519,
    )
    public_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Base64 of the raw 32 byte Ed25519 public key",
    )
    private_key_encrypted: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Fernet token over the base64 raw private key",
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Set on rotation. A retired key still verifies what it signed.",
    )
    rotated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    @property
    def active(self) -> bool:
        """True while this key is the one new signatures are made with."""
        return self.retired_at is None

    def __repr__(self) -> str:
        """Describe the key without exposing any secret material."""
        state = "active" if self.retired_at is None else "retired"
        return f"<AccountSigningKey {self.key_id} {self.algorithm} {state}>"
