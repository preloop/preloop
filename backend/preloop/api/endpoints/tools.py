"""Tools router for managing available tools and their configurations.

IMPORTANT: BUILTIN_TOOLS metadata must match the tool implementations in
preloop/services/initialize_mcp.py to ensure consistency between REST API and MCP.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.models.user import User
from preloop.models.crud import (
    crud_approval_workflow,
    crud_mcp_server,
    crud_mcp_tool,
    crud_tool_configuration,
    crud_tool_access_rule,
    crud_tracker,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.schemas.tool_configuration import (
    ApprovalWorkflowCreate,
    ApprovalWorkflowResponse,
    ApprovalWorkflowUpdate,
    ToolConfigurationCreate,
    ToolConfigurationResponse,
    ToolConfigurationUpdate,
)
from preloop.schemas.tool_approval_condition import (
    ToolApprovalConditionCreate,
    ToolApprovalConditionResponse,
    ConditionTestRequest,
    ConditionTestResponse,
)
from preloop.services.policy_evaluator import evaluate_cel_expression
from preloop.services.tool_usage_stats import ToolUsageStatsService
from preloop.schemas.gateway_usage import GatewayUsageByTool
from preloop.utils.audit import log_config_change
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)
router = APIRouter()

# Define builtin tools metadata
# NOTE: These must match the @mcp.tool() decorators in initialize_mcp.py
BUILTIN_TOOLS = [
    {
        "name": "request_approval",
        "description": "Request approval for an operation before executing it",
        "source": "builtin",
        "requires_tracker": False,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "Description of the operation requiring approval",
                },
                "context": {
                    "type": "string",
                    "description": "Additional context about the situation",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Explanation of why this operation is needed",
                },
                "caller": {
                    "type": "string",
                    "description": "Optional: Name of the agent or flow requesting approval (auto-populated if not specified)",
                },
                "approval_workflow": {
                    "type": "string",
                    "description": "Optional name of the approval workflow to use",
                },
            },
            "required": ["operation", "context", "reasoning"],
        },
    },
    {
        "name": "get_issue",
        "description": "Get detailed information about an issue by its identifier (URL, key, or ID)",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "description": "Issue identifier (URL, key, or ID)",
                }
            },
            "required": ["issue"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create a new issue in a project",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project identifier"},
                "title": {"type": "string", "description": "Issue title"},
                "description": {"type": "string", "description": "Issue description"},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Issue labels",
                },
                "assignee": {"type": "string", "description": "Assignee username"},
                "priority": {"type": "string", "description": "Issue priority"},
                "status": {"type": "string", "description": "Issue status"},
            },
            "required": ["project", "title", "description"],
        },
    },
    {
        "name": "update_issue",
        "description": "Update an existing issue",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string", "description": "Issue identifier"},
                "title": {"type": "string", "description": "New title"},
                "description": {"type": "string", "description": "New description"},
                "status": {"type": "string", "description": "New status"},
                "priority": {"type": "string", "description": "New priority"},
                "assignee": {"type": "string", "description": "New assignee"},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "search",
        "description": "Search for issues and comments using similarity or fulltext search",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "project": {"type": "string", "description": "Project identifier"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum results",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "estimate_compliance",
        "description": "Estimate compliance for a list of issues provided as URLs or issue keys",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "issues": {"type": "array", "items": {"type": "string"}},
                "compliance_metric": {"type": "string", "default": "DoR"},
            },
            "required": ["issues"],
        },
    },
    {
        "name": "improve_compliance",
        "description": "Get suggestions to improve compliance for a list of issues",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "issues": {"type": "array", "items": {"type": "string"}},
                "compliance_metric": {"type": "string", "default": "DoR"},
            },
            "required": ["issues"],
        },
    },
    {
        "name": "add_comment",
        "description": "Add a comment to an issue, pull request, or merge request. For general comments: provide just target and comment. For inline code comments: also provide path and line. To reply to a thread: provide in_reply_to with the comment ID.",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Issue, PR, or MR identifier (URL, key, or ID)",
                },
                "comment": {
                    "type": "string",
                    "description": "Comment text (supports markdown)",
                },
                "path": {
                    "type": "string",
                    "description": "File path for inline comment",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number in the diff",
                },
                "side": {
                    "type": "string",
                    "enum": ["LEFT", "RIGHT"],
                    "description": "OLD or NEW file",
                    "default": "RIGHT",
                },
                "in_reply_to": {
                    "type": "string",
                    "description": "Comment ID to reply to (creates thread)",
                },
            },
            "required": ["target", "comment"],
        },
    },
    {
        "name": "update_comment",
        "description": "Update or resolve an existing comment on a pull request or merge request. Supports both inline review comments and PR conversation comments (issue comments). To update the comment text: provide body with new content. To resolve/unresolve a thread: provide resolved as true/false (only works for review_comment type). Use comment_type to specify the comment type, or omit to auto-detect.",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": ["github", "gitlab"],
        "schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "PR/MR identifier (URL or owner/repo#number format)",
                },
                "comment_id": {
                    "type": "string",
                    "description": "ID of the comment to update",
                },
                "body": {
                    "type": "string",
                    "description": "New comment content",
                },
                "resolved": {
                    "type": "boolean",
                    "description": "Resolve or unresolve the thread (only works for review_comment type)",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Thread/discussion ID for resolution. For GitHub: the review thread node_id (e.g., 'PRRT_...'). For GitLab: the discussion ID.",
                },
                "comment_type": {
                    "type": "string",
                    "enum": ["review_comment", "issue_comment"],
                    "description": "Type of comment: 'review_comment' for inline code review comments, 'issue_comment' for PR conversation comments. If omitted, tries review_comment first then issue_comment. Tip: get_pull_request includes 'type' field for each comment.",
                },
            },
            "required": ["target", "comment_id"],
        },
    },
    {
        "name": "get_pull_request",
        "description": "Get details of a pull request (GitHub) or merge request (GitLab). Auto-detects platform from URL. Returns PR metadata, comments, and file changes.",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": ["github", "gitlab"],
        "schema": {
            "type": "object",
            "properties": {
                "pull_request": {
                    "type": "string",
                    "description": "PR/MR identifier (URL, slug, or number)",
                },
                "include_comments": {
                    "type": "boolean",
                    "description": "Include all comments/discussions",
                    "default": True,
                },
                "include_diff": {
                    "type": "boolean",
                    "description": "Include file changes",
                    "default": True,
                },
            },
            "required": ["pull_request"],
        },
    },
    {
        "name": "update_pull_request",
        "description": "Update a pull request's metadata, submit a review, and/or manage reactions. To update PR properties: provide title, description, labels, state, assignees, reviewers, draft. To submit a review: provide review_action (approve/request_changes/comment) with optional review_body and review_comments for inline feedback. To add/remove reactions: use add_reaction or remove_reaction with emoji names.",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": ["github", "gitlab"],
        "schema": {
            "type": "object",
            "properties": {
                "pull_request": {
                    "type": "string",
                    "description": "PR/MR identifier",
                },
                "title": {"type": "string", "description": "New title"},
                "description": {
                    "type": "string",
                    "description": "New description/body",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "closed"],
                    "description": "New state (open/closed for GitHub, close/reopen for GitLab)",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names",
                },
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Assignee usernames",
                },
                "reviewers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reviewer usernames",
                },
                "draft": {"type": "boolean", "description": "Mark as draft"},
                "review_action": {
                    "type": "string",
                    "enum": ["approve", "request_changes", "comment"],
                    "description": "Submit a review with this action",
                },
                "review_body": {
                    "type": "string",
                    "description": "Summary comment for the review",
                },
                "review_comments": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Inline comments: [{path, line, body, side}]. Each comment requires path, line, and body.",
                },
                "add_reaction": {
                    "type": "string",
                    "description": "Add a reaction emoji. GitHub: +1, -1, laugh, confused, heart, hooray, rocket, eyes. GitLab: thumbsup, thumbsdown, smile, eyes, rocket, etc.",
                },
                "remove_reaction": {
                    "type": "string",
                    "description": "Remove a reaction emoji (same names as add_reaction)",
                },
            },
            "required": ["pull_request"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request (GitHub) or merge request (GitLab). Auto-detects platform from project. Use extra_options for GitLab-specific options like squash, remove_source_branch, assignee_ids, reviewer_ids, milestone_id.",
        "source": "builtin",
        "requires_tracker": True,
        "required_tracker_types": ["github", "gitlab"],
        "schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project identifier (slug like owner/repo, full path, or URL)",
                },
                "title": {
                    "type": "string",
                    "description": "PR/MR title",
                },
                "source_branch": {
                    "type": "string",
                    "description": "Branch containing the changes (head branch)",
                },
                "target_branch": {
                    "type": "string",
                    "description": "Branch to merge into (base branch)",
                },
                "description": {
                    "type": "string",
                    "description": "PR/MR description/body",
                },
                "draft": {
                    "type": "boolean",
                    "description": "Create as draft",
                    "default": False,
                },
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Assignee usernames",
                },
                "reviewers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reviewer usernames",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names",
                },
                "milestone": {
                    "type": "string",
                    "description": "Milestone number or title",
                },
                "extra_options": {
                    "type": "object",
                    "description": "Additional options (GitLab: squash, remove_source_branch, assignee_ids, reviewer_ids, milestone_id, allow_collaboration)",
                },
            },
            "required": ["project", "title", "source_branch", "target_branch"],
        },
    },
    {
        "name": "get_approval_status",
        "description": "Check the status of a pending approval request. Returns a detailed event log of the approval workflow.",
        "source": "builtin",
        "requires_tracker": False,
        "required_tracker_types": [],
        "schema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "The approval request ID returned by a tool that triggered an approval workflow",
                },
            },
            "required": ["request_id"],
        },
    },
]


class ToolUsageStatsResponse(BaseModel):
    """Account-wide tool invocation and schema-cost stats."""

    period_start: datetime
    period_end: datetime
    tools: List[GatewayUsageByTool] = Field(default_factory=list)


@router.get("/tools/stats", response_model=ToolUsageStatsResponse)
@require_permission("view_cost")
def get_tool_usage_stats(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ToolUsageStatsResponse:
    """Return account-wide tool invocation counts and schema-injection cost."""
    service = ToolUsageStatsService(db)
    end = end_date or datetime.now(timezone.utc)
    start = start_date or (end - timedelta(days=30))
    tools = service.get_account_usage_by_tool(
        account_id=str(account.id),
        start_date=start,
        end_date=end,
    )
    return ToolUsageStatsResponse(
        period_start=start,
        period_end=end,
        tools=tools,
    )


@router.get("/tools", response_model=List[Dict])
@require_permission("view_tools")
def list_all_tools(
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[Dict]:
    """List all available tools (builtin + external) with their configuration status.

    Returns a comprehensive list of:
    - All builtin tools
    - All tools from active MCP servers
    - Configuration status for each tool (enabled/disabled, preloop)

    Args:
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        List of tool dictionaries with metadata and configuration
    """
    # Get all tool configurations for this account
    tool_configs = crud_tool_configuration.get_multi_by_account(
        db, account_id=str(account.id)
    )

    # Create a lookup map: (tool_name, source, mcp_server_id) -> config
    config_map = {
        (
            tc.tool_name,
            tc.tool_source,
            str(tc.mcp_server_id) if tc.mcp_server_id else None,
        ): tc
        for tc in tool_configs
    }

    # Get all access rules for this account
    access_rules = crud_tool_access_rule.get_multi_by_account(
        db, account_id=str(account.id)
    )

    # Create map of config_id -> list of rule dicts
    rules_by_config: Dict[str, list] = {}
    condition_map: Dict[str, bool] = {}
    for rule in access_rules:
        config_id_str = str(rule.tool_configuration_id)
        if config_id_str not in rules_by_config:
            rules_by_config[config_id_str] = []
        rules_by_config[config_id_str].append(
            {
                "id": str(rule.id),
                "action": rule.action,
                "condition_expression": rule.condition_expression,
                "condition_type": rule.condition_type,
                "priority": rule.priority,
                "description": rule.description,
                "is_enabled": rule.is_enabled,
                "approval_workflow_id": str(rule.approval_workflow_id)
                if rule.approval_workflow_id
                else None,
            }
        )
        if rule.condition_expression and rule.is_enabled:
            condition_map[config_id_str] = True

    trackers = crud_tracker.get_for_account(db, account_id=str(account.id))
    tracker_types = list(set(tracker.tracker_type for tracker in trackers))
    has_tracker = len(tracker_types) > 0

    tools = []

    # Add builtin tools
    for builtin_tool in BUILTIN_TOOLS:
        config = config_map.get((builtin_tool["name"], "builtin", None))
        config_id = str(config.id) if config else None

        requires_tracker = builtin_tool.get("requires_tracker", False)
        required_tracker_types = builtin_tool.get("required_tracker_types") or []

        is_supported = True
        unsupported_reason = None
        if requires_tracker and not has_tracker:
            is_supported = False
            unsupported_reason = "Add a tracker to enable this tool"
        elif required_tracker_types and not any(
            t in tracker_types for t in required_tracker_types
        ):
            is_supported = False
            required_str = ", ".join(required_tracker_types)
            unsupported_reason = f"Add a {required_str} tracker to enable this tool"

        tools.append(
            {
                "name": builtin_tool["name"],
                "description": builtin_tool["description"],
                "source": "builtin",
                "source_id": None,
                "source_name": "Built-in",
                "schema": builtin_tool["schema"],
                "is_enabled": config.is_enabled if config else True,
                "requires_tracker": requires_tracker,
                "required_tracker_types": required_tracker_types,
                "is_supported": is_supported,
                "unsupported_reason": unsupported_reason,
                "approval_workflow_id": str(config.approval_workflow_id)
                if config and config.approval_workflow_id
                else None,
                "config_id": config_id,
                "has_approval_condition": condition_map.get(config_id, False)
                if config_id
                else False,
                "access_rules": rules_by_config.get(config_id, []) if config_id else [],
                "justification_mode": config.justification_mode if config else None,
            }
        )

    # Add external MCP tools
    mcp_servers = crud_mcp_server.get_active_by_account(db, account_id=str(account.id))

    for server in mcp_servers:
        mcp_tools = crud_mcp_tool.get_by_server(db, server_id=server.id)

        for mcp_tool in mcp_tools:
            config = config_map.get((mcp_tool.name, "mcp", str(server.id)))
            config_id = str(config.id) if config else None
            tools.append(
                {
                    "name": mcp_tool.name,
                    "description": mcp_tool.description or "",
                    "source": "mcp",
                    "source_id": str(server.id),
                    "source_name": server.name,
                    "schema": mcp_tool.input_schema,
                    "is_enabled": config.is_enabled if config else True,
                    "requires_tracker": False,
                    "required_tracker_types": [],
                    "is_supported": True,
                    "unsupported_reason": None,
                    "approval_workflow_id": str(config.approval_workflow_id)
                    if config and config.approval_workflow_id
                    else None,
                    "config_id": config_id,
                    "has_approval_condition": condition_map.get(config_id, False)
                    if config_id
                    else False,
                    "access_rules": rules_by_config.get(config_id, [])
                    if config_id
                    else [],
                    "justification_mode": config.justification_mode if config else None,
                }
            )

    logger.info(
        f"Returning {len(tools)} tools for account {account.id} "
        f"({len(BUILTIN_TOOLS)} builtin, {len(tools) - len(BUILTIN_TOOLS)} external)"
    )

    return tools


@router.post("/tool-configurations", status_code=status.HTTP_201_CREATED)
@require_permission("manage_tools")
async def create_tool_configuration(
    config_data: ToolConfigurationCreate,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ToolConfigurationResponse:
    """Create a new tool configuration.

    Args:
        config_data: Tool configuration data
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Created tool configuration

    Raises:
        HTTPException: If configuration already exists or creation fails
    """
    # Check if configuration already exists
    # Get all configs and filter in Python since we need multi-field matching
    all_configs = crud_tool_configuration.get_multi_by_account(
        db, account_id=str(account.id), limit=1000
    )
    existing_config = next(
        (
            c
            for c in all_configs
            if c.tool_name == config_data.tool_name
            and c.tool_source == config_data.tool_source
            and c.mcp_server_id == config_data.mcp_server_id
        ),
        None,
    )

    if existing_config:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Configuration for tool '{config_data.tool_name}' already exists",
        )

    try:
        # Override account_id from authenticated user for security
        safe_config = config_data.model_copy(
            update={
                "account_id": str(account.id),
                "is_enabled": config_data.is_enabled
                if config_data.is_enabled is not None
                else True,
            }
        )

        new_config = crud_tool_configuration.create(db, config_in=safe_config)

        logger.info(
            f"Created tool configuration for {config_data.tool_name} "
            f"(user: {account.id})"
        )

        return ToolConfigurationResponse.model_validate(new_config)

    except IntegrityError as e:
        db.rollback()
        logger.info(
            f"Tool configuration for {config_data.tool_name} already exists, fetching existing config"
        )

        # Fetch the existing configuration
        existing_config = crud_tool_configuration.get_by_tool_name_and_source(
            db,
            account_id=str(account.id),
            tool_name=config_data.tool_name,
            tool_source=config_data.tool_source,
        )

        if existing_config:
            # Configuration already exists - return it (idempotent behavior)
            # This handles race conditions where multiple requests try to create the same config
            logger.info(
                f"Returning existing tool configuration for {config_data.tool_name} "
                f"(idempotent behavior for race condition)"
            )
            return ToolConfigurationResponse.model_validate(existing_config)

        # If we still can't find it, something went wrong
        logger.error(f"IntegrityError but config not found for {config_data.tool_name}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating tool configuration: {str(e)}",
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating tool configuration: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating tool configuration: {str(e)}",
        )


@router.get(
    "/tool-configurations/{config_id}", response_model=ToolConfigurationResponse
)
@require_permission("view_tools")
async def get_tool_configuration(
    config_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ToolConfigurationResponse:
    """Get a specific tool configuration.

    Args:
        config_id: Tool configuration ID
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Tool configuration details

    Raises:
        HTTPException: If configuration not found or access denied
    """
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found or access denied",
        )

    return ToolConfigurationResponse.model_validate(config)


@router.put(
    "/tool-configurations/{config_id}", response_model=ToolConfigurationResponse
)
@require_permission("manage_tools")
async def update_tool_configuration(
    config_id: UUID,
    config_update: ToolConfigurationUpdate,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ToolConfigurationResponse:
    """Update an existing tool configuration.

    Args:
        config_id: Tool configuration ID
        config_update: Updated configuration data
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Updated tool configuration

    Raises:
        HTTPException: If configuration not found or update fails
    """
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found or access denied",
        )

    try:
        old_enabled = config.is_enabled
        updated_config = crud_tool_configuration.update(
            db, db_obj=config, config_in=config_update
        )

        # Determine if this was an enable/disable toggle or a general update
        update_fields = config_update.model_dump(exclude_unset=True)
        if "is_enabled" in update_fields and update_fields["is_enabled"] != old_enabled:
            action = "enabled" if updated_config.is_enabled else "disabled"
        else:
            action = "updated"

        log_config_change(
            db,
            user=current_user,
            config_type="tool_configuration",
            action=action,
            new_value={
                "id": str(config_id),
                "tool_name": config.tool_name,
                **update_fields,
            },
        )

        logger.info(f"Updated tool configuration {config_id} for user {account.id}")

        return ToolConfigurationResponse.model_validate(updated_config)

    except Exception as e:
        db.rollback()
        logger.error(
            f"Error updating tool configuration {config_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating tool configuration: {str(e)}",
        )


@router.put(
    "/tool-configurations/{config_id}/condition",
    response_model=ToolConfigurationResponse,
)
@require_permission("manage_tools")
async def update_tool_approval_condition(
    config_id: UUID,
    condition_data: Dict[str, Any],
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ToolConfigurationResponse:
    """Update or create approval condition for a tool configuration.

    This endpoint uses the new ToolAccessRule model (replaced ToolApprovalCondition).
    For backward compatibility, it manages a single 'require_approval' rule per tool.

    Args:
        config_id: Tool configuration ID
        condition_data: Condition data with 'approval_condition' field
        account: Current user's account
        db: Database session

    Returns:
        Updated tool configuration

    Raises:
        HTTPException: If configuration not found or update fails
    """
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found or access denied",
        )

    approval_condition_expr = condition_data.get("approval_condition")

    try:
        # Get existing access rule (first one with require_approval action)
        existing_rule = crud_tool_access_rule.get_first_by_config(
            db,
            config_id=str(config_id),
            account_id=str(account.id),
            action="require_approval",
        )

        if approval_condition_expr:
            # Create or update access rule
            if existing_rule:
                # Update existing rule
                crud_tool_access_rule.update(
                    db,
                    db_obj=existing_rule,
                    obj_in={
                        "condition_expression": approval_condition_expr,
                        "is_enabled": True,
                    },
                )
                logger.info(f"Updated access rule for tool config {config_id}")
            else:
                # Create new access rule
                crud_tool_access_rule.create(
                    db,
                    obj_in={
                        "tool_configuration_id": str(config_id),
                        "account_id": str(account.id),
                        "condition_type": "cel",
                        "condition_expression": approval_condition_expr,
                        "action": "require_approval",
                        "priority": 0,
                        "is_enabled": True,
                    },
                )
                logger.info(f"Created access rule for tool config {config_id}")
        else:
            # Delete rule if expression is empty
            if existing_rule:
                crud_tool_access_rule.remove(
                    db,
                    id=str(existing_rule.id),
                    account_id=str(account.id),
                )
                logger.info(f"Deleted access rule for tool config {config_id}")

        db.refresh(config)
        return ToolConfigurationResponse.model_validate(config)

    except Exception as e:
        db.rollback()
        logger.error(
            f"Error updating approval condition for {config_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating approval condition: {str(e)}",
        )


@router.delete("/tool-configurations/{config_id}", status_code=status.HTTP_200_OK)
@require_permission("manage_tools")
async def delete_tool_configuration(
    config_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Delete a tool configuration.

    Args:
        config_id: Tool configuration ID
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: If configuration not found or deletion fails
    """
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found or access denied",
        )

    try:
        crud_tool_configuration.remove(
            db, id=str(config_id), account_id=str(account.id)
        )

        logger.info(f"Deleted tool configuration {config_id} for user {account.id}")

        return {"message": "Tool configuration deleted successfully"}

    except Exception as e:
        db.rollback()
        logger.error(
            f"Error deleting tool configuration {config_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting tool configuration: {str(e)}",
        )


