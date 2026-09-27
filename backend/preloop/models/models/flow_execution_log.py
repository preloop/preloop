"""Flow execution log entry model — normalized from JSONB to individual rows."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from .base import Base


class FlowExecutionLog(Base):
    """Individual log entry for a flow execution.

    Replaces the JSONB execution_logs array on FlowExecution to avoid O(n)
    write amplification on every append. Each log line is a cheap INSERT.
    """

    __tablename__ = "flow_execution_log"
    __table_args__ = (
        # The execution page reads one run's newest rows of a given type
        # (model calls) without walking the run's agent log lines.
        Index(
            "ix_flow_execution_log_execution_type_ts",
            "execution_id",
            "log_type",
            "timestamp",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    log_type = Column(
        String(50), nullable=True
    )  # 'log', 'tool_call', 'status_update', 'error'
    message = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=True)

    # Relationship
    execution = relationship("FlowExecution", back_populates="log_entries")

    def __repr__(self):
        return f"<FlowExecutionLog(id={self.id}, execution_id={self.execution_id}, type={self.log_type})>"
