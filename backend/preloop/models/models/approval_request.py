"""Approval request model for tool execution approvals."""

import secrets
import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import String, DateTime, Text, ForeignKey, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .account import Account
from .tool_configuration import ApprovalWorkflow, ToolConfiguration

from .base import Base

if TYPE_CHECKING:
    from .approval_event import ApprovalEvent


class ApprovalRequestStatus(str):
    """Status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalRequest(Base):
    """Approval request for tool execution.

    An approval request is created when a tool requiring approval is called.
    The MCP tool execution pauses and waits for approval/decline via webhook link.
    """

    __tablename__ = "approval_request"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The account this approval request belongs to",
    )

    tool_configuration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_configuration.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the tool configuration",
    )

    approval_workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_workflow.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the approval workflow",
    )

    execution_id: Mapped[str] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="Flow execution ID (if applicable)",
    )

    # Managed-agent / runtime-session linkage. Populated when an approval is
    # raised for an onboarded agent's tool call (e.g. a Claude Code PreToolUse
    # hook routed through /api/v1/agents/permission-check) so operator surfaces
    # (mobile/watch) can show WHICH agent is asking. Nullable + un-constrained
    # to stay backward compatible with flow/MCP-originated approvals.
    managed_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="Managed agent that requested approval (if applicable)",
    )

    runtime_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="Runtime session the approval was raised in (if applicable)",
    )

    managed_agent_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Denormalized managed-agent display name for operator surfaces",
    )

    tool_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Name of the tool being executed",
    )

    tool_args: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="Arguments passed to the tool",
    )

    agent_reasoning: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Agent's reasoning for the tool call",
    )

    tool_result: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        default=None,
        comment="Cached result of tool execution after approval (for async mode)",
    )

    status: Mapped[str] = mapped_column(
        SQLEnum(
            "pending",
            "approved",
            "declined",
            "expired",
            "cancelled",
            name="approval_request_status",
        ),
        nullable=False,
        default=ApprovalRequestStatus.PENDING,
        index=True,
        comment="Current status of the approval request",
    )

    requested_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When the approval was requested",
    )

    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="When the approval was resolved (approved/declined)",
    )

    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="When the approval request expires",
    )

    approver_comment: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Comment from the approver",
    )

    # Quorum tracking: stores individual votes
    # Format: [{"user_id": "uuid", "decision": "approved"|"declined", "comment": "...", "timestamp": "iso"}]
    responses: Mapped[Optional[list]] = mapped_column(
        JSONB,
        nullable=True,
        default=None,
        comment="Individual approval/decline responses for quorum tracking",
    )

    # Escalation tracking
    escalation_triggered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="When escalation was triggered (if applicable)",
    )

    webhook_posted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="When the webhook notification was posted",
    )

    webhook_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if webhook posting failed",
    )

    approval_token: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        default=lambda: secrets.token_urlsafe(32),
        comment="Secure token for public approval links",
    )

    # AI decision tracking fields
    decided_by_ai: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
        comment="Whether this request was decided by AI",
    )
    ai_model: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="AI model that made the decision (if decided_by_ai=True)",
    )
    ai_confidence: Mapped[Optional[float]] = mapped_column(
        nullable=True,
        comment="AI confidence score for the decision (0.0-1.0)",
    )
    ai_reasoning: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="AI's reasoning for the approval/denial decision",
    )

    # Relationships
    account: Mapped["Account"] = relationship(
        "Account", back_populates="approval_requests"
    )
    tool_configuration: Mapped["ToolConfiguration"] = relationship(
        "ToolConfiguration", back_populates="approval_requests"
    )
    approval_workflow: Mapped["ApprovalWorkflow"] = relationship(
        "ApprovalWorkflow", back_populates="approval_requests"
    )
    events: Mapped[list["ApprovalEvent"]] = relationship(
        "ApprovalEvent",
        back_populates="approval_request",
        cascade="all, delete-orphan",
        order_by="ApprovalEvent.timestamp",
    )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<ApprovalRequest(id={self.id}, tool_name={self.tool_name}, "
            f"status={self.status}, requested_at={self.requested_at})>"
        )
