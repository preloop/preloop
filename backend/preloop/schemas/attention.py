"""Pydantic schemas for console attention dismissals."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: Maximum snooze, so "remind me later" can never become "never again".
MAX_SNOOZE_DAYS = 90


class AttentionDismissalUpsertRequest(BaseModel):
    """Body of ``PUT /attention/dismissals/{item_id}``."""

    fingerprint: str = Field(
        ...,
        max_length=4096,
        description=(
            "Why the item is showing (latest failed run id, unpriced model "
            "aliases, agent onboarding and validation state). The item comes "
            "back when this changes."
        ),
    )
    reason: Literal["expected", "snoozed", "fixed"] = Field(
        ...,
        description=(
            "expected: this is how the account is configured. "
            "snoozed: hide it for snooze_days. fixed: it has been dealt with."
        ),
    )
    snooze_days: Optional[int] = Field(
        None,
        ge=1,
        le=MAX_SNOOZE_DAYS,
        description="Required for reason='snoozed'; ignored otherwise.",
    )


class AttentionDismissalResponse(BaseModel):
    """One dismissal that is still in force."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    item_id: str
    fingerprint: str
    reason: str
    snooze_until: Optional[datetime] = None
    dismissed_by_user_id: Optional[UUID] = None
    dismissed_by_username: Optional[str] = None
    created_at: datetime


class AttentionDismissalListResponse(BaseModel):
    """Every dismissal in force for the account."""

    items: List[AttentionDismissalResponse] = Field(default_factory=list)
    total: int = 0
