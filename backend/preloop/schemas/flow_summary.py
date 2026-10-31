"""Flow catalogue metadata for list views and name selectors."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


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
    #: True for a read-only review flow, which only its restricted CI
    #: identity may start; a person's trigger is refused with 409.
    ci_only: bool = False

    @model_validator(mode="before")
    @classmethod
    def derive_ci_only(cls, data: Any) -> Any:
        """Derive ``ci_only`` from the row's clone config without exposing it."""
        from preloop.services.read_only_review import read_only_review

        if isinstance(data, dict):
            if "ci_only" not in data:
                config = data.get("git_clone_config")
                return {**data, "ci_only": read_only_review(config)}
            return data
        config = getattr(data, "git_clone_config", None)
        if not read_only_review(config):
            return data
        fields = {
            name: getattr(data, name)
            for name in cls.model_fields
            if name != "ci_only" and hasattr(data, name)
        }
        return {**fields, "ci_only": True}
