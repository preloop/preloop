"""Bounded current identity read for historical device evidence."""

from uuid import UUID
from pydantic import BaseModel, Field


class FleetIdentityQuery(BaseModel):
    """Only identifiers already present in the device projection are needed."""

    agent_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    observer_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class FleetAgentIdentity(BaseModel):
    """Current registry state, separate from collector assertions."""

    id: UUID
    name: str | None
    lifecycle_state: str | None


class FleetIdentities(BaseModel):
    """Names and enrollment state, with no credential contents."""

    agents: list[FleetAgentIdentity]
    observers: dict[str, str]
