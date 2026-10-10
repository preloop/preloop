"""Schemas for the Anthropic usage import on the Cost page (#1413).

Every figure is imported from the Anthropic Admin API, not metered by the
gateway. The Admin key is write-only: responses carry ``has_key`` and a
four-character hint, never the key.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class AnthropicConnectionUpsert(BaseModel):
    """Create or update the account's Anthropic import connection.

    Omit ``admin_key`` to keep the stored key.
    """

    admin_key: Optional[str] = Field(
        None,
        min_length=8,
        max_length=512,
        description=(
            "Anthropic Admin API key dedicated to Preloop. Required when "
            "creating the connection. Never returned."
        ),
    )
    gateway_key_names: Optional[List[str]] = Field(
        None,
        max_length=200,
        description=(
            "Anthropic API key names Preloop uses as upstream credentials, in "
            "addition to the ones matched automatically. Usage under these "
            "names is already metered by the gateway and is not counted again."
        ),
    )
    is_active: Optional[bool] = Field(
        None, description="Pause (false) or resume (true) scheduled imports."
    )

    @field_validator("gateway_key_names")
    @classmethod
    def _clean_names(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        cleaned = sorted({name.strip()[:255] for name in value if name.strip()})
        return cleaned


class AnthropicConnectionResponse(BaseModel):
    """Connection state without any key material."""

    id: UUID
    has_key: bool = True
    key_hint: Optional[str] = None
    gateway_key_names: List[str] = Field(default_factory=list)
    is_active: bool = True
    last_synced_at: Optional[datetime] = None
    last_synced_day: Optional[date] = None
    last_error: Optional[str] = None
    last_warning: Optional[str] = None


class AnthropicSyncResponse(BaseModel):
    """Acknowledgement that a sync was queued."""

    status: Literal["queued"] = "queued"


class AnthropicConnectionTestResponse(BaseModel):
    """Outcome of the cheap read used to test the stored key."""

    ok: bool
    error: Optional[str] = None


class AnthropicExcludedUsage(BaseModel):
    """Imported usage already metered by the gateway, never added to totals."""

    estimated_cost: float = 0.0
    tokens: int = 0
    actors: List[str] = Field(default_factory=list)


class AnthropicActorUsage(BaseModel):
    """One actor's imported Claude Code usage over the window."""

    actor: str
    actor_type: Optional[str] = None
    estimated_cost: float = 0.0
    tokens: int = 0
    num_sessions: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    commits: int = 0
    pull_requests: int = 0
    days: int = 0
    user_id: Optional[UUID] = None
    mapping_source: Optional[Literal["mapping", "member_email"]] = None
    gateway_subject_id: Optional[UUID] = None


class AnthropicModelUsage(BaseModel):
    """Imported estimated cost and tokens for one model."""

    model: str
    estimated_cost: float = 0.0
    tokens: int = 0


class AnthropicUsageSummaryResponse(BaseModel):
    """The Anthropic section of the Cost page."""

    metered_by_gateway: Literal[False] = False
    marker: str
    period_start: datetime
    period_end: datetime
    connection: Optional[AnthropicConnectionResponse] = None
    total_estimated_cost: Optional[float] = Field(
        None,
        description="Estimated cost of usage outside the gateway; null without data.",
    )
    total_tokens: int = 0
    currency: str = "USD"
    excluded_metered_by_gateway: AnthropicExcludedUsage
    by_actor: List[AnthropicActorUsage] = Field(default_factory=list)
    by_model: List[AnthropicModelUsage] = Field(default_factory=list)
    not_attributable: List[str] = Field(default_factory=list)


class AnthropicUserMappingUpsert(BaseModel):
    """Map one imported actor (email, or ``key:<name>``) to one user."""

    actor: str = Field(..., min_length=1, max_length=255)
    user_id: UUID


class AnthropicUserMappingResponse(BaseModel):
    """One stored mapping."""

    actor: str
    user_id: UUID
    created_at: datetime
    updated_at: datetime


class AnthropicUserMappingListResponse(BaseModel):
    """Every actor mapping of the account."""

    items: List[AnthropicUserMappingResponse] = Field(default_factory=list)
    total: int = 0
