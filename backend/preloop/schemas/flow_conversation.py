"""Shared console, mobile and chat conversation contract."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InteractiveSettings(BaseModel):
    """Explicit flow choices; defaults are applied only when omitted."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_turns: int = Field(default=30, ge=1)
    idle_timeout_seconds: int = Field(default=600, ge=1)


class ConversationStart(BaseModel):
    """Optional text is substituted for {{conversation.message}} or appended."""

    message: str = Field(default="", max_length=65536)
    request_id: UUID
    origin: Literal["console", "slack", "mattermost", "discord"] = "console"


class ConversationSend(BaseModel):
    """A stable request ID makes retries safe across tabs and mobile clients."""

    message: str = Field(min_length=1, max_length=65536)
    request_id: UUID


class ConversationMessage(BaseModel):
    """An admitted turn; answer is updated while the runtime streams."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    turn: int
    text: str
    answer: str
    status: str
    execution_id: UUID | None


class ConversationResponse(BaseModel):
    """Durable thread projection; polling is independent of browser presence."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    flow_id: UUID
    owner_user_id: UUID
    execution_id: UUID | None
    state: Literal["starting", "working", "waiting", "stopped", "ended", "failed"]
    notice: str
    can_write: bool = False
    flow_name: str = "Flow conversation"
    origin: str = "console"
    turns: int
    max_turns: int
    idle_timeout_seconds: int
    idle_deadline: datetime | None
    messages: list[ConversationMessage] = Field(default_factory=list)


class RuntimeTurnUpdate(BaseModel):
    """Execution-bound snapshot, safe to retry without duplicating text."""

    turn: int = Field(ge=1)
    answer: str = Field(default="", max_length=1048576)
    finished: bool = False
    error: bool = False
    interrupted: bool = False
