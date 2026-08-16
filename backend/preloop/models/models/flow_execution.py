import uuid
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, query_expression, relationship

from .base import Base

# Reserved key under which the compact, human-readable execution subject is
# stored inside FlowExecution.trigger_event_details. Defined here (rather than
# alongside the extraction logic in preloop.sync.event_normalizer) so the CRUD
# layer can project it without models depending on sync.
TRIGGER_SUBJECT_KEY = "_subject"


class FlowExecution(Base):
    __tablename__ = "flow_execution"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flow_id = Column(
        UUID(as_uuid=True), ForeignKey("flow.id"), nullable=False, index=True
    )
    trigger_event_id = Column(
        String, nullable=True, index=True
    )  # From StandardizedNatsEvent.event_id
    trigger_event_details = Column(
        JSONB, nullable=True
    )  # Snapshot of StandardizedNatsEvent.data or full event
    status = Column(
        String, nullable=False, default="PENDING", index=True
    )  # PENDING, INITIALIZING, RUNNING, etc.
    start_time = Column(
        DateTime, default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    end_time = Column(DateTime, nullable=True)
    resolved_input_prompt = Column(Text, nullable=True)
    model_output_summary = Column(Text, nullable=True)
    actions_taken_summary = Column(
        JSONB, nullable=True
    )  # Structured log of agent actions
    mcp_usage_logs = Column(JSONB, nullable=True)  # Detailed log of MCP tool calls
    # Structured result artifact reported by the agent (eval/observe runs):
    # the parsed contents of /workspace/result.json captured by the runner
    # after the agent finishes. First-class alternative to scraping logs.
    result = Column(JSONB, nullable=True)
    execution_logs = Column(
        JSONB, nullable=True
    )  # Full execution logs (array of log messages)
    agent_session_reference = Column(
        String, nullable=True
    )  # e.g., agent session ID, K8s job ID, Docker container ID, process ID
    error_message = Column(Text, nullable=True)

    # Retry tracking
    retry_of_execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id"),
        nullable=True,
        index=True,
    )  # Links to the original execution this is a retry of

    # Execution metrics
    tool_calls_count = Column(
        Integer, nullable=True, default=0
    )  # Total number of tool/MCP calls made
    total_tokens = Column(
        Integer, nullable=True, default=0
    )  # Total tokens used (input + output)
    estimated_cost = Column(
        Numeric(10, 4), nullable=True, default=0.0
    )  # Estimated cost in USD

    # Worker claim lease for multi-replica-safe orchestration
    orchestrator_worker_id = Column(String(255), nullable=True, index=True)
    orchestrator_claimed_at = Column(DateTime(timezone=True), nullable=True)
    orchestrator_heartbeat_at = Column(
        DateTime(timezone=True), nullable=True, index=True
    )

    created_at = Column(DateTime, default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Human-readable subject for list views (e.g.
    # "preloop/preloop #78 · Pull Request Updated · 5167595c").
    #
    # Not a real column: the value is stored denormalized inside
    # trigger_event_details under the "_subject" key when the execution is
    # created, and projected out by the query rather than shipping the whole
    # trigger payload to list callers. Populated via with_expression() in
    # CRUDFlowExecution.get_multi; None on rows created before subjects
    # existed, and on any query that does not request it.
    trigger_subject: Mapped[Optional[str]] = query_expression()
    trigger_subject_url: Mapped[Optional[str]] = query_expression()

    # Relationships
    flow = relationship(
        "Flow", back_populates="executions"
    )  # Assuming Flow model has 'executions'
    log_entries = relationship(
        "FlowExecutionLog", back_populates="execution", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<FlowExecution(id={self.id}, flow_id={self.flow_id}, status='{self.status}')>"
