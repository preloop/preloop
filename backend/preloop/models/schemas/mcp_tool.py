"""Pydantic schemas for MCP tool definitions."""

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class MCPToolBase(BaseModel):
    """Base schema for MCP tool definition."""

    name: Optional[str] = Field(None, description="Tool name")
    description: Optional[str] = Field(None, description="Tool description")
    input_schema: Optional[Dict[str, Any]] = Field(
        None, description="JSON schema defining tool input parameters"
    )


class MCPToolCreate(MCPToolBase):
    """Schema for creating an MCP tool."""

    name: str
    input_schema: Dict[str, Any]
    mcp_server_id: UUID
    discovered_at: str


class MCPToolUpdate(MCPToolBase):
    """Schema for updating an MCP tool."""

    pass


class MCPToolResponse(MCPToolBase):
    """Schema for MCP tool response."""

    id: UUID
    mcp_server_id: UUID
    name: str
    input_schema: Dict[str, Any]
    # ``discovered_at`` is stored as a string column on ``MCPTool`` (the
    # discovery timestamp is recorded by the scanner), not a SQL timestamp.
    discovered_at: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("id", "mcp_server_id")
    def serialize_uuids(self, value: UUID) -> str:
        """Serialize UUID fields to strings for JSON responses."""
        return str(value)
