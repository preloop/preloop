"""Flow catalogue metadata for list views and name selectors."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FlowSummaryResponse(BaseModel):
    """A flow row without prompts, agent configuration or tool definitions."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID | None = None
    name: str
    description: str | None = None
    icon: str | None = None
    created_at: datetime
    updated_at: datetime
    trigger_event_source: str | None = None
    trigger_event_types: list[str] | None = None
    ai_model_id: UUID | None = None
    ai_model_name: str | None = None
    agent_type: str
    is_enabled: bool
    is_preset: bool
    source_preset_id: UUID | None = None
    prompt_customized: bool
    tools_customized: bool
    preset_update_available: bool
    schedule_state: dict[str, Any] | None = None
    execution_stats: dict[str, Any] | None = None
