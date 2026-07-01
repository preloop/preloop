"""Pydantic schemas for tool output filters.

These schemas back the billing cost API's ``/output-filters`` endpoints and are
consumed verbatim by the frontend, so the field names here are load-bearing:
``id``, ``account_id``, ``server_name``, ``tool_name``, ``managed_agent_id``,
``dropped_fields``, ``enabled``, ``created_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ToolOutputFilterResponse(BaseModel):
    """One tool output filter that strips fields from a tool's JSON result.

    Attributes:
        id: Unique identifier for the filter.
        account_id: The account the filter belongs to.
        server_name: MCP server the filter is scoped to; ``None`` = any server.
        tool_name: Name of the tool whose results are filtered.
        managed_agent_id: Managed agent scope; ``None`` = account-wide.
        dropped_fields: Top-level field names stripped from each result object.
        enabled: Whether the filter is active.
        created_at: Timestamp when the filter was created.
    """

    id: uuid.UUID
    account_id: uuid.UUID
    server_name: Optional[str] = None
    tool_name: str
    managed_agent_id: Optional[uuid.UUID] = None
    dropped_fields: list[str] = Field(default_factory=list)
    enabled: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ToolOutputFilterListResponse(BaseModel):
    """Envelope for tool output filter listings.

    Attributes:
        items: The filters owned by the current account.
    """

    items: list[ToolOutputFilterResponse] = Field(default_factory=list)


class ToolOutputFilterCreate(BaseModel):
    """Request body to create a tool output filter.

    Attributes:
        server_name: MCP server to scope to; ``None`` matches any server.
        tool_name: Name of the tool whose results are filtered.
        dropped_fields: Top-level field names to strip from each result object.
        managed_agent_id: Managed agent to scope to; ``None`` = account-wide.
    """

    server_name: Optional[str] = None
    tool_name: str
    dropped_fields: list[str] = Field(default_factory=list)
    managed_agent_id: Optional[uuid.UUID] = None
