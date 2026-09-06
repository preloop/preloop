import uuid
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, query_expression, relationship

from .base import Base

# Reserved key under which the compact, human-readable execution subject is
# stored inside FlowExecution.trigger_event_details. Defined here (rather than
# alongside the extraction logic in preloop.sync.event_normalizer) so the CRUD
# layer can project it without models depending on sync.
TRIGGER_SUBJECT_KEY = "_subject"

# Reserved key under which per-cell matrix overrides are stored inside
# FlowExecution.trigger_event_details when an execution was created as part of
# a matrix/batch trigger. Shape:
# {"batch_id": str, "index": int, "agent_type": str?, "ai_model_id": str?}
# Keeping the overrides on the execution makes each cell self-describing (the
# orchestrator applies them without any flow mutation) and lets retries of a
# single cell keep their overrides for free.
MATRIX_OVERRIDES_KEY = "_matrix"

# Reserved key under which the controller records the model/harness chosen
# for one execution (matched routing rule or the flow default). Written only
# by the controller after validating account-owned models. Never accepted
# from webhook bodies, tracker payloads, or an unvalidated trigger body.
# Shape: {"schema_version": 1, "ai_model_id", "agent_type", "source",
# "rule_id"?, "label_snapshot", "reason"}.
ROUTING_RECORD_KEY = "_model_routing"


def resolve_matrix_agent_selection(
    trigger_event_details: Optional[dict],
    *,
    flow_agent_type: Optional[str] = None,
    flow_ai_model_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Effective ``(agent_type, ai_model_id)`` for one execution.

    Matrix cells persist their overrides under ``MATRIX_OVERRIDES_KEY`` in
    ``trigger_event_details``. Every code path that (re)builds an agent
    executor for an execution MUST resolve the agent type through this helper
    (initial run, resume, monitor, recovery) — otherwise an interrupted matrix
    cell would be inspected with the flow-default harness, whose session
    references are not compatible with the cell's actual harness.
    """
    overrides = (trigger_event_details or {}).get(MATRIX_OVERRIDES_KEY) or {}
    return (
        overrides.get("agent_type") or flow_agent_type,
        overrides.get("ai_model_id") or flow_ai_model_id,
    )


def resolve_execution_agent_selection(
    trigger_event_details: Optional[dict],
    *,
    flow_agent_type: Optional[str] = None,
    flow_ai_model_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Effective ``(agent_type, ai_model_id)`` including flow routing records.

    Authorized matrix cells still win (eval fan-out is not production
    routing). Otherwise a controller-written ``ROUTING_RECORD_KEY`` pins the
    model and harness for retries and native continuation. Flow defaults are
    used when neither reserved key is present.
    """
    details = trigger_event_details or {}
    overrides = details.get(MATRIX_OVERRIDES_KEY) or {}
    if (
        isinstance(overrides, dict)
        and "agent_type" in overrides
        and "ai_model_id" in overrides
    ):
        return overrides["agent_type"], overrides["ai_model_id"]
    if isinstance(overrides, dict) and (
        overrides.get("agent_type") or overrides.get("ai_model_id")
    ):
        return resolve_matrix_agent_selection(
            details,
            flow_agent_type=flow_agent_type,
            flow_ai_model_id=flow_ai_model_id,
        )
    routing = details.get(ROUTING_RECORD_KEY) or {}
    if isinstance(routing, dict) and (
        routing.get("agent_type") or routing.get("ai_model_id")
    ):
        return (
            routing.get("agent_type"),
            routing.get("ai_model_id"),
        )
    return (flow_agent_type, flow_ai_model_id)


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
    # Evidence pack (tar.gz of /workspace/evidence) captured by the runner for
    # audit-style flows. Size-capped at capture time
    # (MAX_EVIDENCE_ARCHIVE_BYTES); served by
    # GET /flows/executions/{id}/evidence. Deliberately NOT exposed on the
    # execution response schemas.
    evidence_archive = Column(LargeBinary, nullable=True)
    # Workspace snapshot (tar.gz of /workspace, .git included) captured by the
    # runner on every terminal path so work that was never pushed survives the
    # container. Size-capped at capture time
    # (settings.workspace_snapshot_max_bytes); served by
    # GET /flows/executions/{id}/workspace, restored on a correlated resume,
    # and reaped by the workspace janitor after
    # settings.workspace_snapshot_ttl_hours. Deliberately NOT exposed on the
    # execution response schemas.
    workspace_snapshot = Column(LargeBinary, nullable=True)
    execution_logs = Column(
        JSONB, nullable=True
    )  # Full execution logs (array of log messages)
    agent_session_reference = Column(
        String, nullable=True
    )  # e.g., agent session ID, K8s job ID, Docker container ID, process ID
    # Native CLI agent session (OpenCode/Codex) captured from the container
    # log stream via the PRELOOP_AGENT_SESSION marker:
    # {"agent_type": "opencode", "session_id": "ses_..."}. A correlated
    # PR-comment resume hands it back to the agent script so it can restore
    # the packed session storage and invoke the CLI resume flag. Deliberately
    # NOT exposed on the execution response schemas.
    cli_session = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    # Coarse machine-readable reason a terminal execution did not succeed, from
    # the closed vocabulary in preloop.services.flow_failure_category (e.g.
    # "runner_conflict", "model_transient", "agent_error"). error_message stays
    # the human-readable detail; this column is what you group by when asking
    # "what is actually breaking?" — indexed for exactly that query. NULL for
    # successful runs, for still-running rows, and for rows that predate the
    # column.
    failure_category = Column(String(32), nullable=True, index=True)

    # Retry tracking
    retry_of_execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id"),
        nullable=True,
        index=True,
    )  # Links to the original execution this is a retry of

    # Batch/matrix fan-out: executions created from one matrix trigger share a
    # batch_id so the whole batch can be listed and rolled up as a unit.
    batch_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    runner_id = Column(UUID(as_uuid=True), nullable=True, index=True)

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
