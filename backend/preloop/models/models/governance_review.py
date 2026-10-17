"""Weekly governance review tables.

Five account-scoped tables for the governance and efficiency review. Processing
lives in an Enterprise plugin; this module is only the store.

* ``governance_policy_document`` is the prose policy, one versioned row per
  edit. History is never deleted. A partial unique index keeps a single
  active version per account.
* ``governance_review_settings`` is one row per account. A missing row means
  the deployment default (Cloud ``on_platform``, self-hosted ``off`` until an
  analysis model is configured).
* ``governance_session_digest`` is one row per session segment, not one row
  per session. Stage B reads the segments whose ``window_end`` falls in the
  review week.
* ``governance_review_run`` is one weekly pass, unique per account and
  ``window_start`` so a retry returns the same run.
* ``governance_finding`` is one finding. ``(account_id, fingerprint)`` makes
  a retried run insert nothing. Callers put the review window in the
  fingerprint when the same condition should be recorded again next week.
"""

from datetime import datetime
from typing import Any, Optional
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from preloop.models.models.access_values import in_list_check
from preloop.models.models.base import Base

#: Where a policy document body came from. Editing and restoring both insert
#: a new version; they never update an old row.
POLICY_DOCUMENT_SOURCE_DEFAULT = "default_template"
POLICY_DOCUMENT_SOURCE_USER = "user"
POLICY_DOCUMENT_SOURCE_RESTORE = "restore"
POLICY_DOCUMENT_SOURCES: tuple[str, ...] = (
    POLICY_DOCUMENT_SOURCE_DEFAULT,
    POLICY_DOCUMENT_SOURCE_USER,
    POLICY_DOCUMENT_SOURCE_RESTORE,
)

#: 64 KiB, the spec cap on ``body_markdown``.
POLICY_DOCUMENT_BODY_MAX_BYTES = 64 * 1024

#: Cap on the stored review settings for the measured checks.
DETECTOR_THRESHOLDS_MAX_BYTES = 4 * 1024

#: Review mode. A missing settings row is not stored; the CRUD layer fills
#: the deployment default.
REVIEW_MODE_ON_PLATFORM = "on_platform"
REVIEW_MODE_OFF_OVER_BUDGET = "off_over_budget"
REVIEW_MODE_ON_OWN_MODEL = "on_own_model"
REVIEW_MODE_OFF = "off"
REVIEW_MODES: tuple[str, ...] = (
    REVIEW_MODE_ON_PLATFORM,
    REVIEW_MODE_OFF_OVER_BUDGET,
    REVIEW_MODE_ON_OWN_MODEL,
    REVIEW_MODE_OFF,
)

#: Who paid for the model call that wrote a segment.
DIGEST_FUNDING_PLATFORM = "platform"
DIGEST_FUNDING_ACCOUNT = "account"
DIGEST_FUNDING: tuple[str, ...] = (
    DIGEST_FUNDING_PLATFORM,
    DIGEST_FUNDING_ACCOUNT,
)

#: Stable literal shared by the retry reader and its partial queue index.
SESSION_DIGEST_RETRY_PREDICATE = (
    "(digest ->> 'summary_status') IN "
    "('budget_limited', 'provider_failed', 'summarizing')"
)

#: Structured digest JSON, serialized, from spec section 4.3.
DIGEST_MAX_BYTES = 4 * 1024
DIGEST_INTENT_MAX_CHARS = 300
DIGEST_NOTABLE_MAX = 5
DIGEST_NOTABLE_MAX_CHARS = 280
OBSERVATION_EXCERPT_MAX_CHARS = 280

#: Spec 4.3 outcomes, plus ``done`` from the later summary-schema addendum
#: so a segment writer can use either word.
DIGEST_OUTCOMES: tuple[str, ...] = (
    "completed",
    "abandoned",
    "failed",
    "unknown",
    "done",
)
DIGEST_TRIVIALITY: tuple[str, ...] = ("trivial", "routine", "complex")

RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_PARTIAL = "partial"
RUN_STATUS_SKIPPED = "skipped"
RUN_STATUS_FAILED = "failed"
RUN_STATUSES: tuple[str, ...] = (
    RUN_STATUS_RUNNING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_SKIPPED,
    RUN_STATUS_FAILED,
)

