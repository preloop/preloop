"""Self-hosted flow runner registered by the Preloop CLI."""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

#: Slots a runner gets when nobody says otherwise. Two, not one: a single
#: slot makes one long review block every other flow the account routes to
#: this machine, and two is what an ordinary laptop or small VM can host
#: without the jobs starving each other.
DEFAULT_RUNNER_CONCURRENCY = 2

#: Ceiling on what an owner or a runner may ask for. A runner is someone's
#: workstation or small VM; an unbounded value is a mistake, not a plan.
MAX_RUNNER_CONCURRENCY = 32


class FlowRunner(Base):
    """A CLI process that leases and runs flow executions.

    User/account level: who registered it, visible to the account.
    Optional instance_id when a self-hosted control plane reports through
    instance_tracker.
    """

    __tablename__ = "flow_runner"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    registered_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    instance_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    hostname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    os: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    arch: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    labels: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="offline", index=True
    )
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    #: How many executions this runner may hold at once. The server side
    #: value is the ceiling: the owner edits it in the console, and a runner
    #: process may report less, never more.
    concurrency: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_RUNNER_CONCURRENCY,
        server_default=str(DEFAULT_RUNNER_CONCURRENCY),
    )
    #: What the connected runner process last said it can run at once. None
    #: until a runner that speaks concurrency connects.
    reported_concurrency: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_capabilities: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    capabilities: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    account = relationship("Account")
    registered_by = relationship("User")
    assignments = relationship(
        "FlowRunnerAssignment",
        back_populates="runner",
        cascade="all, delete-orphan",
        order_by="FlowRunnerAssignment.assigned_at",
        lazy="selectin",
    )

    @property
    def capacity(self) -> int:
        """Slots this runner may fill right now.

        The stored value is the owner's ceiling; a runner process started
        with a lower ``--concurrency`` lowers it for as long as it is
        connected. It can never raise it: capacity on someone else's machine
        is not the runner's decision.
        """
        ceiling = max(1, int(self.concurrency or DEFAULT_RUNNER_CONCURRENCY))
        reported = self.reported_concurrency
        if reported is None:
            return ceiling
        return max(1, min(ceiling, int(reported)))

    @property
    def running_count(self) -> int:
        """How many executions this runner holds."""
        return len(self.assignments or [])

    @property
    def free_slots(self) -> int:
        """Slots a dispatcher may still fill. Zero means busy."""
        return max(0, self.capacity - self.running_count)

    @property
    def running_execution_ids(self) -> List[uuid.UUID]:
        """Every execution this runner holds, oldest assignment first."""
        rows = list(self.assignments or [])
        return [row.execution_id for row in rows]

    @property
    def current_assignment(self) -> Any:
        """The oldest live assignment, or None."""
        rows = list(self.assignments or [])
        dated = [row for row in rows if row.assigned_at is not None]
        if dated:
            return min(dated, key=lambda row: row.assigned_at)
        return rows[0] if rows else None

    @property
    def current_execution_id(self) -> Optional[uuid.UUID]:
        """Compatibility shim: the oldest assignment's execution.

        ``flow_runner.current_execution_id`` used to be a column, and one
        answer is still what a single slot runner, the console link and the
        CLI status output want. Anything that needs every job reads
        ``assignments``.
        """
        assignment = self.current_assignment
        return assignment.execution_id if assignment is not None else None

    @property
    def pending_job(self) -> Optional[Dict[str, Any]]:
        """Compatibility shim: the oldest assignment's leased payload."""
        assignment = self.current_assignment
        return assignment.pending_job if assignment is not None else None

    def assignment_for(self, execution_id: Any) -> Any:
        """The assignment for one execution on this runner, or None."""
        if execution_id is None:
            return None
        wanted = str(execution_id)
        for row in self.assignments or []:
            if str(row.execution_id) == wanted:
                return row
        return None

    def __repr__(self) -> str:
        return f"<FlowRunner {self.id} {self.name} {self.status}>"
