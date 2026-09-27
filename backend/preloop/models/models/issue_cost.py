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
  ambiguous and attributed to none.
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
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    false as sa_false,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


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
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IssueCostExecution(Base):
    """One execution's contribution; ``rollup_id`` NULL means unassigned."""

    __tablename__ = "issue_cost_execution"

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
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
