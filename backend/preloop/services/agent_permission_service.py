"""Permission checks for onboarded agents' native tool calls.

This is the shared backend seam behind ``POST /api/v1/agents/permission-check``.
Onboarded agents (Claude Code via a PreToolUse hook, Codex via a
PermissionRequest hook, OpenClaw/Hermes via their runtime plugins) call the
endpoint before running a native/built-in tool. We reuse the existing
:class:`ApprovalService` pipeline (create + notify mobile/watch + decide) and
return a simple allow/deny the adapter maps back into the agent's hook format.

Policy (v1): honor the client's own decision. The adapter passes
``client_decision`` = what the agent would have done; only ``ask``/absent
escalates to a human. Central Preloop-side policy is a documented future phase.
"""

import asyncio
import logging
import uuid
from typing import Any, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.models.db.session import get_async_db_session
from preloop.services.approval_workflow_service import DEFAULT_APPROVAL_TYPE

logger = logging.getLogger(__name__)

# Tool source recorded for native agent tools, distinct from "mcp"/"builtin"
# so central-policy rules and analytics can target them later.
AGENT_TOOL_SOURCE = "agent"
AGENT_TOOL_APPROVALS_WORKFLOW_NAME = "Agent Tool Approvals"


async def _fetch_agent_tool_approvals_workflow(
    db: Any, account_id: str
) -> models.ApprovalWorkflow | None:
    """Return the auto-created agent-tool workflow for an account, if present."""
    result = await db.execute(
        select(models.ApprovalWorkflow)
        .where(
            models.ApprovalWorkflow.account_id == account_id,
            models.ApprovalWorkflow.name == AGENT_TOOL_APPROVALS_WORKFLOW_NAME,
        )
        .limit(1)
    )
    return result.scalars().first()


async def _resolve_workflow(
    db: Any, account_id: str, approver_user_id: Optional[uuid.UUID]
) -> models.ApprovalWorkflow:
    """Resolve the approval workflow to use, creating a minimal one if needed.

    Prefers the account default, then any existing workflow, then creates a
    dedicated "Agent Tool Approvals" standard workflow that notifies the
    calling user so the request can reach their devices.
    """
    result = await db.execute(
        select(models.ApprovalWorkflow)
        .where(
            models.ApprovalWorkflow.account_id == account_id,
            models.ApprovalWorkflow.is_default.is_(True),
        )
        .limit(1)
    )
    workflow = result.scalars().first()
    if workflow is not None:
        return workflow

    result = await db.execute(
        select(models.ApprovalWorkflow)
        .where(models.ApprovalWorkflow.account_id == account_id)
        .limit(1)
    )
    workflow = result.scalars().first()
    if workflow is not None:
        return workflow

    workflow = await _fetch_agent_tool_approvals_workflow(db, account_id)
    if workflow is not None:
        return workflow

    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name=AGENT_TOOL_APPROVALS_WORKFLOW_NAME,
        description="Auto-created for onboarded-agent native tool approvals",
        approval_type=DEFAULT_APPROVAL_TYPE,
        timeout_seconds=300,
        require_reason=False,
        async_approval_enabled=False,
        is_default=False,
        approver_user_ids=[str(approver_user_id)] if approver_user_id else [],
    )
    db.add(workflow)
    try:
        await db.commit()
        await db.refresh(workflow)
    except IntegrityError:
        await db.rollback()
        existing = await _fetch_agent_tool_approvals_workflow(db, account_id)
        if existing is None:
            raise
        return existing

    logger.info("Created default Agent Tool Approvals workflow %s", workflow.id)
    return workflow


async def _resolve_tool_config(
    db: Any, account_id: str, tool_name: str, workflow_id: uuid.UUID
) -> Any:
    """Get or create a ToolConfiguration row for this native tool."""
    from preloop.models.crud.tool_configuration import (
        get_tool_config_by_name_and_source_async,
        create_tool_configuration_async,
    )
    from preloop.models.schemas.tool_configuration import ToolConfigurationCreate

    config = await get_tool_config_by_name_and_source_async(
        db,
        account_id=account_id,
        tool_name=tool_name,
        tool_source=AGENT_TOOL_SOURCE,
    )
    if config is not None:
        return config

    return await create_tool_configuration_async(
        db,
        obj_in=ToolConfigurationCreate(
            tool_name=tool_name,
            tool_source=AGENT_TOOL_SOURCE,
            account_id=account_id,
            approval_workflow_id=workflow_id,
            is_enabled=True,
            custom_config={},
        ),
        account_id=account_id,
    )


