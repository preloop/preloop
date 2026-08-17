"""Approval wrapper for MCP tools with streaming progress support.

This module provides a decorator that wraps tool functions to add approval logic
while maintaining proper streaming of progress notifications to the MCP client.
"""

import asyncio
import logging
import os
from functools import wraps
from typing import Any, Callable, Optional

from fastmcp import Context

logger = logging.getLogger(__name__)


def with_approval(tool_func: Callable) -> Callable:
    """Decorator that adds approval flow to a tool function with streaming support.

    This decorator checks if the tool requires approval for the current user.
    If approval is required, it:
    1. Creates an approval request
    2. Sends notifications to the approval channel
    3. Streams progress updates to the MCP client via Context
    4. Waits for approval before executing the tool

    The key difference from the previous approach is that this runs at the tool
    function level where FastMCP's Context provides proper streaming support.

    Args:
        tool_func: The tool function to wrap

    Returns:
        Wrapped function with approval logic
    """

    @wraps(tool_func)
    async def wrapper(*args, ctx: Optional[Context] = None, **kwargs) -> Any:
        """Wrapper function that adds approval logic with streaming."""
        # Get tool name from function
        tool_name = tool_func.__name__

        # Get user context from the dynamic context variable
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            logger.warning(f"No user context available for tool {tool_name}")
            return "Error: No user context available"

        # Check if tool requires approval
        try:
            from preloop.models.db.session import get_async_db_session
            from preloop.models.crud.tool_configuration import (
                get_tool_config_by_name_and_source_async,
            )
            from preloop.models.crud.approval_workflow import (
                get_approval_workflow_async,
            )
            from preloop.services.policy_evaluator import evaluate_policy_async

            logger.info(
                f"Checking approval requirement for tool '{tool_name}' "
                f"(account_id={user_context.account_id})"
            )

            async with get_async_db_session() as db:
                # Check for tool configuration using CRUD
                config = await get_tool_config_by_name_and_source_async(
                    db,
                    account_id=user_context.account_id,
                    tool_name=tool_name,
                    tool_source="builtin",
                )

                # Evaluate workflow rules (ToolAccessRule) to determine action
                # This checks allow/deny/require_approval rules with conditions

                policy_decision = await evaluate_policy_async(
                    db=db,
                    tool_name=tool_name,
                    tool_args=kwargs,
                    account_id=user_context.account_id,
                    tool_configuration_id=config.id if config else None,
                    user_id=getattr(user_context, "user_id", None),
                    execution_id=getattr(user_context, "execution_id", None),
                )
                action, approval_workflow_id, rule_description = policy_decision

                logger.info(
                    f"Policy evaluation for tool '{tool_name}': "
                    f"action={action}, rule='{rule_description}'"
                )

                # Handle deny action - block execution
                if action == "deny":
                    logger.warning(
                        f"Tool {tool_name} execution DENIED by policy rule: {rule_description}"
                    )
                    return f"Tool execution denied: {rule_description}"

                # Handle allow action - execute directly
                if action == "allow":
                    logger.info(
                        f"Tool {tool_name} allowed by policy rule: {rule_description}"
                    )
                    return await tool_func(*args, **kwargs)

                # Handle require_approval action
                # Get approval workflow (from rule evaluation or tool config fallback)
                workflow_id = approval_workflow_id or (
                    config.approval_workflow_id if config else None
                )

                if not workflow_id:
                    # No approval workflow configured - fail closed (require approval by default)
                    logger.warning(
                        f"Tool {tool_name} requires approval but no workflow configured - failing closed"
                    )
                    return (
                        "Tool requires approval but no approval workflow is configured"
                    )

                # Get approval workflow using CRUD
                workflow = await get_approval_workflow_async(
                    db, workflow_id=workflow_id
                )

                if not workflow:
                    logger.error(
                        f"Approval workflow {workflow_id} not found for tool {tool_name}"
                    )
                    return f"Error: Approval workflow not found for tool '{tool_name}'"

                # Tool requires approval - handle it with streaming
                logger.info(
                    f"Tool {tool_name} requires approval - initiating approval flow with streaming"
                )

                # Create approval request
                from preloop.services.approval_service import ApprovalService
                from preloop.models.schemas.approval_request import (
                    ApprovalRequestUpdate,
                )

                base_url = os.getenv("PRELOOP_URL", "http://localhost:8000")
                approval_service = ApprovalService(db, base_url)

                try:
                    # Create approval request and send notification
                    approval_request = await approval_service.create_and_notify(
                        account_id=user_context.account_id,
                        tool_configuration_id=config.id,
                        approval_workflow=workflow,
                        tool_name=tool_name,
                        tool_args=kwargs,  # Use kwargs as tool arguments
                        agent_reasoning=None,
                        execution_id=None,
                        # getattr: a patched evaluator may return a plain
                        # tuple, and a missing snapshot must read as
                        # "not recorded" rather than raise here.
                        rule_context=getattr(policy_decision, "rule_context", None),
                    )

                    notification_channel = (
                        f"#{workflow.channel}"
                        if workflow.channel
                        else f"@{workflow.user}"
                        if workflow.user
                        else "webhook"
                    )

                    from preloop.utils.redaction import redact_for_log

                    logger.warning(
                        f"\n{'=' * 60}\n"
                        f"🚨 APPROVAL REQUIRED 🚨\n"
                        f"{'=' * 60}\n"
                        f"Tool: {tool_name}\n"
                        f"Arguments: {redact_for_log(kwargs)}\n"
                        f"Request ID: {approval_request.id}\n"
                        f"Notification sent to: {workflow.approval_type} ({notification_channel})\n"
                        f"Timeout: {workflow.timeout_seconds or 300}s\n"
                        f"Approval URL: [sent via notification]\n"
                        f"{'=' * 60}\n"
                        f"⏳ Waiting for approval (polling every 2s)..."
                    )

                    # Send initial notification via Context (FastMCP streaming)
                    if ctx:
                        try:
                            # Report progress at 0%
                            await ctx.report_progress(progress=0, total=100)
                            logger.info("✅ Sent initial progress via Context (0%)")
                        except Exception as e:
                            logger.warning(f"Could not send progress via Context: {e}")

                    # Polling loop with progress updates
                    poll_interval = 2.0
                    timeout_seconds = workflow.timeout_seconds or 300
                    elapsed = 0

                    while True:
                        # Check approval status with fresh database session
                        from preloop.models.crud.approval_request import (
                            get_approval_request_async,
                        )

                        async with get_async_db_session() as poll_db:
                            current_request = await get_approval_request_async(
                                poll_db, request_id=approval_request.id
                            )
                            # Extract needed fields before session closes to avoid DetachedInstanceError
                            current_status = (
                                current_request.status if current_request else None
                            )
                            current_comment = (
                                current_request.approver_comment
                                if current_request
                                else None
                            )

                        logger.info(
                            f"[Polling] Checked approval status: {current_status if current_status else 'NOT_FOUND'} "
                            f"(elapsed: {elapsed}s)"
                        )

                        if not current_request:
                            raise ValueError(
                                f"Approval request {approval_request.id} not found"
                            )

                        # Check if resolved
                        if current_status in [
                            "approved",
                            "declined",
                            "cancelled",
                        ]:
                            logger.info(
                                f"[Polling] ✅ Approval resolved with status: {current_status}"
                            )
                            final_status = current_status
                            final_comment = current_comment
                            break

                        # Check if expired
                        if elapsed >= timeout_seconds:
                            async with get_async_db_session() as update_db:
                                update_service = ApprovalService(update_db, base_url)
                                await update_service.update_approval_request(
                                    approval_request.id,
                                    ApprovalRequestUpdate(status="expired"),
                                )
                            raise TimeoutError(
                                f"Approval request {approval_request.id} expired without response"
                            )

                        # Send progress update every 10 seconds via Context
                        if ctx and int(elapsed) % 10 == 0 and elapsed > 0:
                            try:
                                progress_pct = int((elapsed / timeout_seconds) * 100)
                                remaining = timeout_seconds - elapsed

                                # Use Context.report_progress for streaming
                                await ctx.report_progress(
                                    progress=progress_pct, total=100
                                )
                                logger.info(
                                    f"[Polling] Sent progress via Context ({progress_pct}%, {int(remaining)}s remaining)"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to send progress update: {e}",
                                    exc_info=True,
                                )

                        # Wait before next poll
                        await asyncio.sleep(poll_interval)
                        elapsed += poll_interval

                    # Send completion notification
                    if ctx:
                        try:
                            await ctx.report_progress(progress=100, total=100)
                            logger.info(
                                "[Polling] Sent completion progress via Context (100%)"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to send completion notification: {e}",
                                exc_info=True,
                            )

                    # Check final status
                    if final_status == "declined":
                        logger.warning(f"Tool {tool_name} execution declined")
                        comment = f": {final_comment}" if final_comment else ""
                        return f"Tool execution declined{comment}"
                    elif final_status == "cancelled":
                        logger.warning(f"Tool {tool_name} execution cancelled")
                        return "Tool execution cancelled"
                    elif final_status != "approved":
                        logger.error(
                            f"Unexpected approval status for tool {tool_name}: {final_status}"
                        )
                        return f"Unexpected approval status: {final_status}"

                    # Approved! Continue with execution
                    logger.warning(
                        f"✅ Tool {tool_name} APPROVED - proceeding with execution"
                    )

                except TimeoutError as e:
                    logger.error(f"⏱️ Approval timeout for tool {tool_name}: {e}")
                    return f"Approval timeout: {str(e)}"
                except Exception as e:
                    logger.error(
                        f"Approval flow error for tool {tool_name}: {e}", exc_info=True
                    )
                    return f"Approval error: {str(e)}"

        except Exception as e:
            # SECURITY: Fail closed on errors - do not execute tool
            # If we can't determine whether approval is required, we must assume it is
            logger.error(
                f"Error checking approval requirement for {tool_name}: {e}",
                exc_info=True,
            )
            return (
                f"Tool execution blocked: Error evaluating access workflow - {str(e)}"
            )

        # This should not be reached - all paths above either return or raise
        logger.error(f"Unexpected code path reached for tool {tool_name}")
        return "Tool execution blocked: Unexpected error in approval flow"

    return wrapper
