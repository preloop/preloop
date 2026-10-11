"""Remote session runner protocol schemas (personal runners, contract C).

Websocket messages exchanged between the server and a runner that hosts a
remote harness session, plus the session states, end reasons and runner-side
rejection codes. Behaviour lives with the session endpoints and the runner;
this module only fixes the wire shapes.
"""

import json
from typing import Annotated, Any, Dict, List, Literal, Optional, Union, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from preloop.models.schemas.flow_runner import HarnessId

RunnerSessionState = Literal[
    "requested",
    "starting",
    "idle",
    "running",
    "stopping",
    "ended",
    "failed",
]
RUNNER_SESSION_STATES = frozenset(get_args(RunnerSessionState))

RunnerSessionRejectionCode = Literal[
    "harness_not_enabled_for_sessions",
    "harness_signed_out",
    "max_concurrent_reached",
    "workspace_not_authorized",
    "workspace_dirty",
    "checkout_failed",
    "sessions_disabled_on_host",
]
RUNNER_SESSION_REJECTION_CODES = frozenset(get_args(RunnerSessionRejectionCode))

RUNNER_SESSION_END_REASONS = frozenset(
    {
        "stopped_by_actor",
        "stopped_on_host",
        "idle_timeout",
        "runner_offline",
        "killed_by_kill_switch",
        "harness_exited",
        "max_duration",
    }
)
RUNNER_REJECTED_PREFIX = "runner_rejected:"

RunnerSessionStopMode = Literal["graceful", "kill"]
RunnerSessionEventKind = Literal[
    "agent_message", "tool_call", "tool_result", "usage", "stderr"
]
RunnerSessionTurnStatus = Literal["ok", "error"]

#: Upper bound on one ``session_event`` payload, serialized.
MAX_SESSION_EVENT_PAYLOAD_BYTES = 64 * 1024


def is_valid_end_reason(value: str) -> bool:
    """True for a fixed end reason or ``runner_rejected:<rejection code>``."""
    if value in RUNNER_SESSION_END_REASONS:
        return True
    if value.startswith(RUNNER_REJECTED_PREFIX):
        return value[len(RUNNER_REJECTED_PREFIX) :] in RUNNER_SESSION_REJECTION_CODES
    return False


class _Message(BaseModel):
    # Unknown keys are ignored so either side can add optional fields.
    model_config = ConfigDict(extra="ignore")


class SessionActor(_Message):
    user_id: str
    display_name: Optional[str] = None


# Server -> runner


class SessionStartMessage(_Message):
    type: Literal["session_start"] = "session_start"
    remote_session_id: str
    harness: HarnessId
    model: Optional[str] = Field(None, max_length=128)
    #: Workspace spec (contract D, owned by #1484); kept opaque here.
    workspace: Dict[str, Any]
    first_prompt: Optional[str] = None
    actor: SessionActor
    limits: Dict[str, Any] = Field(default_factory=dict)


class SessionTurnMessage(_Message):
    type: Literal["session_turn"] = "session_turn"
    remote_session_id: str
    turn_id: str
    text: str


class SessionStopMessage(_Message):
    type: Literal["session_stop"] = "session_stop"
    remote_session_id: str
    mode: RunnerSessionStopMode = "graceful"


# Runner -> server


class SessionStateMessage(_Message):
    type: Literal["session_state"] = "session_state"
    remote_session_id: str
    state: RunnerSessionState
    end_reason: Optional[str] = None
    harness_session_id: Optional[str] = Field(None, max_length=256)
    error_code: Optional[RunnerSessionRejectionCode] = None

    @field_validator("end_reason")
    @classmethod
    def check_end_reason(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not is_valid_end_reason(value):
            raise ValueError(f"unknown end_reason: {value}")
        return value


class SessionEventMessage(_Message):
    type: Literal["session_event"] = "session_event"
    remote_session_id: str
    turn_id: str
    seq: int = Field(ge=0)
    kind: RunnerSessionEventKind
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def check_payload_size(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        size = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
        if size > MAX_SESSION_EVENT_PAYLOAD_BYTES:
            raise ValueError("session_event payload exceeds 64 KB")
        return value


class SessionTurnDoneMessage(_Message):
    type: Literal["session_turn_done"] = "session_turn_done"
    remote_session_id: str
    turn_id: str
    status: RunnerSessionTurnStatus
    usage: Dict[str, Any] = Field(default_factory=dict)


ServerToRunnerSessionMessage = Union[
    SessionStartMessage, SessionTurnMessage, SessionStopMessage
]
RunnerToServerSessionMessage = Union[
    SessionStateMessage, SessionEventMessage, SessionTurnDoneMessage
]
RunnerSessionMessage = Union[ServerToRunnerSessionMessage, RunnerToServerSessionMessage]

_MESSAGE_ADAPTER: TypeAdapter[Any] = TypeAdapter(
    Annotated[
        Union[
            SessionStartMessage,
            SessionTurnMessage,
            SessionStopMessage,
            SessionStateMessage,
            SessionEventMessage,
            SessionTurnDoneMessage,
        ],
        Field(discriminator="type"),
    ]
)
SESSION_MESSAGE_TYPES: List[str] = [
    "session_start",
    "session_turn",
    "session_stop",
    "session_state",
    "session_event",
    "session_turn_done",
]


def parse_runner_session_message(data: Dict[str, Any]) -> RunnerSessionMessage:
    """Validate one websocket message by its ``type`` discriminator."""
    return _MESSAGE_ADAPTER.validate_python(data)
