"""Private-runner host execution profiles (named local CLIs).

The control plane stores advertised profile names only. Executables, argv,
and credentials stay on the runner host. Native success uses a distinct
``host_exec`` completion protocol; Docker launch v1 stays on the Docker path.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

HOST_EXEC_AGENT_TYPE = "cursor"
HOST_EXEC_COMPLETION_PROTOCOL = "host_exec"
HOST_EXEC_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HOST_EXEC_CAPABILITIES = frozenset({"host_exec", "cursor_cli", "stdout", "cancel"})
HOST_EXEC_MAX_RESULT_BYTES = 256 * 1024
_HOST_EXEC_TERMINAL = frozenset(
    {"SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT", "CANCELLED"}
)


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
    runner: Any, name: str, model_identifier: Optional[str] = None
) -> bool:
    """True when the runner advertised this host execution profile."""
    want = (name or "").strip().lower()
    if not want:
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
            if not isinstance(caps, list) or not {"host_exec", "cursor_cli"}.issubset(
                caps
            ):
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
    kind = (agent_type or "").strip().lower() if isinstance(agent_type, str) else ""
    pool = (runner_pool or "").strip().lower() if isinstance(runner_pool, str) else ""
    if profile is None and isinstance(agent_config, Mapping):
        raw = agent_config.get("host_exec_profile")
        if isinstance(raw, str) and raw.strip() and not _validated_profile_name(raw):
            return "host_exec_profile is not a valid profile name"
    if kind == HOST_EXEC_AGENT_TYPE and not profile:
        return (
            "agent type cursor requires agent_config.host_exec_profile on a "
            "private runner"
        )
    if profile and kind and kind != HOST_EXEC_AGENT_TYPE:
        return (
            "host_exec_profile requires agent_type cursor; Docker harnesses "
            f"cannot use a host execution profile (got {kind})"
        )
    if profile and pool == "server":
        return "host execution profiles cannot run on hosted compute"
    return None


def host_exec_unavailable_reason(
    *,
    git_clone_config: Any = None,
    resume_from: Any = None,
    session_id: Any = None,
    custom_commands: Any = None,
) -> Optional[str]:
    """Fail closed for publication and native resume in this first slice."""
    if isinstance(session_id, str) and session_id.strip():
        return "host execution does not accept server-supplied session ids"
    if isinstance(resume_from, str) and resume_from.strip():
        return "host execution does not resume native CLI sessions in this version"
    clone = git_clone_config
    if hasattr(clone, "model_dump"):
        clone = clone.model_dump()
    elif hasattr(clone, "create_pull_request") and not isinstance(clone, Mapping):
        if getattr(clone, "create_pull_request", False):
            return (
                "host execution cannot publish pull requests; isolated "
                "publication is unavailable on this path"
            )
        clone = None
    if isinstance(clone, Mapping) and clone.get("create_pull_request"):
        return (
            "host execution cannot publish pull requests; isolated "
            "publication is unavailable on this path"
        )
    if isinstance(clone, Mapping) and (
        clone.get("enabled") or clone.get("repositories") or clone.get("setup_commands")
    ):
        return "host execution does not support remote clone/setup commands in this version"
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
        if (
            not profile
            or pending.get("agent_type") != HOST_EXEC_AGENT_TYPE
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
        if status == "SUCCEEDED" and (result or {}).get("harness") != "cursor_cli":
            return (
                "FAILED",
                "Completion does not identify the leased Cursor harness",
                result,
            )
        return status, error, result
    # Protocol selection comes only from persisted lease metadata. A message
    # cannot opt into a weaker/legacy validator by naming another protocol.
    if profile:
        return "FAILED", "Invalid durable runner lease protocol", None
    return validate_runner_completion(dict(message), leased_job=dict(pending))
