"""Delivery Report assessments with provenance and history (#2016).

An assessment is one verdict on landed work: a merged issue completion audit
(``source="audit"``) or the Delivery Report's own pass (``source="report"``).
Rows are append-only. Each is pinned to the revision it assessed and keeps the
criteria as they read at that time, so a later edit to the issue never changes
what an old verdict was about. Readers take the newest row per subject and
revision; nothing points at a "latest" row.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

ASSESSMENT_SOURCES = ("audit", "report")
ASSESSMENT_VERDICTS = ("covered", "partial", "unverified", "no_criteria")


class Assessment(Base):
    """One verdict on an issue or pull request at one revision."""

    __tablename__ = "assessment"
    __table_args__ = (
        Index(
            "ix_assessment_account_issue_assessed",
            "account_id",
            "issue_id",
            "assessed_at",
            postgresql_ops={"assessed_at": "DESC"},
        ),
        Index(
            "ix_assessment_account_pull_request_assessed",
            "account_id",
            "issue_cost_pull_request_id",
            "assessed_at",
            postgresql_ops={"assessed_at": "DESC"},
        ),
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
    )
    issue_cost_pull_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("issue_cost_pull_request.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The execution that wrote it: the audit run or the Delivery Report run.
    flow_execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # The commit SHA the assessment is pinned to.
    revision: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # The acceptance criteria as read at assessment time.
    criteria_snapshot: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    purpose_verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_flags: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    evaluator_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        # clock_timestamp, not now(): rows appended in one transaction differ.
        server_default=func.clock_timestamp(),
    )
    cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
