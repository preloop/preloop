"""Event payload builders and the emit calls used at each chokepoint.

One module so the shape of every v1 event is reviewable in one place, and so
a chokepoint only has to add two lines. Every function here swallows its own
errors: an unreachable receiver must never break an approval, a policy
decision or a flow run.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from preloop.services.event_webhooks import outbox
from preloop.services.event_webhooks.events import (
    EVENT_APPROVAL_CREATED,
    EVENT_APPROVAL_DECIDED,
    EVENT_BUDGET_EXCEEDED,
    EVENT_BUDGET_THRESHOLD,
    EVENT_FLOW_EXECUTION_FINISHED,
    EVENT_POLICY_DENIED,
    EVENT_SESSION_ENDED,
)

logger = logging.getLogger(__name__)

# Statuses that count as a decision. "pending" and the interim quorum states
# are not decisions and must not fire approval.decided.
DECIDED_STATUSES = frozenset({"approved", "declined", "expired", "cancelled"})

# Actor kinds reported on approval.decided. Kept as a closed set so a
# receiver can branch on it without string matching free text.
ACTOR_USER = "user"
ACTOR_AI = "ai"
ACTOR_BYPASS = "bypass"
ACTOR_SYSTEM = "system"


def _iso(value: Any) -> Optional[str]:
    """Render a timestamp as UTC ISO-8601, or None."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _str(value: Any) -> Optional[str]:
    """Stringify ids without turning None into "None"."""
    return None if value is None else str(value)


# --- approvals -------------------------------------------------------------


def _approval_common(request: Any) -> dict[str, Any]:
    """Fields shared by approval.created and approval.decided.

    Tool arguments are deliberately absent: they routinely carry the payload
    the approval exists to guard, and a webhook target is not the audit log.
    """
    return {
        "approval_request_id": _str(getattr(request, "id", None)),
        "status": getattr(request, "status", None),
        "tool_name": getattr(request, "tool_name", None),
        "summary": getattr(request, "summary", None),
        "approval_workflow_id": _str(getattr(request, "approval_workflow_id", None)),
        "tool_configuration_id": _str(getattr(request, "tool_configuration_id", None)),
        "execution_id": getattr(request, "execution_id", None),
        "managed_agent_id": _str(getattr(request, "managed_agent_id", None)),
        "managed_agent_name": getattr(request, "managed_agent_name", None),
        "runtime_session_id": _str(getattr(request, "runtime_session_id", None)),
        "api_key_id": _str(getattr(request, "api_key_id", None)),
        "requested_at": _iso(getattr(request, "requested_at", None)),
        "expires_at": _iso(getattr(request, "expires_at", None)),
    }


def _decision_rule(request: Any) -> Optional[dict[str, Any]]:
    """The rule or policy that asked for this approval, when one was recorded.

    Read straight off the snapshot taken at creation time, so a later edit to
    the rule cannot rewrite what the receiver is told.
    """
    context = getattr(request, "rule_context", None)
    if not isinstance(context, Mapping):
        return None
    return {
        "source": context.get("source"),
        "rule_id": _str(context.get("rule_id")),
        "rule_name": context.get("rule_name"),
        "expression": context.get("expression"),
        "priority": context.get("priority"),
    }


def _decision_actor(request: Any) -> dict[str, Any]:
    """Who decided, as a closed kind plus an id when one exists.

    An expiry has no actor; saying "system" is honest, inventing a user is
    not. A bypass is reported as a bypass rather than as an approver, because
    nobody judged the call.
    """
    status = getattr(request, "status", None)
    if getattr(request, "decided_by_ai", False):
        return {
            "kind": ACTOR_AI,
            "id": None,
            "model": getattr(request, "ai_model", None),
            "confidence": getattr(request, "ai_confidence", None),
        }
    if getattr(request, "auto_approved_reason", None):
        return {
            "kind": ACTOR_BYPASS,
            "id": _str(getattr(request, "auto_approval_bypass_id", None)),
            "reason": getattr(request, "auto_approved_reason", None),
        }
    if status in {"expired", "cancelled"}:
        return {"kind": ACTOR_SYSTEM, "id": None}

    responses = getattr(request, "responses", None)
    if isinstance(responses, list):
        for entry in reversed(responses):
            if isinstance(entry, Mapping) and entry.get("user_id"):
                return {"kind": ACTOR_USER, "id": _str(entry.get("user_id"))}
    return {"kind": ACTOR_USER, "id": None}


def approval_created_data(request: Any) -> dict[str, Any]:
    """Body of ``approval.created``."""
    data = _approval_common(request)
    data["rule"] = _decision_rule(request)
    return data


def approval_decided_data(request: Any) -> dict[str, Any]:
    """Body of ``approval.decided``."""
    data = _approval_common(request)
    data.update(
        {
            "decision": getattr(request, "status", None),
            "resolved_at": _iso(getattr(request, "resolved_at", None)),
            "actor": _decision_actor(request),
            "rule": _decision_rule(request),
            "comment": getattr(request, "approver_comment", None),
        }
    )
    return data


