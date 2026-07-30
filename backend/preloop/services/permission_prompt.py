"""Claude Code ``--permission-prompt-tool`` adapter for Preloop approvals.

Claude Code's headless mode can delegate every would-be permission prompt to
an MCP tool::

    claude -p --permission-prompt-tool mcp__preloop__permission_prompt "task"

That tool receives ``{tool_name, input, tool_use_id?}`` and MUST return a
JSON-stringified decision in Claude's behavior schema::

    {"behavior": "allow", "updatedInput": {...}}
    {"behavior": "deny", "message": "..."}

This module implements the decision logic behind the ``permission_prompt``
builtin (registered in ``initialize_mcp.py``; metadata in
``tools/builtin_defs.py``). It reuses the exact machinery of the agents
permission-check endpoint (``agent_permission_service``): workflow resolution
(per-agent subject-governance pin -> account default -> auto-created "Agent
Tool Approvals"), ToolConfiguration rows under ``tool_source="agent"`` so
these approvals share analytics/policy with the PreToolUse-hook path, and the
``native_tool_approvals: "off"`` standing bypass (recorded, auto-approved).

Timeout mismatch — the explicit design decision
-----------------------------------------------
Claude Code waits only a short time for MCP tool responses (30s MCP timeouts;
additionally v2.1.199+ converts ``allow`` -> ``deny`` for MCP tools marked as
requiring user interaction), while Preloop approval workflows default to
minutes (300s+, with escalation doubling). We therefore:

1. Fast-poll the approval for a bounded budget (default 25s, configurable via
   ``PRELOOP_PERMISSION_PROMPT_WAIT_SECONDS``) — comfortably inside Claude's
   30s window.
2. If still undecided, return ``behavior: "deny"`` whose ``message`` starts
   with :data:`PENDING_MARKER` and explicitly tells the model this is NOT a
   rejection and to retry the same tool call.
3. On retry, the identical ``(tool_name, input)`` fingerprint re-attaches to
   the still-pending approval request instead of creating (and re-notifying)
   a new one. Successive retries therefore cover the full workflow timeout
   without spamming approvers.
4. A human decision that lands between retries is picked up by the next call:
   resolved requests stay consumable for
   :data:`CONSUMABLE_RESOLUTION_TTL_SECONDS` and are marked consumed
   (``tool_args._permission_prompt_consumed``) once returned, so a stale
   approval can never silently allow a *later* identical call.

``request_approval`` keeps its existing envelope untouched; this is a
separate tool precisely so we do not break that contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from preloop.models import models
from preloop.models.db.session import get_async_db_session
from preloop.services.agent_permission_service import (
    AGENT_TOOL_SOURCE,  # noqa: F401  (re-exported: shared tool_source contract)
    _native_tool_approvals_disabled,
    _resolve_tool_config,
    _resolve_workflow,
)

logger = logging.getLogger(__name__)

#: Default in-call wait before returning the retryable pending deny. Must stay
#: below Claude Code's 30s MCP timeout with margin for transport overhead.
DEFAULT_WAIT_SECONDS = 25.0

#: How often the bounded wait re-reads the approval row.
POLL_INTERVAL_SECONDS = 1.0

#: A resolved (approved/declined) request may be returned to a re-attaching
#: retry for this long after resolution. Past that, an identical call creates
#: a fresh approval request.
CONSUMABLE_RESOLUTION_TTL_SECONDS = 600

#: tool_args key carrying the dedupe fingerprint of (tool_name, input).
FINGERPRINT_KEY = "_permission_prompt_fingerprint"

#: tool_args key set once a resolution has been delivered to Claude. Consumed
#: requests are never matched again (single-use decisions).
CONSUMED_KEY = "_permission_prompt_consumed"

#: tool_args key preserving Claude's tool_use_id for the audit trail.
TOOL_USE_ID_KEY = "_claude_tool_use_id"

#: Prefix of the deny message that means "still pending — retry".
PENDING_MARKER = "PRELOOP_APPROVAL_PENDING"

_TERMINAL_STATUSES = ("approved", "declined", "cancelled", "expired")


def _wait_budget_seconds() -> float:
    """Resolve the bounded wait from env, clamped to a sane range."""
    raw = os.getenv("PRELOOP_PERMISSION_PROMPT_WAIT_SECONDS", "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_WAIT_SECONDS
    except ValueError:
        logger.warning(
            "Invalid PRELOOP_PERMISSION_PROMPT_WAIT_SECONDS %r; using default %s",
            raw,
            DEFAULT_WAIT_SECONDS,
        )
        value = DEFAULT_WAIT_SECONDS
    return max(1.0, min(value, 600.0))


def permission_prompt_fingerprint(tool_name: str, tool_input: Optional[dict]) -> str:
    """Stable fingerprint of a native tool call for retry re-attachment."""
    canonical = json.dumps(
        {"tool_name": tool_name, "input": tool_input or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dedupe_stmt(account_id: str, fingerprint: str):
    """Select recent approval requests carrying this call fingerprint.

    Exposed at module level so the real-database regression test can execute
    the exact JSONB path expression against Postgres (mock-based tests never
    run the SQL).
    """
    return (
        select(models.ApprovalRequest)
        .where(
            models.ApprovalRequest.account_id == account_id,
            models.ApprovalRequest.tool_args[FINGERPRINT_KEY].astext == fingerprint,
        )
        .order_by(models.ApprovalRequest.requested_at.desc())
        .limit(5)
    )


def _allow(tool_input: dict) -> Dict[str, Any]:
    # updatedInput is required by the contract; we pass the input through
    # unmodified (Preloop approves/denies, it does not rewrite arguments).
    return {"behavior": "allow", "updatedInput": dict(tool_input or {})}


def _deny(message: str) -> Dict[str, Any]:
    return {"behavior": "deny", "message": message}


def _pending_deny(request_id: str, waited: float) -> Dict[str, Any]:
    return _deny(
        f"{PENDING_MARKER}: approval request {request_id} is still awaiting a "
        f"human decision in Preloop (waited {int(waited)}s; the approver has "
        "been notified). This is NOT a rejection — retry the exact same tool "
        "call to keep waiting for the decision."
    )


def _map_terminal(
    status: str,
    comment: Optional[str],
    tool_input: dict,
    request_id: str,
) -> Dict[str, Any]:
    """Map a terminal approval status onto Claude's behavior schema."""
    if status == "approved":
        return _allow(tool_input)
    if status == "declined":
        suffix = f": {comment}" if comment else ""
        return _deny(f"Denied by approver in Preloop{suffix}")
    if status == "cancelled":
        return _deny("The approval request was cancelled in Preloop.")
    if status == "expired":
        return _deny(
            f"Approval request {request_id} expired without a decision. "
            "Retry the tool call to request approval again."
        )
    return _deny(f"Unexpected approval status: {status}")


