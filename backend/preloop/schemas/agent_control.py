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


AgentControlDeliveryState = Literal[
    "queued", "delivered", "started", "finished", "failed", "expired", "cancelled"
]


class AgentControlCommandStatusResponse(BaseModel):
    """Delivery state of one operator command (#1150).

    ``delivery_state`` is what an operator sees: queued (persisted, not yet
    on the agent's connection), delivered (sent on the connection), started
    (the agent acknowledged it), then finished, failed, expired or
    cancelled. ``status`` is the stored row status it is derived from.
    """

    command_id: str
    managed_agent_id: UUID
    status: str
    delivery_state: AgentControlDeliveryState
    terminal: bool
    result_status: Optional[str] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class RuntimeSessionControlResponse(BaseModel):
    """Whether a line typed on an attached session starts a new turn (#1150).

    ``mode`` is ``command`` when the session is run by an active managed
    agent with a verified Agent Control plugin and a live control
    connection: send the line with POST
    ``/agents/{managed_agent_id}/control/prompts`` and ``target_session_id``
    set to this session. Otherwise ``mode`` is ``note`` and ``reason`` says
    why, in words fit to show the operator.
    """

    runtime_session_id: UUID
    mode: Literal["command", "note"]
    reason_code: Optional[
        Literal[
            "session_ended",
            "not_managed",
            "unsupported_kind",
            "agent_inactive",
            "no_control_plugin",
            "control_offline",
        ]
    ] = None
    reason: Optional[str] = None
    #: Where a command-mode line is POSTed as ``{"text": ...}`` when the
    #: session is not run by a managed agent. Set for runner-hosted sessions
    #: (``agent_kind="runner_session"``, #1482); absent otherwise.
    send_path: Optional[str] = None
    managed_agent_id: Optional[UUID] = None
    agent_name: Optional[str] = None
    agent_kind: Optional[str] = None