# Approval workflow endpoints


@router.get("/approval-workflows", response_model=List[ApprovalWorkflowResponse])
@require_permission("view_approvals")
async def list_approval_workflows(
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[ApprovalWorkflowResponse]:
    """List all approval workflows for the current user's account.

    Args:
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        List of approval workflows
    """
    policies = crud_approval_workflow.get_multi_by_account(
        db, account_id=str(account.id)
    )

    logger.info(f"Returning {len(policies)} approval workflows for user {account.id}")

    return [ApprovalWorkflowResponse.model_validate(p) for p in policies]


@router.post("/approval-workflows", status_code=status.HTTP_201_CREATED)
@require_permission("manage_approval_workflows")
async def create_approval_workflow(
    workflow_data: ApprovalWorkflowCreate,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApprovalWorkflowResponse:
    """Create a reusable approval workflow.

    Args:
        workflow_data: Approval workflow data
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Created approval workflow

    Raises:
        HTTPException: If workflow with same name already exists or creation fails
    """
    # Check if workflow with same name already exists
    existing_workflow = crud_approval_workflow.get_by_name(
        db, account_id=str(account.id), name=workflow_data.name
    )

    if existing_workflow:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Approval workflow with name '{workflow_data.name}' already exists",
        )

    try:
        # Use CRUD layer for proper default workflow handling
        new_workflow = crud_approval_workflow.create(
            db, obj_in=workflow_data, account_id=str(account.id)
        )

        log_config_change(
            db,
            user=current_user,
            config_type="approval_workflow",
            action="created",
            new_value={"id": str(new_workflow.id), "name": new_workflow.name},
        )

        logger.info(
            f"Created approval workflow '{workflow_data.name}' (user: {account.id}, is_default: {new_workflow.is_default})"
        )

        return ApprovalWorkflowResponse.model_validate(new_workflow)

    except Exception as e:
        db.rollback()
        logger.error(f"Error creating approval workflow: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating approval workflow: {str(e)}",
        )


@router.get(
    "/approval-workflows/{workflow_id}", response_model=ApprovalWorkflowResponse
)
@require_permission("view_approvals")
async def get_approval_workflow(
    workflow_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApprovalWorkflowResponse:
    """Get an approval workflow by ID.

    Args:
        workflow_id: Approval workflow ID
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Approval workflow details

    Raises:
        HTTPException: If workflow not found or access denied
    """
    workflow = crud_approval_workflow.get(
        db, id=workflow_id, account_id=str(account.id)
    )

    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval workflow not found or access denied",
        )

    return ApprovalWorkflowResponse.model_validate(workflow)


@router.put(
    "/approval-workflows/{workflow_id}", response_model=ApprovalWorkflowResponse
)
@require_permission("manage_approval_workflows")
async def update_approval_workflow(
    workflow_id: UUID,
    workflow_update: ApprovalWorkflowUpdate,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApprovalWorkflowResponse:
    """Update an approval workflow.

    Args:
        workflow_id: Approval workflow ID
        workflow_update: Updated workflow data
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Updated approval workflow

    Raises:
        HTTPException: If workflow not found or update fails
    """
    workflow = crud_approval_workflow.get(
        db, id=workflow_id, account_id=str(account.id)
    )

    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval workflow not found or access denied",
        )

    # Update fields
    update_data = workflow_update.model_dump(exclude_unset=True)

    try:
        # Check if name is being updated and if it conflicts
        if "name" in update_data and update_data["name"] != workflow.name:
            existing_workflow = crud_approval_workflow.get_by_name(
                db, account_id=str(account.id), name=update_data["name"]
            )
            if existing_workflow and existing_workflow.id != workflow_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Approval workflow with name '{update_data['name']}' already exists",
                )

        # Use CRUD layer for proper default workflow handling
        updated_workflow = crud_approval_workflow.update(
            db, db_obj=workflow, obj_in=workflow_update
        )

        log_config_change(
            db,
            user=current_user,
            config_type="approval_workflow",
            action="updated",
            old_value={"id": str(workflow_id), "name": workflow.name},
            new_value={"id": str(workflow_id), "name": updated_workflow.name},
        )

        logger.info(
            f"Updated approval workflow {workflow_id} for user {account.id} (is_default: {updated_workflow.is_default})"
        )

        return ApprovalWorkflowResponse.model_validate(updated_workflow)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(
            f"Error updating approval workflow {workflow_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating approval workflow: {str(e)}",
        )


