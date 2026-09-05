"""Tool configuration model for managing tool settings and approval workflows."""

import uuid
from typing import TYPE_CHECKING, Dict, List, Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Integer, Float
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Boolean, JSON

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .managed_agent import ManagedAgent
    from .mcp_server import MCPServer
    from .approval_request import ApprovalRequest
    from .tool_access_rule import ToolAccessRule


class ToolConfiguration(Base):
    """Tool configuration model for managing per-account tool settings.

    This model controls which tools are available to each account and
    their approval workflows. It supports:
    - Built-in/default tools (always available)
    - External MCP server tools
    - Future HTTP tools

    Attributes:
        id: Unique identifier for the configuration.
        account_id: The account this configuration belongs to.
        tool_name: Name of the tool.
        tool_source: Source type ('builtin', 'mcp', 'http', 'agent').
        mcp_server_id: Reference to MCP server (if tool_source='mcp').
        managed_agent_id: Optional agent scope. Null means the row applies
            account-wide (the historical behavior). When set, the row applies
            only to that managed agent: it can enable a default-disabled
            builtin for one agent without exposing it to the rest of the
            account, and it is invisible to every other caller.
        is_enabled: Whether the tool is enabled for this account.
        requires_approval: Whether the tool requires pre-execution approval ("preloop").
        approval_workflow_id: Reference to approval workflow (if requires_approval=True).
        tool_description: Description of what the tool does.
        tool_schema: JSON schema for tool parameters.
        custom_config: Additional configuration options.
    """

    __tablename__ = "tool_configuration"

    # Tool identification
    tool_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True, comment="Name of the tool"
    )
    tool_source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="builtin",
        index=True,
        comment="Source type: 'builtin', 'mcp', 'http'",
    )

    # For external tools, reference to the source
    mcp_server_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_server.id", ondelete="CASCADE"),
        nullable=True,
        comment="Reference to MCP server (if tool_source='mcp')",
    )
    http_endpoint_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="Reference to HTTP endpoint (future: if tool_source='http')",
    )

    # Optional agent scope: null = account-wide row (historical behavior).
    managed_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("managed_agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment=(
            "Optional managed-agent scope; null = account-wide, set = the "
            "configuration applies only to that agent"
        ),
    )

    # Configuration
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="Whether the tool is enabled"
    )

    # Reference to reusable approval workflow
    approval_workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_workflow.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Reference to approval workflow (if set, tool requires approval)",
    )

    # Metadata
    tool_description: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, comment="Description of what the tool does"
    )
    tool_schema: Mapped[Optional[Dict]] = mapped_column(
        JSON, nullable=True, comment="JSON schema for tool parameters"
    )
    custom_config: Mapped[Optional[Dict]] = mapped_column(
        JSON, nullable=True, comment="Additional configuration options"
    )
    justification_mode: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        default=None,
        comment="Justification parameter mode: null/disabled, 'optional', or 'required'",
    )

    # Foreign keys
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    account: Mapped["Account"] = relationship(
        "Account", back_populates="tool_configurations"
    )
    mcp_server: Mapped[Optional["MCPServer"]] = relationship(
        "MCPServer", back_populates="tool_configurations"
    )
    managed_agent: Mapped[Optional["ManagedAgent"]] = relationship("ManagedAgent")
    approval_workflow: Mapped[Optional["ApprovalWorkflow"]] = relationship(
        "ApprovalWorkflow",
        back_populates="tool_configurations",
    )
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(
        "ApprovalRequest",
        back_populates="tool_configuration",
        cascade="all, delete-orphan",
    )
    access_rules: Mapped[list["ToolAccessRule"]] = relationship(
        "ToolAccessRule",
        back_populates="tool_configuration",
        cascade="all, delete-orphan",
    )

    # Unique constraint: one configuration per tool+source per account and
    # per scope (account-wide vs a specific managed agent).
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "tool_name",
            "tool_source",
            "mcp_server_id",
            "managed_agent_id",
            name="uq_account_tool_source",
        ),
    )

    def __repr__(self) -> str:
        """Return string representation of the configuration."""
        status = "enabled" if self.is_enabled else "disabled"
        approval = " (approval required)" if self.approval_workflow_id else ""
        return f"<ToolConfiguration {self.tool_name} [{self.tool_source}] ({status}{approval}) for account {self.account_id}>"


