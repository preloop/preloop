"""Content-free durable receipts for synchronous provider callback verdicts."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CallbackReceipt(Base):
    """A completed delivery, whose identifiers and body are keyed digests."""

    __tablename__ = "callback_receipt"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "integration_id",
            "delivery_digest",
            name="uq_callback_receipt_delivery",
        ),
        Index("ix_callback_receipt_expiry", "expires_at"),
        Index(
            "ix_callback_receipt_account_integration",
            "account_id",
            "integration_id",
            "created_at",
        ),
    )
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    integration_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="CASCADE"),
        nullable=False,
    )
    delivery_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    body_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CallbackKeyBinding(Base):
    """A fingerprint-only uniqueness registry; secret material stays elsewhere."""

    __tablename__ = "callback_key_binding"
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    integration_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    signing_key_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    digest_epoch: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
