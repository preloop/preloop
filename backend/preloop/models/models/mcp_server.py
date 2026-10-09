"""MCPServer model for storing user-configured external MCP servers."""

import uuid
from typing import TYPE_CHECKING, Dict, List, Optional

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .mcp_tool import MCPTool
    from .tool_configuration import ToolConfiguration


class MCPServer(Base):
    """
    Stores user-configured external MCP servers.

    Users can add external MCP servers that provide additional tools.
    Each server configuration includes connection details, authentication,
    and tracking of tool discovery scans.
    """

    __tablename__ = "mcp_server"

    # Ownership
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id"), nullable=False, index=True
    )

    # Server configuration
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    transport: Mapped[str] = mapped_column(
        String(50), nullable=False, default="http-streaming"
    )

    # Optional explicit prefix: when set, this server's tools are exposed as
    # ``<prefix>_<tool>`` for listing, routing, policy and configuration.
    # Never set automatically (#1135).
    tool_prefix: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Authentication configuration
    auth_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="none"
    )  # "none", "bearer", "api_key"
    auth_config: Mapped[Optional[Dict]] = mapped_column(
        JSONB, nullable=True
    )  # Encrypted credentials

    # Status tracking
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="active"
    )  # "active", "error", "disabled"
    last_scan_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    account: Mapped["Account"] = relationship(back_populates="mcp_servers")
    tools: Mapped[List["MCPTool"]] = relationship(
        back_populates="mcp_server", cascade="all, delete-orphan"
    )
    tool_configurations: Mapped[List["ToolConfiguration"]] = relationship(
        back_populates="mcp_server", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<MCPServer(id={self.id}, name='{self.name}', url='{self.url}')>"