async def emit_approval_event_async(db: Any, request: Any, event_type: str) -> None:
    """Enqueue one approval event on the caller's async session.

    Args:
        db: ``AsyncSession`` (or the sync approval adapter).
        request: The ``ApprovalRequest`` row.
        event_type: ``approval.created`` or ``approval.decided``.
    """
    try:
        request_id = getattr(request, "id", None)
        if request_id is None:
            return
        if event_type == EVENT_APPROVAL_CREATED:
            data = approval_created_data(request)
            occurred_at = getattr(request, "requested_at", None)
            natural_key = f"{EVENT_APPROVAL_CREATED}:{request_id}"
        else:
            status = getattr(request, "status", None)
            if status not in DECIDED_STATUSES:
                return
            data = approval_decided_data(request)
            occurred_at = getattr(request, "resolved_at", None)
            # Keyed on the terminal status too: a request cannot legitimately
            # move between terminal states, and if it somehow does the
            # receiver should hear about it rather than have it swallowed.
            natural_key = f"{EVENT_APPROVAL_DECIDED}:{request_id}:{status}"

        result = await outbox.enqueue_event_async(
            db,
            account_id=getattr(request, "account_id", None),
            event_type=event_type,
            data=data,
            occurred_at=occurred_at,
            natural_key=natural_key,
            subject_id=request_id,
        )
        if result.delivery_ids:
            await db.commit()
    except Exception:  # noqa: BLE001 - approvals must not fail on webhooks
        logger.warning("Failed to emit %s webhook event", event_type, exc_info=True)


# --- policy ----------------------------------------------------------------


