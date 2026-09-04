from sqlalchemy import (  # Added JSON
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    JSON,
)

from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship

from .base import Base


class Flow(Base):
    __tablename__ = "flow"

    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String, nullable=True)
    # For tracker triggers: source = tracker_id, type = event_type
    # For webhook triggers: source = 'webhook', type = 'webhook'
    trigger_event_source = Column(String, nullable=True)
    # Event types that trigger this flow (e.g., ['pull_request_created', 'pull_request_updated'])
    trigger_event_types = Column(ARRAY(String), nullable=True, default=None)
    # Organization to scope trigger
    trigger_organization_id = Column(String, nullable=True)
    # Project IDs that can trigger this flow (empty = all projects in org)
    trigger_project_ids = Column(ARRAY(String), nullable=True, default=None)
    trigger_config = Column(JSON, nullable=True)  # Changed from JSONB
    # Webhook-specific configuration
    # Structure: {
    #     "webhook_secret": str - secret token for authenticating webhook requests
    # }
    webhook_config = Column(JSON, nullable=True, default=None)
    # Schedule-specific configuration (for trigger_event_source == 'schedule')
    # Discriminated on "type" (see schemas.flow.ScheduleConfig):
    #   {"type": "cron", "expr": "0 6 * * 1-5", "timezone": "Europe/Athens"}
    #   {"type": "interval", "every": 30, "unit": "minutes", "timezone": ...}
    #   {"type": "daily", "at": "06:30", "timezone": ...}
    #   {"type": "weekly", "days": ["mon", "fri"], "at": "09:00", "timezone": ...}
    # Legacy rows may lack "type" and use {"cron": ..., "timezone": ...}.
    schedule_config = Column(JSON, nullable=True, default=None)
    prompt_template = Column(Text, nullable=False)
    ai_model_id = Column(
        UUID(as_uuid=True),
        ForeignKey("ai_model.id"),
        nullable=True,
    )
    agent_type = Column(String, nullable=False, default="openhands")
    agent_config = Column(JSON, nullable=False)  # Changed from JSONB
    allowed_mcp_servers = Column(
        JSON,
        nullable=False,
        default=[],  # Changed from JSONB
    )  # Assuming JSON Array of strings
    allowed_mcp_tools = Column(
        JSON,
        nullable=False,
        default=[],  # Changed from JSONB
    )  # Assuming JSON Array of objects

    # Git clone configuration for flows that need source code
    # Structure: {
    #     "enabled": bool,
    #     "repositories": [
    #         {
    #             "tracker_id": str,
    #             "project_id": str (optional),
    #             "repository_url": str (optional - uses project's default if not specified),
    #             "clone_path": str (relative path where to clone, default: "workspace"),
    #             "branch": str (optional - branch to clone, for backwards compatibility)
    #         }
    #     ],
    #     "git_user_name": str (default: "Preloop"),
    #     "git_user_email": str (default: "git@preloop.ai"),
    #     "source_branch": str (branch to checkout, default: "main"),
    #     "target_branch": str (branch to create for commits, optional - auto-generated if empty),
    #     "create_pull_request": bool (create PR/MR after commits, default: False),
    #     "pull_request_title": str (optional - title for PR/MR),
    #     "pull_request_description": str (optional - description for PR/MR)
    # }
    git_clone_config = Column(JSON, nullable=True, default=None)

    # Custom commands to run before agent starts (admin-only feature)
    # Security: Only users with is_superuser=True can configure this
    # Structure: {
    #     "enabled": bool,
    #     "commands": List[str] - list of shell commands to execute
    # }
    custom_commands = Column(JSON, nullable=True, default=None)

    is_preset = Column(Boolean, default=False, nullable=False)
    is_enabled = Column(Boolean, default=True, nullable=False)
    account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("account.id"),
        nullable=True,
        index=True,
    )

    # Template tracking for flows cloned from presets
    # Allows auto-updating non-customized flows when presets change
    source_preset_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Hash of the original prompt_template when cloned (for detecting changes)
    source_prompt_hash = Column(String(32), nullable=True)
    # Hash of the original allowed_mcp_tools when cloned
    source_tools_hash = Column(String(32), nullable=True)
    # Flags indicating if user customized these fields (prevents auto-update)
    prompt_customized = Column(Boolean, default=False, nullable=False)
    tools_customized = Column(Boolean, default=False, nullable=False)
    # Flag indicating if a newer preset version is available (for notifications)
    preset_update_available = Column(Boolean, default=False, nullable=False)
    # When set, executions lease to a matching self-hosted runner (id, name,
    # or label). The literal "server" opts into the hosted executor. NULL
    # inherits account.default_runner_pool, then any online private runner.
    runner_pool = Column(String(200), nullable=True)
    # Wall-clock budget for one execution of this flow, in seconds. NULL means
    # the global default (FLOW_EXECUTION_MAX_WAIT_SECONDS). A review flow that
    # should finish in minutes and a nightly audit that legitimately runs for
    # hours cannot share one ceiling: with a single global value, "stuck" and
    # "genuinely long" produce the same timeout row.
    timeout_seconds = Column(Integer, nullable=True)
    # Terminal-path notifications. NULL means no comments or attention items.
    # Shape:
    # {
    #     "on_failure": {
    #         "comment_on_trigger_issue": bool,
    #         "attention_item": bool,
    #     },
    #     "on_success": {
    #         "comment_on_trigger_issue": bool,
    #     },
    # }
    notifications = Column(JSONB, nullable=True, default=None)

    ai_model = relationship("AIModel", back_populates="flows")
    account = relationship("Account", back_populates="flows", foreign_keys=[account_id])
    executions = relationship(
        "FlowExecution", back_populates="flow", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Flow(id={self.id}, name='{self.name}')>"
