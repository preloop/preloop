"""Private-runner host execution profiles (named local CLIs).

The control plane stores advertised profile names only. Executables, argv,
and credentials stay on the runner host. Native success uses a distinct
``host_exec`` completion protocol; Docker launch v1 stays on the Docker path.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from uuid import UUID

HOST_EXEC_AGENT_TYPE = "cursor"
#: Agent types that run only as a named host profile, mapped to the harness
#: capability the runner advertises and reports in its completion result.
HOST_EXEC_HARNESSES: Mapping[str, str] = {
    "cursor": "cursor_cli",
    "copilot": "copilot_cli",
    "devin": "devin_cli",
    "claude_code": "claude_code",
    "codex": "codex_cli",
    "opencode": "opencode",
    "deepseek": "deepseek",
}
HOST_EXEC_AGENT_TYPES = frozenset({"cursor", "copilot", "devin", "claude_code"})
# These harnesses also support Docker; native execution requires an explicit profile.
HOST_EXEC_PROFILE_AGENT_TYPES = frozenset(HOST_EXEC_HARNESSES)
HOST_EXEC_LABELS = {
    "cursor": "Cursor",
    "copilot": "Copilot CLI",
    "devin": "Devin CLI",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "deepseek": "DeepSeek CLI (dsh)",
}
#: Agent config key holding the per-flow model alias for each host harness.
HOST_EXEC_MODEL_KEYS: Mapping[str, str] = {
    "cursor": "cursor_model",
    "copilot": "copilot_model",
    "devin": "devin_model",
    "claude_code": "claude_code_model",
    "codex": "codex_model",
    "opencode": "opencode_model",
    "deepseek": "deepseek_model",
}
HOST_EXEC_COMPLETION_PROTOCOL = "host_exec"
#: Inverse of ``HOST_EXEC_HARNESSES``: harness inventory id -> agent type.
HOST_EXEC_AGENT_TYPE_BY_HARNESS: Mapping[str, str] = {
    harness: kind for kind, harness in HOST_EXEC_HARNESSES.items()
}
#: What to do when no runner is eligible for ``agent_config.harness``.
HARNESS_FALLBACK_QUEUE = "queue"
HARNESS_FALLBACK_SERVER = "fallback_server"
HARNESS_FALLBACK_FAIL = "fail"
HARNESS_FALLBACK_MODES = frozenset(
    {HARNESS_FALLBACK_QUEUE, HARNESS_FALLBACK_SERVER, HARNESS_FALLBACK_FAIL}
)
HARNESS_QUEUE_TIMEOUT_DEFAULT_SECONDS = 1800
HARNESS_QUEUE_TIMEOUT_MIN_SECONDS = 60
HARNESS_QUEUE_TIMEOUT_MAX_SECONDS = 86400
#: Hosted harness a ``fallback_server`` run uses with its fallback model.
HARNESS_SERVER_FALLBACK_AGENT_TYPE = "codex"
#: Execution context flag set when a run fell back to the server pool.
HARNESS_FALLBACK_CONTEXT_KEY = "_harness_fell_back_to_server"
#: Routing reasons stored on ``flow_executions.routing_reason`` (contract B).
ROUTING_NO_RUNNER_WITH_HARNESS = "no_runner_with_harness"
ROUTING_HARNESS_SIGNED_OUT = "harness_signed_out"
ROUTING_HARNESS_DISABLED = "harness_disabled"
ROUTING_MODEL_NOT_AVAILABLE = "model_not_available"
ROUTING_RUNNER_OFFLINE = "runner_offline"
ROUTING_OWNER_HAS_NO_RUNNER = "owner_has_no_runner"
ROUTING_QUEUE_TIMEOUT = "queue_timeout"
ROUTING_FELL_BACK_TO_SERVER = "fell_back_to_server"
ROUTING_REASONS = frozenset(
    {
        ROUTING_NO_RUNNER_WITH_HARNESS,
        ROUTING_HARNESS_SIGNED_OUT,
        ROUTING_HARNESS_DISABLED,
        ROUTING_MODEL_NOT_AVAILABLE,
        ROUTING_RUNNER_OFFLINE,
        ROUTING_OWNER_HAS_NO_RUNNER,
        ROUTING_QUEUE_TIMEOUT,
        ROUTING_FELL_BACK_TO_SERVER,
    }
)
#: Billing modes on executions (contract B cost label).
BILLING_MODE_SEAT = "seat"
BILLING_MODE_METERED = "metered"
BILLING_MODE_UNKNOWN = "unknown"
BILLING_MODES = frozenset(
    {BILLING_MODE_SEAT, BILLING_MODE_METERED, BILLING_MODE_UNKNOWN}
)
#: Console and export wording for a seat run (matches NOT_METERED_MARKER).
SEAT_BILLING_LABEL = "Seat (not metered by gateway)"
HOST_EXEC_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HOST_EXEC_CAPABILITIES = frozenset(
    {
        "host_exec",
        "cursor_cli",
        "copilot_cli",
        "devin_cli",
        "claude_code",
        "codex_cli",
        "opencode",
        "deepseek",
        "stdout",
        "cancel",
        "host_publication",
        "host_continuation",
        "host_read_only_review",
    }
)
HOST_EXEC_MAX_RESULT_BYTES = 256 * 1024
#: Lease key naming the authorized directory a host run works in (#1669).
HOST_EXEC_WORKSPACE_KEY = "host_exec_workspace"
#: ``agent_config.workspace.kind`` a host-exec flow may select. The shape is
#: shared with persistent flows; host runs accept only authorized directories.
WORKSPACE_KIND_AUTHORIZED_DIRECTORY = "authorized_directory"
_AUTHORIZED_DIR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HOST_EXEC_TERMINAL = frozenset(
    {"SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT", "CANCELLED"}
)


def _normalized_agent_type(agent_type: Any) -> str:
    return agent_type.strip().lower() if isinstance(agent_type, str) else ""


def is_host_exec_agent_type(agent_type: Any) -> bool:
    """Return True for agent types that only run as a named host profile.

    Args:
        agent_type: Flow or lease agent type.

    Returns:
        True for ``cursor``, ``copilot``, ``devin`` and ``claude_code``.
    """
    return _normalized_agent_type(agent_type) in HOST_EXEC_AGENT_TYPES


def host_exec_harness(agent_type: Any) -> Optional[str]:
    """Return the runner harness capability for a host-exec agent type.

    Args:
        agent_type: Flow or lease agent type.

    Returns:
        ``cursor_cli``, ``copilot_cli``, ``devin_cli`` or ``claude_code``,
        or None for other agent types.
    """
    return HOST_EXEC_HARNESSES.get(_normalized_agent_type(agent_type))


def host_exec_model_identifier(agent_type: Any, agent_config: Any) -> Optional[str]:
    """Return the per-flow host model alias from agent_config, if set.

    Args:
        agent_type: Host-exec agent type.
        agent_config: Flow agent configuration.

    Returns:
        The stripped alias, or None when unset. The runner maps it through
        its local profile ``model_map``.
    """
    key = HOST_EXEC_MODEL_KEYS.get(_normalized_agent_type(agent_type))
    if not key or not isinstance(agent_config, Mapping):
        return None
    raw = agent_config.get(key)
    value = raw.strip() if isinstance(raw, str) else ""
    return value or None


def host_exec_profile_name(
    agent_config: Any = None,
    context: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return the advertised profile name from context or agent_config."""
    if context:
        direct = context.get("host_exec_profile")
        if isinstance(direct, str) and direct.strip():
            return _validated_profile_name(direct)
        nested = context.get("agent_config")
        name = _profile_from_mapping(nested)
        if name:
            return name
    return _profile_from_mapping(agent_config)


