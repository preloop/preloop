"""Transactionally maintained usage totals for agent list reads."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Numeric,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentUsageTotal(Base):
    """One principal's usage in one session, independent of agent ownership."""

    __tablename__ = "agent_usage_total"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "principal_type",
            "principal_id",
            "session_id",
            name="uq_agent_usage_total_scope",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_agent_usage_total_session", "account_id", "session_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    principal_type: Mapped[Optional[str]] = mapped_column(String(64))
    principal_id: Mapped[Optional[str]] = mapped_column(String(255))
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    request_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    success_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    error_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    covered_prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, server_default="0"
    )
    sub_requests: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    sub_prompt: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    sub_completion: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    sub_total: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    sub_covered: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    sub_equivalent: Mapped[Decimal] = mapped_column(
        Numeric, nullable=False, server_default="0"
    )
    last_request_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_usage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    latest_model_alias: Mapped[Optional[str]] = mapped_column(String(255))
    latest_provider_name: Mapped[Optional[str]] = mapped_column(String(255))


class AgentUsageCounted(Base):
    """Usage IDs already included, making online backfill resumable."""

    __tablename__ = "agent_usage_counted"
    id: Mapped[uuid.UUID] = mapped_column(
        "usage_id", UUID(as_uuid=True), primary_key=True
    )


class AgentUsageBackfill(Base):
    """Completion gate: partial stored totals must never reach list readers."""

    __tablename__ = "agent_usage_backfill"
    id: Mapped[str] = mapped_column("name", String(32), primary_key=True)
    last_usage_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
