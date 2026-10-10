"""Managed legacy publication for native Copilot host profiles (#1069).

Scope: one repository, legacy publication, Copilot host profiles whose runner
advertises ``host_publication`` (the local profile sets ``allow_publish`` and
``allow_checkout``). The runner commits and pushes the checkout to the
managed ``preloop/`` branch after the CLI exits successfully and reports a
receipt on the completion envelope. The control plane then opens the pull
request with the bound code-host tracker client and binds it through
``record_opened_pr``. The agent never supplies a pull request URL.

Isolated publication, multi-repository publication and Cursor publication
stay unavailable with explicit validation errors.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Mapping, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

HOST_PUBLICATION_CAPABILITY = "host_publication"
#: Control-plane owned key on the execution result. Agent JSON cannot set it.
HOST_PUBLICATION_RESULT_KEY = "host_publication"
#: Transient lease key. Persisted leases carry only ``{"mode": "legacy"}``;
#: delivery replaces it with the runner plan.
HOST_PUBLICATION_LEASE_KEY = "host_exec_publication"
HOST_PUBLICATION_AGENT_TYPES = frozenset({"copilot"})

MULTI_REPOSITORY_PUBLICATION_UNAVAILABLE = (
    "host publication supports exactly one repository; multi-repository "
    "publication is unavailable on native host profiles"
)
PUBLICATION_UNSUPPORTED_TRACKER = (
    "host publication opens pull requests on Bitbucket Cloud repositories only"
)

_RECEIPT_STATUSES = frozenset({"pushed", "no_changes", "failed"})
_RECEIPT_REASONS = frozenset(
    {"push_conflict", "credential_rejected", "repository_config_unsafe", "push_failed"}
)
_BRANCH_RE = re.compile(r"^preloop/[A-Za-z0-9._/+-]{1,240}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def _clone_mapping(git_clone_config: Any) -> Optional[Mapping[str, Any]]:
    if hasattr(git_clone_config, "model_dump"):
        git_clone_config = git_clone_config.model_dump()
    return git_clone_config if isinstance(git_clone_config, Mapping) else None


def host_publication_requested(git_clone_config: Any) -> bool:
    """True when the flow clones and opens a pull request."""
    clone = _clone_mapping(git_clone_config)
    if clone is None:
        return bool(getattr(git_clone_config, "create_pull_request", False))
    return bool(clone.get("create_pull_request"))


def host_publication_config_error(
    agent_type: Any, git_clone_config: Any
) -> Optional[str]:
    """Validate a publishing host flow configuration (no runner check).

    Args:
        agent_type: Flow agent type.
        git_clone_config: Flow clone configuration.

    Returns:
        A reason string, or None when the configuration is the supported
        Copilot / one repository / legacy shape.
    """
    from preloop.services.host_exec import (
        ISOLATED_PUBLICATION_UNAVAILABLE,
        PULL_REQUEST_UNAVAILABLE,
    )

    kind = agent_type.strip().lower() if isinstance(agent_type, str) else ""
    if kind not in HOST_PUBLICATION_AGENT_TYPES:
        # Cursor and callers that do not name the agent keep the original
        # refusal, so older code paths stay fail-closed.
        return PULL_REQUEST_UNAVAILABLE
    clone = _clone_mapping(git_clone_config)
    mode = (
        clone.get("publication_mode")
        if clone is not None
        else getattr(git_clone_config, "publication_mode", None)
    )
    if mode not in (None, "", "legacy"):
        return ISOLATED_PUBLICATION_UNAVAILABLE
    repositories = (
        clone.get("repositories")
        if clone is not None
        else getattr(git_clone_config, "repositories", None)
    )
    if isinstance(repositories, list) and len(repositories) > 1:
        return MULTI_REPOSITORY_PUBLICATION_UNAVAILABLE
    return None


def runner_profile_can_publish(runner: Any, profile: str) -> bool:
    """True when ``runner`` advertised ``host_publication`` for ``profile``."""
    capabilities = getattr(runner, "capabilities", None) or {}
    if not isinstance(capabilities, Mapping):
        return False
    want = (profile or "").strip().lower()
    for item in capabilities.get("host_exec_profiles") or []:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip().lower() == want:
            caps = item.get("capabilities") or []
            return isinstance(caps, list) and HOST_PUBLICATION_CAPABILITY in caps
    return False


def account_has_publishing_runner(
    db: Session, *, account_id: Any, runner_pool: Any, profile: str
) -> bool:
    """True when a registered runner in the pool can publish for ``profile``.

    Offline runners count: a saved flow waits for its runner. A runner that
    never advertised the capability (an old CLI or a profile without
    ``allow_publish``) does not.
    """
    from preloop.models.crud import crud_flow_runner

    if account_id is None:
        return False
    pool = runner_pool if isinstance(runner_pool, str) else ""
    runners = crud_flow_runner.find_matching(
        db, account_id=account_id, pool=pool, online_only=False
    )
    return any(runner_profile_can_publish(runner, profile) for runner in runners)


def trusted_host_publication_receipt(
    message: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Validate the runner-authored receipt from the completion envelope.

    Args:
        message: Runner ``complete`` message.

    Returns:
        A bounded receipt, or None when absent or malformed.
    """
    raw = message.get("host_publication")
    if not isinstance(raw, Mapping):
        return None
    status = raw.get("status")
    branch = raw.get("branch")
    if status not in _RECEIPT_STATUSES or not isinstance(branch, str):
        return None
    if not _BRANCH_RE.fullmatch(branch) or ".." in branch:
        return None
    receipt: Dict[str, Any] = {"status": status, "branch": branch}
    head = raw.get("head_sha")
    if head is not None:
        if not isinstance(head, str) or not _SHA_RE.fullmatch(head):
            return None
        receipt["head_sha"] = head
    if status == "pushed" and "head_sha" not in receipt:
        return None
    if status == "failed":
        reason = raw.get("reason")
        receipt["reason"] = reason if reason in _RECEIPT_REASONS else "push_failed"
        receipt["recoverable"] = True
    return receipt