def _unwrapped_config(agent_config: Any) -> Mapping[str, Any]:
    if not isinstance(agent_config, Mapping):
        return {}
    inner = agent_config.get("agent_config")
    if set(agent_config) == {"agent_config"} and isinstance(inner, Mapping):
        return inner
    return agent_config


def host_exec_harness_selector(agent_type: Any, agent_config: Any) -> Optional[str]:
    """Return the inventory harness id when the flow routes by harness.

    A flow routes by harness when ``agent_config.harness`` is set and no
    ``host_exec_profile`` is pinned (an explicit profile wins, so #956 flows
    route exactly as before).

    Args:
        agent_type: Flow agent type (``copilot``, ``cursor``, ``devin`` or
            ``claude_code``).
        agent_config: Flow agent configuration.

    Returns:
        ``copilot_cli`` / ``cursor_cli`` / ``devin_cli`` / ``claude_code``,
        or None for profile-pinned and non-host flows.
    """
    config = _unwrapped_config(agent_config)
    raw = config.get("harness")
    harness = raw.strip() if isinstance(raw, str) else ""
    if (
        not harness
        or host_exec_profile_name(config)
        or _normalized_agent_type(agent_type) not in HOST_EXEC_AGENT_TYPES
    ):
        return None
    if HOST_EXEC_HARNESSES.get(_normalized_agent_type(agent_type)) != harness:
        return None
    return harness


