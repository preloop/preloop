"""Per-tracker-issue cost and cycle-time rollup (#958).

Executions already carry cost, tokens and timing. What nothing records is
which tracker issue an execution worked on, so a period report would have to
parse every stored webhook body to answer "what did this ticket cost". These
three tables hold that answer once, written when an execution finishes or a
pull-request webhook arrives:

* ``IssueCostRollup``: one row per tracker issue per account, with the sums
  and the four cycle-time timestamps.
* ``IssueCostExecution``: one fact per execution that was attributed (or
  deliberately left unassigned). Unique on the execution id, so a replayed
  finish or webhook overwrites the fact instead of adding a second one.
* ``IssueCostPullRequest``: one row per pull/merge request, holding the
  publication, approval and merge times and the issue the pull request was
  attributed to. A pull request that points at more than one issue is marked
  ambiguous and attributed to none. It is also the one authoritative record of
  a pull request for the Delivery Report, whoever opened it: ``origin``,
  ``author_id``, ``head_sha`` and ``state`` identify it, ``merged_at`` stays
  its only merge fact.
* ``IssueCostPullRequestIssue``: every issue a pull request claims (#2016).
  Cost attribution still follows ``rollup_id``; this link keeps all issues of
  a pull request that addresses several, which ``rollup_id`` cannot.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    false as sa_false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

PULL_REQUEST_ORIGINS = ("preloop_flow", "copilot", "human", "unknown")
PULL_REQUEST_STATES = ("open", "merged", "closed")


class IssueCostRollup(Base):
    """Cost, tokens, runs and cycle-time timestamps for one tracker issue."""

    __tablename__ = "issue_cost_rollup"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "tracker_id", "issue_key", name="uq_issue_cost_rollup_issue"
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tracker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracker.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Canonical tracker key: "org/repo#12" (GitHub), "group/project#12"
    # (GitLab) or "PROJ-12" (Jira). Repository paths are lower-cased.
    issue_key: Mapped[str] = mapped_column(String(512), nullable=False)
    issue_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue.id", ondelete="SET NULL"),
        nullable=True,
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    issue_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    pr_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    total_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=0, server_default="0"
    )
    run_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_run_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    first_event_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    pr_opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ``IssueCostPullRequest.opened_at_source`` of the pull request that set
    # ``pr_opened_at``.
    pr_opened_at_source: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The human estimate as the tracker states it, for "AI cost vs estimate".
    # Read from the tracker only (a native field, a configured custom field or
    # a configured label); never derived. NULL when the tracker has none.
    estimate_hours: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    estimate_hours_source: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    estimate_points: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    estimate_points_source: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )


class IssueCostExecution(Base):
    """One execution's contribution; ``rollup_id`` NULL means unassigned."""

    __tablename__ = "issue_cost_execution"
    __table_args__ = (
        # Fleet Map merges join executions to a pull request by key.
        # Migration 20261031_code_tool_idx.
        Index(
            "ix_issue_cost_execution_account_pr_key",
            "account_id",
            "pr_key",
            postgresql_where=text("pr_key IS NOT NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rollup_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue_cost_rollup.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Normalized pull/merge request URL this execution worked on, if any.
    pr_key: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    # How the execution was attributed: lifecycle, trigger_issue, resume,
    # delegated, pull_request, closing_reference; or unassigned / ambiguous.
    link: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # The execution's failure_category, copied so exports can group by it.
    failure_category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    total_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    estimated_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IssueCostPullRequest(Base):
    """Publication, approval and merge times of one pull/merge request."""

    __tablename__ = "issue_cost_pull_request"
    __table_args__ = (
        UniqueConstraint("account_id", "pr_key", name="uq_issue_cost_pull_request"),
        Index("ix_issue_cost_pull_request_refresh_due", "refresh_due_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pr_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    rollup_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue_cost_rollup.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Set once two different issues claimed this pull request. An ambiguous
    # pull request is never attributed again; its executions stay unassigned.
    ambiguous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Where ``opened_at`` came from: ``forge`` (the pull request's own
    # ``created_at``), ``bind`` (when Preloop bound it to an execution) or
    # ``run_end`` (the publishing run's end, the last resort).
    opened_at_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Bounded forge evidence cache for weekly outcome attribution. NULL means
    # not observed, never zero changes. Refresh open requests once per week.
    value_evidence: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    value_observed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Who produced the pull request: one of ``PULL_REQUEST_ORIGINS``.
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    # Immutable forge user id of the author, never the display name.
    author_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    head_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Forge state: one of ``PULL_REQUEST_STATES``; NULL until observed.
    state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # Forge refresh bookkeeping. ``refresh_due_at`` NULL means "due" while the
    # state is open or unknown and "not scheduled" once merged or closed; a
    # webhook for the pull request sets it to now. ``refresh_etag`` is the
    # validator sent as ``If-None-Match`` on the next read.
    refresh_due_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refreshed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_etag: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class IssueCostPullRequestIssue(Base):
    """One issue a pull request claims; a pull request may claim several."""

    __tablename__ = "issue_cost_pull_request_issue"
    __table_args__ = (
        UniqueConstraint(
            "pull_request_id", "issue_id", name="uq_issue_cost_pull_request_issue"
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pull_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue_cost_pull_request.id", ondelete="CASCADE"),
        nullable=False,
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class ForgeWebhookDelivery(Base):
    """A forge delivery id already queued, so a redelivery is not queued again."""

    __tablename__ = "forge_webhook_delivery"
    __table_args__ = (
        UniqueConstraint("tracker_id", "delivery_id", name="uq_forge_webhook_delivery"),
    )

    tracker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracker.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``X-GitHub-Delivery``, ``X-Gitlab-Event-UUID`` or ``X-Request-UUID``.
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
