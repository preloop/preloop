"""Live activity rollups and presence state (#1889).

These tables are written only by an extension plugin (the Cloud and
Enterprise ``live_activity`` worker). The open-source edition ships them
empty, as it does ``fleet_finding``. They hold counts, timestamps and state
names; never prompt text, tool arguments or transcripts.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# Latency histogram edges in seconds; a histogram has one more bucket, the
# overflow past the last edge.
LATENCY_EDGES_S: tuple[int, ...] = (5, 15, 30, 60, 120, 300, 600, 1800, 3600, 14400)
LATENCY_BUCKETS = len(LATENCY_EDGES_S) + 1

ROLLUP_GRANULARITIES = (60, 3600)
ROLLUP_SUBJECT_TYPES = ("account", "person", "agent", "device", "team")
PRESENCE_SUBJECT_TYPES = ("person", "agent", "person_device")
WATERMARK_SOURCES = (
    "api_usage",
    "runtime_session_activity",
    "approval_request",
    "event",
)


class ActivityRollup(Base):
    """Counts for one account, subject and time bucket."""

    __tablename__ = "activity_rollup"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "granularity",
            "subject_type",
            "subject_id",
            "bucket_start",
            name="uq_activity_rollup_key",
        ),
        Index(
            "ix_activity_rollup_account_bucket",
            "account_id",
            "granularity",
            text("bucket_start DESC"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    granularity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    turn_ends: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approvals_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approvals_decided: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    # NULL until a priced request lands: unpriced is not $0.
    spend_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 6), nullable=True)
    reply_latency_hist: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False
    )
    approval_latency_hist: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False
    )
    active_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_pass_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )


class PresenceState(Base):
    """A subject's current state (``until`` NULL) and its recent history."""

    __tablename__ = "presence_state"
    __table_args__ = (
        Index(
            "uq_presence_state_current",
            "account_id",
            "subject_type",
            "subject_id",
            unique=True,
            postgresql_where=text("until IS NULL"),
        ),
        Index(
            "ix_presence_state_account_since",
            "account_id",
            "subject_type",
            text("since DESC"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_signal_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_signal_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    awaiting_reply_since: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    awaiting_approval_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0
    )
    runtime_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    device_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class ActivityWorkerWatermark(Base):
    """How far the worker has read one source, across every account."""

    __tablename__ = "activity_worker_watermark"

    source: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActivitySettings(Base):
    """Per-account presence switch; an absent row means enabled."""

    __tablename__ = "activity_settings"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    presence_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