def policy_denied_data(
    *,
    tool_name: str,
    rule_description: Optional[str],
    condition_matched: Optional[str],
    execution_id: Any,
    user_id: Any,
    correlation_id: Optional[str],
    extra_details: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Body of ``policy.denied``.

    Tool arguments are not included, for the same reason as approvals: the
    denied call's arguments are often exactly what the policy exists to keep
    out of third-party systems.
    """
    return {
        "tool_name": tool_name,
        "decision": "deny",
        "rule_description": rule_description,
        "condition_matched": condition_matched,
        "execution_id": _str(execution_id),
        "user_id": _str(user_id),
        "correlation_id": correlation_id,
        "details": dict(extra_details) if isinstance(extra_details, Mapping) else None,
    }


def emit_policy_denied(
    *,
    account_id: Any,
    tool_name: str,
    rule_description: Optional[str] = None,
    condition_matched: Optional[str] = None,
    execution_id: Any = None,
    user_id: Any = None,
    correlation_id: Optional[str] = None,
    extra_details: Optional[Mapping[str, Any]] = None,
) -> None:
    """Enqueue ``policy.denied`` on a detached session.

    The policy evaluator records a decision rather than changing state, so
    there is no caller transaction worth joining and the row is committed on
    its own.
    """
    outbox.enqueue_event_detached(
        account_id=account_id,
        event_type=EVENT_POLICY_DENIED,
        data=policy_denied_data(
            tool_name=tool_name,
            rule_description=rule_description,
            condition_matched=condition_matched,
            execution_id=execution_id,
            user_id=user_id,
            correlation_id=correlation_id,
            extra_details=extra_details,
        ),
    )


# --- runtime sessions ------------------------------------------------------


def session_ended_data(session: Any, *, reason: str) -> dict[str, Any]:
    """Body of ``session.ended``."""
    started_at = getattr(session, "started_at", None)
    ended_at = getattr(session, "ended_at", None)
    duration = None
    if isinstance(started_at, datetime) and isinstance(ended_at, datetime):
        start = started_at.replace(tzinfo=None)
        end = ended_at.replace(tzinfo=None)
        duration = max(0, int((end - start).total_seconds()))
    return {
        "runtime_session_id": _str(getattr(session, "id", None)),
        "reason": reason,
        "session_source_type": getattr(session, "session_source_type", None),
        "session_source_id": getattr(session, "session_source_id", None),
        "runtime_principal_type": getattr(session, "runtime_principal_type", None),
        "runtime_principal_id": getattr(session, "runtime_principal_id", None),
        "runtime_principal_name": getattr(session, "runtime_principal_name", None),
        "started_at": _iso(started_at),
        "ended_at": _iso(ended_at),
        "duration_seconds": duration,
    }


def emit_session_ended(db: Any, session: Any, *, reason: str) -> None:
    """Enqueue ``session.ended`` in the caller's transaction.

    Args:
        db: The sync session that just stamped ``ended_at``.
        session: The ``RuntimeSession`` row.
        reason: Why it ended (``operator``, ``idle``, ``execution_finished``).
    """
    session_id = getattr(session, "id", None)
    if session_id is None:
        return
    outbox.enqueue_event(
        db,
        account_id=getattr(session, "account_id", None),
        event_type=EVENT_SESSION_ENDED,
        data=session_ended_data(session, reason=reason),
        occurred_at=getattr(session, "ended_at", None),
        natural_key=f"{EVENT_SESSION_ENDED}:{session_id}",
        subject_id=session_id,
    )


# --- budgets ---------------------------------------------------------------


def budget_data(
    *,
    scope: str,
    scope_id: Any,
    period: str,
    limit_amount: Any,
    spent_amount: Any,
    threshold_percent: Optional[int],
    currency: str = "USD",
) -> dict[str, Any]:
    """Body of ``budget.threshold`` and ``budget.exceeded``."""
    try:
        percent_used = (
            round(float(spent_amount) / float(limit_amount) * 100, 2)
            if limit_amount
            else None
        )
    except (TypeError, ValueError, ZeroDivisionError):
        percent_used = None
    return {
        "scope": scope,
        "scope_id": _str(scope_id),
        "period": period,
        "currency": currency,
        "limit_amount": float(limit_amount) if limit_amount is not None else None,
        "spent_amount": float(spent_amount) if spent_amount is not None else None,
        "percent_used": percent_used,
        "threshold_percent": threshold_percent,
    }


def emit_budget_event(
    db: Any,
    *,
    account_id: Any,
    exceeded: bool,
    scope: str,
    scope_id: Any = None,
    period: str,
    limit_amount: Any,
    spent_amount: Any,
    threshold_percent: Optional[int] = None,
    currency: str = "USD",
    occurred_at: Optional[datetime] = None,
) -> outbox.EnqueueResult:
    """Enqueue a budget event in the caller's transaction.

    The natural key covers the budget and the period but not the spend, so a
    spend that keeps climbing past a limit produces one event per period
    rather than one per model call. That is the difference between a useful
    alert and a pager loop.

    Returns:
        What the outbox inserted, so callers that cannot join a later commit
        (the model gateway) can commit the row themselves.
    """
    event_type = EVENT_BUDGET_EXCEEDED if exceeded else EVENT_BUDGET_THRESHOLD
    key_parts = [
        event_type,
        _str(account_id) or "",
        scope,
        _str(scope_id) or "",
        period,
        str(limit_amount),
        str(threshold_percent or ""),
    ]
    return outbox.enqueue_event(
        db,
        account_id=account_id,
        event_type=event_type,
        data=budget_data(
            scope=scope,
            scope_id=scope_id,
            period=period,
            limit_amount=limit_amount,
            spent_amount=spent_amount,
            threshold_percent=threshold_percent,
            currency=currency,
        ),
        occurred_at=occurred_at,
        natural_key=":".join(key_parts),
        subject_id=scope_id if isinstance(scope_id, uuid.UUID) else None,
    )


# --- flow executions -------------------------------------------------------

# Receipt keys forwarded verbatim. Availability and retention facts only; the
# digest is identity metadata from the stored manifest and is not a claim
# that anything was verified on the way out (see
# docs/guide/flows/evidence-storage.md).
RECEIPT_KEYS = (
    "status",
    "transport",
    "artifact_id",
    "sha256",
    "manifest_sha256",
    "size_bytes",
    "created_at",
    "expires_at",
    "retention_hours",
    "object_lock",
    "legal_hold",
    "integrity_verified",
)


def flow_execution_finished_data(
    execution: Any,
    flow: Any,
    *,
    status: str,
    failure_category: Optional[str],
) -> dict[str, Any]:
    """Body of ``flow.execution.finished``."""
    receipt = getattr(execution, "evidence_receipt", None)
    evidence = (
        {key: receipt.get(key) for key in RECEIPT_KEYS if key in receipt}
        if isinstance(receipt, Mapping)
        else None
    )
    return {
        "execution_id": _str(getattr(execution, "id", None)),
        "flow_id": _str(getattr(flow, "id", None)),
        "flow_name": getattr(flow, "name", None),
        "status": status,
        "failure_category": failure_category,
        "trigger_type": getattr(execution, "trigger_type", None),
        "started_at": _iso(getattr(execution, "start_time", None)),
        "finished_at": _iso(getattr(execution, "end_time", None)),
        "evidence_receipt": evidence,
    }


def emit_flow_execution_finished(
    db: Any,
    execution: Any,
    flow: Any,
    *,
    status: str,
    failure_category: Optional[str] = None,
) -> None:
    """Enqueue ``flow.execution.finished`` in the caller's transaction."""
    execution_id = getattr(execution, "id", None)
    if execution_id is None:
        return
    outbox.enqueue_event(
        db,
        account_id=getattr(flow, "account_id", None),
        event_type=EVENT_FLOW_EXECUTION_FINISHED,
        data=flow_execution_finished_data(
            execution, flow, status=status, failure_category=failure_category
        ),
        occurred_at=getattr(execution, "end_time", None),
        # Keyed on the terminal status: a retried execution keeps its id, and
        # a run that fails then succeeds is two facts, not one.
        natural_key=f"{EVENT_FLOW_EXECUTION_FINISHED}:{execution_id}:{status}",
        subject_id=execution_id,
    )