@router.delete("/approval-workflows/{workflow_id}", status_code=status.HTTP_200_OK)
@require_permission("manage_approval_workflows")
async def delete_approval_workflow(
    workflow_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Delete an approval workflow.

    Note: This will set approval_workflow_id to NULL for any tool configurations
    using this workflow (due to ondelete="SET NULL").

    Args:
        workflow_id: Approval workflow ID
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: If workflow not found or deletion fails
    """
    workflow = crud_approval_workflow.get(
        db, id=workflow_id, account_id=str(account.id)
    )

    if not workflow:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval workflow not found or access denied",
        )

    try:
        # Count how many tool configurations use this workflow
        tool_count = crud_tool_configuration.count_by_workflow(
            db, workflow_id=str(workflow_id)
        )

        # Use CRUD layer for proper default workflow handling
        deleted_workflow = crud_approval_workflow.remove(
            db, id=workflow_id, account_id=str(account.id)
        )

        if not deleted_workflow:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Approval workflow not found or already deleted",
            )

        log_config_change(
            db,
            user=current_user,
            config_type="approval_workflow",
            action="deleted",
            old_value={"id": str(workflow_id), "name": workflow.name},
        )

        logger.info(
            f"Deleted approval workflow {workflow_id} (was used by {tool_count} tools) "
            f"for user {account.id}"
        )

        return {
            "message": f"Approval workflow deleted successfully. {tool_count} tool(s) were using this workflow."
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(
            f"Error deleting approval workflow {workflow_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting approval workflow: {str(e)}",
        )


# Tool Access Rule endpoints (replaces Tool Approval Condition endpoints)


@router.get(
    "/tool-configurations/{config_id}/approval-condition",
    response_model=ToolApprovalConditionResponse,
)
@require_permission("view_tools")
async def get_tool_approval_condition(
    config_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ToolApprovalConditionResponse:
    """Get the access rule (approval condition) for a tool configuration.

    This endpoint uses the new ToolAccessRule model (replaced ToolApprovalCondition).
    For backward compatibility, it returns the first 'require_approval' rule.

    Args:
        config_id: Tool configuration ID
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Tool access rule as approval condition response

    Raises:
        HTTPException: If tool configuration not found or no rules found
    """
    # Verify tool configuration exists and belongs to account
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found or access denied",
        )

    # Get first access rule (for backward compatibility)
    rule = crud_tool_access_rule.get_first_by_config(
        db, config_id=str(config_id), account_id=str(account.id)
    )

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No access rule found for this tool configuration",
        )

    # Map ToolAccessRule to ToolApprovalConditionResponse format
    return ToolApprovalConditionResponse(
        id=rule.id,
        account_id=rule.account_id,
        tool_configuration_id=rule.tool_configuration_id,
        name=rule.description,  # Map description -> name
        description=rule.description,
        is_enabled=rule.is_enabled,
        condition_type=rule.condition_type,
        condition_expression=rule.condition_expression,
        condition_config=None,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@router.put(
    "/tool-configurations/{config_id}/approval-condition",
    response_model=ToolApprovalConditionResponse,
)
@require_permission("manage_tools")
async def create_or_update_tool_approval_condition(
    config_id: UUID,
    condition_in: ToolApprovalConditionCreate,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ToolApprovalConditionResponse:
    """Create or update the access rule for a tool configuration.

    This endpoint uses the new ToolAccessRule model (replaced ToolApprovalCondition).
    For backward compatibility, it manages a single 'require_approval' rule per tool.

    Args:
        config_id: Tool configuration ID
        condition_in: Condition data
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Created or updated access rule as approval condition response

    Raises:
        HTTPException: If tool configuration not found or creation fails
    """
    # Verify tool configuration exists and belongs to account
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found or access denied",
        )

    # Validate CEL expression if provided (proprietary feature)
    if condition_in.condition_expression:
        try:
            # Test with empty args to check syntax
            evaluate_cel_expression(condition_in.condition_expression, {})
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid CEL expression: {str(e)}",
            )

    try:
        # Get existing rule or create new one
        existing_rule = crud_tool_access_rule.get_first_by_config(
            db, config_id=str(config_id), account_id=str(account.id)
        )

        if existing_rule:
            # Update existing rule
            rule = crud_tool_access_rule.update(
                db,
                db_obj=existing_rule,
                obj_in={
                    "description": condition_in.description or condition_in.name,
                    "is_enabled": condition_in.is_enabled,
                    "condition_type": condition_in.condition_type or "cel",
                    "condition_expression": condition_in.condition_expression,
                },
            )
        else:
            # Create new rule
            rule = crud_tool_access_rule.create(
                db,
                obj_in={
                    "tool_configuration_id": str(config_id),
                    "account_id": str(account.id),
                    "description": condition_in.description or condition_in.name,
                    "is_enabled": condition_in.is_enabled,
                    "condition_type": condition_in.condition_type or "cel",
                    "condition_expression": condition_in.condition_expression,
                    "action": "require_approval",
                    "priority": 0,
                },
            )

        logger.info(
            f"Created/updated access rule for tool config {config_id} "
            f"(account: {account.id})"
        )

        # Return in ToolApprovalConditionResponse format
        return ToolApprovalConditionResponse(
            id=rule.id,
            account_id=rule.account_id,
            tool_configuration_id=rule.tool_configuration_id,
            name=rule.description,
            description=rule.description,
            is_enabled=rule.is_enabled,
            condition_type=rule.condition_type,
            condition_expression=rule.condition_expression,
            condition_config=None,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    except Exception as e:
        db.rollback()
        logger.error(
            f"Error creating/updating approval condition for tool config {config_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating/updating approval condition: {str(e)}",
        )


@router.delete(
    "/tool-configurations/{config_id}/approval-condition",
    status_code=status.HTTP_200_OK,
)
@require_permission("manage_tools")
async def delete_tool_approval_condition(
    config_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Delete the access rules for a tool configuration.

    This endpoint uses the new ToolAccessRule model (replaced ToolApprovalCondition).
    It deletes all access rules for the specified tool configuration.

    Args:
        config_id: Tool configuration ID
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: If tool configuration not found or no rules found
    """
    # Verify tool configuration exists and belongs to account
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found or access denied",
        )

    # Check if any rules exist
    rules = crud_tool_access_rule.get_multi_by_config(
        db, config_id=str(config_id), account_id=str(account.id)
    )

    if not rules:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No access rules found for this tool configuration",
        )

    try:
        # Delete all access rules for this tool configuration
        deleted_count = crud_tool_access_rule.remove_by_config(
            db, config_id=str(config_id), account_id=str(account.id)
        )

        logger.info(
            f"Deleted {deleted_count} access rules for tool config {config_id} "
            f"(account: {account.id})"
        )

        return {"message": "Access rules deleted successfully"}

    except Exception as e:
        db.rollback()
        logger.error(
            f"Error deleting approval condition for tool config {config_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting approval condition: {str(e)}",
        )


