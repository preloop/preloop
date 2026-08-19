"""Shared schemas for managed-agent control-plane messages."""

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


AgentControlEnvelopeType = Literal["command", "event", "presence", "ack", "error"]
AgentControlInboundType = Literal["event", "status", "presence", "heartbeat"]
AgentControlInputMode = Literal["text", "voice_transcript"]
AgentControlSessionMode = Literal["existing", "new", "current"]


class AgentControlEnvelope(BaseModel):
    """Typed envelope exchanged over the shared managed-agent control plane."""

    type: AgentControlEnvelopeType
    name: str
    message_id: str
    account_id: UUID
    managed_agent_id: UUID
    runtime_session_id: UUID
    session_source_type: str
    session_source_id: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentControlInboundEnvelope(BaseModel):
    """Minimal agent-to-Preloop message accepted by the control WebSocket."""

    type: AgentControlInboundType
    name: Optional[str] = Field(default=None, max_length=128)
    message_id: Optional[str] = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentControlSendMessageRequest(BaseModel):
    """Operator request for routing a prompt to an online managed agent."""

    message: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    target_session_id: Optional[UUID] = None
    start_new_session: bool = False
    input_mode: AgentControlInputMode = "text"
    voice: dict[str, Any] = Field(default_factory=dict)
    spawn_worktree: bool = False
    interrupt: bool = False

    @model_validator(mode="after")
    def validate_session_target(self) -> "AgentControlSendMessageRequest":
        if self.start_new_session and self.target_session_id is not None:
            raise ValueError(
                "Use either start_new_session or target_session_id, not both"
            )
        return self


class AgentControlVoiceTranscriptRequest(BaseModel):
    """Mobile-friendly request for routing a spoken prompt transcript."""

    transcript: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    voice: dict[str, Any] = Field(default_factory=dict)
    target_session_id: Optional[UUID] = None
    start_new_session: bool = False


class AgentControlSessionActionRequest(BaseModel):
    """Takeover or release a Claude Code (or other) session."""

    target_session_id: Optional[UUID] = None
    start_new_session: bool = False
    spawn_worktree: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_session_target(self) -> "AgentControlSessionActionRequest":
        if self.start_new_session and self.target_session_id is not None:
            raise ValueError(
                "Use either start_new_session or target_session_id, not both"
            )
        return self


class AgentControlCommandResponse(BaseModel):
    """Result of routing an operator command to a managed agent."""

    command_id: str
    managed_agent_id: UUID
    runtime_session_id: Optional[UUID] = None
    target_session_id: Optional[UUID] = None
    session_source_id: Optional[str] = None
    session_reference: Optional[str] = None
    session_mode: AgentControlSessionMode
    subject: Optional[str] = None
    local_delivery: bool = False
    published: bool = False
    # Durable persistence state: pending|delivered|acked|failed|expired.
    command_status: Optional[str] = None
    # Absolute expiry for pending redelivery, and the configured TTL used.
    expires_at: Optional[datetime] = None
    command_ttl_seconds: Optional[int] = None
    command_envelope: AgentControlEnvelope
