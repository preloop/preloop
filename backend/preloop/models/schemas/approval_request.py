"""Pydantic schemas for approval requests."""

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, computed_field


class ApprovalRequestBase(BaseModel):
    """Base schema for approval requests."""

    tool_name: str = Field(..., description="Name of the tool being executed")
    tool_args: Dict[str, Any] = Field(
        default_factory=dict, description="Arguments passed to the tool"
    )
    agent_reasoning: Optional[str] = Field(
        None, description="Agent's reasoning for the tool call"
    )
    execution_id: Optional[str] = Field(
        None, description="Flow execution ID (if applicable)"
    )


class ApprovalRequestCreate(ApprovalRequestBase):
    """Schema for creating a new approval request."""

    account_id: str
    tool_configuration_id: UUID
    approval_workflow_id: UUID
    expires_at: Optional[datetime] = None


class ApprovalRequestUpdate(BaseModel):
    """Schema for updating an approval request."""

    status: Optional[str] = None
    approver_comment: Optional[str] = None
    resolved_at: Optional[datetime] = None
    webhook_posted_at: Optional[datetime] = None
    webhook_error: Optional[str] = None
    # AI decision tracking fields
    decided_by_ai: Optional[bool] = None
    ai_model: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_reasoning: Optional[str] = None


class ApprovalRequestResponse(ApprovalRequestBase):
    """Schema for approval request response."""

    id: UUID
    account_id: UUID  # Changed from str to UUID for validation, serializer converts to str for JSON
    tool_configuration_id: UUID
    approval_workflow_id: UUID
    status: str
    requested_at: datetime
    resolved_at: Optional[datetime]
    expires_at: Optional[datetime]
    approver_comment: Optional[str]
    webhook_posted_at: Optional[datetime]
    webhook_error: Optional[str]
    # Managed-agent linkage (populated for onboarded-agent tool approvals).
    # Operator surfaces (mobile/watch) show managed_agent_name to identify
    # WHICH agent is asking.
    managed_agent_id: Optional[UUID] = None
    runtime_session_id: Optional[UUID] = None
    managed_agent_name: Optional[str] = None
    # AI decision tracking fields
    decided_by_ai: bool = False
    ai_model: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_reasoning: Optional[str] = None

    # Computed fields for backward compatibility
    @computed_field
    def approval_policy_id(self) -> str:
        """Alias for backward compatibility with older mobile app versions."""
        return str(self.approval_workflow_id)

    # --- Question ("ask the user") surface -------------------------------
    # ask_user stores its prompt in tool_args (no schema migration needed).
    # These computed fields expose it so console/mobile can render a question
    # with option buttons and/or a free-text answer field instead of a plain
    # approve/decline gate.
    @computed_field
    def is_question(self) -> bool:
        """True when this request is an ask_user question, not a gate."""
        return bool(
            isinstance(self.tool_args, dict) and self.tool_args.get("is_question")
        )

    @computed_field
    def question(self) -> Optional[str]:
        """The question text the agent is asking (ask_user only)."""
        if isinstance(self.tool_args, dict):
            value = self.tool_args.get("question")
            if isinstance(value, str):
                return value
        return None

    @computed_field
    def question_options(self) -> list[str]:
        """Ordered answer options the agent offered (may be empty)."""
        if isinstance(self.tool_args, dict):
            options = self.tool_args.get("options")
            if isinstance(options, list):
                return [str(o) for o in options]
        return []

    @computed_field
    def allow_free_text(self) -> bool:
        """Whether the user may type a free-text answer (default True)."""
        if isinstance(self.tool_args, dict) and "allow_free_text" in self.tool_args:
            return bool(self.tool_args.get("allow_free_text"))
        return True

    model_config = ConfigDict(from_attributes=True)

    @field_serializer(
        "id", "account_id", "tool_configuration_id", "approval_workflow_id"
    )
    def serialize_uuid(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string."""
        return str(value) if value else None


class ApprovalDecision(BaseModel):
    """Schema for an approval decision or a question answer."""

    approved: bool = Field(
        ..., description="Whether the request is approved or declined"
    )
    comment: Optional[str] = Field(None, description="Comment from the approver")
    selected_option: Optional[str] = Field(
        None, description="For ask_user questions: the option the user chose"
    )
    answer_text: Optional[str] = Field(
        None, description="For ask_user questions: a free-text answer the user typed"
    )

    @property
    def effective_comment(self) -> Optional[str]:
        """The text to persist/return: answer for questions, else the comment.

        A typed free-text answer wins over a picked option, which wins over a
        plain comment — so a single field carries the human's response back to
        the agent regardless of how they replied.
        """
        return self.answer_text or self.selected_option or self.comment