#: Profile name the runner synthesises for an inventory harness (contract A
#: reserved names). A hand-written profile with the same name overrides it on
#: the host, so the name is stable either way.
HARNESS_GENERATED_PROFILES: Mapping[str, str] = {
    "copilot_cli": "copilot",
    "cursor_cli": "cursor",
    "devin_cli": "devin",
    "claude_code": "claude",
}


def host_exec_effective_profile(agent_type: Any, agent_config: Any) -> Optional[str]:
    """Profile name a host run leases: the pinned one, else the harness's.

    ``host_exec_profile`` wins when both are set (explicit beats inferred).
    """
    pinned = host_exec_profile_name(agent_config)
    if pinned:
        return pinned
    harness = host_exec_harness_selector(agent_type, agent_config)
    return HARNESS_GENERATED_PROFILES.get(harness) if harness else None


def host_exec_workspace(agent_config: Any) -> Optional[Dict[str, Any]]:
    """Return the validated authorized directory a host run works in.

    ``agent_config.workspace`` uses the same shape as persistent flows:
    ``{"kind": "authorized_directory", "id": ..., "allow_dirty": bool}``.
    The runner resolves the id against its own ``preloop runner dirs`` list
    and keeps refusing the filesystem root, the home directory, a
    ``read_only`` entry, a harness the entry does not allow, and a git tree
    with uncommitted changes unless ``allow_dirty`` is true.

    Args:
        agent_config: Flow agent configuration (wrapped or not).

    Returns:
        ``{"kind": "authorized_directory", "id": ..., "allow_dirty": bool}``,
        or None when the flow names no workspace.

    Raises:
        ValueError: When ``agent_config.workspace`` is not that shape.
    """
    raw = _unwrapped_config(agent_config).get("workspace")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("agent_config.workspace must be an object")
    unknown = sorted(
        str(key) for key in raw if key not in {"kind", "id", "allow_dirty"}
    )
    if unknown:
        raise ValueError(
            "agent_config.workspace has unsupported keys for host execution: "
            + ", ".join(unknown)
        )
    if raw.get("kind") != WORKSPACE_KIND_AUTHORIZED_DIRECTORY:
        raise ValueError(
            'host execution runs in an authorized directory: agent_config.workspace.kind must be "authorized_directory"'
        )
    directory_id = raw.get("id")
    if not isinstance(directory_id, str) or not _AUTHORIZED_DIR_ID_RE.fullmatch(
        directory_id
    ):
        raise ValueError(
            "agent_config.workspace.id must be an authorized directory id "
            "(see `preloop runner dirs list`)"
        )
    allow_dirty = raw.get("allow_dirty", False)
    if not isinstance(allow_dirty, bool):
        raise ValueError("agent_config.workspace.allow_dirty must be a boolean")
    return {
        "kind": WORKSPACE_KIND_AUTHORIZED_DIRECTORY,
        "id": directory_id,
        "allow_dirty": allow_dirty,
    }


def runner_offers_authorized_directory(
    runner: Any, directory_id: str, harness: Optional[str]
) -> bool:
    """True when the runner advertises the directory for this harness.

    Args:
        runner: Runner row with ``capabilities``.
        directory_id: ``agent_config.workspace.id``.
        harness: Harness id of the lease (``claude_code``, ``copilot_cli``...).

    Returns:
        True when the directory is advertised, writable and allows the
        harness. The runner re-checks everything locally.
    """
    from preloop.services.runner_workspace_credentials import (
        runner_authorized_directories,
    )

    for item in runner_authorized_directories(runner):
        if item.get("id") != directory_id:
            continue
        if item.get("mode") != "write":
            return False
        harnesses = item.get("harnesses")
        return harnesses == "all" or (
            harness is not None and isinstance(harnesses, list) and harness in harnesses
        )
    return False


