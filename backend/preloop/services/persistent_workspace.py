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
import re
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

from preloop.services.runner_service import unwrap_agent_config
from preloop.utils.git_credentials import strip_url_credentials

logger = logging.getLogger(__name__)

MODE_PERSISTENT_CHECKOUT = "persistent_checkout"
MODE_CLONE_LESS = "clone_less"
MODE_EPHEMERAL = "ephemeral"


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


_SLUG_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:=@+-]*$")


def _safe_slug(value: str) -> Optional[str]:
    text = value.strip().strip("/")
    if not text or "\\" in text:
        return None
    parts = text.split("/")
    if any(part in {"", ".."} for part in parts):
        return None
    if any(_SLUG_SEGMENT.fullmatch(part) is None for part in parts):
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


def _first_repository(git_config: Mapping[str, Any]) -> Dict[str, Any]:
    repositories = git_config.get("repositories")
    if isinstance(repositories, list) and len(repositories) == 1:
        first = repositories[0]
        if isinstance(first, dict):
            return first
    return {}


def credential_free_clone_url(url: str) -> Optional[str]:
    """Clone URL with any password removed.

    ``https`` userinfo is stripped entirely. ``ssh://git@host/...`` keeps
    the username, because ``git`` is the protocol user, not a secret.
    Other schemes are refused.
    """

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in {"http", "https"}:
        return strip_url_credentials(url)
    if scheme == "ssh":
        if not parsed.password:
            return url
        user = parsed.username or "git"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"ssh://{user}@{host}{port}{parsed.path}"
    if scheme == "git":
        return url
    if not scheme and url.startswith("git@"):
        return url
    return None


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
    repositories = git_config.get("repositories")
    if isinstance(repositories, list) and len(repositories) > 1:
        # The container clones every entry. One workspace object cannot
        # name them all, so this run stays clone-less instead of checking
        # out only the first.
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
    if repository_url:
        cleaned = credential_free_clone_url(repository_url)
        if cleaned is None:
            return None
        repository_url = cleaned
    repository_slug = _repository_slug(payload, repository_url)
    if not repository_url and not repository_slug:
        return None

    commit_sha = host._extract_commit_sha_from_trigger(trigger)
    source_branch = (
        str(repo.get("branch") or "").strip()
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
        ref = source_branch
    fetch_ref = host._extract_merge_request_ref_from_trigger(trigger)
    identity: Dict[str, Any] = {
        "repository_url": repository_url or None,
        "repository_slug": repository_slug,
        "default_branch": _default_branch(payload, git_config, host, trigger),
        "ref": ref or None,
        "fetch_ref": fetch_ref,
        "sha": commit_sha or None,
        "pr_number": _pr_number(payload),
    }
    return identity


def persistent_preset_rejection(
    agent_config: Any, preset_name: Optional[str]
) -> Optional[str]:
    """Reason a persistent run cannot use this catalog preset, if any.

    Blank flows and renamed account copies have no catalog name, so they
    are not rejected here.
    """

    config = unwrap_agent_config(agent_config)
    if not isinstance(config, dict):
        config = agent_config if isinstance(agent_config, dict) else {}
    if not isinstance(config, dict) or config.get("execution_path") != "persistent":
        return None
    if not preset_name:
        return None
    from preloop.flow_presets import PRESET_SLUGS_BY_NAME, supports_persistent_for_slug

    slug = PRESET_SLUGS_BY_NAME.get(preset_name)
    if slug is None or supports_persistent_for_slug(slug):
        return None
    return (
        "This preset does not support persistent execution. "
        "It expects an ephemeral checkout."
    )


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
