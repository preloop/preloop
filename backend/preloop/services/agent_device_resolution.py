"""Place managed agents on fleet devices when no fingerprint link exists.

The device fleet links a managed agent to a device through a discovery
candidate or runner inventory entry carrying the agent's id. Agents enrolled
before the CLI sent its device link on every enrollment have none, but they
do carry the hostname they enrolled from. This module resolves such an agent
to the one device of the account whose hostname matches, so the fleet map and
the agent page agree on where the agent runs. It never rewrites data.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

_SEPARATORS = re.compile(r"[\s_\-]+")

# Runner inventory names harnesses by their CLI; managed agents use the kind.
HARNESS_AGENT_KIND = {
    "codex_cli": "codex",
    "cursor_cli": "cursor",
    "devin_cli": "devin",
    "vscode_copilot": "vscode",
    "vs_code_copilot": "vscode",
    "vs_code": "vscode",
    "github_copilot_cli": "copilot_cli",
}


def agent_kind_for_harness(kind: Any) -> str:
    """Return the managed-agent kind a reported harness or candidate names.

    Accepts harness ids (``codex_cli``) and display names (``Codex CLI``,
    ``GitHub Copilot CLI``, ``VS Code Copilot``).
    """
    value = _SEPARATORS.sub("_", str(kind or "").strip().lower()).strip("_")
    return HARNESS_AGENT_KIND.get(value, value)


def normalize_hostname(name: Any) -> str | None:
    """Reduce a hostname to a comparable machine name.

    Lowercases, drops a ``.local`` suffix or any domain, and treats ``-``,
    ``_`` and whitespace alike, so ``Example-Laptop.local`` and
    ``example-laptop`` compare equal.

    Args:
        name: Hostname as reported by a runner or an enrollment.

    Returns:
        Normalized name, or None when nothing usable remains.
    """
    if not isinstance(name, str):
        return None
    value = name.strip().lower().rstrip(".")
    value = value.split(".", 1)[0]
    value = _SEPARATORS.sub("-", value).strip("-")
    return value or None


def _device_user_ids(device: Mapping[str, Any]) -> set[str]:
    return {str(u["id"]) for u in device.get("users") or [] if u.get("id")}


def resolve_hostname_devices(
    devices: Iterable[Mapping[str, Any]], agents: Iterable[Any]
) -> dict[str, str]:
    """Map managed agents with no id link to the device they run on.

    An agent already linked to any device by id is left alone. Two signals
    follow, strongest first:

    1. A device's unlinked inventory or discovery entry of the agent's kind,
       when the agent's owner is among the device's users and its
       enrollment hostname, if known, names that device. Placed only when
       exactly one such agent matches the entry and the agent matches on
       one device only; agents an entry cannot tell apart stay unplaced.
    2. Otherwise, the agent's ``enrollment_hostname`` names exactly one device and,
       when both are known, the agent's owner is among the device's users.

    Args:
        devices: Device fleet projection items.
        agents: Managed agent rows with ``id``, ``agent_kind``,
            ``enrollment_hostname`` and ``owner_user_id``.

    Returns:
        Device key keyed by managed agent id.
    """
    devices = list(devices)
    linked = {
        str(entry["managed_agent_id"])
        for device in devices
        for entry in device.get("agents") or []
        if entry.get("managed_agent_id")
    }
    unlinked = [agent for agent in agents if str(agent.id) not in linked]
    device_names = {
        str(device["device_key"]): {normalize_hostname(device.get("hostname"))}
        | {
            normalize_hostname(runner.get("hostname"))
            for runner in device.get("runners") or []
        }
        - {None}
        for device in devices
    }

    # Index unlinked agents by (kind, owner) once, with their hostname
    # normalised once, so each entry only looks at agents it could be.
    candidates: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    for agent in unlinked:
        owner = getattr(agent, "owner_user_id", None)
        if not owner:
            continue
        kind = agent_kind_for_harness(getattr(agent, "agent_kind", None))
        candidates.setdefault((kind, str(owner)), []).append(
            (
                str(agent.id),
                normalize_hostname(getattr(agent, "enrollment_hostname", None)),
            )
        )
    by_kind: dict[str, set[str]] = {}
    # Agents an entry could belong to but cannot tell apart stay unplaced.
    ambiguous: set[str] = set()
    for device in devices:
        key = str(device["device_key"])
        names = device_names[key]
        users = _device_user_ids(device)
        for entry in device.get("agents") or []:
            if entry.get("managed_agent_id") or entry.get("status") in {
                "ignored",
                "dismissed",
            }:
                continue
            kind = agent_kind_for_harness(entry.get("kind"))
            matches = [
                agent_id
                for user_id in users
                for agent_id, name in candidates.get((kind, user_id), [])
                if name is None or name in names
            ]
            if len(matches) == 1:
                by_kind.setdefault(matches[0], set()).add(key)
            else:
                ambiguous.update(matches)
    resolved = {
        agent_id: next(iter(keys))
        for agent_id, keys in by_kind.items()
        if len(keys) == 1 and agent_id not in ambiguous
    }

    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for device in devices:
        for name in device_names[str(device["device_key"])]:
            by_name.setdefault(str(name), []).append(device)
    for agent in unlinked:
        agent_id = str(agent.id)
        name = normalize_hostname(getattr(agent, "enrollment_hostname", None))
        if agent_id in by_kind or agent_id in ambiguous or name is None:
            continue
        matches = by_name.get(name, [])
        if len(matches) != 1:
            continue
        device = matches[0]
        owner = getattr(agent, "owner_user_id", None)
        users = _device_user_ids(device)
        if owner and users and str(owner) not in users:
            continue
        resolved[agent_id] = str(device["device_key"])
    return resolved
