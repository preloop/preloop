"""Human context for the "Allow this flow to run on your runner?" approval.

The runner owner decides whether a flow may run on their machine. The
approval's arguments are only ``runner_id`` and ``execution_id`` (they bind
the decision to one run), which tell the owner nothing. This module builds a
plain-language summary and a structured snapshot of what will run, on which
device, why it started, what it touches and what approving covers.

The snapshot is stored under ``ApprovalRequest.rule_context["runner_flow"]``
when the request is created, like the other server-owned bindings on that
column. It is never read back for authorization; the consent binding stays
``tool_args``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.utils.git_credentials import strip_url_credentials

logger = logging.getLogger(__name__)

#: Key under ``rule_context`` holding the snapshot.
RUNNER_FLOW_CONTEXT_KEY = "runner_flow"

#: What approving covers. Owner consent is consumed by one execution; the
#: only durable setting is the runner-wide ``flow_execution_consent``.
SCOPE_THIS_RUN = "this_run"

_HARNESS_LABELS = {
    "copilot_cli": "GitHub Copilot CLI",
    "cursor_cli": "Cursor CLI",
    "devin_cli": "Devin CLI",
    "claude_code": "Claude Code",
    "codex_cli": "Codex CLI",
    "opencode": "OpenCode",
    "deepseek": "DeepSeek CLI",
    "gemini_cli": "Gemini CLI",
    "claude_desktop": "Claude Desktop",
    "vscode_copilot": "VS Code Copilot",
}

_AGENT_TYPE_LABELS = {
    "codex": "Codex CLI",
    "claude_code": "Claude Code",
    "opencode": "OpenCode",
    "cursor": "Cursor CLI",
    "copilot": "GitHub Copilot CLI",
    "devin": "Devin CLI",
    "deepseek": "DeepSeek CLI",
    "openhands": "OpenHands",
    "pi": "Pi",
}

_OS_LABELS = {"darwin": "macOS", "linux": "Linux", "windows": "Windows"}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def harness_label(payload: Mapping[str, Any]) -> Optional[str]:
    """Return the display name of the harness the lease asks for.

    Args:
        payload: The lease payload being routed.

    Returns:
        A display name such as ``Codex CLI``, or None when unknown.
    """
    lease = payload.get("host_exec")
    harness = _text(lease.get("harness")) if isinstance(lease, Mapping) else None
    if harness:
        return _HARNESS_LABELS.get(harness, harness)
    agent_type = _text(payload.get("agent_type"))
    if agent_type:
        return _AGENT_TYPE_LABELS.get(agent_type, agent_type)
    return None


def describe_trigger(
    details: Any,
    *,
    retry_of_execution_id: Any = None,
    parent_execution_id: Any = None,
) -> Dict[str, Any]:
    """Classify what started an execution.

    Args:
        details: ``FlowExecution.trigger_event_details``.
        retry_of_execution_id: ``FlowExecution.retry_of_execution_id``.
        parent_execution_id: ``FlowExecution.parent_execution_id``. A
            delegated child also records it under ``details["delegation"]``.

    Returns:
        ``{"kind": ..., "label": ...}`` plus ``actor`` for a manual run and
        ``source``/``event`` for an event. ``kind`` is one of ``schedule``,
        ``manual``, ``retry``, ``delegated``, ``event`` or ``unknown``.
    """
    data = _as_mapping(details)
    source = _text(data.get("source"))
    event = _text(data.get("type")) or _text(data.get("trigger_type"))
    actor = _text(data.get("triggered_by"))
    # A retry copies the original trigger details, so the row's retry link
    # wins over whatever the original run was started by.
    if retry_of_execution_id:
        if actor:
            return {"kind": "retry", "label": f"Retry by {actor}", "actor": actor}
        return {"kind": "retry", "label": "Retry of an earlier run"}
    if source == "schedule" or data.get("trigger_source") == "schedule":
        return {"kind": "schedule", "label": "Scheduled run"}
    delegation = _as_mapping(data.get("delegation"))
    if parent_execution_id or delegation.get("parent_execution_id"):
        parent = _text(delegation.get("parent_flow_name"))
        delegated: Dict[str, Any] = {
            "kind": "delegated",
            "label": "Started by another flow",
        }
        if parent:
            delegated["label"] = f'Started by flow "{parent}"'
            delegated["parent_flow"] = parent
        return delegated
    if actor:
        return {
            "kind": "manual",
            "label": f"Started manually by {actor}",
            "actor": actor,
        }
    if source or event:
        trigger: Dict[str, Any] = {
            "kind": "event",
            "label": " ".join(part for part in (source, event) if part) + " event",
        }
        if source:
            trigger["source"] = source
        if event:
            trigger["event"] = event
        return trigger
    if data.get("test_mode"):
        return {"kind": "manual", "label": "Started manually"}
    return {"kind": "unknown", "label": "Unknown trigger"}


def _repositories(git_clone_config: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if not git_clone_config.get("enabled", True):
        return []
    rows: List[Dict[str, Any]] = []
    for item in git_clone_config.get("repositories") or []:
        repo = _as_mapping(item)
        url = _text(repo.get("repository_url"))
        # The snapshot reaches the owner, push payloads and the CLI: never
        # carry userinfo (a token in https://user:token@host/...).
        name = strip_url_credentials(url) if url else _text(repo.get("clone_path"))
        if not name:
            continue
        row: Dict[str, Any] = {"repository": name}
        branch = _text(repo.get("branch"))
        if branch:
            row["branch"] = branch
        rows.append(row)
    return rows


def _publication(
    git_clone_config: Mapping[str, Any], payload: Mapping[str, Any]
) -> Dict[str, Any]:
    mode = _text(git_clone_config.get("publication_mode")) or "legacy"
    if payload.get("host_exec_read_only") or mode == "read_only":
        return {"can_push": False, "label": "Read only: cannot push or publish"}
    if payload.get("host_exec_publication") or git_clone_config.get(
        "create_pull_request"
    ):
        label = (
            "Can push a branch and open a pull request through Preloop"
            if mode == "isolated"
            else "Can push a branch and open a pull request"
        )
        return {"can_push": True, "label": label}
    if _repositories(git_clone_config):
        return {
            "can_push": False,
            "label": "Checks out code; no pull request is configured",
        }
    return {"can_push": False, "label": "No repository publication configured"}


def _directory(runner: Any, payload: Mapping[str, Any]) -> Optional[str]:
    workspace = payload.get("host_exec_workspace")
    if not isinstance(workspace, Mapping):
        return None
    directory_id = _text(workspace.get("id"))
    if not directory_id:
        return None
    from preloop.services.runner_workspace_credentials import (
        runner_authorized_directories,
    )

    for item in runner_authorized_directories(runner):
        if item.get("id") == directory_id:
            return _text(item.get("label")) or directory_id
    return directory_id


def build_runner_flow_context(
    db: Session,
    *,
    runner: Any,
    execution_id: UUID | str,
    payload: Mapping[str, Any],
    base_url: str = "",
) -> Dict[str, Any]:
    """Snapshot what the owner is asked to allow.

    Args:
        db: Database session.
        runner: The ``FlowRunner`` the run would lease to.
        execution_id: The queued execution.
        payload: The lease payload (harness, model, workspace, checkout).
        base_url: Optional public URL prefix for the flow link.

    Returns:
        A JSON-serialisable dict. Missing facts are omitted, never invented.
    """
    execution = crud_flow_execution.get(db, id=execution_id)
    flow = (
        crud_flow.get(db, id=str(execution.flow_id))
        if execution is not None and execution.flow_id
        else None
    )
    os_name = _text(getattr(runner, "os", None))
    device: Dict[str, Any] = {
        key: value
        for key, value in {
            "hostname": _text(getattr(runner, "hostname", None)),
            "os": _OS_LABELS.get((os_name or "").lower(), os_name),
            "arch": _text(getattr(runner, "arch", None)),
        }.items()
        if value
    }
    context: Dict[str, Any] = {
        "runner": {"name": _text(getattr(runner, "name", None)) or "your runner"},
        "scope": SCOPE_THIS_RUN,
        "scope_label": (
            "Approving allows this run only. Later runs of this flow ask again "
            "unless the runner is set to always allow flows."
        ),
    }
    if device:
        context["runner"]["device"] = device
    harness = harness_label(payload)
    if harness:
        context["harness"] = harness
    model = _text(payload.get("model_identifier"))
    if model:
        context["model"] = model
    if flow is not None:
        context["flow"] = {
            "id": str(flow.id),
            "name": flow.name,
            "href": f"{base_url.rstrip('/')}/console/flows/{flow.id}",
        }
    if execution is not None:
        context["trigger"] = describe_trigger(
            execution.trigger_event_details,
            retry_of_execution_id=getattr(execution, "retry_of_execution_id", None),
            parent_execution_id=getattr(execution, "parent_execution_id", None),
        )
    git_clone_config = _as_mapping(payload.get("git_clone_config")) or _as_mapping(
        getattr(flow, "git_clone_config", None)
    )
    repositories = _repositories(git_clone_config)
    if repositories:
        context["repositories"] = repositories
    directory = _directory(runner, payload)
    if directory:
        context["directory"] = directory
    context["publication"] = _publication(git_clone_config, payload)
    timeout = payload.get("timeout_seconds") or getattr(flow, "timeout_seconds", None)
    if isinstance(timeout, int) and timeout > 0:
        context["limits"] = {"timeout_seconds": timeout}
    return context


def runner_flow_summary(context: Mapping[str, Any]) -> str:
    """One sentence for notification bodies, email and the CLI.

    Args:
        context: Output of :func:`build_runner_flow_context`.

    Returns:
        For example ``Allow flow "Daily review" to run Codex CLI (model-x) on
        laptop (laptop.local, macOS)? Scheduled run.``
    """
    flow = _as_mapping(context.get("flow"))
    runner = _as_mapping(context.get("runner"))
    device = _as_mapping(runner.get("device"))
    subject = f'flow "{flow["name"]}"' if flow.get("name") else "this flow"
    what = context.get("harness") or ""
    if what and context.get("model"):
        what = f"{what} ({context['model']})"
    where = str(runner.get("name") or "your runner")
    extras = [
        str(value)
        for value in (device.get("hostname"), device.get("os"))
        if value and value != where
    ]
    if extras:
        where = f"{where} ({', '.join(extras)})"
    sentence = (
        f"Allow {subject} to run {what} on {where}?"
        if what
        else f"Allow {subject} to run on {where}?"
    )
    trigger = _as_mapping(context.get("trigger"))
    if trigger.get("label") and trigger.get("kind") != "unknown":
        sentence = f"{sentence} {trigger['label']}."
    return sentence


def runner_flow_request(
    db: Session,
    *,
    runner: Any,
    execution_id: UUID | str,
    payload: Mapping[str, Any],
    base_url: str = "",
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Return ``(summary, rule_context)`` for a new runner flow approval.

    A failure here must never block the consent request, so it falls back to
    the generic wording.

    Args:
        db: Database session.
        runner: The candidate runner.
        execution_id: The queued execution.
        payload: The lease payload.
        base_url: Optional public URL prefix for the flow link.

    Returns:
        The summary and the ``rule_context`` to store, or ``(None, None)``.
    """
    try:
        context = build_runner_flow_context(
            db,
            runner=runner,
            execution_id=execution_id,
            payload=payload,
            base_url=base_url,
        )
    except Exception:
        logger.warning(
            "Runner flow approval context unavailable for %s",
            execution_id,
            exc_info=True,
        )
        return None, None
    return runner_flow_summary(context), {RUNNER_FLOW_CONTEXT_KEY: context}