async def _mark_consumed(db: Any, request: models.ApprovalRequest) -> None:
    """Flag a resolved request as delivered so it is never matched again."""
    args = dict(request.tool_args or {})
    args[CONSUMED_KEY] = True
    request.tool_args = args
    flag_modified(request, "tool_args")
    await db.commit()


async def _poll_for_decision(
    request_id: str, tool_input: dict, budget: float
) -> Dict[str, Any]:
    """Fast-poll one approval request for up to ``budget`` seconds."""
    from preloop.models.crud.approval_request import get_approval_request_async

    elapsed = 0.0
    while True:
        async with get_async_db_session() as db:
            request = await get_approval_request_async(
                db, request_id=uuid.UUID(request_id)
            )
            if request is None:
                return _deny(f"Approval request {request_id} not found")
            status = request.status
            comment = request.approver_comment
            if status in _TERMINAL_STATUSES:
                await _mark_consumed(db, request)
                return _map_terminal(status, comment, tool_input, request_id)
            if request.expires_at and datetime.utcnow() > request.expires_at:
                # Nobody expired the row yet, but its window has passed.
                # Report expiry; the next identical call skips dead pending
                # rows and raises a fresh approval.
                return _map_terminal("expired", comment, tool_input, request_id)
        if elapsed >= budget:
            return _pending_deny(request_id, elapsed)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS


