"""Membership-owned authenticator factors and one-time recovery proofs."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class UserMfaFactor(Base):
    """An unconfirmed enrollment or a confirmed TOTP factor."""

    __tablename__ = "user_mfa_factor"
    __table_args__ = (UniqueConstraint("user_id", "kind", name="uq_user_mfa_kind"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16), default="totp", server_default="totp")
    secret_encrypted: Mapped[str] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_step: Mapped[int | None] = mapped_column(BigInteger)


class UserMfaRecoveryCode(Base):
    """A password-hashed recovery code, retained after use for accounting."""

    __tablename__ = "user_mfa_recovery_code"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(Text)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