FINDING_KIND_VIOLATION = "violation"
FINDING_KIND_INEFFICIENCY = "inefficiency"
FINDING_KIND_ANOMALY = "anomaly"
FINDING_KINDS: tuple[str, ...] = (
    FINDING_KIND_VIOLATION,
    FINDING_KIND_INEFFICIENCY,
    FINDING_KIND_ANOMALY,
)
FINDING_SEVERITIES: tuple[str, ...] = ("high", "medium", "low")
FINDING_TITLE_MAX_CHARS = 160
FINDING_SUGGESTION_MAX_CHARS = 600
FINDING_SESSION_IDS_MAX = 20
FINDING_EVIDENCE_EXCERPT_MAX_CHARS = 280

_MODE_LIST = in_list_check("mode", REVIEW_MODES)
_MODE_BEFORE_LIST = in_list_check("mode_before", REVIEW_MODES)
_MODE_AFTER_LIST = in_list_check("mode_after", REVIEW_MODES)


class GovernancePolicyDocument(Base):
    """One version of an account's governance policy prose.

    Separate from ``PolicyVersion``. That table snapshots executable policy
    (MCP servers, rules) and is applied. This document is prose a model
    reads. A prose edit must not look like a policy rollback.
    """

    __tablename__ = "governance_policy_document"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "version",
            name="uq_governance_policy_document_account_version",
        ),
        Index(
            "uq_governance_policy_document_one_active",
            "account_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_governance_policy_document_version",
        ),
        CheckConstraint(
            "octet_length(body_markdown) <= 65536",
            name="ck_governance_policy_document_body_len",
        ),
        CheckConstraint(
            in_list_check("source", POLICY_DOCUMENT_SOURCES),
            name="ck_governance_policy_document_source",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Per-account version, starting at 1",
    )
    body_markdown: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Policy prose, at most 64 KiB",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="Exactly one active version per account",
    )
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="default_template | user | restore",
    )

    def __repr__(self) -> str:
        active = " active" if self.is_active else ""
        return (
            f"<GovernancePolicyDocument account={str(self.account_id)[:8]} "
            f"v{self.version}{active}>"
        )


class GovernanceReviewSettings(Base):
    """One account's governance review mode.

    Absent row means the deployment default. ``account_id`` is unique, which
    is the one-row-per-account key the spec calls the primary key. The
    surrogate ``id`` stays so this table matches the rest of the schema.
    """

    __tablename__ = "governance_review_settings"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            name="uq_governance_review_settings_account",
        ),
        CheckConstraint(
            _MODE_LIST,
            name="ck_governance_review_settings_mode",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="on_platform | off_over_budget | on_own_model | off",
    )
    analysis_model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_model.id", ondelete="SET NULL"),
        nullable=True,
    )
    mode_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    mode_changed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("governance_review_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    detector_thresholds: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "Review settings the measured checks read, keyed by rule kind. "
            "Null or a missing key keeps the default."
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<GovernanceReviewSettings account={str(self.account_id)[:8]} "
            f"mode={self.mode}>"
        )


class GovernanceSessionDigest(Base):
    """One summarised segment of a runtime session.

    A session that stays open for weeks gets a new segment when it closes,
    when it is idle, when the unsummarised transcript hits the token budget,
    or at the Monday cutoff. Stage B reads ``rolling_summary`` from the
    previous segment instead of the old transcript.

    ``digest`` is the spec 4.3 structured record. ``summary`` is the
    policy-aware generic digest for this segment (same JSON shape). ``observations``
    holds ``{rule_ref, excerpt, confidence}`` rows. ``policy_document_version``
    is the active document version the segment was summarised under.
    """

    __tablename__ = "governance_session_digest"
    __table_args__ = (
        UniqueConstraint(
            "runtime_session_id",
            "segment_index",
            name="uq_governance_session_digest_segment",
        ),
        Index(
            "ix_governance_session_digest_account_window_end",
            "account_id",
            "window_end",
        ),
        Index(
            "ix_governance_session_digest_retry_queue",
            "account_id",
            "window_end",
            "runtime_session_id",
            "segment_index",
            "id",
            postgresql_where=text(SESSION_DIGEST_RETRY_PREDICATE),
        ),
        CheckConstraint(
            "segment_index >= 0",
            name="ck_governance_session_digest_segment_index",
        ),
        CheckConstraint(
            "window_end >= window_start",
            name="ck_governance_session_digest_window",
        ),
        CheckConstraint(
            in_list_check("funding", DIGEST_FUNDING),
            name="ck_governance_session_digest_funding",
        ),
        CheckConstraint(
            "policy_document_version IS NULL OR policy_document_version >= 1",
            name="ck_governance_session_digest_policy_version",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    runtime_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Zero-based segment of this session",
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Stage B includes the segment when this falls in the review week",
    )
    policy_document_version: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Active policy document version this segment was summarised under",
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    digest: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Spec 4.3 structured digest, at most 4 KiB serialized",
    )
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Policy-aware generic digest for this segment",
    )
    observations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="rule_ref, excerpt (<=280 chars), confidence",
    )
    rolling_summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Context the next segment and Stage B read",
    )
    digest_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    request_watermark: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="Request count covered by this segment",
    )
    model_alias: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    estimated_cost: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default=text("0"),
    )
    funding: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DIGEST_FUNDING_PLATFORM,
        server_default=text("'platform'"),
        comment="platform | account",
    )
    truncated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    def __repr__(self) -> str:
        return (
            f"<GovernanceSessionDigest session={str(self.runtime_session_id)[:8]} "
            f"segment={self.segment_index}>"
        )


