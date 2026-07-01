"""Tool output filter model for stripping fields from MCP tool results."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account


class ToolOutputFilter(Base):
    """An account-level rule that strips named fields from a tool's JSON result.

    The MCP proxy applies matching, enabled filters to a tool call's result
    *before* it reaches the calling agent: each top-level field named in
    ``dropped_fields`` is removed from every result object. This trims wasted
    context tokens without modifying the upstream tool.

    Matching is account-scoped. A ``NULL`` ``server_name`` matches any MCP
    server, and a ``NULL`` ``managed_agent_id`` matches any managed agent
    (account-wide). A non-null value narrows the filter to that server/agent.

    Attributes:
        id: Unique identifier for the filter.
        account_id: The account this filter belongs to.
        server_name: Name of the MCP server to scope to; NULL = any server.
        tool_name: Name of the tool whose results are filtered.
        managed_agent_id: Managed agent to scope to; NULL = account-wide.
        dropped_fields: Top-level field names to strip from each result object.
        enabled: Whether this filter is active.
        created_at: Timestamp when the filter was created.
        updated_at: Timestamp when the filter was last updated.
    """

    __tablename__ = "tool_output_filter"
    __table_args__ = (
        Index(
            "ix_tool_output_filter_account_id",
            "account_id",
        ),
        Index(
            "ix_tool_output_filter_account_tool",
            "account_id",
            "tool_name",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The account this filter belongs to",
    )
    server_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="MCP server name to scope to; NULL matches any server",
    )
    tool_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Name of the tool whose results are filtered",
    )
    managed_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("managed_agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Managed agent to scope to; NULL matches any agent (account-wide)",
    )
    dropped_fields: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="Top-level field names to strip from each result object",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Whether this filter is active",
    )

    account: Mapped[Optional["Account"]] = relationship("Account")

    def __repr__(self) -> str:
        """Return string representation of the filter."""
        return (
            f"<ToolOutputFilter(tool={self.tool_name}, "
            f"server={self.server_name}, enabled={self.enabled})>"
        )
