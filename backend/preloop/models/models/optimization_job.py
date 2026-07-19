"""Async session-optimization analysis jobs.

An :class:`OptimizationJob` tracks one background run of the (blocking,
LLM-backed) session-optimization analysis for a runtime session, so the
console can submit the work, close the request, and poll for the result
instead of holding an HTTP connection open for the 1-2 minute model pass.

Lifecycle: ``pending`` (queued) -> ``running`` (a worker thread claimed it,
``heartbeat_at`` ticks while the model call is in flight) -> ``succeeded``
(``result`` holds the serialized optimization response) or ``failed``
(``error`` holds a user-facing message; detail goes to logs only). A partial
unique index guarantees at most one active (pending/running) job per
account/session pair, which is what makes double-click submission idempotent
instead of double-spending on the model.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OptimizationJobStatus(str):
    """Status values for an optimization job."""

    #: Created and queued; no worker thread has picked it up yet.
    PENDING = "pending"
    #: A worker thread is executing the analysis.
    RUNNING = "running"
    #: Analysis finished; ``result`` holds the optimization response.
    SUCCEEDED = "succeeded"
    #: Analysis did not complete; ``error`` holds the user-facing message.
    FAILED = "failed"


#: Statuses that count as "in flight" for idempotent submission and recovery.
ACTIVE_JOB_STATUSES = (
    OptimizationJobStatus.PENDING,
    OptimizationJobStatus.RUNNING,
)

#: Terminal statuses eligible for retention pruning.
FINISHED_JOB_STATUSES = (
    OptimizationJobStatus.SUCCEEDED,
    OptimizationJobStatus.FAILED,
)


class OptimizationJob(Base):
    """One background session-optimization analysis run."""

    __tablename__ = "optimization_job"
    __table_args__ = (
        # At most one active job per (account, session): double-click and
        # concurrent submits converge on the same row instead of spending the
        # model budget twice.
        Index(
            "uq_optimization_job_active",
            "account_id",
            "runtime_session_id",
            unique=True,
            postgresql_where="status IN ('pending', 'running')",
        ),
        Index("ix_optimization_job_status_created_at", "status", "created_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        SQLEnum(
            "pending",
            "running",
            "succeeded",
            "failed",
            name="optimization_job_status",
        ),
        nullable=False,
        default=OptimizationJobStatus.PENDING,
    )
    #: Serialized RuntimeSessionOptimizationResponse when ``status`` is
    #: ``succeeded``; NULL otherwise.
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    #: User-facing failure message. Diagnostic detail is deliberately kept in
    #: server logs only — never surfaced to the console.
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    #: Last liveness signal from the worker thread; the recovery sweep fails
    #: running jobs whose heartbeat has gone stale (worker died mid-call).
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<OptimizationJob(id={self.id}, session={self.runtime_session_id}, "
            f"status={self.status})>"
        )