def harness_fallback_mode(agent_config: Any) -> str:
    """``queue`` (default), ``fallback_server`` or ``fail``."""
    raw = _unwrapped_config(agent_config).get("harness_fallback")
    return raw if raw in HARNESS_FALLBACK_MODES else HARNESS_FALLBACK_QUEUE


def harness_queue_timeout_seconds(agent_config: Any) -> int:
    """Queue wait for a harness-routed run, clamped to the allowed range."""
    raw = _unwrapped_config(agent_config).get("harness_queue_timeout_seconds")
    if type(raw) is not int:
        return HARNESS_QUEUE_TIMEOUT_DEFAULT_SECONDS
    return max(
        HARNESS_QUEUE_TIMEOUT_MIN_SECONDS,
        min(HARNESS_QUEUE_TIMEOUT_MAX_SECONDS, raw),
    )


def harness_pinned_runner_id(agent_config: Any) -> Optional[UUID]:
    """Optional ``agent_config.runner_id`` pin, or None."""
    raw = _unwrapped_config(agent_config).get("runner_id")
    if raw in (None, ""):
        return None
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        return None


def harness_selector_config_error(agent_type: Any, agent_config: Any) -> Optional[str]:
    """Validate the contract B harness keys inside ``agent_config``.

    Returns:
        A validation message, or None when the keys are absent or valid.
    """
    config = _unwrapped_config(agent_config)
    keys = (
        "harness",
        "runner_id",
        "harness_fallback",
        "harness_queue_timeout_seconds",
        "fallback_model_identifier",
    )
    if not any(config.get(key) is not None for key in keys):
        return None
    harness = config.get("harness")
    if harness is not None:
        if (
            not isinstance(harness, str)
            or harness not in HOST_EXEC_AGENT_TYPE_BY_HARNESS
        ):
            return "agent_config.harness must be one of " + ", ".join(
                sorted(HOST_EXEC_AGENT_TYPE_BY_HARNESS)
            )
        if HOST_EXEC_AGENT_TYPE_BY_HARNESS[harness] != _normalized_agent_type(
            agent_type
        ):
            return (
                f"agent_config.harness {harness} requires agent_type "
                f"{HOST_EXEC_AGENT_TYPE_BY_HARNESS[harness]}"
            )
    runner_id = config.get("runner_id")
    if runner_id not in (None, ""):
        try:
            UUID(str(runner_id))
        except (TypeError, ValueError):
            return "agent_config.runner_id must be a runner id (UUID)"
    mode = config.get("harness_fallback")
    if mode is not None and mode not in HARNESS_FALLBACK_MODES:
        return "agent_config.harness_fallback must be queue, fallback_server or fail"
    timeout = config.get("harness_queue_timeout_seconds")
    if timeout is not None and (
        type(timeout) is not int
        or not HARNESS_QUEUE_TIMEOUT_MIN_SECONDS
        <= timeout
        <= HARNESS_QUEUE_TIMEOUT_MAX_SECONDS
    ):
        return (
            "agent_config.harness_queue_timeout_seconds must be an integer "
            f"between {HARNESS_QUEUE_TIMEOUT_MIN_SECONDS} and "
            f"{HARNESS_QUEUE_TIMEOUT_MAX_SECONDS}"
        )
    fallback_model = config.get("fallback_model_identifier")
    if fallback_model is not None and (
        not isinstance(fallback_model, str)
        or not fallback_model.strip()
        or len(fallback_model) > 255
    ):
        return "agent_config.fallback_model_identifier must be a model identifier"
    if mode == HARNESS_FALLBACK_SERVER and not fallback_model:
        return (
            "agent_config.harness_fallback fallback_server requires "
            "agent_config.fallback_model_identifier"
        )
    return None


def _profile_from_mapping(value: Any) -> Optional[str]:
    if not isinstance(value, Mapping):
        return None
    if set(value) == {"agent_config"} and isinstance(value.get("agent_config"), dict):
        value = value["agent_config"]
    raw = value.get("host_exec_profile")
    if isinstance(raw, str) and raw.strip():
        return _validated_profile_name(raw)
    return None


def _as_host_exec_mapping(item: Any) -> Optional[Mapping[str, Any]]:
    """Accept JSON objects or pydantic advertisements from register()."""
    if isinstance(item, Mapping):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _validated_profile_name(raw: str) -> Optional[str]:
    name = raw.strip()
    if HOST_EXEC_PROFILE_NAME_RE.fullmatch(name):
        return name
    return None


