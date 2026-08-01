"""Schemas for the usage ingest API (issue #123).

Normalized usage events observed OUTSIDE the model gateway (e.g. Cursor
bundled-model spend exported from the Cursor dashboard) are ingested into the
cost ledger with ``usage_source='imported'`` so they are visible in Cost
analytics without ever mixing into gateway budget accounting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

#: Hard cap on events per ingest request; callers must batch beyond this.
MAX_EVENTS_PER_REQUEST = 5000


class UsageImportEvent(BaseModel):
    """One normalized usage event observed outside the model gateway."""

    timestamp: datetime
    model: str = Field(..., min_length=1, max_length=255)
    prompt_tokens: Optional[int] = Field(default=None, ge=0)
    completion_tokens: Optional[int] = Field(default=None, ge=0)
    total_tokens: Optional[int] = Field(default=None, ge=0)
    cache_read_tokens: Optional[int] = Field(default=None, ge=0)
    cache_creation_tokens: Optional[int] = Field(default=None, ge=0)
    charged_cents: Optional[float] = Field(
        default=None,
        ge=0,
        description="Amount charged by the source vendor, in cents (USD).",
    )
    cost_usd: Optional[float] = Field(
        default=None,
        ge=0,
        description="Amount charged in USD. Mutually exclusive with charged_cents.",
    )
    session_id: Optional[str] = Field(default=None, max_length=255)
    kind: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Source billing category (e.g. 'Usage-based', 'Included').",
    )
    max_mode: Optional[bool] = None
    meta: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_measurement_present(self) -> "UsageImportEvent":
        """Require at least one measurable quantity (tokens or money)."""
        if self.charged_cents is not None and self.cost_usd is not None:
            raise ValueError("Provide charged_cents or cost_usd, not both")
        has_tokens = any(
            value is not None
            for value in (
                self.prompt_tokens,
                self.completion_tokens,
                self.total_tokens,
            )
        )
        has_cost = self.charged_cents is not None or self.cost_usd is not None
        if not has_tokens and not has_cost:
            raise ValueError("Event must carry token counts and/or a charged amount")
        return self

    def resolved_cost_usd(self) -> Optional[float]:
        """Return the event's charged amount in USD, if any."""
        if self.cost_usd is not None:
            return float(self.cost_usd)
        if self.charged_cents is not None:
            return float(self.charged_cents) / 100.0
        return None


class UsageImportRequest(BaseModel):
    """Batch of normalized usage events to ingest."""

    events: List[UsageImportEvent] = Field(
        ..., min_length=1, max_length=MAX_EVENTS_PER_REQUEST
    )
    agent_id: Optional[UUID] = Field(
        default=None,
        description=(
            "Managed agent to attribute the events to. When omitted, the "
            "account's managed Cursor agent (from `preloop agents onboard "
            "cursor`) is used if exactly resolvable."
        ),
    )
    source: str = Field(
        default="cursor",
        min_length=1,
        max_length=64,
        description="Origin label stored on each row (e.g. 'cursor').",
    )

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        """Normalize the source label for stable filtering."""
        return value.strip().lower()


class UsageImportResponse(BaseModel):
    """Result of an ingest request."""

    imported: int = 0
    skipped_duplicates: int = 0
    agent_id: Optional[UUID] = None
    agent_display_name: Optional[str] = None
    source: str = "cursor"


class UsageImportCsvResponse(UsageImportResponse):
    """Result of a CSV import, including rows the parser could not use."""

    parsed_rows: int = 0
    skipped_rows: int = 0
    skipped_row_reasons: List[str] = Field(default_factory=list)


class ImportedUsageByModel(BaseModel):
    """Imported-usage aggregate grouped by model."""

    model_alias: Optional[str] = None
    source: Optional[str] = None
    request_count: int = 0
    total_tokens: int = 0
    imported_cost: float = 0.0
    last_event_at: Optional[datetime] = None
