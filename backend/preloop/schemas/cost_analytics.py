"""Schemas for cost analytics, budgets, and pricing overrides."""

from __future__ import annotations

from datetime import datetime
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
        return self


class ModelPriceOverrideCreate(ModelPriceOverrideBase):
    """Payload for creating a pricing override."""


class ModelPriceOverrideUpdate(BaseModel):
    """Payload for updating a pricing override."""

    ai_model_id: Optional[UUID] = None
    provider_name: Optional[str] = Field(default=None, max_length=255)
    model_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
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


class CostAnalyticsSummaryResponse(BaseModel):
    """Open-source cost overview response."""

    period_start: datetime
    period_end: datetime
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    token_usage: GatewayTokenUsage
    estimated_cost: float = 0.0
    budget: GatewayBudgetSummary
    requests_by_day: List[GatewayUsageByDay] = Field(default_factory=list)
    usage_by_model: List[GatewayUsageByModel] = Field(default_factory=list)
    usage_by_flow: List[GatewayUsageByFlow] = Field(default_factory=list)
    usage_by_session: List[GatewayUsageBySession] = Field(default_factory=list)
    usage_by_tool: List[GatewayUsageByTool] = Field(default_factory=list)
