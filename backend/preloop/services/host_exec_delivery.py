"""Transient flow inputs for host-exec runner leases.

A host-exec lease persists only data (prompt, profile, model alias). At
delivery time the control plane adds, in memory only:

* ``host_exec_mcp``: a flow-scoped runtime token for the Preloop MCP server,
  minted when the flow has MCP tools. The runner writes it into a per-job
  MCP config file for the local CLI. The token is revoked when the execution
  completes.
* ``host_exec_checkout``: the flow's ``git_clone_config`` resolved to clone
  URLs, refs, relative checkout paths and per-repository credentials. The
  runner clones only when its local profile opts in with ``allow_checkout``.

Neither value is written to ``pending_job``.
"""

from __future__ import annotations

import logging
import posixpath
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow, crud_flow_execution, crud_runtime_session

logger = logging.getLogger(__name__)

#: Upper bound the runner also enforces.
HOST_EXEC_MAX_CHECKOUT_REPOSITORIES = 8


class HostExecDeliveryError(ValueError):
    """The control plane cannot prepare the flow inputs for a host lease."""


def host_exec_checkout_path(container_path: str) -> str:
    """Map a container clone path to a path relative to the execution dir.

    Flow configs use container paths such as ``/workspace`` or
    ``/workspace-2``. On a host the runner clones below its execution
    directory, so the leading slash is dropped.

    Args:
        container_path: Absolute container path from the clone resolver.

    Returns:
        A normalized relative POSIX path.

    Raises:
        HostExecDeliveryError: When the path escapes the execution directory.
    """
    raw = str(container_path or "")
    if ".." in raw.split("/"):
        raise HostExecDeliveryError("Host checkout path is not a repository path")
    relative = posixpath.normpath("/" + raw.lstrip("/")).lstrip("/")
    if not relative or relative == ".":
        raise HostExecDeliveryError("Host checkout path is not a repository path")
    return relative


def flow_uses_mcp(flow: Any) -> bool:
    """Return True when the flow grants any MCP server or tool.

    Args:
        flow: Flow row.

    Returns:
        True when the flow lists allowed MCP servers or tools.
    """
    return bool(
        getattr(flow, "allowed_mcp_tools", None)
        or getattr(flow, "allowed_mcp_servers", None)
    )


