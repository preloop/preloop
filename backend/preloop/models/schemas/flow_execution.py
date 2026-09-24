import uuid
from datetime import datetime, UTC
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict

from preloop.schemas.gateway_usage import GatewayTokenUsage


class ExecutionModelUsage(BaseModel):
    """One model alias that served requests during an execution."""

    model_alias: str = Field(
        ..., description="Gateway model alias, e.g. 'openai/gpt-5'"
    )
    provider_name: Optional[str] = Field(
        None, description="Provider that served the requests, when recorded"
    )
    request_count: int = Field(
        0, description="Gateway requests this execution sent to that alias"
    )

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


class ExecutionResumeTotals(BaseModel):
    """Combined tokens and cost for a publishing execution and its repairs."""

    total_tokens: int = Field(0, description="Summed tokens across the resume chain")
    estimated_cost: float = Field(
        0.0, description="Summed estimated cost across the resume chain"
    )

    model_config = ConfigDict(from_attributes=True)


class ExecutionModelProjection(BaseModel):
    """Which model(s) ran an execution, derived from gateway usage.

    Mixed into both the list and the detail response so "what ran this" is
    answerable without a second call. Every field is None/empty for an
    execution whose gateway traffic recorded no model alias (a run that never
    called the gateway, or one that predates alias recording).
    """

    model_alias: Optional[str] = Field(
        None,
        description=(
            "Alias of the model that served most of this execution's gateway "
            "requests. Null when the execution has no attributable gateway "
            "usage."
        ),
    )
    provider_name: Optional[str] = Field(
        None, description="Provider behind ``model_alias``, when recorded"
    )
    models_used: List[ExecutionModelUsage] = Field(
        default_factory=list,
        description=(
            "Every distinct model alias this execution used with its request "
            "count, most used first."
        ),
    )
    token_usage: Optional[GatewayTokenUsage] = Field(
        None,
        description=(
            "Tokens this execution's gateway traffic consumed, split by "
            "direction and cache participation. Null when the run has no "
            "attributable gateway usage, which is not the same as zero."
        ),
    )
    resume_of: Optional[uuid.UUID] = Field(
        None,
        description=(
            "Publishing execution this repair resumes. Null when the row is "
            "not a review/CI resumption. Distinct from parent_execution_id "
            "(delegation)."
        ),
    )
    resume_totals: Optional[ExecutionResumeTotals] = Field(
        None,
        description=(
            "Summed total_tokens and estimated_cost for the publishing "
            "execution and every repair that points at it. Null when the "
            "execution is not part of a multi-turn resume chain."
        ),
    )

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


class ExecutionRunner(BaseModel):
    """Where an execution ran: a private CLI runner or Preloop hosted.

    Derived at read time from ``FlowExecution.runner_id`` and
    ``agent_session_reference`` ``runner:...`` forms. An absent runtime
    assignment is unknown until an executor actually starts.
    """

    kind: Literal["private", "hosted", "unknown"] = Field(
        ...,
        description=(
            "private when the run was leased to a self-hosted CLI runner; "
            "hosted when it used the built-in Preloop executor; "
            "unknown when no runtime assignment is recorded"
        ),
    )
    id: Optional[uuid.UUID] = Field(
        None,
        description="FlowRunner id when kind is private and a runner was assigned",
    )
    name: str = Field(
        ...,
        description='Display name. "Preloop hosted" when kind is hosted.',
    )
    pool: Optional[str] = Field(
        None,
        description="Resolved runner pool string, when known",
    )

    model_config = ConfigDict(from_attributes=True)


class ExecutionRunnerSummary(BaseModel):
    """List-row runner: kind and name only."""

    kind: Literal["private", "hosted", "unknown"] = Field(
        ...,
        description="private for a CLI runner; hosted for built-in compute; unknown when unassigned",
    )
    name: str = Field(
        ...,
        description='Display name. "Preloop hosted" when kind is hosted.',
    )

    model_config = ConfigDict(from_attributes=True)


def _unknown_runner() -> ExecutionRunner:
    return ExecutionRunner(kind="unknown", name="Not recorded")


def _unknown_runner_summary() -> ExecutionRunnerSummary:
    return ExecutionRunnerSummary(kind="unknown", name="Not recorded")


class ExecutionPark(BaseModel):
    """Why a ``WAITING_FOR_HUMAN`` execution is waiting, and until when.

    Projected onto the detail response so the console can render "waiting for
    <who> since <when>, expires <when>" without the page having to know how
    approvals are stored. A parked run holds no container and no runner: this
    is a governance object, not a spinner.
    """

    request_id: uuid.UUID = Field(..., description="The pending approval request")
    since: Optional[datetime] = Field(None, description="When the execution was parked")
    expires_at: Optional[datetime] = Field(
        None, description="When the approval window closes"
    )
    waiting_for: Optional[str] = Field(
        None,
        description=(
            "Who the run is waiting on: the approval workflow's name, which "
            "is the routing decision an operator actually made."
        ),
    )
    tool_name: Optional[str] = Field(
        None, description="The gated call that raised the question"
    )
    question: Optional[str] = Field(
        None, description="The question text, for ask_user requests"
    )

    model_config = ConfigDict(from_attributes=True)


