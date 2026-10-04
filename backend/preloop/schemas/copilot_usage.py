"""Schemas for the GitHub Copilot usage import on the Cost page.

Every figure here is imported from GitHub, not metered by the gateway. The
summary carries ``metered_by_gateway=False`` and a human-readable marker so
no client can mistake these rows for gateway spend.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

#: GitHub organization and enterprise slugs: alphanumerics and hyphens.
_SLUG_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?$"
#: GitHub user logins after canonicalisation: alphanumerics, hyphens and the
#: underscore that enterprise managed users carry. No whitespace, no slash.
_LOGIN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")


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
    is_active: Optional[bool] = Field(
        None,
        description=(
            "Pause (false) or resume (true) scheduled imports; omitted keeps "
            "the current value (new connections start active)."
        ),
    )


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
    last_warning: Optional[str] = None


class CopilotSyncResponse(BaseModel):
    """Acknowledgement that a sync was queued."""

    status: Literal["queued"] = "queued"


class CopilotUserMappingUpsert(BaseModel):
    """Map one GitHub login of the connected organization to one user.

    The login is trimmed and lowercased before it is stored, so writing the
    same login in another letter case updates the one mapping. The user must
    be an active user of the caller's account.
    """

    github_login: str = Field(..., min_length=1, max_length=100)
    user_id: UUID

    @field_validator("github_login")
    @classmethod
    def _canonical_login(cls, value: str) -> str:
        canonical = value.strip().lower()
        if not _LOGIN_PATTERN.match(canonical):
            raise ValueError(
                "github_login must be a GitHub login: letters, digits, hyphens "
                "or underscores, no whitespace"
            )
        return canonical


class CopilotUserMappingResponse(BaseModel):
    """One stored mapping; the user name is resolved within the account."""

    github_login: str
    user_id: UUID
    user_name: Optional[str] = None
    organization: str
    created_at: datetime
    updated_at: datetime


class CopilotUserMappingListResponse(BaseModel):
    """Mappings for the account's current Copilot organization."""

    organization: Optional[str] = Field(
        None, description="Connected organization; null without a connection."
    )
    items: List[CopilotUserMappingResponse] = Field(default_factory=list)
    total: int = 0


class CopilotSpendCoverageResponse(BaseModel):
    """How much stored premium-request spend the spend outlier rules can see.

    Counts rows per outcome over ``[period_start, period_end]`` (UTC days)
    for the current organization. ``mapped_net_amount`` is null when no row
    was mapped: unknown is not zero.
    """

    organization: Optional[str] = None
    connection_active: bool = False
    period_start: date
    period_end: date
    mapped_rows: int = 0
    mapped_net_amount: Optional[float] = Field(
        None,
        description=(
            "Sum of the positive per user, day and model nets, which is exactly "
            "what the rules evaluate. Null when no row was mapped."
        ),
    )
    credited_net_amount: Optional[float] = Field(
        None,
        description=(
            "Per user, day and model nets at or below zero (credits exceeding "
            "charges); these reach no rule. Null when there were none."
        ),
    )
    known_zero_rows: int = Field(
        0, description="Mapped rows whose billed amount is exactly zero."
    )
    excluded: Dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Rows the rules do not see, by reason: unmapped, unknown_amount, "
            "unsupported_currency, nonfinite_amount, aggregate_only, "
            "unattributed, not_daily."
        ),
    )
    unmapped_logins: List[str] = Field(default_factory=list)
    mapped_logins: List[str] = Field(default_factory=list)


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
    unattributed_net_amount: Optional[float] = Field(
        None,
        description=(
            "Spend on per-user days that no current seat holder explains, "
            "for example a developer whose seat was removed."
        ),
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
