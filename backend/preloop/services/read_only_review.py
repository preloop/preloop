"""Explicit native restricted-CI checkout without provider publication authority."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.services.publication_credentials import mint_repository_lease
from preloop.services.trusted_publisher import PublicationLease


def read_only_review(config: Any) -> bool:
    """Whether an administrator selected the read-only review contract."""
    return isinstance(config, dict) and config.get("publication_mode") == "read_only"


def validate_read_only_configuration(config: dict[str, Any]) -> None:
    """Reject publication and alternate repository selection in this mode."""
    if not read_only_review(config):
        return
    if (
        config.get("enabled") is not True
        or len(config.get("repositories") or []) != 1
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
    if (
        flow.runner_pool != "server"
        or flow.allowed_mcp_servers
        or flow.allowed_mcp_tools
    ):
        raise ValueError(
            "Read-only review requires server execution without MCP tools or servers"
        )


async def prepare_read_only_review(
    db: Session,
    flow: models.Flow,
    execution: models.FlowExecution,
    context: dict[str, Any],
) -> PublicationLease:
    """Reauthorize persisted ownership, then mint only a read-scoped App lease."""
    validate_read_only_flow(flow)
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
    repository = dict(flow.git_clone_config["repositories"][0])
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
        repositories=[repository], source_branch=binding.head_sha, target_branch=None
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
