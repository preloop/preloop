"""Explicit native restricted-CI checkout without provider publication authority."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.services.publication_credentials import mint_repository_lease
from preloop.services.trusted_publisher import PublicationLease


# Initial native capability is verified against the actual Codex executor.
# New executors require an explicit exact-checkout and launch-boundary review.
READ_ONLY_NATIVE_AGENT_TYPES: frozenset[str] = frozenset({"codex"})


def read_only_review(config: Any) -> bool:
    """Whether an administrator selected the read-only review contract."""
    return isinstance(config, dict) and config.get("publication_mode") == "read_only"


def validate_read_only_configuration(config: dict[str, Any]) -> None:
    """Reject publication and alternate repository selection in this mode."""
    shared = config.get("ci_project_selection") is True
    if shared and not read_only_review(config):
        raise ValueError("CI project selection requires read_only publication mode")
    if not read_only_review(config):
        return
    repositories = config.get("repositories") or []
    if shared and (
        not isinstance(repositories, list)
        or not repositories
        or any(
            not isinstance(repo, dict)
            or not repo.get("project_id")
            or not repo.get("tracker_id")
            or repo.get("repository_url")
            for repo in repositories
        )
        or len({repo["project_id"] for repo in repositories}) != len(repositories)
    ):
        raise ValueError(
            "Shared CI review requires unique explicit project repositories"
        )
    if (
        config.get("enabled") is not True
        or (not shared and len(repositories) != 1)
        or config.get("repository_bindings")
        or config.get("create_pull_request")
        or config.get("publication_approval")
        or any(
            config.get(key)
            for key in (
                "report_publication",
                "follow_up_filing",
                "backport",
                "verification",
            )
        )
    ):
        raise ValueError(
            "Read-only review requires one native repository and no publication configuration"
        )


def validate_read_only_flow(flow: Any) -> None:
    """This initial capability has no external MCP tools or restored workspace."""
    config = flow.git_clone_config or {}
    if not read_only_review(config):
        return
    validate_read_only_configuration(config)
    validate_read_only_executor(flow)
    if (
        flow.runner_pool != "server"
        or flow.allowed_mcp_servers
        or flow.allowed_mcp_tools
    ):
        raise ValueError(
            "Read-only review requires server execution without MCP tools or servers"
        )


def validate_read_only_executor(
    flow: Any, context: dict[str, Any] | None = None, execution: Any = None
) -> None:
    """Check effective routing at admission, before lease issuance, and launch."""
    from preloop.services.runner_service import unwrap_agent_config

    reference = getattr(execution, "agent_session_reference", None)
    if isinstance(reference, str) and reference.startswith("runner:"):
        raise ValueError("Read-only review cannot resume a private runner session")
    context = context or {}
    kind = context.get("agent_type", getattr(flow, "agent_type", None))
    configs = [getattr(flow, "agent_config", None), context.get("agent_config")]
    if (
        kind not in READ_ONLY_NATIVE_AGENT_TYPES
        or getattr(flow, "runner_pool", None) != "server"
    ):
        raise ValueError("Read-only review requires a supported hosted native executor")
    for config in configs:
        config = unwrap_agent_config(config) or {}
        if not isinstance(config, dict):
            raise ValueError("Read-only review requires valid native configuration")
        for source in (config, config.get("agent_config") or {}):
            if isinstance(source, dict) and (
                source.get("execution_path") == "persistent"
                or source.get("host_exec_profile")
                or source.get("harness")
            ):
                raise ValueError(
                    "Read-only review requires a supported hosted native executor"
                )
    details = context.get("trigger_event_data") or {}
    payload = details.get("payload") or {}
    if context.get("host_exec_profile") or any(
        source.get("_runner") not in (None, "server")
        for source in (details, payload)
        if isinstance(source, dict)
    ):
        raise ValueError("Read-only review cannot override hosted native routing")


async def prepare_read_only_review(
    db: Session,
    flow: models.Flow,
    execution: models.FlowExecution,
    context: dict[str, Any],
) -> PublicationLease:
    """Reauthorize persisted ownership, then mint only a read-scoped App lease."""
    validate_read_only_flow(flow)
    validate_read_only_executor(flow, context, execution)
    binding = crud.crud_ci_execution.authorize_dispatch(db, execution=execution)
    if binding is None or not read_only_review(flow.git_clone_config):
        raise ValueError("Read-only review requires a restricted CI execution")
    if str(flow.id) != str(binding.flow_id) or str(flow.account_id) != str(
        binding.account_id
    ):
        raise ValueError("Read-only review flow binding changed")
    if any(
        context.get(key)
        for key in (
            "workspace_restore_archive",
            "cli_session_restore_archive",
            "native_session_reference",
            "checkpoint_resume_authorized",
            "published_branch_handoff_authorized",
        )
    ) or (context.get("trigger_event_data") or {}).get("_resume"):
        raise ValueError(
            "Read-only review does not support workspace or session restoration"
        )
    tracker = crud.crud_tracker.get_by_id_and_account(
        db, id=str(binding.tracker_id), account_id=str(binding.account_id)
    )
    if tracker is None or binding.tracker_type != "github":
        raise ValueError("Read-only review requires the owning GitHub App tracker")
    candidates = [
        repo
        for repo in flow.git_clone_config["repositories"]
        if repo.get("project_id") == str(binding.project_id)
    ]
    if len(candidates) != 1:
        raise ValueError("Read-only review requires one principal-bound repository")
    repository = dict(candidates[0])
    if (
        repository.get("project_id") != str(binding.project_id)
        or repository.get("tracker_id") != str(binding.tracker_id)
        or repository.get("repository_url")
    ):
        raise ValueError("Read-only review repository binding changed")
    url = f"https://github.com/{binding.repository_slug}.git"
    async with httpx.AsyncClient() as client:
        lease = await mint_repository_lease(tracker, url, write=False, client=client)
    if (
        not isinstance(lease.token, str)
        or not lease.token
        or lease.repository_url != url
        or lease.expires_at.tzinfo is None
        or lease.expires_at <= datetime.now(timezone.utc)
    ):
        raise ValueError("Read-only review lease is invalid or expired")
    repository.update(
        repository_url=url, branch=binding.base_branch, commit=binding.head_sha
    )
    config = dict(flow.git_clone_config)
    config.update(
        repositories=[repository],
        source_branch=binding.head_sha,
        target_branch=None,
        ci_project_selection=False,
    )
    context["git_clone_config"] = config
    context["read_only_review"] = True
    context["git_credentials_map"] = {
        url: {
            "token": lease.token,
            "tracker_type": "github",
            "permission": "read",
            "username": "x-access-token",
        }
    }
    return lease
