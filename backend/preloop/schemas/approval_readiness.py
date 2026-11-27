"""Read-only approval routing facts, without delivery secrets."""

from pydantic import BaseModel, Field


class ApprovalReadiness(BaseModel):
    """Configured human route, independent of enforcement or notification delivery."""

    enforced: bool = True
    workflow_name: str | None = None
    workflow_id: str | None = None
    recipients: list[str] = Field(default_factory=list)
    deciders: list[str] = Field(default_factory=list)
    destinations: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    timeout_seconds: int | None = None
    ai_fallback: str | None = None
    external: bool = False
