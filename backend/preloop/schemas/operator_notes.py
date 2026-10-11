"""Schemas for operator notes: the API shape of a steered instruction."""

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from preloop.services.operator_notes import MAX_NOTE_BODY_CHARS

OperatorNoteState = Literal[
    "pending",
    "claimed",
    "delivered",
    "acknowledged",
    "cancelled",
    "expired",
    "failed",
    "undeliverable",
]
OperatorNoteChannel = Literal["gateway", "hook", "claude_channel", "claude_message"]
# Harness pull only. ``gateway`` is stamped by the gateway path itself; a hook
# must not be able to mislabel a delivery as a turn-boundary inject.
OperatorNotePullChannel = Literal["hook", "claude_channel", "claude_message"]
# The lifecycle point that pulled the note. Bounded on purpose: the value lands
# in the audit record, so the hook must not be able to write arbitrary text.
OperatorNoteEvent = Literal["session_start", "stop", "pre_tool_use"]


class OperatorNoteCreate(BaseModel):
    """Send one note to a running agent, session or flow execution.

    Exactly one target. A session id steers that conversation; an execution id
    resolves to the session that execution is running on; an agent id with no
    open session waits for the agent's next one.
    """

    source: Literal["api", "chat"] = "api"
    correlation_id: Optional[UUID] = None

    text: str = Field(
        ...,
        min_length=1,
        max_length=MAX_NOTE_BODY_CHARS,
        description="What the agent should be told, as the author typed it.",
    )
    agent_id: Optional[UUID] = Field(
        None, description="Managed agent to steer, current or next session."
    )
    runtime_session_id: Optional[UUID] = Field(
        None, description="One runtime session, and only that session."
    )
    execution_id: Optional[UUID] = Field(
        None, description="Flow execution, resolved to its live runtime session."
    )
    expires_in_seconds: Optional[int] = Field(
        None,
        ge=60,
        le=7 * 24 * 60 * 60,
        description=(
            "How long the note stays deliverable. Default 24 hours: an "
            "undelivered note is stale advice, and expiring visibly beats "
            "rotting silently."
        ),
    )

    @model_validator(mode="after")
    def validate_single_target(self) -> "OperatorNoteCreate":
        targets = [self.agent_id, self.runtime_session_id, self.execution_id]
        if sum(1 for target in targets if target is not None) != 1:
            raise ValueError(
                "Name exactly one of agent_id, runtime_session_id, execution_id"
            )
        return self


class OperatorNoteAuthor(BaseModel):
    """Who sent the note, as stamped into the delivered label.

    Exactly one of ``user_id`` and ``agent_id`` is set: a person wrote it, or
    a managed agent did through the ``send_note`` tool. ``auth_method`` is
    ``agent`` in the second case and is always derived server side.
    """

    user_id: Optional[UUID] = None
    agent_id: Optional[UUID] = Field(
        None, description="Managed agent that wrote the note, when not a person."
    )
    display: Optional[str] = None
    auth_method: Optional[str] = None


class OperatorNoteResponse(BaseModel):
    """One note and everything the sender needs to know about its fate."""

    note_id: str
    state: OperatorNoteState
    text: str
    managed_agent_id: Optional[UUID] = None
    runtime_session_id: Optional[UUID] = None
    author: OperatorNoteAuthor
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    claimed_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None
    undeliverable_at: Optional[datetime] = None
    undeliverable_reason: Optional[Literal["session_ended", "spool_dropped"]] = None
    delivered_at: Optional[datetime] = None
    delivery_channel: Optional[OperatorNoteChannel] = None
    delivery_event: Optional[OperatorNoteEvent] = None
    delivered_turn_index: Optional[int] = None
    acknowledged_turn_id: Optional[str] = None
    cancelled_at: Optional[datetime] = None


class OperatorNoteList(BaseModel):
    """Notes for one agent, session or execution, newest first."""

    notes: list[OperatorNoteResponse] = Field(default_factory=list)


class OperatorNotePendingRequest(BaseModel):
    """A harness-side pull for whatever this session has been told.

    Called by a hook, a Claude Code channel server or an inbox bridge, never
    by the model: an agent must not spend tokens deciding to check.
    """

    channel: OperatorNotePullChannel = Field(
        "hook",
        description=(
            "Which harness transport is about to carry the note. Gateway "
            "delivery is recorded by the gateway itself, never by this pull."
        ),
    )
    defer_delivery: bool = Field(
        False,
        description="Claim without delivery until a carrying hook acknowledges the note.",
    )
    event: OperatorNoteEvent = Field(
        "pre_tool_use",
        description=(
            "The harness lifecycle point asking for notes: session_start, "
            "stop or pre_tool_use. Recorded on the delivery for audit."
        ),
    )
    session_id: Optional[str] = Field(
        None, description="Harness session id, for logging only."
    )


class OperatorNotePendingResponse(BaseModel):
    """Pending notes, rendered once for the model and once for a machine."""

    notes: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per note: id, the stored A2A envelope, the rendered element.",
    )
    text: Optional[str] = Field(
        None,
        description=(
            "The framed block to hand the model verbatim as additional "
            "context. Null when nothing is pending."
        ),
    )
    channel_event: Optional[dict[str, Any]] = Field(
        None,
        description=(
            "The same block shaped as a Claude Code channel notification, so "
            "a channel server can forward it unchanged."
        ),
    )


class OperatorSessionEndRequest(BaseModel):
    """A harness telling Preloop its session ended (SessionEnd hook).

    The runtime bearer already names the session, so this body is only the
    harness's own identifiers and the reason; the server never trusts it to
    pick a different session.
    """

    external_session_id: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "Harness session id, for logging only; never used to pick a session."
        ),
    )
    reason: Optional[str] = Field(
        None,
        max_length=255,
        description="Why the harness ended the session, e.g. 'exit' or 'clear'.",
    )


class OperatorSessionEndResponse(BaseModel):
    """What the sweep changed: the bound session and how many notes it failed."""

    runtime_session_id: Optional[UUID] = None
    marked: int = Field(
        0, description="Session-only pending notes moved to undeliverable."
    )


class OperatorNoteAckRequest(BaseModel):
    """Receipt from a hook after writing a claimed note into carrying output."""

    note_ids: list[str] = Field(default_factory=list, max_length=100)
    session_id: Optional[str] = Field(None, max_length=255)
    undeliverable_reason: Optional[Literal["spool_dropped"]] = None


class OperatorNoteAckResponse(BaseModel):
    """No per-id results, so ignored ids reveal no existence information."""

    ok: bool = True