@router.post(
    "/tool-configurations/{config_id}/approval-condition/test",
    response_model=ConditionTestResponse,
)
@require_permission("manage_tools")
async def test_approval_condition(
    config_id: UUID,
    test_request: ConditionTestRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ConditionTestResponse:
    """Test a CEL expression against sample arguments.

    This endpoint allows testing approval conditions before saving them.
    It's a proprietary feature for validating CEL expressions.

    Args:
        config_id: Tool configuration ID
        test_request: Test request with expression and sample args
        account: Current user's account (from dependency)
        db: Database session

    Returns:
        Test result with match status and evaluation context

    Raises:
        HTTPException: If tool configuration not found or evaluation fails
    """
    # Verify tool configuration exists and belongs to account
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found or access denied",
        )

    try:
        # Evaluate CEL expression
        matches = evaluate_cel_expression(
            test_request.expression, test_request.sample_args
        )

        return ConditionTestResponse(
            matches=matches,
            error=None,
            evaluation_context={
                "expression": test_request.expression,
                "sample_args": test_request.sample_args,
                "result": matches,
            },
        )

    except Exception as e:
        logger.warning(
            f"CEL expression evaluation failed for tool config {config_id}: {e}"
        )

        return ConditionTestResponse(
            matches=False,
            error=str(e),
            evaluation_context={
                "expression": test_request.expression,
                "sample_args": test_request.sample_args,
            },
        )