async def evaluate_permission_prompt(
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
    tool_use_id: Optional[str] = None,
    agent_reasoning: Optional[str] = None,
    wait_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Decide a Claude Code permission prompt; returns the behavior schema.

    See the module docstring for the retry/re-attach protocol that bridges
    Claude's short MCP wait and Preloop's long approval timeouts.
    """
    tool_input = dict(tool_input or {})
    budget = wait_seconds if wait_seconds is not None else _wait_budget_seconds()
    fingerprint = permission_prompt_fingerprint(tool_name, tool_input)

    # 1) Re-attach to an existing request for the same call, if any.
    async with get_async_db_session() as db:
        result = await db.execute(dedupe_stmt(account_id, fingerprint))
        rows: List[models.ApprovalRequest] = list(result.scalars().all())
        now = datetime.utcnow()
        pending_request_id: Optional[str] = None
        for request in rows:
            args = request.tool_args or {}
            if args.get(CONSUMED_KEY):
                continue
            if request.status == "pending":
                if request.expires_at and now > request.expires_at:
                    continue  # dead pending row; fall through to create anew
                pending_request_id = str(request.id)
                break
            if (
                request.status in ("approved", "declined")
                and request.resolved_at is not None
                and (now - request.resolved_at).total_seconds()
                <= CONSUMABLE_RESOLUTION_TTL_SECONDS
            ):
                # A human decided between Claude's retries: deliver it once.
                status = request.status
                comment = request.approver_comment
                request_id = str(request.id)
                await _mark_consumed(db, request)
                logger.info(
                    "permission_prompt: delivering %s decision for re-attached "
                    "request %s (tool %s)",
                    status,
                    request_id,
                    tool_name,
                )
                return _map_terminal(status, comment, tool_input, request_id)

    if pending_request_id is not None:
        logger.info(
            "permission_prompt: re-attached to pending approval %s for tool %s",
            pending_request_id,
            tool_name,
        )
        return await _poll_for_decision(pending_request_id, tool_input, budget)

    # 2) No reusable request — raise a fresh approval through the shared
    #    agent-permission machinery.
    from preloop.services.approval_service import ApprovalService

    async with get_async_db_session() as db:
        approvals_off = managed_agent_id is not None and (
            await _native_tool_approvals_disabled(db, account_id, managed_agent_id)
        )
        workflow = await _resolve_workflow(
            db, account_id, user_id, managed_agent_id=managed_agent_id
        )
        config = await _resolve_tool_config(db, account_id, tool_name, workflow.id)

        tool_args = dict(tool_input)
        tool_args[FINGERPRINT_KEY] = fingerprint
        if tool_use_id:
            tool_args[TOOL_USE_ID_KEY] = str(tool_use_id)
        if source and source.strip():
            tool_args["_preloop_source"] = source.strip()

        service = ApprovalService(db, base_url)
        approval = await service.create_and_notify(
            account_id=account_id,
            tool_configuration_id=config.id,
            approval_workflow=workflow,
            tool_name=tool_name,
            tool_args=tool_args,
            agent_reasoning=agent_reasoning,
            execution_id=None,
            user_id=user_id,
            managed_agent_id=managed_agent_id,
            runtime_session_id=runtime_session_id,
            managed_agent_name=managed_agent_name,
            standing_bypass_reason=(
                models.AutoApprovedReason.NATIVE_TOOL_APPROVALS_OFF
                if approvals_off
                else None
            ),
        )
        request_id = str(approval.id)
        status = approval.status
        comment = approval.approver_comment
        if status in _TERMINAL_STATUSES:
            # AI-driven workflows and standing bypasses resolve inside
            # create_and_notify; consume immediately so the row can never be
            # replayed by a later identical call.
            await _mark_consumed(db, approval)

    if status in _TERMINAL_STATUSES:
        return _map_terminal(status, comment, tool_input, request_id)

    return await _poll_for_decision(request_id, tool_input, budget)
