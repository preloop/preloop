"""Schemas for cost analytics, budgets, and pricing overrides."""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from preloop.models.models.budget import BudgetPeriod
from preloop.schemas.gateway_usage import (
    GatewayBudgetSummary,
    GatewayTokenUsage,
    GatewayUsageByDay,
    GatewayUsageByFlow,
    GatewayUsageByModel,
    GatewayUsageBySession,
    GatewayUsageByTool,
)


class BudgetPolicyBase(BaseModel):
    """Shared budget policy fields."""

    subject_type: str = Field(..., min_length=1, max_length=64)
    subject_id: Optional[UUID] = None
    model_alias: Optional[str] = Field(default=None, max_length=255)
    period: BudgetPeriod
    hard_limit_usd: Optional[float] = Field(default=None, ge=0)
    soft_limit_usd: Optional[float] = Field(default=None, ge=0)
    notify_on_soft: bool = False
    notify_on_hard: bool = False
    notification_user_ids: Optional[List[UUID]] = None
    notification_team_ids: Optional[List[UUID]] = None
    notification_emails: Optional[List[str]] = None


class BudgetPolicyCreate(BudgetPolicyBase):
    """Payload for creating a budget policy in the open-source core."""

    @model_validator(mode="after")
    def validate_limits(self) -> "BudgetPolicyCreate":
        """Require at least one limit and keep soft <= hard when both exist."""
        if self.hard_limit_usd is None and self.soft_limit_usd is None:
            raise ValueError("At least one budget limit must be configured")
        if (
            self.hard_limit_usd is not None
            and self.soft_limit_usd is not None
            and self.soft_limit_usd > self.hard_limit_usd
        ):
            raise ValueError("Soft limit cannot exceed hard limit")
        return self


class BudgetPolicyUpdate(BaseModel):
    """Payload for updating an existing budget policy."""

    hard_limit_usd: Optional[float] = Field(default=None, ge=0)
    soft_limit_usd: Optional[float] = Field(default=None, ge=0)
    notify_on_soft: Optional[bool] = None
    notify_on_hard: Optional[bool] = None
    notification_user_ids: Optional[List[UUID]] = None
    notification_team_ids: Optional[List[UUID]] = None
    notification_emails: Optional[List[str]] = None

    @model_validator(mode="after")
    def validate_limits(self) -> "BudgetPolicyUpdate":
        """Keep soft <= hard when both are supplied in the same request."""
        if (
            self.hard_limit_usd is not None
            and self.soft_limit_usd is not None
            and self.soft_limit_usd > self.hard_limit_usd
        ):
            raise ValueError("Soft limit cannot exceed hard limit")
        return self


class BudgetPolicyResponse(BudgetPolicyBase):
    """Budget policy returned by the API."""

    id: UUID

    class Config:
        from_attributes = True


