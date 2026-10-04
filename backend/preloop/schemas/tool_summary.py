"""Lightweight tool catalogue rows for console lists."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolSummaryResponse(BaseModel):
    """Tool metadata and policy state without its potentially large schema.

    Schema token estimates still describe the full tool definition served to
    agents. Access rules remain available for list filtering and policy editing.
    """

    name: str
    description: str
    source: Literal["builtin", "mcp", "agent"]
    source_id: str | None
    source_name: str
    is_enabled: bool
    requires_tracker: bool
    required_tracker_types: list[str]
    is_supported: bool
    unsupported_reason: str | None
    approval_workflow_id: str | None
    config_id: str | None
    has_approval_condition: bool
    access_rules: list[dict[str, Any]]
    justification_mode: str | None
    enabled_for_agents: list[str]
    schema_tokens_estimate: int = Field(ge=0)
    adapters: list[str] = Field(default_factory=list)
    has_condition: bool = False
