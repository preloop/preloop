import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer


# Base Pydantic model for FlowExecution attributes, used for API responses
class FlowExecutionBase(BaseModel):
    id: uuid.UUID
    flow_id: uuid.UUID = Field(..., description="Identifier of the Flow definition")
    trigger_event_id: Optional[str] = Field(
        None,
        description="Identifier of the specific event that triggered this execution",
    )
    trigger_event_details: Optional[Dict[str, Any]] = Field(
        None, description="A snapshot of the payload of the triggering event"
    )
    status: str = Field(
        ...,
        description="Status of the execution (e.g., PENDING, RUNNING, SUCCEEDED, FAILED)",
    )
    start_time: datetime = Field(
        ..., description="Timestamp when the execution started"
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
    openhands_session_reference: Optional[str] = Field(
        None, description="Reference to OpenHands session (e.g., ID, K8s job ID)"
    )
    error_message: Optional[str] = Field(
        None, description="Error message if the execution failed"
    )
    created_at: datetime
    updated_at: datetime

    @field_serializer("id", "flow_id")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        """Serialize UUID to string for JSON response."""
        return str(value)

    model_config = ConfigDict(from_attributes=True)


# Pydantic model for representing a FlowExecution in API list responses (potentially a subset of fields)
class FlowExecutionListResponse(BaseModel):
    id: uuid.UUID
    flow_id: uuid.UUID
    status: str
    start_time: datetime
    end_time: Optional[datetime] = None
    created_at: datetime

    @field_serializer("id", "flow_id")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        """Serialize UUID to string for JSON response."""
        return str(value)

    model_config = ConfigDict(from_attributes=True)


# Pydantic model for representing a detailed FlowExecution in API GET responses
class FlowExecutionDetailResponse(FlowExecutionBase):
    # Potentially include related objects if needed, e.g.:
    # flow: Optional[FlowResponse] = None # Assuming FlowResponse exists
    pass


# Note: Creation and Update schemas for FlowExecution are typically not exposed via public API
# as executions are managed internally by the Flow Trigger and Orchestrator services.
# If direct API manipulation were needed, FlowExecutionCreate and FlowExecutionUpdate schemas
# similar to those in SpaceModels/schemas/flow_execution.py would be defined here.
