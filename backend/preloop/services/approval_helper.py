"""Reusable approval helper with streaming progress support.

This module provides a helper function for checking and waiting for tool approval
with real-time progress updates via FastMCP Context.
"""

import asyncio
import logging
import os
from contextvars import ContextVar
from typing import Any, Dict, NamedTuple, Optional, Tuple

from fastmcp import Context
from preloop.models import models

logger = logging.getLogger(__name__)

#: Audit metadata of the most recent approval request this task created and
#: saw resolved. ``ask_user`` consumes it to stamp the platform approval id
#: (and the approver identity captured by the approval workflow) into its
#: returned answer, so agents transcribing human decisions — e.g. waiver
#: collection in the security-audit presets — can record WHICH governed
#: approval produced the answer without needing any extra tool.
_last_approval_meta_var: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "last_approval_meta", default=None
)


def _set_last_approval_meta(
    request_id: Any,
    status: str,
    resolved_at: Any = None,
    responded_by: Optional[str] = None,
) -> None:
    """Record the approval audit metadata for the current task."""
    _last_approval_meta_var.set(
        {
            "request_id": str(request_id),
            "status": status,
            "resolved_at": (
                resolved_at.isoformat()
                if hasattr(resolved_at, "isoformat")
                else resolved_at
            ),
            "responded_by": responded_by,
        }
    )


def consume_last_approval_meta() -> Optional[Dict[str, Any]]:
    """Return and clear the last approval audit metadata, if any."""
    meta = _last_approval_meta_var.get(None)
    _last_approval_meta_var.set(None)
    return meta


#: Builtin tools whose approval request is a *question to the human* rather
#: than a gate on a side-effecting tool. These get in-session delivery via
#: Agent Control (issue #130) in addition to the remote approval channels.
_QUESTION_TOOLS = frozenset({"ask_user", "request_approval"})


class _InBandSessionContext(NamedTuple):
    """Session identity used to route in-band question delivery."""

    runtime_session_id: Optional[str]
    managed_agent_id: Optional[str]
    user_id: Optional[str]


def _session_context_for_in_band() -> _InBandSessionContext:
    """Best-effort lookup of the asking session's identity.

    Reads the MCP request's user context when available. Returns all-``None``
    for non-MCP callers (flows, tests) — in-band delivery simply degrades to
    the remote channels.
    """
    empty = _InBandSessionContext(None, None, None)
    try:
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()
    except Exception:  # pragma: no cover - context lookup is optional
        return empty
    if not user_context:
        return empty
    return _InBandSessionContext(
        runtime_session_id=getattr(user_context, "runtime_session_id", None),
        managed_agent_id=getattr(user_context, "managed_agent_id", None),
        user_id=getattr(user_context, "user_id", None),
    )


async def _deliver_question_in_band(
    *,
    tool_name: str,
    arguments: dict,
    account_id: str,
    approval_request: "models.ApprovalRequest",
    base_url: str,
) -> bool:
    """Best-effort in-session delivery of a pending question (issue #130).

    Never raises and never blocks the approval flow; remote channels have
    already been notified when this runs.
    """
    try:
        from preloop.services.ask_user_inband import (
            approval_console_url,
            approval_mobile_deep_link,
            deliver_question_to_session,
        )

        session_ctx = _session_context_for_in_band()
        if not session_ctx.runtime_session_id and not session_ctx.managed_agent_id:
            return False
        return await deliver_question_to_session(
            account_id=str(account_id),
            approval_request_id=approval_request.id,
            tool_name=tool_name,
            arguments=arguments,
            console_url=approval_console_url(base_url, approval_request.id),
            mobile_link=approval_mobile_deep_link(approval_request.id),
            runtime_session_id=session_ctx.runtime_session_id,
            managed_agent_id=session_ctx.managed_agent_id,
            user_id=session_ctx.user_id,
            expires_at=getattr(approval_request, "expires_at", None),
        )
    except Exception:
        logger.warning(
            "In-band question delivery failed for approval %s",
            getattr(approval_request, "id", None),
            exc_info=True,
        )
        return False