class GovernanceReviewRun(Base):
    """One weekly governance review pass for one account."""

    __tablename__ = "governance_review_run"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "window_start",
            name="uq_governance_review_run_account_window",
        ),
        CheckConstraint(
            in_list_check("status", RUN_STATUSES),
            name="ck_governance_review_run_status",
        ),
        CheckConstraint(
            f"mode_before IS NULL OR {_MODE_BEFORE_LIST}",
            name="ck_governance_review_run_mode_before",
        ),
        CheckConstraint(
            f"mode_after IS NULL OR {_MODE_AFTER_LIST}",
            name="ck_governance_review_run_mode_after",
        ),
        CheckConstraint(
            "window_end >= window_start",
            name="ck_governance_review_run_window",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    policy_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("governance_policy_document.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RUN_STATUS_RUNNING,
        server_default=text("'running'"),
        comment="running | completed | partial | skipped | failed",
    )
    skip_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    estimated_cost_preflight: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default=text("0"),
    )
    platform_cost: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default=text("0"),
    )
    account_cost: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.0,
        server_default=text("0"),
    )
    sessions_considered: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    sessions_summarized: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    findings_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    mode_before: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    mode_after: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return (
            f"<GovernanceReviewRun account={str(self.account_id)[:8]} "
            f"status={self.status}>"
        )


class GovernanceFinding(Base):
    """One governance finding from a weekly run."""

    __tablename__ = "governance_finding"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "fingerprint",
            name="uq_governance_finding_account_fingerprint",
        ),
        CheckConstraint(
            in_list_check("kind", FINDING_KINDS),
            name="ck_governance_finding_kind",
        ),
        CheckConstraint(
            in_list_check("severity", FINDING_SEVERITIES),
            name="ck_governance_finding_severity",
        ),
        CheckConstraint(
            f"char_length(title) <= {FINDING_TITLE_MAX_CHARS}",
            name="ck_governance_finding_title_len",
        ),
        CheckConstraint(
            f"char_length(suggestion) <= {FINDING_SUGGESTION_MAX_CHARS}",
            name="ck_governance_finding_suggestion_len",
        ),
        CheckConstraint(
            f"cardinality(session_ids) <= {FINDING_SESSION_IDS_MAX}",
            name="ck_governance_finding_session_ids",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("governance_review_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="violation | inefficiency | anomaly",
    )
    category: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="unapproved_tool, unused_tools, cache_miss, and the rest of the taxonomy",
    )
    severity: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        comment="high | medium | low",
    )
    title: Mapped[str] = mapped_column(String(FINDING_TITLE_MAX_CHARS), nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
        comment="excerpt (<=280), source, optional audit_event_id and api_usage_id",
    )
    session_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        default=list,
        server_default="{}",
        comment="At most 20 runtime session ids",
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    suggestion: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="At most 600 characters",
    )
    estimated_weekly_savings: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    fingerprint: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Unique per account. Include the review window to recur weekly.",
    )
    #: Which policy rule the finding cites. Null for a finding that has no
    #: rule yet. Spec addendum: both must-rules and prefer-rules carry this.
    rule_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dismissed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    dismissed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    dismissed_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<GovernanceFinding {self.kind}/{self.category} "
            f"account={str(self.account_id)[:8]}>"
        )