async def request_agent_permission(
    *,
    base_url: str,
    account_id: str,
    user_id: Optional[uuid.UUID],
    managed_agent_id: Optional[uuid.UUID],
    runtime_session_id: Optional[uuid.UUID],
    managed_agent_name: Optional[str],
    source: Optional[str],
    tool_name: str,
    tool_input: Optional[dict],
    agent_reasoning: Optional[str],
    client_decision: Optional[str],
) -> Tuple[str, str, Optional[str]]:
    """Decide whether an agent's native tool call may proceed.

    When ``client_decision`` is ``allow`` or ``deny``, the client's own policy
    is honored immediately. Otherwise an approval request is created, human
    approvers are notified, and this function polls until decided or timed out.

    Args:
        base_url: Preloop base URL for approval links and notifications.
        account_id: Owning account id.
        user_id: User associated with the managed agent credential.
        managed_agent_id: Managed agent raising the request, if known.
        runtime_session_id: Active runtime session id, if known.
        managed_agent_name: Display name shown to approvers.
        source: Originating agent adapter (e.g. ``claude_code``).
        tool_name: Native tool name (e.g. ``Bash``).
        tool_input: Tool arguments persisted as approval ``tool_args``.
        agent_reasoning: Optional explanation shown to the approver.
        client_decision: Client policy outcome: ``allow``, ``deny``, or absent/
            ``ask`` to escalate to human approval.

    Returns:
        Tuple of ``(decision, reason, request_id)`` where ``decision`` is
        ``"allow"`` or ``"deny"`` and ``request_id`` is set when an approval
        row was created.
    """
    decision = (client_decision or "").strip().lower()
    if decision == "allow":
        return ("allow", "", None)
    if decision == "deny":
        return ("deny", "Denied by client policy", None)

    from preloop.services.approval_service import ApprovalService
    from preloop.models.crud.approval_request import get_approval_request_async

    async with get_async_db_session() as db:
        workflow = await _resolve_workflow(db, account_id, user_id)
        timeout_seconds = workflow.timeout_seconds or 300
        config = await _resolve_tool_config(db, account_id, tool_name, workflow.id)

        service = ApprovalService(db, base_url)
        approval = await service.create_and_notify(
            account_id=account_id,
            tool_configuration_id=config.id,
            approval_workflow=workflow,
            tool_name=tool_name,
            tool_args=tool_input or {},
            agent_reasoning=agent_reasoning,
            execution_id=None,
            user_id=user_id,
            managed_agent_id=managed_agent_id,
            runtime_session_id=runtime_session_id,
            managed_agent_name=managed_agent_name,
        )
        request_id = str(approval.id)
        status = approval.status
        comment = approval.approver_comment

    # AI-driven workflows resolve immediately inside create_and_notify.
    if status in ("approved", "declined", "cancelled", "expired"):
        return (
            "allow" if status == "approved" else "deny",
            comment or f"Approval {status}",
            request_id,
        )

    # Poll with a FRESH session each iteration so the external decide() commit
    # is observed (a single long-lived session would serve a stale identity-map
    # copy and never see the decision).
    poll_interval = 2.0
    elapsed = 0.0
    deadline = timeout_seconds + poll_interval
    while elapsed < deadline:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        async with get_async_db_session() as poll_db:
            req = await get_approval_request_async(
                poll_db, request_id=uuid.UUID(request_id)
            )
            if req is None:
                return ("deny", "Approval request not found", request_id)
            status = req.status
            comment = req.approver_comment
        if status == "approved":
            return ("allow", comment or "", request_id)
        if status in ("declined", "cancelled", "expired"):
            return ("deny", comment or f"Approval {status}", request_id)

    return ("deny", "Approval request timed out", request_id)