def apply_host_publication_completion(
    status: str,
    error: Optional[str],
    result: Optional[Dict[str, Any]],
    message: Mapping[str, Any],
    pending_job: Mapping[str, Any],
) -> tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Bind the trusted receipt onto a host-exec completion.

    An agent-authored ``host_publication`` key is always dropped. A publishing
    lease that claims success without a pushed receipt fails.
    """
    if isinstance(result, dict) and HOST_PUBLICATION_RESULT_KEY in result:
        result = {k: v for k, v in result.items() if k != HOST_PUBLICATION_RESULT_KEY}
    if not pending_job.get(HOST_PUBLICATION_LEASE_KEY):
        return status, error, result
    receipt = trusted_host_publication_receipt(message)
    if receipt is not None:
        result = {**(result or {}), HOST_PUBLICATION_RESULT_KEY: receipt}
    if status != "SUCCEEDED":
        return status, error, result
    if receipt is None or receipt["status"] == "failed":
        return (
            "FAILED",
            "publication_missing: the runner did not report a managed push",
            result,
        )
    if receipt["status"] == "no_changes":
        return (
            "FAILED",
            f"publication_missing: the run left no changes on {receipt['branch']}, "
            "so nothing was pushed and no pull request was opened",
            result,
        )
    return status, error, result


async def open_and_bind_host_pull_request(
    db: Session,
    *,
    execution_id: Any,
    client: Any,
    branch: str,
    base_branch: str,
    title: str,
    description: str,
) -> Dict[str, str]:
    """Open (or find) the pull request for ``branch`` and bind it.

    Idempotent: an open pull request whose head is ``branch`` is reused, and
    a create call whose response was lost is reconciled by looking the branch
    up again, so a retry never opens a second pull request.

    Args:
        db: Database session.
        execution_id: Execution that pushed ``branch``.
        client: Code-host tracker client of the bound repository.
        branch: Managed source branch the runner pushed.
        base_branch: Branch to merge into.
        title: Pull request title.
        description: Pull request body.

    Returns:
        ``{"url", "branch", "provider", "source"}``.

    Raises:
        ValueError: The tracker cannot open pull requests here.
        Exception: The create call failed and no pull request exists.
    """
    from preloop.services.flow_pr_binding import record_opened_pr
    from preloop.sync.trackers.base import BaseTracker

    kind = str(getattr(client, "tracker_type", "") or "").lower()
    if kind != "bitbucket":
        raise ValueError(PUBLICATION_UNSUPPORTED_TRACKER)

    async def lookup() -> Optional[Dict[str, Any]]:
        listing = await client.list_open_pull_requests_by_source_branch(branch)
        return BaseTracker._first_listed_for_branch(listing, branch)

    found = await lookup()
    source = "branch_lookup"
    if found is None:
        try:
            found = await client.create_pull_request(
                title=title,
                source_branch=branch,
                target_branch=base_branch,
                description=description,
            )
            source = "control_plane_create"
        except Exception:
            # The create may have succeeded with the response lost. The
            # forge is the source of truth; never create twice.
            found = await lookup()
            if found is None:
                raise
    url = str((found or {}).get("url") or "")
    if not url:
        raise ValueError("the code host did not return a pull request URL")
    record_opened_pr(
        db,
        execution_id,
        url,
        source_branch=branch,
        opened_at=found.get("created_at"),
        raise_errors=True,
    )
    return {"url": url, "branch": branch, "provider": kind, "source": source}
