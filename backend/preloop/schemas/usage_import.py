"""Schemas for the usage ingest API (issue #123).

Normalized usage events observed OUTSIDE the model gateway (e.g. Cursor
bundled-model spend exported from the Cursor dashboard) are ingested into the
cost ledger with ``usage_source='imported'`` so they are visible in Cost
analytics without ever mixing into gateway budget accounting.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

#: Hard cap on events per ingest request; callers must batch beyond this.
MAX_EVENTS_PER_REQUEST = 5000

#: Hard cap on records per push-ingest request; harnesses stream small
#: continuous batches, so this bounds request size without hurting them.
MAX_INGEST_RECORDS_PER_REQUEST = 1000

#: Hard cap on the serialized size of one record's ``metadata`` object.
MAX_INGEST_METADATA_BYTES = 8 * 1024

#: Hook-shaped lifecycle event types accepted alongside plain usage
#: records. Lifecycle events may carry zero token/cost data; they exist so
#: analytics can count fan-out (e.g. ``subagent_start`` per
#: ``parent_conversation_id``) in near-real-time.
IngestEventType = Literal[
    "session_start",
    "session_end",
    "subagent_start",
    "subagent_stop",
    "response",
    "compaction",
    "usage",
]

#: How the record's charged_cost was determined. Reconciled records
#: (billing export) supersede estimated records (hook/transcript-derived)
#: with the same (source, conversation_id) scope in cost summaries — the
#: two are never summed together.
IngestCostBasis = Literal["estimated", "reconciled"]


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
                self.cache_read_tokens,
                self.cache_creation_tokens,
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


#: Cap on transcript messages one pushed record may carry, and on their
#: combined text. Transcript shipping is opt-in on the shipper side
#: (`preloop usage hook --store-transcript`); these bounds keep one hook
#: delivery small even when a generation produced a lot of text.
MAX_INGEST_TRANSCRIPT_MESSAGES = 50
MAX_INGEST_TRANSCRIPT_TEXT_CHARS = 4000
MAX_INGEST_TRANSCRIPT_TOTAL_CHARS = 64 * 1024

IngestTranscriptRole = Literal["user", "assistant", "tool", "tool_use"]


class UsageIngestTranscriptMessage(BaseModel):
    """One role-tagged text chunk of a conversation transcript."""

    role: IngestTranscriptRole
    text: str = Field(..., min_length=1, max_length=MAX_INGEST_TRANSCRIPT_TEXT_CHARS)
    timestamp: Optional[datetime] = None


class UsageIngestRecord(BaseModel):
    """One usage record pushed continuously by an external harness.

    Unlike ``UsageImportEvent`` (batch CSV/JSON import, content-hash
    dedupe), a pushed record carries a caller-supplied ``external_id`` so
    replays after network timeouts are idempotent, plus conversation ids so
    subagent workers billed on separate conversations can be rolled up
    under their parent thread in Cost analytics.
    """

    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Source-side record/turn id; dedupe key with `source`.",
    )
    conversation_id: Optional[str] = Field(default=None, max_length=255)
    parent_conversation_id: Optional[str] = Field(
        default=None,
        max_length=255,
        description=(
            "Conversation this record's conversation was spawned from "
            "(e.g. a subagent worker's parent thread), for cost rollup."
        ),
    )
    timestamp: datetime
    event_type: IngestEventType = Field(
        default="usage",
        description=(
            "Hook-shaped lifecycle marker. 'usage' (default) requires a "
            "model and at least one measurement; lifecycle events "
            "(session/subagent start/stop, response, compaction) may carry "
            "zero token/cost data and no model."
        ),
    )
    model: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Source-reported model name. Required for event_type='usage'.",
    )
    charged_cost: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description=(
            "Amount the source vendor charged, in USD. The billing truth; "
            "never derived from tokens. Stored as a float on the ledger "
            "(estimated_cost is a Float column), matching the CSV import path."
        ),
    )
    cost_basis: IngestCostBasis = Field(
        default="estimated",
        description=(
            "'estimated' (hook/transcript-derived) or 'reconciled' (billing "
            "export). Reconciled records supersede estimated records with "
            "the same (source, conversation_id) in cost summaries; the two "
            "are never summed together."
        ),
    )
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    cache_read_tokens: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Cache-read tokens, reported distinctly; never summed into "
            "charged spend or total tokens."
        ),
    )
    message_count: Optional[int] = Field(
        default=None,
        ge=0,
        description="Messages in the conversation so far (growth tripwire).",
    )
    tool_call_count: Optional[int] = Field(
        default=None,
        ge=0,
        description="Tool calls in the conversation so far (growth tripwire).",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Small JSON object. Recognized session keys: `session_title` "
            "(sets the runtime session title), `session_title_default` "
            "(fills the title only while it is empty) and `session_summary` "
            "(replaces the runtime session summary)."
        ),
    )
    transcript: Optional[List[UsageIngestTranscriptMessage]] = Field(
        default=None,
        max_length=MAX_INGEST_TRANSCRIPT_MESSAGES,
        description=(
            "Opt-in transcript text for this record's conversation, stored "
            "as runtime session activities. Shippers send it only when the "
            "operator enabled transcript storage; by default only counts, "
            "titles and short summaries leave the machine."
        ),
    )

    @field_validator("transcript")
    @classmethod
    def cap_transcript_size(
        cls, value: Optional[List[UsageIngestTranscriptMessage]]
    ) -> Optional[List[UsageIngestTranscriptMessage]]:
        """Reject transcript payloads whose combined text exceeds the cap."""
        if not value:
            return value
        total = sum(len(message.text) for message in value)
        if total > MAX_INGEST_TRANSCRIPT_TOTAL_CHARS:
            raise ValueError(
                f"transcript exceeds {MAX_INGEST_TRANSCRIPT_TOTAL_CHARS} "
                f"characters ({total} characters)"
            )
        return value

    @field_validator("external_id")
    @classmethod
    def strip_external_id(cls, value: str) -> str:
        """Normalize the dedupe key; blank values are invalid."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("external_id must not be blank")
        return stripped

    @field_validator("conversation_id", "parent_conversation_id")
    @classmethod
    def strip_conversation_ids(cls, value: Optional[str]) -> Optional[str]:
        """Normalize conversation ids; empty strings collapse to None."""
        if value is None:
            return None
        return value.strip() or None

    @field_validator("metadata")
    @classmethod
    def cap_metadata_size(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Reject metadata objects larger than the serialized cap."""
        if value is None:
            return None
        size = len(json.dumps(value, default=str).encode("utf-8"))
        if size > MAX_INGEST_METADATA_BYTES:
            raise ValueError(
                f"metadata exceeds {MAX_INGEST_METADATA_BYTES} bytes "
                f"serialized ({size} bytes)"
            )
        return value

    @field_validator("model")
    @classmethod
    def strip_model(cls, value: Optional[str]) -> Optional[str]:
        """Normalize the model name; empty strings collapse to None."""
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_usage_event_shape(self) -> "UsageIngestRecord":
        """Require model + a measurable quantity for plain usage records.

        Lifecycle events (``event_type != 'usage'``) may carry zero
        token/cost data and no model — they exist so analytics can count
        fan-out in near-real-time.
        """
        if self.event_type != "usage":
            return self
        if self.model is None:
            raise ValueError("Usage records must carry a model name")
        has_measurement = any(
            value is not None
            for value in (
                self.charged_cost,
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
            )
        )
        if not has_measurement:
            raise ValueError("Record must carry token counts and/or a charged_cost")
        return self


class UsageIngestRequest(BaseModel):
    """Batch of pushed usage records to ingest."""

    records: List[UsageIngestRecord] = Field(
        ..., min_length=1, max_length=MAX_INGEST_RECORDS_PER_REQUEST
    )
    agent_id: Optional[UUID] = Field(
        default=None,
        description=(
            "Managed agent to attribute the records to. When omitted, the "
            "account's single managed agent whose kind matches `source` is "
            "used if exactly resolvable."
        ),
    )
    source: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Origin label stored on each row (e.g. 'cursor').",
    )

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        """Normalize the source label for stable filtering and dedupe."""
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("source must not be blank")
        return normalized


class UsageIngestRecordResult(BaseModel):
    """Per-record outcome of a push-ingest request."""

    external_id: str
    deduplicated: bool = False
    conflict: bool = Field(
        default=False,
        description=(
            "True when a deduplicated replay carried a DIFFERENT payload "
            "than the stored record (first write wins; the batch still "
            "returns 200 so shippers can retry blindly)."
        ),
    )


class UsageIngestResponse(BaseModel):
    """Result of a push-ingest request.

    Replays return 200 with the replayed records flagged
    ``deduplicated`` — spend is never double-counted. Replays whose
    payload differs from the stored record are additionally flagged
    ``conflict`` (first write wins; never a 409).
    """

    source: str
    agent_id: Optional[UUID] = None
    agent_display_name: Optional[str] = None
    accepted: int = 0
    deduplicated: int = 0
    conflicts: int = 0
    results: List[UsageIngestRecordResult] = Field(default_factory=list)


class UsageImportCsvResponse(UsageImportResponse):
    """Result of a CSV import, including rows the parser could not use."""

    parsed_rows: int = 0
    skipped_rows: int = 0
    skipped_row_reasons: List[str] = Field(default_factory=list)
