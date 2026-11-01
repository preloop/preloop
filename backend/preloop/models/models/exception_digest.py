"""Durable weekly factual digest delivery and all-funding synthesis limits."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExceptionDigestWeek(Base):
    """One stable account/week identity and conservative synthesis reservation."""

    __tablename__ = "exception_digest_week"
    __table_args__ = (
        UniqueConstraint("account_id", "window_end", name="uq_exception_digest_week"),
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    synthesis_reserved_usd: Mapped[Decimal] = mapped_column(
        Numeric(16, 8), nullable=False, default=0, server_default="0"
    )
    synthesis_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    synthesis_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    synthesis_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExceptionDigestDelivery(Base):
    """Immutable recipient snapshot; dispatching/uncertain claims never retry.

    SMTP has no idempotency guarantee. A committed dispatch claim plus stable
    Message-ID avoids automatic duplicate handoffs at the cost of an explicitly
    uncertain outcome after a crash. Claims are never reclaimed automatically.
    """

    __tablename__ = "exception_digest_delivery"
    __table_args__ = (
        UniqueConstraint("week_id", "user_id", name="uq_exception_digest_recipient"),
    )
    week_id: Mapped[UUID] = mapped_column(
        ForeignKey("exception_digest_week.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    sections: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExceptionDigestState(Base):
    """Last delivered/acknowledged material state for one scoped exception."""

    __tablename__ = "exception_digest_state"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "user_id", "exception_key", name="uq_exception_digest_state"
        ),
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    exception_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_scope: Mapped[str | None] = mapped_column(String(80))
    delivered_fingerprint: Mapped[str | None] = mapped_column(String(64))
    acknowledged_fingerprint: Mapped[str | None] = mapped_column(String(64))