# Base Pydantic model for FlowExecution attributes
class FlowExecutionBase(BaseModel):
    flow_id: uuid.UUID = Field(..., description="Foreign Key to Flows.id")
    trigger_event_id: Optional[str] = Field(
        None,
        description="Identifier for the specific event that triggered this execution",
    )
    trigger_event_details: Optional[Dict[str, Any]] = Field(
        None, description="A snapshot of the payload of the triggering event"
    )
    status: str = Field(
        "PENDING",
        description="Status of the execution (e.g., PENDING, RUNNING, SUCCEEDED, FAILED)",
    )
    start_time: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when the execution started",
    )
    end_time: Optional[datetime] = Field(
        None, description="Timestamp when the execution ended"
    )
    resolved_input_prompt: Optional[str] = Field(
        None, description="The full prompt after placeholder resolution"
    )
    model_output_summary: Optional[str] = Field(
        None,
        description="A concise summary of the AI model's final output or key findings",
    )
    actions_taken_summary: Optional[List[Dict[str, Any]]] = Field(
        None,
        description="A structured log of significant actions performed by the agent",
    )
    mcp_usage_logs: Optional[List[Dict[str, Any]]] = Field(
        None, description="Detailed log of each MCP tool call"
    )
    result: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Structured result artifact reported by the agent "
            "(parsed /workspace/result.json, eval/observe runs)"
        ),
    )
    launch_requested_at: Optional[datetime] = None
    parked_at: Optional[datetime] = Field(
        None,
        description=(
            "When this execution was parked waiting for a human decision "
            "(status WAITING_FOR_HUMAN) or for the flows it started (status "
            "WAITING_FOR_CHILDREN). Cleared when the run is resumed."
        ),
    )
    park_expires_at: Optional[datetime] = Field(
        None,
        description=(
            "When the pending approval window closes, or when a run parked "
            "on its children is resumed anyway"
        ),
    )
    park_request_id: Optional[uuid.UUID] = Field(
        None,
        description=(
            "The approval request this execution is parked on, or the wait "
            "grouping the children it is parked on"
        ),
    )
    stop_requested_at: Optional[datetime] = None
    stop_reason: Optional[str] = None
    stop_source: Optional[str] = None
    stop_confirmed_at: Optional[datetime] = None
    agent_session_reference: Optional[str] = Field(
        None,
        description="Reference to agent session (e.g., session ID, K8s job ID, container ID, process ID)",
    )
    error_message: Optional[str] = Field(
        None, description="Error message if the execution failed"
    )
    failure_category: Optional[str] = Field(
        None,
        description=(
            "Coarse machine-readable reason a terminal execution did not "
            "succeed: one of runner_conflict, runner_error, model_transient, "
            "model_auth, provider_billing, model_quota (legacy, superseded "
            "by provider_billing), budget_exceeded, model_config, "
            "no_confirmation, "
            "agent_no_progress, setup_failed, tool_error, agent_error, "
            "timeout, cancelled, "
            "unknown. Null for successful or still-running executions, and "
            "for executions that predate this field."
        ),
    )
    queued_reason: Optional[str] = Field(
        None,
        description=(
            "Why a PENDING execution has not been admitted yet: "
            "account_concurrency_cap when the account already has its "
            "maximum number of executions running. Cleared when the "
            "execution is claimed; null for every other row."
        ),
    )
    retry_of_execution_id: Optional[uuid.UUID] = Field(
        None, description="ID of the original execution if this is a retry"
    )
    batch_id: Optional[uuid.UUID] = Field(
        None,
        description="Shared ID linking executions created by one matrix/batch trigger",
    )
    parent_execution_id: Optional[uuid.UUID] = Field(
        None,
        description=(
            "Execution that directly started this one (the delegation tree "
            "edge). Null for a root run and for executions created before "
            "lineage was recorded."
        ),
    )
    root_execution_id: Optional[uuid.UUID] = Field(
        None,
        description=(
            "First execution of this lineage. Null on the root itself and "
            "on executions created before lineage was recorded. The whole "
            "tree is the root row matched by its own id plus rows whose "
            "root_execution_id points at that root."
        ),
    )
    delegation_depth: int = Field(
        0,
        description=(
            "Distance from the root of the lineage: 0 for a root run, 1 for "
            "a direct child, and so on. Existing rows backfilled by the "
            "migration read back as 0."
        ),
    )
    tool_calls_count: Optional[int] = Field(
        0, description="Total number of tool/MCP calls made during execution"
    )
    total_tokens: Optional[int] = Field(
        0, description="Total tokens used (input + output) during execution"
    )
    estimated_cost: Optional[float] = Field(
        0.0, description="Estimated cost in USD for this execution"
    )

    model_config = ConfigDict(from_attributes=True)


