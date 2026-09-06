"""Pydantic schemas for approval requests."""

import re
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    computed_field,
    model_validator,
)

# `Notification via {channel} to {recipients} ({status})` as stored for the
# authenticated console timeline. Recipients may be emails or usernames.
_NOTIFICATION_DETAIL = re.compile(r"^(Notification via \S+)(?: to (.+))? \(([^)]+)\)$")
_VOTE_DETAIL = re.compile(r"^(Approved|Declined) by (\S+)(.*)$")
_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_MORE_RECIPIENTS = re.compile(r"\+(\d+) more")


def _recipient_count(recipients: str) -> int:
    """Count listed recipients, including a trailing '(+N more)' suffix."""
    extra_match = _MORE_RECIPIENTS.search(recipients)
    extra = int(extra_match.group(1)) if extra_match else 0
    listed = recipients
    if extra_match:
        listed = recipients[: extra_match.start()]
        listed = re.sub(r"\s*\($", "", listed).rstrip(" ,")
    names = [part.strip() for part in listed.split(",") if part.strip()]
    return max(len(names) + extra, 1)


def public_event_detail(event_type: str, detail: str) -> str:
    """Render a timeline detail that is safe to return on a token link.

    The console timeline may include recipient emails and actor ids; the
    public token page must not. Notification rows become a channel +
    recipient count. Vote rows drop the voter id. Any leftover email- or
    UUID-shaped text is stripped.
    """
    text = detail or ""
    if event_type == "notification_sent":
        match = _NOTIFICATION_DETAIL.match(text)
        if match:
            prefix, recipients, status = match.group(1), match.group(2), match.group(3)
            if not recipients:
                return f"{prefix} ({status})"
            count = _recipient_count(recipients)
            noun = "recipient" if count == 1 else "recipients"
            return f"{prefix} to {count} {noun} ({status})"
    if event_type == "vote_received":
        match = _VOTE_DETAIL.match(text)
        if match:
            action, _who, rest = match.group(1), match.group(2), match.group(3)
            text = f"{action} by an approver{rest}"
    text = _UUID_RE.sub("[redacted]", text)
    return _EMAIL_RE.sub("[redacted]", text)


def classify_approval_risk(
    tool_name: Optional[str], tool_args: Optional[Dict[str, Any]] = None
) -> str:
    """Return `low` or `danger`. Unknown tools fail closed as danger."""
    name = (tool_name or "").lower()
    args = tool_args if isinstance(tool_args, dict) else {}
    command = str(args.get("command") or args.get("cmd") or "").lower()
    blob = f"{name} {command}"
    danger_tokens = (
        "push",
        "deploy",
        "rm ",
        "rm\t",
        "delete",
        "drop ",
        "force",
        "prod",
        "production",
        "stripe",
        "charge",
        "transfer",
    )
    if any(token in blob for token in danger_tokens):
        return "danger"
    if name in {"bash", "shell"} and any(
        token in command for token in ("rm", "git push", "kubectl", "terraform")
    ):
        return "danger"
    if name in {"read", "glob", "grep", "ls", "list_dir"}:
        return "low"
    return "danger"


class ApprovalAgentSummary(BaseModel):
    """The managed agent that raised the request."""

    id: UUID
    name: str
    kind: Optional[str] = Field(
        None, description="Agent kind, e.g. 'claude_code' or 'cursor'"
    )

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("id")
    def serialize_id(self, value: UUID) -> str:
        """Serialize UUID to string."""
        return str(value)


class ApprovalApiKeySummary(BaseModel):
    """The API key the requesting caller authenticated with."""

    id: UUID
    name: str

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("id")
    def serialize_id(self, value: UUID) -> str:
        """Serialize UUID to string."""
        return str(value)


class ApprovalSessionSummary(BaseModel):
    """The runtime session the gated call was made in."""

    id: UUID
    subject: Optional[str] = Field(
        None, description="What the session is about (title or reference)"
    )

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("id")
    def serialize_id(self, value: UUID) -> str:
        """Serialize UUID to string."""
        return str(value)


