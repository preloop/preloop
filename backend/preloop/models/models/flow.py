from typing import Optional
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
    # Blocking review rules for the Pull Request Reviewer. Same content as
    # .preloop/review-policy.md, for a repository that cannot commit that
    # file. Injected as {{flow.review_instructions}}. NULL means unset.
    review_instructions = Column(Text, nullable=True)
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

    # Delegation allowlist: which flows an execution of this flow is permitted
    # to run, with a per entry ceiling. NULL or [] means no delegation, so a
    # flow that has the delegation tool enabled but no allowlist can call
    # nothing at all. Nothing enforces this yet (issue #627 is the column).
    # It is a column rather than a field on the api key context an execution
    # carries because that context is minted at launch: an allowlist read from
    # it could not be revoked by an operator while a run is in flight.
    # Structure (see schemas.flow.CallableFlowEntry):
    # [
    #     {
    #         "flow": str - slug or name of a flow in this account,
    #         "max_children": int > 0 (optional) - children per execution,
    #         "max_usd_per_child": float > 0 (optional) - USD ceiling per child,
    #         "allow_self": bool - explicit opt in to a self reference
    #     }
    # ]
    callable_flows = Column(JSON, nullable=True, default=None)

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
    # How long a human has to answer an approval or question raised by an
    # execution of this flow, in seconds. NULL falls back to the approval
    # workflow's timeout and then to the deployment default (300s). A flow
    # whose questions are compliance decisions (a CRA waiver, a release
    # sign-off) sets days here: while the request is outstanding the run is
    # parked, so a long window costs nothing but calendar time.
    approval_window_seconds = Column(Integer, nullable=True)
    # Terminal-path notifications. NULL means no tracker comments.
    # Failed executions always surface as console attention items.
    # Shape:
    # {
    #     "on_success": {
    #         "comment_on_trigger_issue": bool,
    #     },
    # }
    # An "on_failure" block may still be stored on rows written before the
    # failure comment was removed (2026-09). Every key in it is ignored: the
    # column is JSONB, so there is nothing to migrate, and the console drops
    # the block the next time the flow is saved.
    notifications = Column(JSONB, nullable=True, default=None)

    ai_model = relationship("AIModel", back_populates="flows")
    account = relationship("Account", back_populates="flows", foreign_keys=[account_id])
    executions = relationship(
        "FlowExecution", back_populates="flow", cascade="all, delete-orphan"
    )

    @property
    def ai_model_name(self) -> Optional[str]:
        return self.ai_model.name if self.ai_model else None

    def __repr__(self) -> str:
        return f"<Flow(id={self.id}, name='{self.name}')>"
