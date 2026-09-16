"""One job a private runner currently holds.

A runner used to be structurally single slot: ``flow_runner`` carried one
``current_execution_id`` and one ``pending_job``, so "has this runner got
work?" and "which work?" were the same question. A runner that can run two
jobs needs per job state (the leased payload, the reported status, whether
this one execution was halted), and a row per assignment is the honest shape
for that: the uniqueness of an execution's assignment is a constraint the
database enforces, and freeing a slot is a delete rather than a partial
update of a list.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class FlowRunnerAssignment(Base):
    """A lease of one execution to one runner slot."""

    __tablename__ = "flow_runner_assignment"
    __table_args__ = (
        # An execution runs on at most one runner. Without this a retried
        # lease could hand the same work to two runners at once.
        UniqueConstraint("execution_id", name="uq_flow_runner_assignment_execution"),
    )

    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_runner.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pending_job: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Halt is per execution: stopping one of two jobs on a runner must not
    #: stop the other.
    halt_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: What the runner last said about this job (PENDING, STARTING, RUNNING).
    reported_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    runner = relationship("FlowRunner", back_populates="assignments")

    def __repr__(self) -> str:
        return f"<FlowRunnerAssignment {self.runner_id} {self.execution_id}>"