def build_host_exec_checkout(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve the flow checkout into the plan a host runner executes.

    Uses the same resolvers as hosted and Docker runs so repository, branch,
    commit and credential selection stay identical. Credentials never enter
    a URL; each repository carries its own username and token.

    Args:
        context: Output of
            ``FlowExecutionOrchestrator.prepare_host_exec_checkout_context``.

    Returns:
        The checkout plan, or None when no repository applies.

    Raises:
        HostExecDeliveryError: When repositories are configured but none
            resolves to a clone URL, or when there are too many.
    """
    from preloop.agents.codex import CodexAgent
    from preloop.utils.git_credentials import strip_url_credentials

    agent = CodexAgent(config={})
    git_config = context.get("git_clone_config") or {}
    repositories = agent._resolve_git_clone_repositories(context, git_config)
    if not repositories:
        return None
    if len(repositories) > HOST_EXEC_MAX_CHECKOUT_REPOSITORIES:
        raise HostExecDeliveryError(
            "Host checkout supports at most "
            f"{HOST_EXEC_MAX_CHECKOUT_REPOSITORIES} repositories"
        )
    (
        source_branch,
        _target_branch,
        commit_sha,
        git_user_name,
        git_user_email,
    ) = agent._resolve_git_branch_plan(context, git_config)
    trigger_data = context.get("trigger_event_data") or {}
    merge_request_ref = agent._extract_merge_request_ref_from_trigger(trigger_data)
    planned: List[Dict[str, Any]] = []
    for index, repo_config in enumerate(repositories):
        repo_url = agent._resolve_repository_clone_url(
            repo_config, index, context, trigger_data
        )
        if not repo_url:
            continue
        credential = agent._build_git_credential(repo_url, repo_config, context)
        entry: Dict[str, Any] = {
            "url": strip_url_credentials(repo_url),
            "path": host_exec_checkout_path(
                agent._resolve_repository_clone_path(repo_config, index)
            ),
        }
        pin_sha = agent._repository_pin_sha(repo_config)
        if pin_sha:
            entry["commit"] = pin_sha
        else:
            repo_source = (
                str(repo_config.get("source_branch") or repo_config.get("branch") or "")
                or source_branch
            )
            entry["branch"] = agent._resolve_repository_clone_branch(
                repo_config,
                commit_sha=commit_sha,
                source_branch=repo_source,
                trigger_data=trigger_data,
            )
            if commit_sha:
                entry["commit"] = commit_sha
                refs = [repo_source]
                if merge_request_ref:
                    refs.append(merge_request_ref)
                entry["fetch_refs"] = list(dict.fromkeys(ref for ref in refs if ref))
        if credential is not None:
            entry["username"] = credential.username
            entry["token"] = credential.token
        planned.append(entry)
    if not planned:
        raise HostExecDeliveryError(
            "Git clone is enabled but no repository URL could be resolved; set "
            "repository_url or trigger the flow from a project"
        )
    return {
        "repositories": planned,
        "git_user_name": git_user_name,
        "git_user_email": git_user_email,
    }


async def hydrate_host_exec_job(db: Session, job: Dict[str, Any]) -> Dict[str, Any]:
    """Add transient MCP and checkout inputs to a host-exec lease copy.

    Args:
        db: Database session.
        job: Persisted host-exec lease (never modified).

    Returns:
        A copy of ``job`` with ``host_exec_mcp`` and ``host_exec_checkout``
        when the flow uses them.

    Raises:
        HostExecDeliveryError: When the execution or flow is gone, the flow
            now asks for something a host cannot run, or a required input
            cannot be prepared.
    """
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
    from preloop.services.flow_runtime_token import create_flow_runtime_token
    from preloop.services.host_exec import host_exec_unavailable_reason

    try:
        execution_id = UUID(str(job.get("execution_id")))
    except ValueError as exc:
        raise HostExecDeliveryError("Invalid host execution id") from exc
    execution = crud_flow_execution.get(db, id=execution_id)
    if execution is None or getattr(execution, "flow_id", None) is None:
        raise HostExecDeliveryError("Host execution no longer exists")
    flow = crud_flow.get(db, id=execution.flow_id)
    if flow is None:
        raise HostExecDeliveryError("Host execution flow no longer exists")
    blocked = host_exec_unavailable_reason(
        git_clone_config=flow.git_clone_config,
        custom_commands=flow.custom_commands,
    )
    if blocked:
        raise HostExecDeliveryError(blocked)

    hydrated = dict(job)
    if flow_uses_mcp(flow):
        runtime_session = crud_runtime_session.get_by_source(
            db,
            account_id=flow.account_id,
            session_source_type="flow_execution",
            session_source_id=str(execution.id),
        )
        token, _ = create_flow_runtime_token(
            db,
            flow=flow,
            execution_id=execution.id,
            runtime_session_id=getattr(runtime_session, "id", None),
        )
        if not token:
            raise HostExecDeliveryError(
                "Could not mint the flow's MCP token for the host runner"
            )
        hydrated["host_exec_mcp"] = {"token": token}

    clone = flow.git_clone_config
    if isinstance(clone, dict) and clone.get("enabled"):
        orchestrator = FlowExecutionOrchestrator(
            db, execution.flow_id, execution.trigger_event_details or {}, None
        )
        orchestrator.execution_log = execution
        orchestrator._get_flow_details(refresh=True)
        context = await orchestrator.prepare_host_exec_checkout_context()
        checkout = build_host_exec_checkout(context) if context else None
        if checkout:
            hydrated["host_exec_checkout"] = checkout
    return hydrated
