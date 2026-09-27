"""Pydantic schemas for spend outlier alerts (#960)."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from preloop.models.models.spend_outlier import (
    DEFAULT_DAILY_MULTIPLE,
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_TOP_TIER_SHARE,
)

#: Upper bound on the prefix list, so the settings row stays small.
MAX_TOP_TIER_PREFIXES = 50


class SpendOutlierFindingResponse(BaseModel):
    """One open spend outlier, as the Attention page shows it."""

    id: str
    item_id: str = Field(..., description="Attention item id for dismissals")
    fingerprint: str = Field(
        ...,
        description=(
            "Rule, user, and UTC day (or session id). A new day that still "
            "matches is a new fingerprint, so a dismissed card comes back."
        ),
    )
    rule: Literal["daily_spend", "model_mix", "session_cost"]
    rule_label: str
    user_id: Optional[str] = None
    user_name: str
    runtime_session_id: Optional[str] = None
    session_title: Optional[str] = None
    day: str = Field(..., description="UTC day the finding is about")
    detected_at: str
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The numbers: spend_usd, median_usd, multiple (daily_spend); "
            "model, share, previous_share (model_mix); spend_usd, "
            "threshold_usd (session_cost). imported_usd is spend the gateway "
            "did not meter."
        ),
    )
    summary: str


class SpendOutlierListResponse(BaseModel):
    """Open spend outliers for the account."""

    items: List[SpendOutlierFindingResponse] = Field(default_factory=list)
    total: int = 0


class SpendOutlierSettingsPayload(BaseModel):
    """Body of ``PUT /attention/spend-outliers/settings``."""

    daily_multiple: float = Field(
        DEFAULT_DAILY_MULTIPLE,
        ge=1.0,
        le=100.0,
        allow_inf_nan=False,
        description="Fire when yesterday is at least this multiple of the median",
    )
    min_history_days: int = Field(
        DEFAULT_MIN_HISTORY_DAYS,
        ge=1,
        le=28,
        description="Days with spend in the trailing 28 before the rule runs",
    )
    top_tier_model_prefixes: List[str] = Field(
        default_factory=list,
        max_length=MAX_TOP_TIER_PREFIXES,
        description="Model name prefixes to treat as top-tier (case-insensitive)",
    )
    top_tier_share: float = Field(
        DEFAULT_TOP_TIER_SHARE,
        gt=0.0,
        lt=1.0,
        allow_inf_nan=False,
        description="Share of a user's daily spend, as a fraction (0.5 = 50%)",
    )
    session_cost_threshold_usd: Optional[float] = Field(
        None,
        gt=0.0,
        allow_inf_nan=False,
        description="Per-session cost threshold in USD; null turns the rule off",
    )

    @field_validator("top_tier_model_prefixes")
    @classmethod
    def _clean_prefixes(cls, value: List[str]) -> List[str]:
        cleaned: List[str] = []
        for prefix in value:
            item = prefix.strip().lower()
            if not item:
                continue
            if len(item) > 255:
                raise ValueError("model prefixes are limited to 255 characters")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class SpendOutlierSettingsResponse(SpendOutlierSettingsPayload):
    """The account's thresholds; defaults when never configured."""

    configured: bool = Field(
        False, description="False while the account uses every default"
    )
