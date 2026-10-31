"""Park an unattended flow run on an approval-required native command.

A private-runner host profile runs a native harness (Codex, Claude Code, ...)
with the Preloop permission hook and an execution-scoped flow credential.
Before this module, a ``require_approval`` rule held the hook's HTTP call for
the workflow timeout: nothing was parked, the approval was not linked to the
execution, and a refusal or an expiry only denied that one command while the
run carried on and could still succeed.

An unattended run has nobody at the terminal, so the hook call now parks:

1. The approval request is created with ``execution_id`` set, the flow's
   approval window, and a ``_preloop_native_park`` marker carrying the
   command fingerprint. The execution gets a park request (the same row
   write the MCP approval path uses) and the hook answers ``deny`` with a
   stop message, so the command does not run.
2. The orchestrator's park handshake stops the runner job and releases the
   runtime credential, exactly as for any other human park.
3. Approved: the run resumes (natively where the harness can), is told that
   the command has not run, and the next call with the same fingerprint
   spends the approval once. Refused, cancelled or expired: the parked row
   fails closed with ``approval_denied`` / ``approval_timeout``.

While a park is pending every further native call of that execution is
denied without raising a second approval.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: ``tool_args`` key that marks an approval as a native command park.
NATIVE_PARK_MARKER = "_preloop_native_park"

#: Statuses that end a native park without running the command.
_REFUSED_STATUSES = frozenset({"declined", "cancelled"})

_MAX_COMMAND_CHARS = 2000


def command_fingerprint(tool_name: str, tool_input: Optional[Dict[str, Any]]) -> str:
    """Stable fingerprint of one native call, without Preloop's own markers.

    Keys starting with ``_preloop`` and the observed ``cwd`` are context, not
    the command, so they do not change what was approved.

    Args:
        tool_name: Native tool name.
        tool_input: Tool arguments as the hook sent them.

    Returns:
        A sha256 hex digest.
    """
    clean = {
        key: value
        for key, value in (tool_input or {}).items()
        if not str(key).startswith("_preloop") and key != "cwd"
    }
    body = json.dumps(
        {"tool": tool_name, "input": clean}, sort_keys=True, default=str
    ).encode()
    return hashlib.sha256(body).hexdigest()


def is_native_park(request: Any) -> bool:
    """True when an approval request was raised by a native command park."""
    tool_args = getattr(request, "tool_args", None)
    return isinstance(tool_args, dict) and isinstance(
        tool_args.get(NATIVE_PARK_MARKER), dict
    )


def command_text(tool_input: Optional[Dict[str, Any]]) -> str:
    """The command a human approves, bounded, for messages and prompts."""
    data = tool_input or {}
    for key in ("command", "cmd", "file_path", "path"):
        value = data.get(key)
        if isinstance(value, list):
            value = " ".join(str(part) for part in value)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_COMMAND_CHARS]
    clean = {k: v for k, v in data.items() if not str(k).startswith("_preloop")}
    return json.dumps(clean, sort_keys=True, default=str)[:_MAX_COMMAND_CHARS]


def park_marker(execution_id: Any, tool_name: str, tool_input: Any) -> Dict[str, str]:
    """The marker stored on the approval's ``tool_args``."""
    return {
        "execution_id": str(execution_id),
        "fingerprint": command_fingerprint(tool_name, tool_input),
    }


def parked_message(request_id: Any, expires_at: Optional[datetime]) -> str:
    """Hook reason telling the agent the run is paused, not refused."""
    deadline = (
        f" (decision window closes {expires_at.isoformat()})" if expires_at else ""
    )
    return (
        f"Preloop requires human approval for this command (approval request "
        f"{request_id}){deadline}. The command did not run and this execution is "
        "being paused: stop now and do not retry it. If it is approved the run "
        "resumes and the command may run once; if it is refused or the window "
        "closes, the run ends."
    )


def _session_factory() -> Any:
    from preloop.models.db.session import get_session_factory

    return get_session_factory()


def pending_park_reason(execution_id: Any) -> Optional[str]:
    """A deny reason when this execution already waits on a park.

    Args:
        execution_id: Flow execution id from the flow credential.

    Returns:
        The reason to deny with, or None when the run is not parked.
    """
    from preloop.models.crud import crud_flow_execution

    with _session_factory()() as db:
        park = crud_flow_execution.get_park_request(db, execution_id=execution_id)
    if not park or not park.get("request_id"):
        return None
    return (
        "This execution is paused waiting for approval request "
        f"{park['request_id']}. No further commands run until it is decided."
    )