# ============================================================================
# Access Rule CRUD Endpoints
# ============================================================================


class AccessRuleCreate(BaseModel):
    """Schema for creating an access rule."""

    action: str = Field(..., description="Action: 'allow', 'deny', 'require_approval'")
    condition_expression: Optional[str] = Field(
        None, description="CEL or simple expression"
    )
    condition_type: str = Field("cel", description="Type: 'simple' or 'cel'")
    priority: int = Field(0, description="Evaluation order (lower = first)")
    description: Optional[str] = Field(
        None, description="Description or denial message"
    )
    is_enabled: bool = Field(True, description="Whether the rule is active")
    approval_workflow_id: Optional[str] = Field(
        None, description="Approval workflow ID (for 'require_approval' action)"
    )


class AccessRuleUpdate(BaseModel):
    """Schema for updating an access rule."""

    action: Optional[str] = None
    condition_expression: Optional[str] = None
    condition_type: Optional[str] = None
    priority: Optional[int] = None
    description: Optional[str] = None
    is_enabled: Optional[bool] = None
    approval_workflow_id: Optional[str] = None


class AccessRuleResponse(BaseModel):
    """Schema for access rule response."""

    id: str
    account_id: str
    tool_configuration_id: str
    action: str
    condition_expression: Optional[str]
    condition_type: str
    priority: int
    description: Optional[str]
    is_enabled: bool
    approval_workflow_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


