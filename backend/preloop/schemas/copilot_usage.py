"""Schemas for the GitHub Copilot usage import on the Cost page.

Every figure here is imported from GitHub, not metered by the gateway. The
summary carries ``metered_by_gateway=False`` and a human-readable marker so
no client can mistake these rows for gateway spend.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

#: GitHub organization and enterprise slugs: alphanumerics and hyphens.
_SLUG_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?$"


class CopilotConnectionUpsert(BaseModel):
    """Create or update the account's Copilot import connection.

    Tokens are write-only: omit a token to keep the stored one. The seat
    price is replaced on every call, so sending ``null`` clears it.
    """

    organization: str = Field(..., pattern=_SLUG_PATTERN, max_length=100)
    enterprise: Optional[str] = Field(None, pattern=_SLUG_PATTERN, max_length=100)
    token: Optional[str] = Field(
        None,
        min_length=1,
        max_length=512,
        description=(
            "Organization token (organization owner or administrator). "
            "Required when creating the connection."
        ),
    )
    enterprise_token: Optional[str] = Field(
        None,
        min_length=1,
        max_length=512,
        description=(
            "Optional enterprise billing reader token for the enterprise "
            "premium-request route."
        ),
    )
    clear_enterprise_token: bool = False
    seat_price_monthly: Optional[float] = Field(
        None,
        ge=0,
        description="Operator-entered monthly price per seat; null clears it.",
    )
    is_active: bool = True


class CopilotConnectionResponse(BaseModel):
    """Connection state without any token material."""

    id: UUID
    organization: str
    enterprise: Optional[str] = None
    has_enterprise_token: bool = False
    seat_price_monthly: Optional[float] = None
    currency: str = "USD"
    is_active: bool = True
    last_synced_at: Optional[datetime] = None
    last_synced_day: Optional[date] = None
    last_error: Optional[str] = None
    per_user_billing_status: Optional[str] = None
    per_user_billing_reason: Optional[str] = None
    metrics_status: Optional[str] = None
    metrics_reason: Optional[str] = None


class CopilotSyncResponse(BaseModel):
    """Acknowledgement that a sync was queued."""

    status: Literal["queued"] = "queued"


class CopilotSeat(BaseModel):
    """One assigned seat and its last reported activity."""

    login: str
    last_activity_at: Optional[str] = None
    last_activity_editor: Optional[str] = None


class CopilotSeatSummary(BaseModel):
    """Seat count and the operator-priced monthly seat estimate."""

    total_seats: Optional[int] = None
    plan_type: Optional[str] = None
    as_of: Optional[datetime] = None
    seat_price_monthly: Optional[float] = None
    currency: str = "USD"
    monthly_seat_estimate: Optional[float] = Field(
        None,
        description=(
            "seat_price_monthly x total_seats. Null when no seat price was "
            "entered, never a zero."
        ),
    )
    assigned: List[CopilotSeat] = Field(default_factory=list)


class CopilotDeveloperSpend(BaseModel):
    """Premium-request spend for one developer."""

    login: str
    net_amount: float
    net_quantity: float


class CopilotModelSpend(BaseModel):
    """Premium-request spend for one model."""

    model: str
    net_amount: float
    net_quantity: float


class CopilotPremiumRequests(BaseModel):
    """Premium-request spend (usage, not the seat fee) for the window."""

    total_net_amount: Optional[float] = None
    currency: str = "USD"
    per_user_status: str = Field(
        ...,
        description="available, unavailable or no_data.",
    )
    per_user_unavailable_reason: Optional[str] = None
    org_aggregate_net_amount: Optional[float] = Field(
        None,
        description="Spend stored only as an organization total.",
    )
    aggregate_days: int = 0
    by_developer: List[CopilotDeveloperSpend] = Field(default_factory=list)
    by_model: List[CopilotModelSpend] = Field(default_factory=list)


class CopilotModelShare(BaseModel):
    """One model's share of a developer's usage."""

    model: str
    value: float
    share: float


class CopilotModelMix(BaseModel):
    """Model mix for one developer."""

    login: str
    basis: Literal["net_amount", "requests"]
    models: List[CopilotModelShare]


class CopilotUsageSummaryResponse(BaseModel):
    """The Copilot section of the Cost page."""

    metered_by_gateway: Literal[False] = False
    marker: str
    period_start: datetime
    period_end: datetime
    connection: Optional[CopilotConnectionResponse] = None
    seats: CopilotSeatSummary
    premium_requests: CopilotPremiumRequests
    model_mix: List[CopilotModelMix] = Field(default_factory=list)
