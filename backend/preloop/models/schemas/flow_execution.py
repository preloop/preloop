import uuid
from datetime import datetime, UTC
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict


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

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())


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
    agent_session_reference: Optional[str] = Field(
        None,
        description="Reference to agent session (e.g., session ID, K8s job ID, container ID, process ID)",
    )
    error_message: Optional[str] = Field(
        None, description="Error message if the execution failed"
    )
    retry_of_execution_id: Optional[uuid.UUID] = Field(
        None, description="ID of the original execution if this is a retry"
    )
    batch_id: Optional[uuid.UUID] = Field(
        None,
        description="Shared ID linking executions created by one matrix/batch trigger",
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
    retry_of_execution_id: Optional[uuid.UUID] = None
    batch_id: Optional[uuid.UUID] = None
    tool_calls_count: Optional[int] = 0
    total_tokens: Optional[int] = 0
    estimated_cost: Optional[float] = 0.0
    created_at: datetime
    updated_at: datetime
    flow_name: Optional[str] = None
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


# Pydantic model for sending commands to a flow execution
class FlowExecutionCommand(BaseModel):
    command: str = Field(..., description="Command to send (e.g., 'stop', 'pause')")
    payload: Optional[Dict[str, Any]] = Field(
        None, description="Optional payload for the command"
    )
