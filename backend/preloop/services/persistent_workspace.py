"""Workspace contract for a persistent flow execution.

The ephemeral path resolves the same repository, ref, and commit inside
``ContainerExecutor`` and clones them into the container. A persistent run
does not get that container. This module asks those extractors for the
values and puts them on the ``send_message`` metadata so the sidecar can
check the repository out on the agent host.

No credential is included. The host uses its own git credentials. Tracker
tokens stay on the control plane.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

from preloop.services.runner_service import unwrap_agent_config
from preloop.utils.git_credentials import strip_url_credentials

logger = logging.getLogger(__name__)

MODE_PERSISTENT_CHECKOUT = "persistent_checkout"
MODE_CLONE_LESS = "clone_less"
MODE_EPHEMERAL = "ephemeral"

_CREDENTIAL_KEYS = frozenset(
    {
        "token",
        "password",
        "secret",
        "credentials",
        "git_credentials",
        "git_credentials_map",
        "authorization",
    }
)


def _mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, dict):
            return dumped
    return {}


def _trigger_payload(trigger: Any) -> Dict[str, Any]:
    if not isinstance(trigger, dict):
        return {}
    nested = trigger.get("payload")
    if isinstance(nested, dict):
        return nested
    return trigger


def _extractor() -> Any:
    """A container executor that only runs the clone-identity helpers.

    ``ContainerExecutor.__init__`` opens a Docker client. The extractors
    used here only log, so a bare instance is enough and the ephemeral
    clone commands stay untouched.
    """

    from preloop.agents.container import ContainerAgentExecutor

    host = ContainerAgentExecutor.__new__(ContainerAgentExecutor)
    host.logger = logger
    return host


def _safe_slug(value: str) -> Optional[str]:
    text = value.strip().strip("/")
    if not text or "\\" in text or ".." in text.split("/"):
        return None
    return text


def _slug_from_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return _safe_slug("/".join(parts[-2:]))
    return None


def _repository_slug(payload: Mapping[str, Any], repository_url: str) -> Optional[str]:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = repository.get("full_name") or repository.get("name")
        if isinstance(full_name, str) and full_name.strip():
            return _safe_slug(full_name)
    elif isinstance(repository, str) and repository.strip():
        return _safe_slug(repository)
    project = payload.get("project")
    if isinstance(project, dict):
        name = project.get("path_with_namespace") or project.get("path")
        if isinstance(name, str) and name.strip():
            return _safe_slug(name)
    if repository_url:
        return _slug_from_url(repository_url)
    return None


def _default_branch(
    payload: Mapping[str, Any],
    git_config: Mapping[str, Any],
    host: Any,
    trigger: Dict[str, Any],
) -> str:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        branch = repository.get("default_branch")
        if isinstance(branch, str) and branch.strip():
            return branch.strip()
    project = payload.get("project")
    if isinstance(project, dict):
        branch = project.get("default_branch")
        if isinstance(branch, str) and branch.strip():
            return branch.strip()
    configured = git_config.get("source_branch")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    target = host._extract_target_branch_from_trigger(trigger)
    if isinstance(target, str) and target.strip():
        return target.strip()
    return "main"


def _pr_number(payload: Mapping[str, Any]) -> Optional[int]:
    pull = payload.get("pull_request")
    if isinstance(pull, dict) and pull.get("number") is not None:
        try:
            return int(pull["number"])
        except (TypeError, ValueError):
            return None
    attributes = payload.get("object_attributes")
    if isinstance(attributes, dict) and attributes.get("iid") is not None:
        try:
            return int(attributes["iid"])
        except (TypeError, ValueError):
            return None
    merge_request = payload.get("merge_request")
    if isinstance(merge_request, dict) and merge_request.get("iid") is not None:
        try:
            return int(merge_request["iid"])
        except (TypeError, ValueError):
            return None
    return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_repository(git_config: Mapping[str, Any]) -> Dict[str, Any]:
    repositories = git_config.get("repositories")
    if isinstance(repositories, list) and repositories:
        first = repositories[0]
        if isinstance(first, dict):
            return first
    return {}


def ephemeral_clone_identity(
    git_clone_config: Any,
    trigger_event_data: Any,
) -> Optional[Dict[str, Any]]:
    """Values the container clone uses, without credentials.

    Returns:
        The checkout identity, or ``None`` when clone is disabled or the
        trigger and config name no repository.
    """

    git_config = _mapping(git_clone_config)
    if not git_config.get("enabled"):
        return None
    trigger = trigger_event_data if isinstance(trigger_event_data, dict) else {}
    payload = _trigger_payload(trigger)
    host = _extractor()
    repo = _first_repository(git_config)
    repository_url = ""
    configured = repo.get("repository_url")
    if isinstance(configured, str) and configured.strip():
        repository_url = configured.strip()
    if not repository_url:
        repository_url = host._extract_repo_url_from_trigger(trigger) or ""
    repository_url = strip_url_credentials(repository_url) if repository_url else ""
    repository_slug = _repository_slug(payload, repository_url)
    if not repository_url and not repository_slug:
        return None

    commit_sha = host._extract_commit_sha_from_trigger(trigger)
    source_branch = (
        str(repo.get("branch") or repo.get("source_branch") or "").strip()
        or host._extract_source_branch_from_trigger(trigger)
        or git_config.get("source_branch")
        or "main"
    )
    ref = host._resolve_repository_clone_branch(
        repo,
        commit_sha=commit_sha,
        source_branch=str(source_branch),
        trigger_data=trigger,
    )
    if not ref:
        ref = host._extract_merge_request_ref_from_trigger(trigger) or source_branch

    depth = _optional_int(repo.get("clone_depth", git_config.get("clone_depth")))
    submodules = bool(repo.get("submodules", git_config.get("submodules", False)))
    identity: Dict[str, Any] = {
        "repository_url": repository_url or None,
        "repository_slug": repository_slug,
        "default_branch": _default_branch(payload, git_config, host, trigger),
        "ref": ref or None,
        "sha": commit_sha or None,
        "pr_number": _pr_number(payload),
        "clone_depth": depth,
        "submodules": submodules,
    }
    return {
        key: value for key, value in identity.items() if key not in _CREDENTIAL_KEYS
    }


def workspace_mode(
    *,
    agent_config: Any = None,
    git_clone_config: Any = None,
    trigger_event_data: Any = None,
) -> str:
    """Prompt ``workspace.mode`` for this run.

    Ephemeral executions stay ``ephemeral`` even when clone is enabled,
    because the container still owns the checkout. Persistent executions
    are ``persistent_checkout`` when a repository can be resolved, and
    ``clone_less`` otherwise.
    """

    config = unwrap_agent_config(agent_config)
    if not isinstance(config, dict):
        config = agent_config if isinstance(agent_config, dict) else {}
    if config.get("execution_path") != "persistent":
        return MODE_EPHEMERAL
    identity = ephemeral_clone_identity(git_clone_config, trigger_event_data)
    if identity is None:
        return MODE_CLONE_LESS
    return MODE_PERSISTENT_CHECKOUT


def workspace_metadata(
    *,
    git_clone_config: Any = None,
    trigger_event_data: Any = None,
) -> Dict[str, Any]:
    """``workspace`` object for a persistent ``send_message``.

    Credentials are never copied from the flow config or the trigger.
    """

    identity = ephemeral_clone_identity(git_clone_config, trigger_event_data)
    if identity is None:
        return {"mode": MODE_CLONE_LESS}
    return {**identity, "mode": MODE_PERSISTENT_CHECKOUT}
