"""Durable audit intents written in the caller's transaction (EE #94).

An ``AuditOutbox`` row is an audit record that has been promised but not yet
written to ``audit_log``. It is added to the same session as the business
write that triggered it, so the intent commits or rolls back with that write.
A relay in the audit service drains pending rows on its own sessions, writes
the ``audit_log`` row and marks the intent delivered in one commit, so a
crash between the two cannot duplicate or lose the record.

The relay only ever inserts unsealed ``audit_log`` rows; chain sealing stays
with the existing sweeper and is unaffected.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, Identity, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base

AUDIT_OUTBOX_PENDING = "pending"
AUDIT_OUTBOX_DELIVERED = "delivered"
AUDIT_OUTBOX_FAILED = "failed"


class AuditOutbox(Base):
    """One audit intent waiting for, or recorded after, relay delivery."""

    __tablename__ = "audit_outbox"
    __table_args__ = (
        Index("ix_audit_outbox_due", "status", "next_attempt_at"),
        Index("ix_audit_outbox_account_id", "account_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Insertion order. ``created_at`` is the transaction start time and ``id``
    # is random, so neither orders two intents from one transaction.
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    priority: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=AUDIT_OUTBOX_PENDING,
        server_default=AUDIT_OUTBOX_PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