def normalize_host_exec_advertisements(raw: Any) -> Dict[str, Any]:
    """Store bounded name+capability advertisements, never executables."""
    profiles: List[Dict[str, Any]] = []
    items: Iterable[Any]
    if isinstance(raw, Mapping):
        items = raw.get("host_exec_profiles") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    seen: set[str] = set()
    if not isinstance(items, list):
        items = []
    for item in items[:64]:
        item = _as_host_exec_mapping(item)
        if item is None:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not HOST_EXEC_PROFILE_NAME_RE.fullmatch(
            name.strip()
        ):
            continue
        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        caps_raw = item.get("capabilities") or []
        caps: List[str] = []
        if isinstance(caps_raw, list):
            for cap in caps_raw[:16]:
                if isinstance(cap, str) and cap in HOST_EXEC_CAPABILITIES:
                    caps.append(cap)
        if "host_exec" not in caps:
            caps.insert(0, "host_exec")
        profile = {"name": name.strip(), "capabilities": caps}
        models = item.get("models")
        if isinstance(models, list):
            profile["models"] = list(
                dict.fromkeys(
                    value
                    for value in models[:64]
                    if isinstance(value, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value)
                )
            )
        profiles.append(profile)
    return {"host_exec_profiles": profiles}


def runner_has_host_exec_profile(
    runner: Any,
    name: str,
    model_identifier: Optional[str] = None,
    agent_type: Any = HOST_EXEC_AGENT_TYPE,
    require_publication: bool = False,
    require_continuation: bool = False,
    require_read_only_review: bool = False,
) -> bool:
    """True when the runner advertised this profile for the leased harness.

    Args:
        runner: Runner row with ``capabilities``.
        name: Profile name from the flow.
        model_identifier: Model alias the profile must advertise, if set.
        agent_type: Host-exec agent type. The profile must advertise the
            matching harness capability (``cursor_cli``, ``copilot_cli``,
            ``devin_cli`` or ``claude_code``).
        require_publication: The lease publishes; the profile must also
            advertise ``host_publication``. Older runners never do.
        require_continuation: The lease resumes a Copilot session; the
            profile must advertise ``host_continuation``.
        require_read_only_review: The lease is a read-only review; the
            profile must advertise ``host_read_only_review``. Older runners
            never do, so they cannot run it with a writable sandbox.

    Returns:
        True when the runner can run this lease.
    """
    want = (name or "").strip().lower()
    harness = host_exec_harness(agent_type)
    if not want or harness is None:
        return False
    capabilities = getattr(runner, "capabilities", None) or {}
    if not isinstance(capabilities, Mapping):
        return False
    advertised = capabilities.get("host_exec_profiles") or []
    if not isinstance(advertised, list):
        return False
    for item in advertised:
        if not isinstance(item, Mapping):
            continue
        item_name = item.get("name")
        if isinstance(item_name, str) and item_name.strip().lower() == want:
            caps = item.get("capabilities") or []
            if not isinstance(caps, list) or not {"host_exec", harness}.issubset(caps):
                return False
            if require_publication and "host_publication" not in caps:
                return False
            if require_continuation and "host_continuation" not in caps:
                return False
            if require_read_only_review and "host_read_only_review" not in caps:
                return False
            models = item.get("models") or []
            return not model_identifier or (
                isinstance(models, list) and model_identifier in models
            )
    return False


