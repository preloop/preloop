"""Portable sharing intent; recipient DTOs intentionally carry no credentials."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResourceShareDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID | None = None
    resource_type: Literal["ai_model", "mcp_server", "managed_agent"]
    resource_id: UUID
    target_mode: Literal["all", "selected", "rule"]
    selected_account_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    access_rule_id: UUID | None = None
    require_approval: bool = False

    @model_validator(mode="after")
    def validate_target(self) -> ResourceShareDefinition:
        if (self.target_mode == "rule") != (self.access_rule_id is not None):
            raise ValueError("rule targets require exactly one access_rule_id")
        if self.target_mode == "selected" and not self.selected_account_ids:
            raise ValueError("selected targets require at least one account")
        if self.target_mode != "selected" and self.selected_account_ids:
            raise ValueError("selected_account_ids are only valid for selected targets")
        if len(set(self.selected_account_ids)) != len(self.selected_account_ids):
            raise ValueError("selected accounts must be distinct")
        if self.require_approval and self.resource_type != "mcp_server":
            raise ValueError("mandatory approval applies to MCP server shares only")
        return self


class SharedResourceRead(BaseModel):
    """The complete public recipient surface; secret fields cannot be serialized."""

    model_config = ConfigDict(extra="forbid")
    id: UUID
    resource_type: Literal["ai_model", "mcp_server", "managed_agent"]
    name: str
    provider: str | None = None
    identifier: str | None = None
    display_name: str | None = None
    agent_kind: str | None = None
    control_enabled: bool = False
    control_online: bool = False
    lifecycle_state: str | None = None
    owner_name: str
    is_shared: Literal[True] = True
    read_only: Literal[True] = True
    price: dict[str, float] | None = None
