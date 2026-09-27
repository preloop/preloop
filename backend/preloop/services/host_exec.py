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
}
HOST_EXEC_AGENT_TYPES = frozenset(HOST_EXEC_HARNESSES)
_HOST_EXEC_LABELS = {"cursor": "Cursor", "copilot": "Copilot CLI"}
#: Agent config key holding the per-flow model alias for each host harness.
HOST_EXEC_MODEL_KEYS: Mapping[str, str] = {
    "cursor": "cursor_model",
    "copilot": "copilot_model",
}
HOST_EXEC_COMPLETION_PROTOCOL = "host_exec"
HOST_EXEC_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HOST_EXEC_CAPABILITIES = frozenset(
    {"host_exec", "cursor_cli", "copilot_cli", "stdout", "cancel"}
)
HOST_EXEC_MAX_RESULT_BYTES = 256 * 1024
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
        True for ``cursor`` and ``copilot``.
    """
    return _normalized_agent_type(agent_type) in HOST_EXEC_AGENT_TYPES


def host_exec_harness(agent_type: Any) -> Optional[str]:
    """Return the runner harness capability for a host-exec agent type.

    Args:
        agent_type: Flow or lease agent type.

    Returns:
        ``cursor_cli`` or ``copilot_cli``, or None for other agent types.
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
) -> bool:
    """True when the runner advertised this profile for the leased harness.

    Args:
        runner: Runner row with ``capabilities``.
        name: Profile name from the flow.
        model_identifier: Model alias the profile must advertise, if set.
        agent_type: Host-exec agent type. The profile must advertise the
            matching harness capability (``cursor_cli`` or ``copilot_cli``).

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
) -> Optional[str]:
    """Return a validation error for invalid host-exec / hosted combinations."""
    profile = host_exec_profile_name(agent_config)
    kind = _normalized_agent_type(agent_type)
    pool = (runner_pool or "").strip().lower() if isinstance(runner_pool, str) else ""
    if profile is None and isinstance(agent_config, Mapping):
        raw = agent_config.get("host_exec_profile")
        if isinstance(raw, str) and raw.strip() and not _validated_profile_name(raw):
            return "host_exec_profile is not a valid profile name"
    if kind in HOST_EXEC_AGENT_TYPES and not profile:
        return (
            f"agent type {kind} requires agent_config.host_exec_profile on a "
            "private runner"
        )
    if profile and kind and kind not in HOST_EXEC_AGENT_TYPES:
        return (
            "host_exec_profile requires agent_type cursor or copilot; Docker "
            f"harnesses cannot use a host execution profile (got {kind})"
        )
    if profile and pool == "server":
        return "host execution profiles cannot run on hosted compute"
    return None


_PULL_REQUEST_UNAVAILABLE = (
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
) -> Optional[str]:
    """Fail closed for publication, setup commands and native resume.

    Cloning the flow's repositories is supported; the runner performs the
    checkout only when its local profile sets ``allow_checkout``.

    Args:
        git_clone_config: Flow checkout config. ``create_pull_request`` and
            ``publication_mode`` are read from a mapping or model.
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
            return _PULL_REQUEST_UNAVAILABLE
        if configured_mode is None:
            configured_mode = getattr(clone, "publication_mode", None)
        clone = None
    if isinstance(clone, Mapping) and clone.get("create_pull_request"):
        return _PULL_REQUEST_UNAVAILABLE
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
            or agent_type not in HOST_EXEC_AGENT_TYPES
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
                f"{_HOST_EXEC_LABELS[agent_type]} harness",
                result,
            )
        return status, error, _mark_not_gateway_metered(result)
    # Protocol selection comes only from persisted lease metadata. A message
    # cannot opt into a weaker/legacy validator by naming another protocol.
    if profile:
        return "FAILED", "Invalid durable runner lease protocol", None
    return validate_runner_completion(dict(message), leased_job=dict(pending))


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
    from preloop.services.host_exec_usage import record_host_exec_completion_usage

    record_host_exec_completion_usage(
        db,
        execution,
        account_id=account_id,
        result=cleaned,
        pending_job=pending_job,
    )