def host_exec_flow_error(
    *,
    agent_type: Any = None,
    agent_config: Any = None,
    runner_pool: Any = None,
    git_clone_config: Any = None,
    uses_mcp: bool = False,
) -> Optional[str]:
    """Return a validation error for invalid host-exec / hosted combinations.

    Args:
        agent_type: Flow agent type.
        agent_config: Flow agent configuration.
        runner_pool: Flow runner pool.
        git_clone_config: Flow checkout configuration.
        uses_mcp: The flow grants Preloop MCP servers or tools, so a host
            run receives a per-job MCP config.

    Returns:
        A validation message, or None when the combination is allowed.
    """
    profile = host_exec_profile_name(agent_config)
    kind = _normalized_agent_type(agent_type)
    pool = (runner_pool or "").strip().lower() if isinstance(runner_pool, str) else ""
    if profile is None and isinstance(agent_config, Mapping):
        raw = agent_config.get("host_exec_profile")
        if isinstance(raw, str) and raw.strip() and not _validated_profile_name(raw):
            return "host_exec_profile is not a valid profile name"
    selector_error = harness_selector_config_error(agent_type, agent_config)
    if selector_error:
        return selector_error
    harness = host_exec_harness_selector(agent_type, agent_config)
    if kind in HOST_EXEC_AGENT_TYPES and not profile and not harness:
        return (
            f"agent type {kind} requires agent_config.host_exec_profile or "
            "agent_config.harness on a private runner"
        )
    if (
        kind in HOST_EXEC_PROFILE_AGENT_TYPES - HOST_EXEC_AGENT_TYPES
        and isinstance(agent_config, Mapping)
        and agent_config.get("harness")
        and not profile
    ):
        return "this native harness requires an explicit host_exec_profile"
    if harness and pool == "server":
        return "harness routing cannot run on hosted compute"
    if profile and kind and kind not in HOST_EXEC_PROFILE_AGENT_TYPES:
        return (
            "host_exec_profile requires a supported native host agent type; "
            f"harnesses cannot use a host execution profile (got {kind})"
        )
    if profile and pool == "server":
        return "host execution profiles cannot run on hosted compute"
    if profile or harness or kind in HOST_EXEC_AGENT_TYPES:
        try:
            workspace = host_exec_workspace(agent_config)
        except ValueError as exc:
            return str(exc)
        if workspace is not None:
            if _clone_enabled(git_clone_config):
                return (
                    "a host flow that runs in an authorized directory cannot "
                    "also clone repositories; disable git_clone_config or "
                    "remove agent_config.workspace"
                )
            if harness_fallback_mode(agent_config) == HARNESS_FALLBACK_SERVER:
                return (
                    "a host flow that runs in an authorized directory cannot "
                    "fall back to hosted compute, where the directory does not "
                    "exist; use harness_fallback queue or fail"
                )
            if uses_mcp and kind in _WORKSPACE_MCP_IN_CWD:
                return (
                    f"{HOST_EXEC_LABELS[kind]} reads MCP configuration from its "
                    "working directory, so a flow with Preloop tools cannot run "
                    "it in an authorized directory; remove the tools or "
                    "agent_config.workspace"
                )
    return None


#: Host CLIs that read the per-job MCP config from their working directory.
#: In an authorized directory the runner would have to write into the
#: operator's project, so it refuses; the API refuses the flow up front.
_WORKSPACE_MCP_IN_CWD = frozenset({"cursor", "devin"})


def _clone_enabled(git_clone_config: Any) -> bool:
    """Whether a flow's checkout config is turned on (mapping or model)."""
    if git_clone_config is None:
        return False
    if hasattr(git_clone_config, "model_dump"):
        git_clone_config = git_clone_config.model_dump()
    if isinstance(git_clone_config, Mapping):
        return bool(git_clone_config.get("enabled"))
    return bool(getattr(git_clone_config, "enabled", False))


PULL_REQUEST_UNAVAILABLE = (
    "host execution cannot publish pull requests; isolated "
    "publication is unavailable on this path"
)
ISOLATED_PUBLICATION_UNAVAILABLE = (
    "isolated publication is unavailable on native host profiles"
)