@router.get(
    "/tool-configurations/{config_id}/access-rules",
    response_model=List[AccessRuleResponse],
)
@require_permission("view_tools")
async def list_access_rules(
    config_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[AccessRuleResponse]:
    """List all access rules for a tool configuration."""
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found",
        )

    rules = crud_tool_access_rule.get_multi_by_config(
        db, config_id=str(config_id), account_id=str(account.id)
    )

    return [
        AccessRuleResponse(
            id=str(r.id),
            account_id=str(r.account_id),
            tool_configuration_id=str(r.tool_configuration_id),
            action=r.action,
            condition_expression=r.condition_expression,
            condition_type=r.condition_type,
            priority=r.priority,
            description=r.description,
            is_enabled=r.is_enabled,
            approval_workflow_id=str(r.approval_workflow_id)
            if r.approval_workflow_id
            else None,
        )
        for r in rules
    ]


@router.post(
    "/tool-configurations/{config_id}/access-rules",
    response_model=AccessRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
@require_permission("manage_tools")
async def create_access_rule(
    config_id: UUID,
    rule_in: AccessRuleCreate,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AccessRuleResponse:
    """Create a new access rule for a tool configuration."""
    config = crud_tool_configuration.get(
        db, id=str(config_id), account_id=str(account.id)
    )
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool configuration not found",
        )

    if rule_in.action not in ("allow", "deny", "require_approval"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Action must be 'allow', 'deny', or 'require_approval'",
        )

    try:
        rule = crud_tool_access_rule.create(
            db,
            obj_in={
                "tool_configuration_id": str(config_id),
                "account_id": str(account.id),
                "action": rule_in.action,
                "condition_expression": rule_in.condition_expression,
                "condition_type": rule_in.condition_type,
                "priority": rule_in.priority,
                "description": rule_in.description,
                "is_enabled": rule_in.is_enabled,
                "approval_workflow_id": rule_in.approval_workflow_id,
            },
        )

        log_config_change(
            db,
            user=current_user,
            config_type="tool_rule",
            action="created",
            new_value={
                "id": str(rule.id),
                "tool_name": config.tool_name,
                "action": rule.action,
                "condition": rule.condition_expression,
            },
        )

        logger.info(
            f"Created access rule {rule.id} for tool config {config_id} "
            f"(account: {account.id})"
        )

        return AccessRuleResponse(
            id=str(rule.id),
            account_id=str(rule.account_id),
            tool_configuration_id=str(rule.tool_configuration_id),
            action=rule.action,
            condition_expression=rule.condition_expression,
            condition_type=rule.condition_type,
            priority=rule.priority,
            description=rule.description,
            is_enabled=rule.is_enabled,
            approval_workflow_id=str(rule.approval_workflow_id)
            if rule.approval_workflow_id
            else None,
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create access rule: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create access rule: {str(e)}",
        )


@router.put(
    "/access-rules/{rule_id}",
    response_model=AccessRuleResponse,
)
@require_permission("manage_tools")
async def update_access_rule(
    rule_id: UUID,
    rule_in: AccessRuleUpdate,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AccessRuleResponse:
    """Update an access rule."""
    rule = crud_tool_access_rule.get(db, id=rule_id, account_id=str(account.id))

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Access rule not found",
        )

    update_data = rule_in.model_dump(exclude_unset=True)
    if "action" in update_data and update_data["action"] not in (
        "allow",
        "deny",
        "require_approval",
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Action must be 'allow', 'deny', or 'require_approval'",
        )

    try:
        old_snapshot = {
            "id": str(rule.id),
            "action": rule.action,
            "condition": rule.condition_expression,
        }

        rule = crud_tool_access_rule.update(db, db_obj=rule, obj_in=update_data)

        log_config_change(
            db,
            user=current_user,
            config_type="tool_rule",
            action="updated",
            old_value=old_snapshot,
            new_value={
                "id": str(rule.id),
                "action": rule.action,
                "condition": rule.condition_expression,
            },
        )

        logger.info(f"Updated access rule {rule_id} (account: {account.id})")

        return AccessRuleResponse(
            id=str(rule.id),
            account_id=str(rule.account_id),
            tool_configuration_id=str(rule.tool_configuration_id),
            action=rule.action,
            condition_expression=rule.condition_expression,
            condition_type=rule.condition_type,
            priority=rule.priority,
            description=rule.description,
            is_enabled=rule.is_enabled,
            approval_workflow_id=str(rule.approval_workflow_id)
            if rule.approval_workflow_id
            else None,
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update access rule: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update access rule: {str(e)}",
        )


@router.delete(
    "/access-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@require_permission("manage_tools")
async def delete_access_rule(
    rule_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> None:
    """Delete an access rule."""
    rule = crud_tool_access_rule.get(db, id=rule_id, account_id=str(account.id))

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Access rule not found",
        )

    try:
        rule_snapshot = {
            "id": str(rule.id),
            "action": rule.action,
            "condition": rule.condition_expression,
        }

        crud_tool_access_rule.remove(db, id=str(rule_id), account_id=str(account.id))

        log_config_change(
            db,
            user=current_user,
            config_type="tool_rule",
            action="deleted",
            old_value=rule_snapshot,
        )

        logger.info(f"Deleted access rule {rule_id} (account: {account.id})")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete access rule: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete access rule: {str(e)}",
        )