# Pydantic model for creating a FlowExecution (API input - likely internal)
class FlowExecutionCreate(FlowExecutionBase):
    pass  # Most fields will be set by the system during creation


# Pydantic model for updating a FlowExecution (API input - likely internal for status changes)
class FlowExecutionUpdate(BaseModel):
    status: Optional[str] = None
    end_time: Optional[datetime] = None
    resolved_input_prompt: Optional[str] = None
    model_output_summary: Optional[str] = None
    actions_taken_summary: Optional[List[Dict[str, Any]]] = None
    mcp_usage_logs: Optional[List[Dict[str, Any]]] = None
    result: Optional[Dict[str, Any]] = None
    agent_session_reference: Optional[str] = None
    error_message: Optional[str] = None
    failure_category: Optional[str] = None
    tool_calls_count: Optional[int] = None
    total_tokens: Optional[int] = None
    estimated_cost: Optional[float] = None


# Pydantic model for representing a FlowExecution in API responses (includes DB fields)
class FlowExecutionResponse(FlowExecutionBase, ExecutionModelProjection):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    # Include flow name for display purposes
    flow_name: Optional[str] = None
    park: Optional[ExecutionPark] = Field(
        None,
        description=(
            "Present while the execution is parked on a human decision "
            "(status WAITING_FOR_HUMAN)."
        ),
    )
    runner: ExecutionRunner = Field(
        default_factory=_unknown_runner,
        description=(
            "Where this execution ran. Hosted when the built-in executor "
            "ran it; private when a self-hosted CLI runner did; unknown until assigned."
        ),
    )

    # Example of how to include related data if needed
    # flow: Optional[FlowResponse] = None # Assuming a FlowResponse Pydantic schema exists


class FlowExecutionListResponse(ExecutionModelProjection):
    """Lightweight flow execution row for list views."""

    id: uuid.UUID
    flow_id: uuid.UUID
    status: str
    start_time: datetime
    end_time: Optional[datetime] = None
    error_message: Optional[str] = None
    failure_category: Optional[str] = Field(
        None,
        description=(
            "Coarse machine-readable failure class for terminal executions "
            "(runner_conflict, model_transient, agent_error, ...). Null when "
            "the execution succeeded, is still running, or predates the field."
        ),
    )
    queued_reason: Optional[str] = Field(
        None,
        description=(
            "Why a PENDING execution has not been admitted yet "
            "(account_concurrency_cap). Null for every other row."
        ),
    )
    retry_of_execution_id: Optional[uuid.UUID] = None
    parent_execution_id: Optional[uuid.UUID] = Field(
        None, description="Execution that directly started this run; null for a root."
    )
    root_execution_id: Optional[uuid.UUID] = Field(
        None, description="First execution of this lineage; null on the root itself."
    )
    delegation_depth: int = Field(
        0, description="Distance from the lineage root; 0 for roots and existing rows."
    )
    batch_id: Optional[uuid.UUID] = None
    tool_calls_count: Optional[int] = 0
    total_tokens: Optional[int] = 0
    estimated_cost: Optional[float] = 0.0
    created_at: datetime
    updated_at: datetime
    flow_name: Optional[str] = None
    parked_at: Optional[datetime] = Field(
        None,
        description=(
            "When this execution was parked waiting for a human decision "
            "(status WAITING_FOR_HUMAN). Null for every other row."
        ),
    )
    park_expires_at: Optional[datetime] = Field(
        None, description="When the pending approval window closes"
    )
    trigger_subject: Optional[str] = Field(
        None,
        description=(
            "Short human-readable description of what triggered this "
            "execution, e.g. 'preloop/preloop #78 · Pull Request Updated · "
            "5167595c'. Null for executions created before subjects were "
            "recorded, or where no identifying detail could be derived."
        ),
    )
    trigger_subject_url: Optional[str] = Field(
        None,
        description=(
            "Link to the resource that triggered this execution (e.g. the "
            "pull request or merge request), when the trigger payload "
            "carries one."
        ),
    )
    runner: ExecutionRunnerSummary = Field(
        default_factory=_unknown_runner_summary,
        description=(
            "Where this execution ran. List rows carry kind and name; "
            "detail adds id and pool."
        ),
    )

    model_config = ConfigDict(from_attributes=True)


