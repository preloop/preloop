"""Account-scoped execution approval for an observed MCP tool route."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ToolRegistryEntry(Base):
    """One approve-once decision; proxy and direct-hook routes remain distinct."""

    __tablename__ = "tool_registry_entry"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "tool_source",
            "mcp_server_id",
            "server_label",
            "tool_name",
            name="uq_tool_registry_entry",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "state IN ('pending', 'approved', 'denied')", name="ck_tool_registry_state"
        ),
        Index("ix_tool_registry_entry_account_state", "account_id", "state"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE")
    )
    tool_source: Mapped[str] = mapped_column(String(32))
    mcp_server_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("mcp_server.id", ondelete="CASCADE")
    )
    server_label: Mapped[str | None] = mapped_column(String(255))
    tool_name: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("managed_agent.id", ondelete="SET NULL")
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    approval_request_id: Mapped[UUID | None] = mapped_column()