class ModelPriceOverrideBase(BaseModel):
    """Shared fields for model price override requests."""

    ai_model_id: Optional[UUID] = None
    provider_name: Optional[str] = Field(default=None, max_length=255)
    model_alias: str = Field(..., min_length=1, max_length=255)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    fx_rate_to_usd: Optional[float] = Field(default=None, gt=0)
    input_price_per_1k: Optional[float] = Field(default=None, ge=0)
    output_price_per_1k: Optional[float] = Field(default=None, ge=0)
    cache_read_input_price_per_1k: Optional[float] = Field(default=None, ge=0)
    cache_creation_input_price_per_1k: Optional[float] = Field(default=None, ge=0)
    price_per_1k: Optional[float] = Field(default=None, ge=0)
    request_price: Optional[float] = Field(default=None, ge=0)
    discount_percent: Optional[float] = Field(default=None, ge=0, le=100)
    prepaid_token_balance: Optional[float] = Field(default=None, ge=0)
    prepaid_credit_balance_usd: Optional[float] = Field(default=None, ge=0)
    effective_from: Optional[datetime] = None
    effective_until: Optional[datetime] = None
    is_active: bool = True
    notes: Optional[str] = None

    @field_validator("provider_name")
    @classmethod
    def normalize_provider_name(cls, value: Optional[str]) -> Optional[str]:
        """Normalize provider names for stable lookup."""
        return (
            value.strip().lower() if isinstance(value, str) and value.strip() else None
        )

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        """Normalize ISO currency codes."""
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_pricing(self) -> "ModelPriceOverrideBase":
        """Require a usable pricing field and a valid effective range."""
        has_pricing = any(
            getattr(self, field_name) is not None
            for field_name in (
                "input_price_per_1k",
                "output_price_per_1k",
                "price_per_1k",
                "request_price",
                "discount_percent",
                "prepaid_token_balance",
                "prepaid_credit_balance_usd",
            )
        )
        if not has_pricing:
            raise ValueError("At least one price field must be configured")
        if (
            self.effective_from is not None
            and self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("effective_until must be after effective_from")
        if self.currency != "USD" and not self.fx_rate_to_usd:
            raise ValueError(
                "Non-USD overrides require fx_rate_to_usd (> 0) so costs can "
                "be recorded in USD"
            )
        return self


class ModelPriceOverrideCreate(ModelPriceOverrideBase):
    """Payload for creating a pricing override."""


class ModelPriceOverrideUpdate(BaseModel):
    """Payload for updating a pricing override."""

    ai_model_id: Optional[UUID] = None
    provider_name: Optional[str] = Field(default=None, max_length=255)
    model_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    fx_rate_to_usd: Optional[float] = Field(default=None, gt=0)
    input_price_per_1k: Optional[float] = Field(default=None, ge=0)
    output_price_per_1k: Optional[float] = Field(default=None, ge=0)
    cache_read_input_price_per_1k: Optional[float] = Field(default=None, ge=0)
    cache_creation_input_price_per_1k: Optional[float] = Field(default=None, ge=0)
    price_per_1k: Optional[float] = Field(default=None, ge=0)
    request_price: Optional[float] = Field(default=None, ge=0)
    discount_percent: Optional[float] = Field(default=None, ge=0, le=100)
    prepaid_token_balance: Optional[float] = Field(default=None, ge=0)
    prepaid_credit_balance_usd: Optional[float] = Field(default=None, ge=0)
    effective_from: Optional[datetime] = None
    effective_until: Optional[datetime] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None

    @field_validator("provider_name")
    @classmethod
    def normalize_provider_name(cls, value: Optional[str]) -> Optional[str]:
        """Normalize provider names for stable lookup."""
        return (
            value.strip().lower() if isinstance(value, str) and value.strip() else None
        )

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: Optional[str]) -> Optional[str]:
        """Normalize ISO currency codes."""
        return value.strip().upper() if isinstance(value, str) else value


class ModelPriceOverrideResponse(ModelPriceOverrideBase):
    """Pricing override returned by the API."""

    id: UUID
    account_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PriceCatalogInfo(BaseModel):
    """Provenance of the vendored model-price catalog."""

    source_url: Optional[str] = None
    fetched_at: Optional[datetime] = None
    model_count: Optional[int] = None


class ImportedUsageByModel(BaseModel):
    """Imported (observed) usage aggregate grouped by source model."""

    model_alias: Optional[str] = None
    source: Optional[str] = None
    request_count: int = 0
    total_tokens: int = 0
    imported_cost: float = 0.0
    last_event_at: Optional[datetime] = None


class ImportedUsageSummary(BaseModel):
    """Spend ingested from outside the gateway (``usage_source='imported'``).

    Kept as a separate block — never merged into ``estimated_cost`` or the
    budget figures — so gateway-metered and imported spend cannot be
    silently mixed.
    """

    event_count: int = 0
    total_tokens: int = 0
    imported_cost: float = 0.0
    usage_by_model: List[ImportedUsageByModel] = Field(default_factory=list)


