"""Thin work item join between tracker issue, run, pull request and cost (#2016).

A work item records that some unit of work is expected to land and where it
stands. It deliberately holds no head SHA, cost or merge time: those are read
through its links (``flow_thread`` for the live head, ``issue_cost_pull_request``
for cost and merge times) so there is one source for each fact.

* ``WorkItem``: one row per piece of expected work, with its state machine.
* ``WorkItemPR``: one row per pull/merge request observed for a work item,
  holding what the forge last reported and the conditional-request etag.
* ``Assessment``: one Delivery Report verdict on landed work, optionally tied
  to a work item.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false as sa_false,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

WORK_ITEM_STATES = ("open", "pr_open", "ready", "merged", "no_change", "closed")
ASSESSMENT_VERDICTS = ("covered", "partial", "unverified", "no_criteria")


class WorkItem(Base):
    """Expected work and its delivery state; facts live behind the links."""

    __tablename__ = "work_item"
    __table_args__ = (
        Index("ix_work_item_account_state", "account_id", "state"),
        Index("ix_work_item_flow_thread", "flow_thread_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    issue_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    flow_execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    flow_thread_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_thread.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Where the work came from: a Preloop flow, a vendor agent (for example a
    # Copilot-authored PR) or the tracker alone.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # What the work is expected to produce, e.g. "pull_request" or "none".
    expected_artifact: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    responsible_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    closed_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Who closed it: a user id, an agent identity or "system".
    closed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latest_assessment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "assessment.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_work_item_latest_assessment",
        ),
        nullable=True,
    )


class WorkItemPR(Base):
    """What the forge last reported for one pull/merge request of a work item."""

    __tablename__ = "work_item_pr"
    __table_args__ = (
        UniqueConstraint(
            "work_item_id", "repo", "pr_number", name="uq_work_item_pr_number"
        ),
        Index("ix_work_item_pr_repo_state", "repo", "state"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_item.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    issue_cost_pull_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue_cost_pull_request.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Repository path as the forge names it, lower-cased: "org/repo".
    repo: Mapped[str] = mapped_column(String(512), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Forge state: open, closed or merged.
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    merged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merger: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    etag: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Assessment(Base):
    """One Delivery Report verdict on landed work, with its evidence."""

    __tablename__ = "assessment"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    work_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_item.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The Delivery Report execution that wrote this assessment.
    report_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    purpose_verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_flags: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
