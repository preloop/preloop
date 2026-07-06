"""Persistent tool cost flags raised by the deterministic detection engine."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account


class ToolCostFlag(Base):
    """An account-level cost flag raised for a single tool over a detection window.

    The deterministic detection engine writes one flag per
    ``account_id`` + ``tool_name`` + ``window_start`` so a tool is not
    duplicated within the same rolling window. The dashboard reads flags to
    surface costly tool definitions, and the ``status`` column persists
    dismiss/snooze decisions so a deliberately-kept tool is never re-flagged.

    Unlike the per-session optimization-result cache (keyed by a scope hash with
    a schema-validated payload), these flags are account-scoped rolling-window
    records, hence a dedicated table.

    Attributes:
        id: Unique identifier for the flag.
        account_id: The account the flag belongs to.
        tool_name: Name of the flagged tool.
        tool_source: Labeled heuristic for the tool's origin ('mcp' | 'payload').
        window_start: Inclusive start of the rolling detection window.
        window_end: Exclusive end of the rolling detection window.
        flag_kind: Category of the flag (e.g. 'definition_cost').
        evidence: Computed numbers plus human-readable claim text.
        estimated_weekly_cost: Upper-bound estimated weekly cost in USD.
        status: Lifecycle status ('open' | 'dismissed' | 'snoozed').
        disable_eligible: Whether the tool can be one-click disabled. False when
            the tool name is ambiguous across MCP servers.
        created_at: Timestamp when the flag was first created.
        updated_at: Timestamp when the flag was last updated.
    """

    __tablename__ = "tool_cost_flag"
    __table_args__ = (
        Index(
            "uq_tool_cost_flag_account_tool_window",
            "account_id",
            "tool_name",
            "window_start",
            unique=True,
        ),
        Index(
            "ix_tool_cost_flag_account_status",
            "account_id",
            "status",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The account this flag belongs to",
    )
    tool_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Name of the flagged tool",
    )
    tool_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "LABELED HEURISTIC for the tool's origin: 'mcp' or 'payload'. This is "
            "a best-effort label inferred by the detection engine, not an "
            "authoritative source of truth."
        ),
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="Inclusive start of the rolling detection window",
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="Exclusive end of the rolling detection window",
    )
    flag_kind: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Category of the flag, e.g. 'definition_cost'",
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="Computed numbers plus human-readable claim text",
    )
    estimated_weekly_cost: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Upper-bound estimated weekly cost in USD",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="open",
        server_default="open",
        comment="Lifecycle status: 'open', 'dismissed', or 'snoozed'",
    )
    disable_eligible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment=(
            "Whether the tool can be one-click disabled. False when the tool "
            "name is ambiguous across MCP servers, suppressing one-click disable."
        ),
    )

    account: Mapped[Optional["Account"]] = relationship("Account")

    def __repr__(self) -> str:
        """Return string representation of the flag."""
        return (
            f"<ToolCostFlag(tool={self.tool_name}, source={self.tool_source}, "
            f"status={self.status}, window_start={self.window_start})>"
        )