class ApprovalFlowExecutionSummary(BaseModel):
    """The flow run the gated call belonged to."""

    id: str
    flow_id: Optional[UUID] = None
    flow_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("flow_id")
    def serialize_flow_id(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string."""
        return str(value) if value else None


class ApprovalRequestBase(BaseModel):
    """Base schema for approval requests."""

    tool_name: str = Field(..., description="Name of the tool being executed")
    tool_args: Dict[str, Any] = Field(
        default_factory=dict, description="Arguments passed to the tool"
    )
    agent_reasoning: Optional[str] = Field(
        None, description="Agent's reasoning for the tool call"
    )
    summary: Optional[str] = Field(
        None,
        description="User-facing plain-language ask (LLM or ask_user question)",
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
    summary: Optional[str] = None
    approver_comment: Optional[str] = None
    resolved_at: Optional[datetime] = None
    webhook_posted_at: Optional[datetime] = None
    webhook_error: Optional[str] = None
    approval_workflow_id: Optional[UUID] = None
    # AI decision tracking fields
    decided_by_ai: Optional[bool] = None
    ai_model: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_reasoning: Optional[str] = None
    # Bypass tracking (auto-approved without a human under a time-boxed bypass)
    auto_approved_reason: Optional[str] = None
    auto_approval_bypass_id: Optional[UUID] = None


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
    # The credential the requesting caller authenticated with. Always known
    # for API-authenticated callers, which is every creation path except a
    # flow raising an approval on its own orchestrator session.
    api_key_id: Optional[UUID] = None
    # AI decision tracking fields
    decided_by_ai: bool = False
    ai_model: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_reasoning: Optional[str] = None
    # Bypass tracking. Set when a time-boxed ApprovalBypass resolved this
    # request instead of a human. Surfaces MUST render these distinctly and
    # MUST NOT count them as human approvals in approval-rate stats.
    auto_approved_reason: Optional[str] = None
    auto_approval_bypass_id: Optional[UUID] = None
    # Why this request exists: the rule that gated the call, snapshotted at
    # creation time. Optional for backward compatibility: rows created before
    # this field existed, and approvals raised without rule evaluation (the
    # request_approval builtin), carry None and surfaces must omit the block
    # rather than fabricate a reason. Shape is documented in
    # services/approval_rule_context.py.
    rule_context: Optional[Dict[str, Any]] = None
    # Attribution, resolved from the ids above so surfaces can name and link
    # what asked instead of printing a truncated UUID or a generic label.
    # Populated by services/approval_attribution.attach_attribution; a part is
    # omitted when its id is unset or the row it points at is gone.
    agent: Optional[ApprovalAgentSummary] = None
    api_key: Optional[ApprovalApiKeySummary] = None
    session: Optional[ApprovalSessionSummary] = None
    flow_execution: Optional[ApprovalFlowExecutionSummary] = None

    @computed_field
    def risk_level(self) -> str:
        """danger for destructive/spend tools, otherwise low.

        Unknown is treated as danger so notification actions stay fail-closed.
        """
        return classify_approval_risk(self.tool_name, self.tool_args)

    @computed_field
    def was_bypassed(self) -> bool:
        """True when a bypass auto-approved this request without a human."""
        return self.auto_approved_reason is not None

    @computed_field
    def decided_by_human(self) -> bool:
        """True only when a person actually made this decision.

        The single field every statistic should filter on. A request is a human
        decision only if it reached a terminal decided state without an AI
        judging it and without a bypass skipping it.
        """
        if self.status not in ("approved", "declined"):
            return False
        return not self.decided_by_ai and self.auto_approved_reason is None

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


class ApprovalEventResponse(BaseModel):
    """One entry in an approval request's workflow-history timeline."""

    id: UUID = Field(..., description="Event ID")
    event_type: str = Field(
        ...,
        description=(
            "approval_requested, notification_sent, viewed, vote_received, "
            "escalation_triggered, approval_complete, expired, ..."
        ),
    )
    detail: str = Field(..., description="Human-readable description of the event")
    comment: Optional[str] = Field(
        None, description="Approver comment attached to the event"
    )
    actor_id: Optional[UUID] = Field(
        None, description="User who triggered the event (None for system/token)"
    )
    actor_email: Optional[str] = Field(
        None,
        description="Resolved display identity of the actor, when known",
    )
    timestamp: datetime = Field(..., description="When the event occurred")

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("id", "actor_id")
    def serialize_uuid(self, value: Optional[UUID]) -> Optional[str]:
        """Serialize UUID to string."""
        return str(value) if value else None


class ApprovalEventPublic(BaseModel):
    """Timeline entry for the public (token) approval page.

    Deliberately excludes actor ids/emails: a token link is a bearer secret
    and must not leak other approvers' identities. ``detail`` is redacted
    on construction so notification recipient emails and vote actor ids
    cannot leak through.
    """

    event_type: str = Field(..., description="Timeline event type")
    detail: str = Field(..., description="Human-readable description of the event")
    comment: Optional[str] = Field(
        None, description="Approver comment attached to the event"
    )
    timestamp: datetime = Field(..., description="When the event occurred")

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def redact_identities(self) -> "ApprovalEventPublic":
        """Strip recipient emails and actor ids from public timeline text."""
        self.detail = public_event_detail(self.event_type, self.detail)
        return self


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