class CostAnalyticsSummaryResponse(BaseModel):
    """Open-source cost overview response."""

    period_start: datetime
    period_end: datetime
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    unpriced_requests: int = 0
    unpriced_tokens: int = 0
    price_catalog: Optional[PriceCatalogInfo] = None
    budget: GatewayBudgetSummary
    requests_by_day: List[GatewayUsageByDay] = Field(default_factory=list)
    usage_by_model: List[GatewayUsageByModel] = Field(default_factory=list)
    usage_by_flow: List[GatewayUsageByFlow] = Field(default_factory=list)
    usage_by_session: List[GatewayUsageBySession] = Field(default_factory=list)
    usage_by_tool: List[GatewayUsageByTool] = Field(default_factory=list)
    imported_usage: Optional[ImportedUsageSummary] = None


class RepriceRequest(BaseModel):
    """Request body for re-pricing historical gateway usage."""

    start_date: datetime
    end_date: datetime
    only_unpriced: bool = True
    dry_run: bool = False

    @model_validator(mode="after")
    def validate_window(self) -> "RepriceRequest":
        """Require a positive time window."""
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class RepriceResponse(BaseModel):
    """Result of a repricing run (or async submission).

    The counters are ``None`` — not ``0`` — when the run was dispatched to a
    background worker (``submitted_async=True``): nothing has been scanned in
    this request, so reporting zeros would be indistinguishable from "the
    window really contained no rows" (the exact confusion behind "reprice
    examined 0 rows" on large windows).
    """

    submitted_async: bool = False
    rows_examined: Optional[int] = None
    rows_updated: Optional[int] = None
    rows_skipped: Optional[int] = None
    cost_before: Optional[float] = None
    cost_after: Optional[float] = None
    dry_run: bool = False


class LedgerBackfillBucket(BaseModel):
    """One (day x model family) allocation bucket of a ledger backfill."""

    day: date
    family: str
    ledger_total: float
    row_count: int
    allocated_total: float
    ledger_models: List[str] = Field(default_factory=list)


class LedgerBackfillResidual(BaseModel):
    """Ledger spend with no eligible usage rows to receive it."""

    day: date
    family: str
    amount_usd: float


class LedgerBackfillUnmatched(BaseModel):
    """Unpriced usage rows whose family had no ledger spend that day."""

    day: date
    family: str
    row_count: int


class LedgerBackfillResponse(BaseModel):
    """Outcome (or dry-run preview) of a provider daily-ledger backfill.

    ``rows_updated`` is ``None`` on a dry run — nothing was written, which is
    different from "wrote 0 rows". ``other_residual_usd`` is the export's
    "Other" bucket inside the window: the provider does not say which models
    it covers, so that spend is reported but never allocated and the matching
    rows stay unpriced.
    """

    dry_run: bool = True
    provider: str
    start: date
    end: date
    ledger_entries: int = 0
    eligible_rows: int = 0
    rows_to_reconcile: int = 0
    total_allocated: float = 0.0
    rows_updated: Optional[int] = None
    other_residual_usd: float = 0.0
    buckets: List[LedgerBackfillBucket] = Field(default_factory=list)
    unallocated_ledger: List[LedgerBackfillResidual] = Field(default_factory=list)
    unmatched_rows: List[LedgerBackfillUnmatched] = Field(default_factory=list)
    skipped_csv_rows: List[str] = Field(default_factory=list)


class CostHealthCheck(BaseModel):
    """One entry in the gateway accounting self-check checklist."""

    key: str
    status: str = Field(..., pattern="^(pass|fail|skip)$")
    detail: str


class CostHealthResponse(BaseModel):
    """Account-scoped gateway accounting self-check result."""

    window_hours: int
    checks: List[CostHealthCheck] = Field(default_factory=list)
    status: str = Field(..., pattern="^(pass|fail|skip)$")