def host_exec_unavailable_reason(
    *,
    git_clone_config: Any = None,
    resume_from: Any = None,
    session_id: Any = None,
    custom_commands: Any = None,
    publication_mode: Any = None,
    agent_type: Any = None,
) -> Optional[str]:
    """Fail closed for publication, setup commands and native resume.

    Cloning the flow's repositories is supported; the runner performs the
    checkout only when its local profile sets ``allow_checkout``.

    Args:
        git_clone_config: Flow checkout config. ``create_pull_request`` and
            ``publication_mode`` are read from a mapping or model.
            ``create_pull_request`` is accepted only for the managed legacy
            Copilot shape (see ``host_exec_publication``); the runner
            capability is checked separately before a lease starts.
        resume_from: Prior execution id for native CLI resume.
        session_id: Server-supplied session id, which host execution rejects.
        custom_commands: Remote command block. Enabled commands are rejected.
        publication_mode: Explicit mode. When omitted, the mode on
            ``git_clone_config`` is used. ``isolated`` on either the explicit
            mode or the configured mode is rejected.

    Returns:
        A reason string when this host profile cannot run the request, or
        None when the request is allowed.
    """
    if isinstance(session_id, str) and session_id.strip():
        return "host execution does not accept server-supplied session ids"
    if isinstance(resume_from, str) and resume_from.strip():
        return "host execution does not resume native CLI sessions in this version"
    clone = git_clone_config
    configured_mode = publication_mode
    if hasattr(clone, "model_dump"):
        clone = clone.model_dump()
    elif hasattr(clone, "create_pull_request") and not isinstance(clone, Mapping):
        if getattr(clone, "create_pull_request", False):
            reason = _publication_reason(agent_type, clone, configured_mode)
            if reason:
                return reason
        if configured_mode is None:
            configured_mode = getattr(clone, "publication_mode", None)
        clone = None
    if isinstance(clone, Mapping) and clone.get("create_pull_request"):
        reason = _publication_reason(agent_type, clone, configured_mode)
        if reason:
            return reason
    if isinstance(clone, Mapping) and configured_mode is None:
        configured_mode = clone.get("publication_mode")
    if configured_mode == "isolated" or (
        isinstance(clone, Mapping) and clone.get("publication_mode") == "isolated"
    ):
        return ISOLATED_PUBLICATION_UNAVAILABLE
    # A checkout itself is allowed: the runner clones into the execution
    # directory when the local profile opts in (``allow_checkout``). Remote
    # setup commands would run control-plane shell on the host, so they stay
    # unavailable.
    if isinstance(clone, Mapping) and clone.get("setup_commands"):
        return "host execution does not run remote clone setup commands"
    commands = custom_commands
    if hasattr(commands, "model_dump"):
        commands = commands.model_dump()
    if isinstance(commands, Mapping) and commands.get("enabled"):
        return "host execution does not support remote custom commands in this version"
    return None


def _publication_reason(
    agent_type: Any, clone: Any, configured_mode: Any
) -> Optional[str]:
    """Why this host flow cannot publish, or None for the managed shape."""
    from preloop.services.host_exec_publication import (
        host_publication_config_error,
    )

    reason = host_publication_config_error(agent_type, clone)
    if reason is None and configured_mode == "isolated":
        return ISOLATED_PUBLICATION_UNAVAILABLE
    return reason