class ApprovalWorkflow(Base):
    """Reusable approval workflow for tools that require pre-execution approval.

    This model defines how approval requests should be handled for tools
    with requires_approval=True. Policies are account-scoped and reusable
    across multiple tool configurations.

    Attributes:
        id: Unique identifier for the workflow.
        account_id: The account this workflow belongs to.
        name: Human-readable name for the workflow.
        description: Optional description of what this workflow does.
        approval_type: Type of approval mechanism ('slack', 'mattermost', etc.).
        channel: Slack/Mattermost channel for approval requests.
        user: Specific user to request approval from.
        approval_config: Generic configuration for approval mechanism.
        timeout_seconds: How long to wait for approval before timing out.
        require_reason: Whether approver must provide a reason.
    """

    __tablename__ = "approval_workflow"

    # workflow identification
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The account this workflow belongs to",
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Human-readable name for the workflow",
    )
    description: Mapped[Optional[str]] = mapped_column(
        String,
        nullable=True,
        comment="Optional description of what this workflow does",
    )

    # Approval mechanism
    approval_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="slack",
        comment="Type of approval: 'slack', 'mattermost', 'webhook', 'manual'",
    )

    # Slack/Mattermost configuration
    channel: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="Channel for approval requests"
    )
    user: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="Specific user to request approval from"
    )

    # Generic approval configuration (for future extensibility)
    approval_config: Mapped[Optional[Dict]] = mapped_column(
        JSON, nullable=True, comment="Generic configuration for approval mechanism"
    )

    # workflow settings
    timeout_seconds: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        default=300,
        comment="How long to wait for approval (default: 5 minutes)",
    )
    require_reason: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether approver must provide a reason",
    )
    async_approval_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="When enabled, tool calls return immediately and agents poll for status",
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Whether this is the default workflow for the account",
    )

    # Workflow configuration (Phase 2+)
    workflow_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="simple",
        comment="Type of approval workflow: 'simple', 'multi_stage', 'consensus'",
    )
    workflow_config: Mapped[Optional[Dict]] = mapped_column(
        JSON,
        nullable=True,
        comment="Configuration for the approval workflow (stages, teams, voting rules)",
    )

    # Approvers (proprietary features)
    approver_user_ids: Mapped[Optional[List[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
        comment="List of user IDs who can approve (proprietary)",
    )
    approver_team_ids: Mapped[Optional[List[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
        comment="List of team IDs whose members can approve (proprietary)",
    )

    # Quorum configuration (proprietary)
    approvals_required: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="Number of approvals required (quorum) - proprietary",
    )

    # Escalation configuration (proprietary)
    escalation_user_ids: Mapped[Optional[List[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
        comment="List of user IDs to escalate to on timeout (proprietary)",
    )
    escalation_team_ids: Mapped[Optional[List[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
        comment="List of team IDs to escalate to on timeout (proprietary)",
    )

    # Notification configuration
    channel_configs: Mapped[Optional[Dict]] = mapped_column(
        JSON,
        nullable=True,
        comment="Configuration for notification channels (Slack/Mattermost/webhook settings)",
    )

    # AI-driven approval settings
    approval_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="standard",
        server_default="standard",
        comment="Approval mode: 'standard' (human) or 'ai_driven' (AI makes decision)",
    )
    ai_model: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="AI model to use for approval decisions (e.g., 'gpt-5.4', 'claude-sonnet-4.7')",
    )
    ai_guidelines: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="User-defined guidelines for AI approval decisions",
    )
    ai_context: Mapped[Optional[Dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Additional context for AI decisions (e.g., examples, constraints)",
    )
    ai_confidence_threshold: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=0.8,
        server_default="0.8",
        comment="Minimum confidence score required for AI to make a decision",
    )
    ai_fallback_behavior: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="escalate",
        server_default="escalate",
        comment="Behavior when AI confidence is below threshold: 'escalate', 'approve', 'deny'",
    )
    escalation_workflow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_workflow.id", ondelete="SET NULL"),
        nullable=True,
        comment="workflow to escalate to when fallback_behavior='escalate'",
    )

    # Relationships
    account: Mapped["Account"] = relationship("Account")
    tool_configurations: Mapped[list["ToolConfiguration"]] = relationship(
        "ToolConfiguration", back_populates="approval_workflow"
    )
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(
        "ApprovalRequest",
        back_populates="approval_workflow",
        cascade="all, delete-orphan",
    )
    # The workflow to escalate to when AI confidence is below threshold
    escalation_workflow: Mapped[Optional["ApprovalWorkflow"]] = relationship(
        "ApprovalWorkflow",
        remote_side="ApprovalWorkflow.id",
        foreign_keys=[escalation_workflow_id],
        uselist=False,
    )

    # Unique constraint: one workflow name per account
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "name",
            name="uq_account_workflow_name",
        ),
    )

    def __repr__(self) -> str:
        """Return string representation of the workflow."""
        target = self.channel or self.user or "unknown"
        return f"<ApprovalWorkflow(name={self.name}, type={self.approval_type}, target={target})>"
