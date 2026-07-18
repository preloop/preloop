"""Pydantic schemas for time-boxed approval bypasses."""

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from preloop.models.models.approval_bypass import MAX_BYPASS_DURATION

BypassMode = Literal["mute_notifications", "auto_approve"]

#: Durations offered as one-tap presets. Kept short and few so the choice is
#: instant for someone whose phone is buzzing.
BYPASS_DURATION_PRESETS_MINUTES = (15, 60, 240, 480)

MAX_BYPASS_MINUTES = int(MAX_BYPASS_DURATION.total_seconds() // 60)


class ApprovalBypassCreate(BaseModel):
    """Request body for opening a bypass.

    ``duration_minutes`` is required and bounded — there is deliberately no way
    to express "forever". An indefinite bypass is the failure mode this feature
    exists to prevent.
    """

    mode: BypassMode = Field(
        ...,
        description=(
            "'mute_notifications' silences alerts but still blocks the agent; "
            "'auto_approve' also approves automatically (governance bypass)"
        ),
    )
    duration_minutes: int = Field(
        ...,
        ge=1,
        le=MAX_BYPASS_MINUTES,
        description=f"How long the bypass lasts (1-{MAX_BYPASS_MINUTES} minutes)",
    )
    managed_agent_id: Optional[UUID] = Field(
        None,
        description="Scope to one agent. Omit for an account-wide bypass.",
    )
    reason: Optional[str] = Field(
        None,
        max_length=500,
        description="Why the bypass was opened (recorded for audit)",
    )
    created_via: Optional[str] = Field(
        None,
        description="Originating surface: console|mobile|watch|api|cli",
    )


class ApprovalBypassResponse(BaseModel):
    """A bypass as returned to console and mobile surfaces."""

    id: UUID
    account_id: UUID
    user_id: UUID
    managed_agent_id: Optional[UUID] = None
    mode: str
    reason: Optional[str] = None
    created_by_user_id: UUID
    created_via: Optional[str] = None
    created_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None
    revoked_by_user_id: Optional[UUID] = None
    auto_approved_count: int = 0

    model_config = ConfigDict(from_attributes=True)

    @computed_field
    def is_account_wide(self) -> bool:
        """True when this bypass applies to every agent for the user."""
        return self.managed_agent_id is None

    @computed_field
    def suppresses_approvals(self) -> bool:
        """True when agents are auto-approved rather than merely unmuted."""
        return self.mode == "auto_approve"


class ApprovalBypassStatus(BaseModel):
    """Aggregate 'is anything bypassed right now?' answer for banners.

    Console and mobile poll this to decide whether to show the persistent
    warning banner, so it is intentionally cheap and shaped for direct
    rendering.
    """

    active: bool = Field(..., description="Whether any bypass is currently in force")
    auto_approve_active: bool = Field(
        ...,
        description="Whether any active bypass is auto-approving (unsupervised)",
    )
    bypasses: list[ApprovalBypassResponse] = Field(default_factory=list)

    @computed_field
    def soonest_expiry(self) -> Optional[datetime]:
        """Earliest expiry among active bypasses, for a countdown."""
        if not self.bypasses:
            return None
        return min(b.expires_at for b in self.bypasses)
