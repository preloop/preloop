"""Response schemas for host-exec session and seat usage of an execution."""

from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class HostExecSession(BaseModel):
    """One CLI session the usage hook observed during a host-exec run."""

    conversation_id: Optional[str] = Field(
        default=None, description="CLI session id reported by the hook."
    )
    source: Optional[str] = Field(
        default=None, description="Ingest source label, e.g. copilot_cli."
    )
    runtime_session_id: Optional[UUID] = None
    event_count: int = 0
    event_types: Dict[str, int] = Field(default_factory=dict)
    first_event_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    models: List[str] = Field(default_factory=list)


class HostExecSessionsResponse(BaseModel):
    """Hook sessions and subscription usage linked to one flow execution."""

    execution_id: UUID
    sessions: List[HostExecSession] = Field(default_factory=list)
    event_count: int = Field(
        default=0, description="Hook events linked to the execution."
    )
    premium_requests: Optional[float] = Field(
        default=None,
        description=(
            "Copilot premium requests the CLI reported for the run. Billed "
            "to the seat, not metered by the gateway."
        ),
    )
    gateway_metered: bool = Field(
        default=False,
        description="Always false: these rows never pass the model gateway.",
    )