def resolve_window(execution_id: Any, workflow: Any) -> Any:
    """The approval window for a park of this execution.

    Args:
        execution_id: Flow execution id.
        workflow: Resolved approval workflow.

    Returns:
        :class:`preloop.services.approval_window.ApprovalWindow`.
    """
    from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
    from preloop.services.approval_window import resolve_approval_window

    flow = account = None
    with _session_factory()() as db:
        execution = crud_flow_execution.get(db, id=str(execution_id))
        if execution is not None:
            flow = crud_flow.get(db, id=str(execution.flow_id))
        if flow is not None:
            account = crud_account.get(db, id=str(flow.account_id))
        return resolve_approval_window(flow=flow, workflow=workflow, account=account)


def _approved_answer_ids(details: Any) -> List[str]:
    """Approved request ids this run resumed with, newest first."""
    if not isinstance(details, dict):
        return []
    chain = details.get("_answers")
    history = chain.get("history") if isinstance(chain, dict) else None
    ids: List[str] = []
    for entry in reversed(history if isinstance(history, list) else []):
        if isinstance(entry, dict) and entry.get("status") == "approved":
            request_id = str(entry.get("request_id") or "")
            if request_id and request_id not in ids:
                ids.append(request_id)
    return ids


def consume_grant(
    *,
    execution_id: Any,
    account_id: Any,
    tool_name: str,
    tool_input: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Spend an approved park for exactly this command, once.

    Args:
        execution_id: Execution making the call (the resumed run).
        account_id: Owning account.
        tool_name: Native tool name.
        tool_input: Tool arguments.

    Returns:
        The approval request id spent, or None.
    """
    from preloop.models.crud import crud_approval_request, crud_flow_execution

    fingerprint = command_fingerprint(tool_name, tool_input)
    with _session_factory()() as db:
        execution = crud_flow_execution.get(db, id=str(execution_id))
        if execution is None:
            return None
        for request_id in _approved_answer_ids(execution.trigger_event_details):
            try:
                request_uuid = uuid.UUID(request_id)
            except ValueError:
                continue
            if crud_approval_request.consume_native_grant(
                db,
                request_id=request_uuid,
                account_id=uuid.UUID(str(account_id)),
                marker_key=NATIVE_PARK_MARKER,
                fingerprint=fingerprint,
                execution_id=str(execution_id),
            ):
                _audit(
                    account_id=account_id,
                    approval_id=request_uuid,
                    event="native_grant_consumed",
                    tool_name=tool_name,
                    execution_id=str(execution_id),
                    reason="Approved native command allowed once in the resumed run",
                )
                return request_id
    return None


def park(
    *,
    execution_id: Any,
    approval_request_id: Any,
    expires_at: Optional[datetime],
    account_id: Any,
    tool_name: str,
) -> bool:
    """Record the park request and its audit row.

    Returns:
        True when the execution will park. False when it is no longer live
        or the approval was already decided.
    """
    from preloop.services.approval_park import request_park

    with _session_factory()() as db:
        parked = request_park(
            db,
            execution_id=execution_id,
            approval_request_id=approval_request_id,
            expires_at=expires_at,
        )
    if parked:
        _audit(
            account_id=account_id,
            approval_id=approval_request_id,
            event="parked",
            tool_name=tool_name,
            execution_id=str(execution_id),
            reason="Unattended run paused on an approval-required native command",
        )
    return parked


def fail_closed(db: Any, parked: Any, request: Any) -> bool:
    """Fail a run parked on a refused or expired native command approval.

    Args:
        db: Sync session.
        parked: Parked flow execution row.
        request: The decided approval request.

    Returns:
        True when this call ended the run.
    """
    from preloop.models.crud import crud_flow_execution
    from preloop.services.flow_failure_category import (
        FAILURE_CATEGORY_APPROVAL_DENIED,
        FAILURE_CATEGORY_APPROVAL_TIMEOUT,
    )

    status = str(getattr(request, "status", "") or "")
    expired = status == "expired"
    category = (
        FAILURE_CATEGORY_APPROVAL_TIMEOUT
        if expired
        else FAILURE_CATEGORY_APPROVAL_DENIED
    )
    command = command_text(getattr(request, "tool_args", None))
    if expired:
        message = (
            f"Approval request {request.id} for {request.tool_name} "
            f"`{command}` expired with no decision; the run failed closed and the "
            "command did not run."
        )
    else:
        message = (
            f"Approval request {request.id} for {request.tool_name} "
            f"`{command}` was {status}; the run failed closed and the command "
            "did not run."
        )
    ended = crud_flow_execution.fail_parked_for_request(
        db,
        execution_id=parked.id,
        approval_request_id=request.id,
        failure_category=category,
        error_message=message,
    )
    if ended:
        logger.info(
            "Execution %s failed closed (%s) on native approval %s",
            parked.id,
            category,
            request.id,
        )
        if expired:
            # The sweep marks the request expired without a lifecycle row.
            _audit(
                account_id=request.account_id,
                approval_id=request.id,
                event="expired",
                tool_name=request.tool_name,
                execution_id=str(parked.id),
                reason="Approval window closed with no decision",
            )
        _audit(
            account_id=request.account_id,
            approval_id=request.id,
            event="execution_failed_closed",
            tool_name=request.tool_name,
            execution_id=str(parked.id),
            reason=category,
        )
    return ended


def refused_or_expired(request: Any) -> bool:
    """True when a native park must end the run instead of resuming it."""
    status = str(getattr(request, "status", "") or "")
    return status in _REFUSED_STATUSES or status == "expired"


def hosted_approval_gap(db: Any, flow: Any) -> Optional[str]:
    """Why a hosted container run cannot honour this flow's approval rules.

    Hosted Codex installs the Preloop command hook in its container and
    parks instead (the orchestrator only marks the launch as requiring the
    hook). Other hosted harnesses have no native command hook, so a native
    ``require_approval`` rule that could apply to this flow (any enabled
    native tool configuration, including agent-pinned ones, or a rule scoped
    to the flow or its managed agent) would be skipped. Conservative on
    purpose: the permission path resolves native configs without an agent
    filter.
    Rather than run ungated, the caller refuses the launch.

    Args:
        db: Sync session.
        flow: The flow about to launch in a hosted container.

    Returns:
        The operator-facing refusal, or None when no such rule applies.
    """
    from preloop.models.crud import crud_account
    from preloop.models.crud.tool_access_rule import (
        list_native_require_approval_tool_names,
    )
    from preloop.services.subject_governance import (
        SUBJECT_TYPE_FLOWS,
        SUBJECT_TYPE_MANAGED_AGENTS,
        get_subject_governance,
    )

    account_id = getattr(flow, "account_id", None)
    if account_id is None:
        return None
    names = set(list_native_require_approval_tool_names(db, account_id=account_id))
    account = crud_account.get(db, id=str(account_id))
    meta = getattr(account, "meta_data", None) if account is not None else None
    scopes = [(SUBJECT_TYPE_FLOWS, str(flow.id))]
    # Employee-bound flows run with the target agent's native policy (see
    # flow_runtime_token), so that agent's scoped rules apply too.
    agent_id = None
    if isinstance(
        (getattr(flow, "trigger_config", None) or {}).get("employee_events"), dict
    ):
        agent_id = (getattr(flow, "agent_config", None) or {}).get("target_agent_id")
    if agent_id:
        scopes.append((SUBJECT_TYPE_MANAGED_AGENTS, str(agent_id)))
    for subject_type, subject_id in scopes:
        scoped = get_subject_governance(
            meta, subject_type=subject_type, subject_id=subject_id
        ).get("tool_rules")
        if not isinstance(scoped, dict):
            continue
        for tool_name, rules in scoped.items():
            for rule in rules if isinstance(rules, list) else []:
                if (
                    isinstance(rule, dict)
                    and rule.get("is_enabled", True)
                    and rule.get("action") == "require_approval"
                    and rule.get("source") in (None, "", "agent")
                ):
                    names.add(str(tool_name))
    if not names:
        return None
    listed = ", ".join(sorted(names)[:5])
    return (
        "Approval-required rules apply to native commands in this flow "
        f"({listed}), and this hosted harness has no Preloop command hook "
        "to pause the run for approval. The run was refused instead of running "
        "those commands ungated. Use Codex (hosted or on a private runner host "
        "profile), where the run pauses for approval, or change the rule."
    )


def _audit(
    *,
    account_id: Any,
    approval_id: Any,
    event: str,
    tool_name: Optional[str],
    execution_id: str,
    reason: str,
) -> None:
    """Best-effort approval lifecycle audit row linked to the execution."""
    try:
        from preloop.services.approval_service import _log_approval_lifecycle_async

        _log_approval_lifecycle_async(
            account_id=str(account_id),
            approval_id=uuid.UUID(str(approval_id)),
            event=event,
            tool_name=tool_name,
            reason=reason,
            execution_id=execution_id,
            extra_details={"native_park": True},
        )
    except Exception:
        logger.exception("Could not audit native park event %s", event)
