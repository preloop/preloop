"""Explicit native restricted-CI checkout without provider publication authority."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.services.publication_credentials import mint_repository_lease
from preloop.services.trusted_publisher import PublicationLease

logger = logging.getLogger(__name__)

# Initial native capability is verified against the actual Codex executor.
# New executors require an explicit exact-checkout and launch-boundary review.
READ_ONLY_NATIVE_AGENT_TYPES: frozenset[str] = frozenset({"codex"})
SERVER_POOL = "server"
AUTO_POOL = "auto"


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


def _flow_pool(flow: Any) -> str | None:
    pool = getattr(flow, "runner_pool", None)
    if not isinstance(pool, str) or not pool.strip():
        return None
    return pool.strip()


def read_only_private_runner(flow: Any) -> bool:
    """Whether the flow runs read-only review on a private runner host profile.

    The pool must be named explicitly (not ``server``, ``auto`` or the
    account default) and the flow's own ``agent_config`` must name the Codex
    host profile. Everything else about the executor is checked by
    :func:`validate_read_only_executor`.

    Args:
        flow: Flow row or namespace.

    Returns:
        True for the private-runner shape, False for the hosted shape.
    """
    from preloop.services.host_exec import host_exec_profile_name

    pool = _flow_pool(flow)
    if pool is None or pool.lower() in {SERVER_POOL, AUTO_POOL}:
        return False
    return bool(host_exec_profile_name(getattr(flow, "agent_config", None)))


def validate_read_only_flow(flow: Any) -> None:
    """Read-only review has no external MCP tools or restored workspace.

    Two executors are admitted: the hosted ephemeral Codex executor on the
    ``server`` pool, and a Codex host profile on an explicitly named private
    runner pool (the host CLI's own login serves the model).
    """
    config = flow.git_clone_config or {}
    if not read_only_review(config):
        return
    validate_read_only_configuration(config)
    validate_read_only_executor(flow)
    if flow.allowed_mcp_servers or flow.allowed_mcp_tools:
        raise ValueError("Read-only review requires no MCP tools or servers")


def _own_runner_reference(reference: str, execution: Any) -> bool:
    """A private-runner lease reference that names this very execution."""
    execution_id = getattr(execution, "id", None)
    parts = reference.split(":")
    return (
        execution_id is not None
        and len(parts) >= 3
        and parts[-1].lower() == str(execution_id).lower()
    )


def validate_read_only_executor(
    flow: Any, context: dict[str, Any] | None = None, execution: Any = None
) -> None:
    """Check effective routing at admission, before lease issuance, and launch.

    Args:
        flow: Flow row or namespace.
        context: Execution context (agent type/config overrides, trigger data).
        execution: Execution row, used to reject resumed runner sessions.

    Raises:
        ValueError: When the effective executor is not an admitted read-only
            review executor.
    """
    from preloop.services.host_exec import host_exec_profile_name, host_exec_workspace
    from preloop.services.runner_service import unwrap_agent_config

    private = read_only_private_runner(flow)
    reference = getattr(execution, "agent_session_reference", None)
    if (
        isinstance(reference, str)
        and reference.startswith("runner:")
        and not (private and _own_runner_reference(reference, execution))
    ):
        raise ValueError("Read-only review cannot resume a private runner session")
    context = context or {}
    kind = context.get("agent_type", getattr(flow, "agent_type", None))
    if kind not in READ_ONLY_NATIVE_AGENT_TYPES or (
        not private and getattr(flow, "runner_pool", None) != SERVER_POOL
    ):
        raise ValueError(
            "Read-only review requires a supported native executor: hosted "
            "Codex on the server pool, or a Codex host_exec_profile on a "
            "named private runner pool"
        )
    profile = (
        host_exec_profile_name(getattr(flow, "agent_config", None)) if private else None
    )
    configs = [getattr(flow, "agent_config", None), context.get("agent_config")]
    for config in configs:
        config = unwrap_agent_config(config) or {}
        if not isinstance(config, dict):
            raise ValueError("Read-only review requires valid native configuration")
        for source in (config, config.get("agent_config") or {}):
            if not isinstance(source, dict):
                continue
            if source.get("execution_path") == "persistent" or source.get("harness"):
                raise ValueError(
                    "Read-only review requires a supported native executor"
                )
            selected = source.get("host_exec_profile")
            if selected and (not private or selected != profile):
                raise ValueError(
                    "Read-only review requires a supported native executor"
                )
            if private and source.get("workspace") is not None:
                raise ValueError(
                    "Read-only review clones the admitted revision; an "
                    "authorized host directory is not supported"
                )
    if private and host_exec_workspace(getattr(flow, "agent_config", None)):
        raise ValueError(
            "Read-only review clones the admitted revision; an authorized "
            "host directory is not supported"
        )
    details = context.get("trigger_event_data") or {}
    payload = details.get("payload") or {}
    override = context.get("host_exec_profile")
    allowed_runner = (None, _flow_pool(flow)) if private else (None, SERVER_POOL)
    if (
        (override and (not private or override != profile))
        or any(
            source.get("_runner") not in allowed_runner
            for source in (details, payload)
            if isinstance(source, dict)
        )
        or (
            private
            and any(
                context.get(key)
                for key in ("host_exec_resume", "resume_from", "session_id")
            )
        )
    ):
        raise ValueError("Read-only review cannot override native routing")


async def prepare_read_only_review(
    db: Session,
    flow: models.Flow,
    execution: models.FlowExecution,
    context: dict[str, Any],
    *,
    mint: bool = True,
) -> PublicationLease | None:
    """Reauthorize persisted ownership, then mint only a read-scoped App lease.

    Args:
        db: Database session.
        flow: Read-only review flow.
        execution: Restricted CI execution row.
        context: Execution or host checkout context, extended in place.
        mint: Mint the lease. A private-runner execution context only needs
            the checks and the exact revision; its lease is minted when the
            checkout plan is delivered to the runner.

    Returns:
        The minted lease, or None when ``mint`` is False.

    Raises:
        ValueError: When authority, binding or executor checks fail.
    """
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
    if not mint:
        context["git_clone_config"] = config
        context["read_only_review"] = True
        context["git_credentials_map"] = {}
        return None
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


#: Lease key naming a read-only review on a host profile. Persisted leases
#: carry only ``{"mode": "read_only"}``; delivery adds the admitted commit.
HOST_READ_ONLY_LEASE_KEY = "host_exec_read_only"
#: Profile capability a runner advertises when it enforces this contract.
HOST_READ_ONLY_CAPABILITY = "host_read_only_review"
#: Control-plane key on the execution result for the runner receipt.
HOST_READ_ONLY_RESULT_KEY = "host_read_only_review"
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

# Leases delivered to a runner from this process, by execution id. Runner
# delivery and completion share one WebSocket (one process), and the
# orchestrator that starts the lease delivers the first plan itself. GitHub
# expiry bounds a lease this process cannot see, and the runner revokes its
# copy as soon as the checkout is done.
_delivered_read_leases: dict[str, list[PublicationLease]] = {}


def register_delivered_read_lease(execution_id: Any, lease: PublicationLease) -> None:
    """Remember a delivered read lease so completion can revoke it."""
    _delivered_read_leases.setdefault(str(execution_id), []).append(lease)


async def revoke_delivered_read_leases(execution_id: Any) -> int:
    """Revoke every read lease this process delivered for an execution.

    Args:
        execution_id: Execution id.

    Returns:
        Number of leases whose revocation GitHub confirmed.
    """
    from preloop.services.publication_credentials import revoke_repository_lease
    from preloop.services.trusted_publisher import PublicationError

    leases = _delivered_read_leases.pop(str(execution_id), [])
    revoked = 0
    if not leases:
        return 0
    async with httpx.AsyncClient() as client:
        for lease in leases:
            try:
                await revoke_repository_lease(lease, client)
                revoked += 1
            except PublicationError:
                logger.warning(
                    "Read-only review lease revocation failed; expiry remains enforced"
                )
    return revoked


def host_read_only_receipt_error(
    message: Mapping[str, Any], pending_job: Mapping[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """Check the runner's read-only receipt for a successful host completion.

    Args:
        message: Runner completion envelope.
        pending_job: Persisted lease, which names the admitted commit.

    Returns:
        ``(error, receipt)``. ``error`` is None when the runner proves it
        checked out the admitted commit and ran Codex in the read-only sandbox.
    """
    lease = pending_job.get(HOST_READ_ONLY_LEASE_KEY)
    expected = lease.get("commit") if isinstance(lease, Mapping) else None
    receipt = message.get(HOST_READ_ONLY_LEASE_KEY)
    if not isinstance(receipt, Mapping):
        return "Runner did not confirm the read-only review contract", None
    commit = receipt.get("commit")
    clean = {
        "commit": commit if isinstance(commit, str) else None,
        "sandbox": receipt.get("sandbox"),
        "lease_revoked": receipt.get("lease_revoked") is True,
    }
    if (
        not isinstance(expected, str)
        or not _SHA_RE.fullmatch(expected)
        or commit != expected
    ):
        return "Runner checkout does not match the admitted revision", clean
    if clean["sandbox"] != "read-only":
        return "Runner did not run Codex in the read-only sandbox", clean
    return None, clean


#: Line the hosted read-only container prints, with the admitted commit, as
#: the last step of its startup chain: after the pinned checkout is verified
#: and setup commands completed, before the agent starts. Any earlier failure
#: exits the container first, so the line is never printed for it.
READ_ONLY_STARTUP_MARKER = "PRELOOP_READ_ONLY_STARTUP"
#: Version of the startup receipt shape.
STARTUP_RECEIPT_VERSION = 1


def read_only_startup_line(commit: str) -> str:
    """The exact startup line for an admitted commit.

    Args:
        commit: Admitted full-length lowercase head SHA.

    Returns:
        The line the container prints once startup completed.

    Raises:
        ValueError: When ``commit`` is not a full-length lowercase SHA.
    """
    if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
        raise ValueError("Read-only review requires a full admitted commit SHA")
    return f"{READ_ONLY_STARTUP_MARKER} {commit}"


def hosted_startup_receipt(
    commit: str, *, setup_completed: bool, agent_started_at: str | None
) -> dict[str, Any]:
    """Build the controller-observed startup receipt for a hosted review.

    Args:
        commit: Admitted head SHA the pinned checkout was verified against.
        setup_completed: Whether the startup chain finished without a setup
            failure being reported.
        agent_started_at: ISO-8601 UTC time the agent start was observed.

    Returns:
        The receipt stored on ``flow_execution.startup_receipt``.
    """
    return {
        "version": STARTUP_RECEIPT_VERSION,
        "executor": "hosted",
        "commit": commit,
        "checkout_verified": setup_completed,
        "setup_completed": setup_completed,
        "agent_started_at": agent_started_at,
        "lease_revoked": None,
    }


def public_startup_receipt(execution: Any) -> dict[str, Any] | None:
    """Project the startup receipt for a restricted CI caller.

    The hosted receipt comes from ``flow_execution.startup_receipt``. A
    private-runner review reports the runner receipt it stored on the result
    under ``host_read_only_review``, mapped to the same shape.

    Args:
        execution: Flow execution row.

    Returns:
        The receipt, or None when the execution has none (yet).
    """
    stored = getattr(execution, "startup_receipt", None)
    if isinstance(stored, Mapping):
        return {
            "version": STARTUP_RECEIPT_VERSION,
            "executor": "hosted",
            "commit": stored.get("commit"),
            "checkout_verified": stored.get("checkout_verified") is True,
            "setup_completed": stored.get("setup_completed") is True,
            "agent_started_at": stored.get("agent_started_at"),
            "lease_revoked": stored.get("lease_revoked"),
        }
    result = getattr(execution, "result", None)
    runner = (
        result.get(HOST_READ_ONLY_RESULT_KEY) if isinstance(result, Mapping) else None
    )
    if not isinstance(runner, Mapping):
        return None
    binding = getattr(execution, "ci_review_binding", None)
    admitted = binding.get("head_sha") if isinstance(binding, Mapping) else None
    commit = runner.get("commit")
    verified = isinstance(commit, str) and commit == admitted
    return {
        "version": STARTUP_RECEIPT_VERSION,
        "executor": "private_runner",
        "commit": commit if isinstance(commit, str) else None,
        "checkout_verified": verified,
        "setup_completed": verified and runner.get("sandbox") == "read-only",
        "agent_started_at": None,
        "lease_revoked": runner.get("lease_revoked") is True,
        "sandbox": runner.get("sandbox"),
    }