async def require_approval(
    tool_name: str,
    tool_source: str,
    account_id: str,
    arguments: dict,
    ctx: Optional[Context] = None,
    workflow_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    justification: Optional[str] = None,
    return_comment_on_approve: bool = False,
    rule_context: Optional[dict] = None,
) -> Tuple[bool, str]:
    """Check if tool requires approval and wait for decision with streaming.

    This function checks ToolConfiguration to see if the tool requires approval.
    If approval is required, it creates an approval request, sends notifications,
    and polls for approval status while streaming progress updates via Context.

    Args:
        tool_name: Name of the tool being executed
        tool_source: Tool source type ("builtin" or "mcp")
        account_id: Account ID of the user executing the tool
        arguments: Tool arguments
        ctx: FastMCP Context for streaming progress updates
        workflow_id: Optional approval workflow ID. If provided, uses this workflow directly
                  instead of looking up from tool configuration. Useful for standalone
                  approval requests where no tool configuration exists.
        correlation_id: Optional correlation ID for grouping related audit events.
        justification: Optional justification text provided by the agent explaining
                      why this tool is being called. Injected by DynamicFastMCP when
                      justification_mode is configured on the tool.
        rule_context: Snapshot of the policy rule that demanded this approval
                      (see services/approval_rule_context.py). When omitted,
                      falls back to whatever DynamicFastMCP's central policy
                      evaluation stored in ``_rule_context_var``, and finally to
                      the rule this function matches itself in the legacy
                      inline path below. None means no rule was evaluated (e.g.
                      the request_approval builtin) and surfaces omit the
                      explanation rather than invent one.

    Returns:
        Tuple of (approved: bool, error_message: str)
        - If approved: (True, "")
        - If declined/error: (False, "error message")
    """
    # Scope the approval audit metadata to THIS call: every `require_approval`
    # sets the ContextVar but only `ask_user` consumes it, and many paths
    # below return without producing fresh metadata (allow/deny rules,
    # workflow-not-found, async approval, poll-loop not-found). Clearing up
    # front guarantees a later consumer can never read a stale approval id
    # left behind by a previous call in the same task context.
    _last_approval_meta_var.set(None)
    try:
        # Check if approval should be bypassed (e.g. during async re-execution
        # of an already-approved tool call).
        from preloop.services.dynamic_fastmcp import (
            _approved_comment_var,
            _bypass_approval_var,
        )

        if _bypass_approval_var.get(False):
            logger.info(f"Bypassing approval for '{tool_name}' (async re-execution)")
            if return_comment_on_approve:
                # ask_user consumes the approver comment as the human's
                # answer; during replay the original decision's comment is
                # carried in _approved_comment_var (set by
                # get_approval_status) so the answer is not lost.
                return (True, _approved_comment_var.get(None) or "")
            return (True, "")

        from preloop.models.db.session import get_async_db_session
        from preloop.models.crud.tool_configuration import (
            get_tool_config_by_name_and_source_async,
        )
        from preloop.models.crud.approval_workflow import get_approval_workflow_async
        from preloop.services.approval_rule_context import (
            SOURCE_TOOL_ACCESS_RULE,
            SOURCE_TOOL_DEFAULT_WORKFLOW,
            build_rule_context,
        )

        # DynamicFastMCP's central policy evaluation runs before the tool
        # wrapper and already knows which rule fired; prefer that over
        # anything we rediscover here, and never overwrite an explicit
        # argument.
        if rule_context is None:
            try:
                from preloop.services.dynamic_fastmcp import _rule_context_var

                rule_context = _rule_context_var.get(None)
            except Exception:  # pragma: no cover - contextvar lookup is optional
                rule_context = None

        logger.info(
            f"Checking approval requirement for {tool_source} tool '{tool_name}' "
            f"(account_id={account_id})"
        )

        async with get_async_db_session() as db:
            # Always try to look up tool configuration first
            config = await get_tool_config_by_name_and_source_async(
                db,
                account_id=account_id,
                tool_name=tool_name,
                tool_source=tool_source,
            )

            # If workflow_id is provided directly (for standalone requests), use it
            if workflow_id:
                logger.info(
                    f"Using explicitly provided workflow_id={workflow_id} for tool {tool_name}"
                )
                workflow = await get_approval_workflow_async(
                    db, workflow_id=workflow_id
                )

                if not workflow:
                    logger.error(f"Provided approval workflow {workflow_id} not found")
                    return (
                        False,
                        f"Error: Approval workflow with ID '{workflow_id}' not found",
                    )

                # If no config exists, this is a standalone approval request
                # Create a tool config for tracking purposes and persist it
                if not config:
                    logger.warning(
                        f"No tool configuration found for {tool_name} ({tool_source}), "
                        "creating config for approval tracking"
                    )
                    from preloop.models.crud.tool_configuration import (
                        create_tool_configuration_async,
                    )
                    from preloop.models.schemas.tool_configuration import (
                        ToolConfigurationCreate,
                    )

                    # Create and persist a minimal tool config
                    config_create = ToolConfigurationCreate(
                        tool_name=tool_name,
                        tool_source=tool_source,
                        account_id=account_id,
                        approval_workflow_id=workflow_id,
                        is_enabled=True,
                        custom_config={},
                    )
                    config = await create_tool_configuration_async(
                        db, obj_in=config_create, account_id=account_id
                    )
                    logger.info(f"Created tool configuration {config.id} for approval")
            else:
                # Evaluate approval requirement with condition checking

                # Convert async db to sync for the evaluator (it uses sync queries)
                # We'll need to fetch the config and evaluate in the async context
                if not config or not config.approval_workflow_id:
                    logger.info(
                        f"Tool {tool_name} ({tool_source}) does not require approval (no workflow configured)"
                    )
                    return (True, "")

                # Check if there are access rules that might override approval requirement
                from sqlalchemy import select

                access_rules_result = await db.execute(
                    select(models.ToolAccessRule)
                    .where(
                        models.ToolAccessRule.tool_configuration_id == config.id,
                        models.ToolAccessRule.account_id == account_id,
                        models.ToolAccessRule.is_enabled.is_(True),
                    )
                    .order_by(models.ToolAccessRule.priority)
                )
                access_rules = list(access_rules_result.scalars())

                logger.info(f"Access rules found: {len(access_rules)}")

                matched_require_approval = False
                for rule in access_rules:
                    logger.info(
                        f"  - rule={rule.id}, action={rule.action}, "
                        f"type={rule.condition_type}, expr={rule.condition_expression}"
                    )

                    if rule.condition_expression:
                        # Evaluate the condition expression
                        try:
                            from preloop.plugins.builtin.argument_evaluator import (
                                ArgumentEvaluator,
                            )

                            evaluator = ArgumentEvaluator()

                            # Normalise expression: prepend 'args.' if missing.
                            # Users configure rules with plain field names,
                            # e.g. "amount > 300" instead of "args.amount > 300".
                            expression = rule.condition_expression.strip()
                            if not expression.startswith("args."):
                                expression = f"args.{expression}"

                            eval_context = {
                                "tool_name": tool_name,
                                "args": arguments,
                                "user_id": str(ctx.request_context.user_context.user_id)
                                if hasattr(ctx, "request_context")
                                and hasattr(ctx.request_context, "user_context")
                                else None,
                                "account_id": str(account_id),
                                "execution_id": None,
                                "trigger_event": {},
                            }

                            matches = await evaluator.evaluate(
                                condition_config={"expression": expression},
                                tool_args=arguments,
                                context=eval_context,
                            )

                            logger.info(f"  - Evaluation result: {matches}")

                            if not matches:
                                continue  # Rule condition didn't match, try next rule
                        except Exception as e:
                            logger.warning(
                                f"Failed to evaluate access rule for {tool_name}: {e}. Skipping rule."
                            )
                            continue
                    # Rule matched (or has no condition expression — unconditional)
                    if rule.action == "allow":
                        logger.info(
                            f"Tool {tool_name} ({tool_source}) allowed by access rule (no approval needed)"
                        )
                        return (True, "")
                    elif rule.action == "deny":
                        logger.info(
                            f"Tool {tool_name} ({tool_source}) denied by access rule"
                        )
                        return (False, f"Tool '{tool_name}' is denied by access rule")
                    elif rule.action == "require_approval":
                        matched_require_approval = True
                        # Record WHICH rule gated the call. Only when the
                        # central evaluator did not already hand us one: it
                        # sees the same rules with more context (subject
                        # scoping, also-matched rules) and is authoritative.
                        if rule_context is None:
                            rule_context = build_rule_context(
                                source=SOURCE_TOOL_ACCESS_RULE,
                                decision="require_approval",
                                rule_id=rule.id,
                                rule_name=rule.description,
                                expression=rule.condition_expression,
                                expression_type=rule.condition_type,
                                priority=rule.priority,
                                tool_configuration_id=config.id,
                            )
                        break  # Proceed to approval flow below

                # If access rules exist but none matched, default to allow.
                # This is consistent with workflow_evaluator.py's behavior.
                if access_rules and not matched_require_approval:
                    logger.info(
                        f"Tool {tool_name} ({tool_source}): {len(access_rules)} "
                        f"access rules exist but none matched — default allow"
                    )
                    return (True, "")

                if not access_rules and rule_context is None:
                    # No rules exist at all: the tool config itself pins an
                    # approval workflow, so every call is gated whatever the
                    # arguments are. Say that plainly rather than leaving the
                    # approver to guess which rule they cannot find.
                    rule_context = build_rule_context(
                        source=SOURCE_TOOL_DEFAULT_WORKFLOW,
                        decision="require_approval",
                        rule_name="Tool default policy",
                        tool_configuration_id=config.id,
                    )

                # Get approval workflow from tool configuration
                workflow = await get_approval_workflow_async(
                    db, workflow_id=config.approval_workflow_id
                )

                if not workflow:
                    logger.error(
                        f"Approval workflow {config.approval_workflow_id} not found for tool {tool_name}"
                    )
                    return (
                        False,
                        f"Error: Approval workflow not found for tool '{tool_name}'",
                    )

            # Tool requires approval - handle it with streaming
            logger.info(
                f"Tool {tool_name} ({tool_source}) requires approval - initiating approval flow with streaming"
            )

            # Create approval request
            from preloop.services.approval_service import ApprovalService
            from preloop.models.schemas.approval_request import ApprovalRequestUpdate

            base_url = os.getenv("PRELOOP_URL", "http://localhost:8000")
            approval_service = ApprovalService(db, base_url)

            try:
                # Create approval request and send notification
                approval_request = await approval_service.create_and_notify(
                    account_id=account_id,
                    tool_configuration_id=config.id,
                    approval_workflow=workflow,
                    tool_name=tool_name,
                    tool_args=arguments,
                    agent_reasoning=justification,
                    execution_id=None,
                    rule_context=rule_context,
                )

                # Derive notification channel from approval_type
                notification_channels = (
                    [workflow.approval_type] if workflow.approval_type else ["manual"]
                )
                channels_display = ", ".join(notification_channels)

                from preloop.utils.redaction import redact_for_log

                logger.warning(
                    f"\n{'=' * 60}\n"
                    f"🚨 APPROVAL REQUIRED ({tool_source.upper()} Tool) 🚨\n"
                    f"{'=' * 60}\n"
                    f"Tool: {tool_name}\n"
                    f"Arguments: {redact_for_log(arguments)}\n"
                    f"Request ID: {approval_request.id}\n"
                    f"Notification sent via: {channels_display}\n"
                    f"Approval type: {workflow.approval_type}\n"
                    f"Timeout: {workflow.timeout_seconds or 300}s\n"
                    f"Approval URL: [sent via notification]\n"
                    f"{'=' * 60}\n"
                    f"⏳ Waiting for approval (polling every 2s)..."
                )

                # Record initial events in the event log
                from preloop.models.models.approval_event import (
                    ApprovalEvent as ApprovalEventModel,
                )

                async with get_async_db_session() as event_db:
                    # Record approval request creation
                    event_db.add(
                        ApprovalEventModel(
                            approval_request_id=approval_request.id,
                            account_id=account_id,
                            event_type="approval_requested",
                            detail=f"Approval request created for tool '{tool_name}'",
                        )
                    )
                    # Record notification events
                    for channel in notification_channels:
                        event_db.add(
                            ApprovalEventModel(
                                approval_request_id=approval_request.id,
                                account_id=account_id,
                                event_type="notification_sent",
                                detail=f"Notification sent via {channel}",
                            )
                        )
                    await event_db.commit()

                # In-session (in-band) delivery for question-style approvals:
                # if the asking session's runtime has a live Agent Control
                # connection, surface the question as an audited turn in that
                # session so the human does not have to switch devices. Best
                # effort; the remote channels above remain authoritative and
                # first-answer-wins is enforced on the approval record.
                delivered_in_band = False
                if tool_name in _QUESTION_TOOLS:
                    delivered_in_band = await _deliver_question_in_band(
                        tool_name=tool_name,
                        arguments=arguments,
                        account_id=account_id,
                        approval_request=approval_request,
                        base_url=base_url,
                    )
                    if delivered_in_band:
                        async with get_async_db_session() as event_db:
                            event_db.add(
                                ApprovalEventModel(
                                    approval_request_id=approval_request.id,
                                    account_id=account_id,
                                    event_type="notification_sent",
                                    detail=(
                                        "Question delivered in-session via "
                                        "agent control channel"
                                    ),
                                )
                            )
                            await event_db.commit()

                # Check if async approval mode is enabled
                if getattr(workflow, "async_approval_enabled", False):
                    import json

                    logger.info(
                        f"Async approval enabled for workflow '{workflow.name}' - "
                        f"returning immediately with polling instructions"
                    )

                    # Build approver display names
                    approver_display = []
                    if workflow.approver_user_ids:
                        # Try to resolve user emails
                        try:
                            from sqlalchemy import select
                            from preloop.models.models.user import User

                            async with get_async_db_session() as user_db:
                                users_result = await user_db.execute(
                                    select(User).where(
                                        User.id.in_(workflow.approver_user_ids)
                                    )
                                )
                                for u in users_result.scalars():
                                    approver_display.append(u.email or u.username)
                        except Exception:
                            approver_display = [
                                str(uid) for uid in workflow.approver_user_ids
                            ]

                    poll_interval = min(15, (workflow.timeout_seconds or 300) // 20)
                    poll_interval = max(5, poll_interval)  # At least 5 seconds

                    from preloop.services.ask_user_inband import (
                        approval_console_url,
                        approval_mobile_deep_link,
                    )

                    # Token-free deep links to THIS question (issue #130).
                    # Safe to show to the agent: acting on them requires an
                    # authenticated console/mobile session, so they cannot be
                    # used to self-approve (unlike the tokenized approval_url,
                    # which stays notification-only — see NOTE below).
                    console_url = approval_console_url(base_url, approval_request.id)
                    mobile_link = approval_mobile_deep_link(approval_request.id)

                    async_response = {
                        "status": "pending_approval",
                        "request_id": str(approval_request.id),
                        "message": (
                            f"This tool call triggered approval workflow '{workflow.name}'. "
                            f"Approval request has been sent to {', '.join(approver_display) if approver_display else 'configured approvers'} "
                            f"via {channels_display}. "
                            f"If the user is present, show them this link to answer directly: "
                            f"{console_url} (mobile: {mobile_link}). "
                            f"Poll the approval status by calling get_approval_status(request_id='{approval_request.id}') "
                            f"every {poll_interval} seconds for up to {workflow.timeout_seconds or 300} seconds. "
                            f"When the status is 'approved', the response will include the tool execution result. "
                            f"When the status is 'declined' or 'expired', stop polling and inform the user."
                        ),
                        "approval_console_url": console_url,
                        "approval_mobile_link": mobile_link,
                        "poll_interval_seconds": poll_interval,
                        "timeout_seconds": workflow.timeout_seconds or 300,
                        "channels": notification_channels,
                        # NOTE: the tokenized approval_url stays intentionally
                        # excluded from the agent-visible response. That URL
                        # contains a bearer token; exposing it would let the
                        # agent self-approve. It is delivered only via trusted
                        # notification channels (email, Slack, mobile push,
                        # web UI). The console/mobile links above carry no
                        # token and require an authenticated human session.
                    }
                    if approver_display:
                        async_response["approvers"] = approver_display

                    return (False, json.dumps(async_response))

                # Send initial notification via Context (FastMCP streaming)
                if ctx:
                    try:
                        logger.debug(
                            f"Context: has report_progress={hasattr(ctx, 'report_progress')}"
                        )
                        # Report progress at 0% with status message. Include
                        # the token-free console deep link so an interactive
                        # user watching the stream can answer without
                        # switching devices (issue #130).
                        from preloop.services.ask_user_inband import (
                            approval_console_url,
                        )

                        status_message = (
                            f"Approval request sent via {channels_display} — "
                            f"answer at "
                            f"{approval_console_url(base_url, approval_request.id)}"
                        )
                        # Check if progressToken is available (do not log - sensitive)
                        progress_token = None
                        try:
                            progress_token = (
                                ctx.request_context.meta.progressToken
                                if ctx.request_context.meta
                                else None
                            )
                        except Exception as e:
                            logger.error(f"Error getting progressToken: {e}")

                        # Only send progress notification if we have a valid progressToken
                        if progress_token is not None:
                            # Try to send directly via session to debug
                            try:
                                await ctx.session.send_progress_notification(
                                    progress_token=progress_token,
                                    progress=0.0,
                                    total=100.0,
                                    message=status_message,
                                    related_request_id=ctx.request_id,
                                )
                                logger.info(
                                    "✅ DIRECT send_progress_notification succeeded"
                                )
                            except Exception as e:
                                logger.error(
                                    f"❌ DIRECT send_progress_notification failed: {e}",
                                    exc_info=True,
                                )

                        result = await ctx.report_progress(
                            progress=0, total=100, message=status_message
                        )
                        logger.info(
                            f"✅ Sent initial progress via ctx.report_progress: {status_message}, result: {result}"
                        )
                        logger.info(
                            f"   Context session: {ctx.session}, request_id: {ctx.request_id}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Could not send progress via Context: {e}", exc_info=True
                        )
                else:
                    logger.warning(
                        "❌ No Context available - cannot send progress notifications!"
                    )

                # Polling loop with progress updates
                poll_interval = 2.0
                timeout_seconds = workflow.timeout_seconds or 300
                elapsed = 0
                escalation_triggered = False

                # Check if escalation is configured
                has_escalation = bool(
                    workflow.escalation_user_ids or workflow.escalation_team_ids
                )
                logger.info(
                    f"[Polling] Escalation configured: {has_escalation}, "
                    f"escalation_user_ids={workflow.escalation_user_ids}, "
                    f"escalation_team_ids={workflow.escalation_team_ids}"
                )

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
                        current_resolved_at = (
                            current_request.resolved_at if current_request else None
                        )
                        current_responses = (
                            list(current_request.responses or [])
                            if current_request
                            else []
                        )
                        current_responded_by = None
                        if current_status in ["approved", "declined", "cancelled"]:
                            responded_by_id = next(
                                (
                                    vote.get("user_id")
                                    for vote in reversed(current_responses)
                                    if isinstance(vote, dict) and vote.get("user_id")
                                ),
                                None,
                            )
                            if responded_by_id:
                                # Resolve to a human-meaningful identity (the
                                # async-approval path does the same): the
                                # trailer's answered_by lands verbatim in
                                # audit evidence such as waiver registers,
                                # where a raw user-id UUID helps nobody. Fall
                                # back to the raw id when resolution fails.
                                current_responded_by = str(responded_by_id)
                                try:
                                    import uuid as uuid_module

                                    from preloop.models.models.user import User

                                    responded_user = await poll_db.get(
                                        User, uuid_module.UUID(str(responded_by_id))
                                    )
                                    if responded_user:
                                        current_responded_by = (
                                            responded_user.email
                                            or responded_user.username
                                            or current_responded_by
                                        )
                                except Exception:
                                    pass

                    logger.info(
                        f"[Polling] Checked approval status: {current_status if current_status else 'NOT_FOUND'} "
                        f"(elapsed: {elapsed}s, escalation_triggered: {escalation_triggered})"
                    )

                    if not current_request:
                        return (
                            False,
                            f"Error: Approval request {approval_request.id} not found",
                        )

                    # Check if resolved
                    if current_status in ["approved", "declined", "cancelled"]:
                        logger.info(
                            f"[Polling] ✅ Approval resolved with status: {current_status}"
                        )
                        final_status = current_status
                        final_comment = current_comment
                        _set_last_approval_meta(
                            approval_request.id,
                            current_status,
                            resolved_at=current_resolved_at,
                            responded_by=current_responded_by,
                        )
                        break

                    # Check if initial timeout expired
                    if elapsed >= timeout_seconds and not escalation_triggered:
                        # Check if we should escalate
                        if has_escalation:
                            # Use row-level locking to prevent duplicate escalation
                            # when multiple workers are polling the same request
                            async with get_async_db_session() as escalation_db:
                                from datetime import datetime, timedelta
                                from preloop.models.crud.approval_request import (
                                    get_approval_request_for_update_async,
                                )

                                # Lock the row and check if already escalated
                                fresh_request = (
                                    await get_approval_request_for_update_async(
                                        escalation_db, request_id=approval_request.id
                                    )
                                )

                                if not fresh_request:
                                    logger.warning(
                                        f"[Polling] Request {approval_request.id} not found during escalation"
                                    )
                                    escalation_triggered = True
                                    continue

                                # Check if another worker already escalated
                                if fresh_request.escalation_triggered_at is not None:
                                    logger.info(
                                        f"[Polling] Escalation already triggered by another worker at "
                                        f"{fresh_request.escalation_triggered_at}"
                                    )
                                    escalation_triggered = True
                                    # Reset elapsed based on when escalation was triggered
                                    elapsed = 0
                                    continue

                                logger.info(
                                    f"[Polling] Initial timeout reached, triggering escalation for request {approval_request.id}"
                                )
                                escalation_triggered = True

                                # Mark escalation as triggered and extend timeout
                                fresh_request.escalation_triggered_at = (
                                    datetime.utcnow()
                                )
                                new_expires_at = datetime.utcnow() + timedelta(
                                    seconds=timeout_seconds
                                )
                                fresh_request.expires_at = new_expires_at
                                await escalation_db.commit()

                                escalation_service = ApprovalService(
                                    escalation_db, base_url
                                )

                                # Send escalation notifications
                                await escalation_service._send_escalation_notifications(
                                    fresh_request, workflow
                                )

                                # Broadcast escalation event
                                await escalation_service._broadcast_approval_update(
                                    fresh_request,
                                    "escalated",
                                    extra_data={
                                        "new_expires_at": new_expires_at.isoformat()
                                    },
                                )

                                logger.info(
                                    f"[Polling] Escalation triggered, new timeout: {new_expires_at}"
                                )

                            # Send escalation notification via Context (outside DB transaction)
                            if ctx:
                                try:
                                    escalation_message = "Escalating approval request - notifying escalation contacts"
                                    await ctx.report_progress(
                                        progress=50,
                                        total=100,
                                        message=escalation_message,
                                    )
                                    logger.info(
                                        f"[Polling] Sent escalation notification: {escalation_message}"
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Failed to send escalation notification: {e}",
                                        exc_info=True,
                                    )

                            # Reset elapsed for escalation period
                            elapsed = 0
                            continue

                        else:
                            # No escalation configured - expire the request
                            if ctx:
                                try:
                                    timeout_message = f"Approval request timed out after {timeout_seconds}s"
                                    await ctx.report_progress(
                                        progress=100, total=100, message=timeout_message
                                    )
                                    logger.info(
                                        f"[Polling] Sent timeout notification: {timeout_message}"
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Failed to send timeout notification: {e}",
                                        exc_info=True,
                                    )

                            async with get_async_db_session() as update_db:
                                update_service = ApprovalService(update_db, base_url)
                                await update_service.update_approval_request(
                                    approval_request.id,
                                    ApprovalRequestUpdate(status="expired"),
                                )
                            _set_last_approval_meta(approval_request.id, "expired")
                            return (
                                False,
                                f"Approval timeout: request expired after {timeout_seconds}s",
                            )

                    # Check if escalation timeout expired (after escalation was triggered)
                    if elapsed >= timeout_seconds and escalation_triggered:
                        # Final timeout after escalation
                        total_timeout = timeout_seconds * 2
                        if ctx:
                            try:
                                timeout_message = f"Approval request timed out after {total_timeout}s (including escalation period)"
                                await ctx.report_progress(
                                    progress=100, total=100, message=timeout_message
                                )
                                logger.info(
                                    f"[Polling] Sent final timeout notification: {timeout_message}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to send timeout notification: {e}",
                                    exc_info=True,
                                )

                        async with get_async_db_session() as update_db:
                            update_service = ApprovalService(update_db, base_url)
                            await update_service.update_approval_request(
                                approval_request.id,
                                ApprovalRequestUpdate(status="expired"),
                            )
                        _set_last_approval_meta(approval_request.id, "expired")
                        return (
                            False,
                            f"Approval timeout: request expired after {total_timeout}s (including escalation)",
                        )

                    # Send progress update every 10 seconds via Context
                    if ctx and int(elapsed) % 10 == 0 and elapsed > 0:
                        try:
                            progress_pct = int((elapsed / timeout_seconds) * 100)
                            remaining = timeout_seconds - elapsed

                            # Create meaningful status message
                            status_message = (
                                f"Waiting for approval via {channels_display} "
                                f"({int(remaining)}s remaining)"
                            )

                            # Use Context.report_progress for streaming
                            await ctx.report_progress(
                                progress=progress_pct, total=100, message=status_message
                            )
                            logger.info(
                                f"[Polling] Sent progress: {progress_pct}% - {status_message}"
                            )
                        except Exception as e:
                            # Ignore ClosedResourceError - client may have disconnected
                            from anyio import ClosedResourceError

                            if isinstance(e, ClosedResourceError):
                                logger.debug(
                                    "Client disconnected, skipping progress updates"
                                )
                            else:
                                logger.error(
                                    f"Failed to send progress update: {e}",
                                    exc_info=True,
                                )

                    # Wait before next poll
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval

                # Send completion notification with final status
                if ctx:
                    try:
                        # Determine final status message
                        if final_status == "approved":
                            status_message = "Approved"
                            if final_comment:
                                status_message += f": {final_comment}"
                        elif final_status == "declined":
                            status_message = "Declined"
                            if final_comment:
                                status_message += f": {final_comment}"
                        elif final_status == "cancelled":
                            status_message = "Request cancelled"
                        else:
                            status_message = f"Unexpected status: {final_status}"

                        await ctx.report_progress(
                            progress=100, total=100, message=status_message
                        )
                        logger.info(
                            f"[Polling] Sent completion: 100% - {status_message}"
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
                    return (False, f"Tool execution declined{comment}")
                elif final_status == "cancelled":
                    logger.warning(f"Tool {tool_name} execution cancelled")
                    return (False, "Tool execution cancelled")
                elif final_status != "approved":
                    logger.error(
                        f"Unexpected approval status for tool {tool_name}: {final_status}"
                    )
                    return (
                        False,
                        f"Unexpected approval status: {final_status}",
                    )

                # Approved! Continue with execution. When the caller asked for
                # it (ask_user, where the "comment" is the human's answer),
                # return the approver comment as the second element; the default
                # stays (True, "") so tool-gating callers are unaffected.
                logger.warning(
                    f"✅ Tool {tool_name} APPROVED - proceeding with execution"
                )
                if return_comment_on_approve:
                    return (True, final_comment or "")
                return (True, "")

            except Exception as e:
                logger.error(
                    f"Approval flow error for tool {tool_name}: {e}", exc_info=True
                )
                return (False, f"Approval error: {str(e)}")

    except Exception as e:
        logger.error(f"Error checking approval requirement: {e}", exc_info=True)
        # SECURITY: fail CLOSED. If the approval check itself fails (DB outage,
        # workflow lookup error, etc.) we must block rather than execute — a
        # tool gated behind ``require_approval`` must never run un-approved just
        # because an infrastructure error prevented the check. The inner
        # approval-flow error path (above) already fails closed; keep this
        # outer handler consistent.
        return (
            False,
            "Approval check failed and the tool call was blocked as a safety "
            "measure. Please retry.",
        )