def _completion_result(message: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    result = message.get("result")
    if not isinstance(result, dict):
        return None
    try:
        encoded = json.dumps(result).encode()
    except (TypeError, ValueError):
        return None
    if len(encoded) > HOST_EXEC_MAX_RESULT_BYTES:
        return None
    return result


def validate_host_exec_completion(
    message: Mapping[str, Any],
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Native host-exec success requires exit 0 and a structured verdict."""
    from preloop.services.flow_orchestrator import _result_artifact_confirmation

    status = str(message.get("status") or "FAILED").upper()
    error = str(message["error"]) if message.get("error") else None
    result = _completion_result(message)
    if status not in _HOST_EXEC_TERMINAL:
        return "FAILED", "Invalid runner completion status", result
    if status != "SUCCEEDED":
        return status, error, result
    if message.get("completion_protocol") != HOST_EXEC_COMPLETION_PROTOCOL:
        return (
            "FAILED",
            "host execution requires the host_exec completion protocol",
            result,
        )
    if type(message.get("exit_code")) is not int or message["exit_code"] != 0:
        return (
            "FAILED",
            "Runner exited without a valid structured completion result",
            result,
        )
    if _result_artifact_confirmation(result) != "success":
        return (
            "FAILED",
            "Runner exited without a valid structured completion result",
            result,
        )
    return "SUCCEEDED", None, result


def finalize_runner_completion(
    message: Mapping[str, Any],
    pending_job: Any = None,
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Validate complete envelopes without weakening Docker launch v1.

    Host-exec jobs never succeed via exit 0 alone or via a Docker
    ``launch_version`` envelope. When Docker launch v1 is present, that
    fail-closed validator stays in charge of Docker jobs.
    """
    from preloop.agents.runner_launch import validate_runner_completion

    pending = pending_job if isinstance(pending_job, Mapping) else {}
    profile = host_exec_profile_name(pending)
    if pending.get("completion_protocol") == HOST_EXEC_COMPLETION_PROTOCOL:
        agent_type = pending.get("agent_type")
        harness = host_exec_harness(agent_type)
        if (
            not profile
            or harness is None
            or agent_type not in HOST_EXEC_PROFILE_AGENT_TYPES
            or pending.get("launch_version") is not None
        ):
            return "FAILED", "Invalid durable host execution lease", None
        if (
            message.get("completion_protocol") != HOST_EXEC_COMPLETION_PROTOCOL
            or message.get("host_exec_profile") != profile
            or message.get("launch_version") is not None
        ):
            return (
                "FAILED",
                "Completion does not match the leased host_exec profile/protocol",
                None,
            )
        status, error, result = validate_host_exec_completion(message)
        if status == "SUCCEEDED" and (result or {}).get("harness") != harness:
            return (
                "FAILED",
                "Completion does not identify the leased "
                f"{HOST_EXEC_LABELS[agent_type]} harness",
                result,
            )
        from preloop.services.host_exec_publication import (
            apply_host_publication_completion,
        )

        from preloop.services.host_exec_continuation import check_resumed_session

        status, error = check_resumed_session(status, error, result, pending)
        status, error, result = apply_host_publication_completion(
            status, error, result, message, pending
        )
        status, error, result = _apply_read_only_completion(
            status, error, result, message, pending
        )
        return status, error, _mark_not_gateway_metered(result)
    # Protocol selection comes only from persisted lease metadata. A message
    # cannot opt into a weaker/legacy validator by naming another protocol.
    if profile:
        return "FAILED", "Invalid durable runner lease protocol", None
    return validate_runner_completion(dict(message), leased_job=dict(pending))


def _apply_read_only_completion(
    status: str,
    error: Optional[str],
    result: Optional[Dict[str, Any]],
    message: Mapping[str, Any],
    pending: Mapping[str, Any],
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Require the runner's read-only receipt and record it on the result.

    A read-only review succeeds only when the runner reports that it checked
    out the admitted commit and ran Codex in the read-only sandbox. Agent JSON
    cannot author the control-plane result key.
    """
    from preloop.services.read_only_review import (
        HOST_READ_ONLY_LEASE_KEY,
        HOST_READ_ONLY_RESULT_KEY,
        host_read_only_receipt_error,
    )

    if result is not None:
        result = {
            key: value
            for key, value in result.items()
            if key != HOST_READ_ONLY_RESULT_KEY
        }
    if not pending.get(HOST_READ_ONLY_LEASE_KEY):
        return status, error, result
    problem, receipt = host_read_only_receipt_error(message, pending)
    if receipt is not None:
        result = {**(result or {}), HOST_READ_ONLY_RESULT_KEY: receipt}
    if status == "SUCCEEDED" and problem:
        return "FAILED", problem, result
    return status, error, result


def _mark_not_gateway_metered(
    result: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Record that a host run bypassed the gateway, overriding agent JSON.

    Host profiles use the runner user's own CLI login, so no gateway row
    exists for the model spend. The UI reads this server-set marker instead
    of implying a cost estimate.
    """
    if result is None:
        return None
    return {**result, "gateway_metered": False}


def apply_runner_completion_to_execution(
    db: Any,
    execution: Any,
    *,
    account_id: UUID,
    status: str,
    error: str | None,
    result: dict[str, Any] | None,
    message: Mapping[str, Any],
    pending_job: Mapping[str, Any] | None = None,
) -> None:
    """Persist terminal status, sanitized result, and trusted evidence outcome.

    WebSocket complete and tests share this path. Agent JSON cannot author
    ``evidence_upload``; only the top-level completion field is bound.
    """
    from preloop.models.crud import crud_flow_execution
    from preloop.services.flow_artifacts import (
        bind_terminal_evidence,
        job_requires_evidence_upload,
        sanitize_captured_result,
        trusted_evidence_upload,
    )

    cleaned = sanitize_captured_result(result) if result is not None else None
    crud_flow_execution.apply_runner_completion(
        db,
        db_obj=execution,
        status=status,
        error=error,
        result=cleaned,
    )
    upload = trusted_evidence_upload(message)
    if upload is None and job_requires_evidence_upload(pending_job):
        upload = "failed"
    bind_terminal_evidence(
        db,
        account_id=account_id,
        execution=execution,
        evidence_upload=upload,
    )
    from preloop.services.host_exec_continuation import (
        record_host_continuation_session,
    )

    record_host_continuation_session(
        db, execution, status=status, result=cleaned, pending_job=pending_job
    )
    from preloop.services.host_exec_usage import record_host_exec_completion_usage

    record_host_exec_completion_usage(
        db,
        execution,
        account_id=account_id,
        result=cleaned,
        pending_job=pending_job,
    )
