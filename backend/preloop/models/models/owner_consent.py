"""Bind an owner's approval to one concrete execution or session intent."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OwnerConsent(Base):
    """Durable, account-scoped idempotency and single-use consent binding."""

    __tablename__ = "owner_consent"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "action", "request_key", name="uq_owner_consent_intent"
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(255))
    request_key: Mapped[str] = mapped_column(String(255))
    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_request.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    notification_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