# Schema for FlowExecution as stored in DB (identical to Response for now)
class FlowExecutionInDB(FlowExecutionResponse):
    pass


# --- Matrix / batch fan-out schemas ---


class FlowMatrixEntry(BaseModel):
    """One cell of a matrix trigger. Empty entry means 'flow defaults'."""

    agent_type: Optional[str] = Field(
        None,
        description="Agent harness override (e.g. 'opencode'); None = flow default",
    )
    ai_model_id: Optional[uuid.UUID] = Field(
        None, description="AI model override; None = flow default"
    )

    model_config = ConfigDict(extra="forbid")


class BatchExecutionRef(BaseModel):
    """Per-cell execution reference in a batch trigger response."""

    index: int
    # Both keys are provided on purpose: "id" matches the existing single
    # trigger response, "execution_id" matches the webhook trigger response
    # shape (see fix/webhook-trigger-execution-id).
    id: uuid.UUID
    execution_id: uuid.UUID
    status: str
    agent_type: Optional[str] = None
    ai_model_id: Optional[uuid.UUID] = None


class BatchTriggerResponse(BaseModel):
    """Response for a trigger request that carried a matrix."""

    batch_id: uuid.UUID
    flow_id: uuid.UUID
    executions: List[BatchExecutionRef]


class BatchRollup(BaseModel):
    """Aggregate metrics over the executions of one batch."""

    total: int
    by_status: Dict[str, int]
    completed: int = Field(
        0, description="Executions in a terminal state (succeeded/failed/etc.)"
    )
    total_tokens: int = 0
    total_estimated_cost: float = 0.0
    total_tool_calls: int = 0


class BatchExecutionListItem(FlowExecutionListResponse):
    """Execution row in a batch listing, annotated with its matrix cell."""

    matrix: Optional[Dict[str, Any]] = Field(
        None,
        description="Matrix cell overrides this execution was created with "
        "(index, agent_type, ai_model_id)",
    )


class BatchExecutionsResponse(BaseModel):
    """Executions of one batch plus a status/cost/token rollup."""

    batch_id: uuid.UUID
    flow_id: uuid.UUID
    rollup: BatchRollup
    executions: List[BatchExecutionListItem]


# --- Delegation tree schemas (#634) ---


class ExecutionTreeNode(FlowExecutionListResponse):
    """One execution in a delegation tree, as the console draws the row.

    A list row plus the two things only a tree knows: who started it
    (``parent_execution_id``, already on the list row) and what it was asked
    to do (``label``).
    """

    label: Optional[str] = Field(
        None,
        description=(
            "Label the caller passed to run_flow for this child, e.g. "
            "'lint the diff'. Null on a run that was not delegated and on a "
            "delegation that passed no label."
        ),
    )
    stop_reason: Optional[str] = Field(
        None,
        description=(
            "Why this execution was stopped, when something other than the "
            "run itself ended it: an account kill switch, or the stop of the "
            "execution that started it. Null for every row nobody stopped, "
            "which lets the tree show which children a parent's stop ended "
            "and which had finished on their own."
        ),
    )


class ExecutionTreeResponse(BaseModel):
    """One execution's subtree plus the rollup over it.

    ``execution`` is the run that was asked about and its rollup fields are
    its own; ``rollup`` covers the descendants only. The two are deliberately
    never added together: "this run cost X, the work it delegated cost Y" is
    the question the tree exists to answer, and one number would hide it.
    """

    execution_id: uuid.UUID = Field(..., description="The execution asked about")
    root_execution_id: uuid.UUID = Field(
        ...,
        description=(
            "Root of the lineage this execution belongs to; the execution's "
            "own id when it is the root."
        ),
    )
    execution: ExecutionTreeNode = Field(
        ..., description="The execution asked about, with its own cost and tokens"
    )
    executions: List[ExecutionTreeNode] = Field(
        default_factory=list,
        description=(
            "Every descendant of this execution, parents before children. "
            "Empty for the overwhelming majority of runs, which delegate "
            "nothing."
        ),
    )
    rollup: BatchRollup = Field(
        ...,
        description=(
            "Status counts, tokens, tool calls and estimated cost summed over "
            "the descendants, in the same shape the batch listing uses. "
            "Excludes the execution itself."
        ),
    )
    truncated: bool = Field(
        False,
        description=(
            "True when the lineage holds more executions than one read "
            "returns, so the tree and the rollup are partial."
        ),
    )


# Pydantic model for sending commands to a flow execution
class FlowExecutionCommand(BaseModel):
    command: str = Field(..., description="Command to send (e.g., 'stop', 'pause')")
    payload: Optional[Dict[str, Any]] = Field(
        None, description="Optional payload for the command"
    )
