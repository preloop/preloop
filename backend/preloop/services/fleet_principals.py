"""Resolve runtime principals to managed agents for the Fleet Map readers.

Native-hook and per-run sessions use a per-conversation principal
``<base>:<conversation>[:<subagent>]`` (for example
``claude-code-16e8f525334c:b8003a9a-...``), while the managed agent is keyed by
the base identity. The session explorer already resolves that suffix; the
fleet readers use the same rule so tool calls, approvals and gateway rows of
every conversation reach their agent.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

T = TypeVar("T")

#: Principal type whose id is the managed agent's own id.
MANAGED_AGENT_PRINCIPAL = "managed_agent"


def base_principal_id(principal_id: str) -> str:
    """Return the agent identity part of a per-conversation principal id.

    Args:
        principal_id: Runtime principal id, possibly ``<base>:<run>[:<sub>]``.

    Returns:
        The part before the first ``:`` (the id itself when there is none).
    """
    return principal_id.split(":", 1)[0]


def principal_lookup_keys(
    principals: Iterable[tuple[str | None, str | None]],
) -> set[tuple[str, str]]:
    """Source pairs that may identify the agents behind ``principals``.

    Args:
        principals: ``(runtime_principal_type, runtime_principal_id)`` pairs.

    Returns:
        Each pair plus its base pair, empty values dropped.
    """
    keys: set[tuple[str, str]] = set()
    for principal_type, principal_id in principals:
        if not principal_type or not principal_id:
            continue
        keys.add((principal_type, principal_id))
        keys.add((principal_type, base_principal_id(principal_id)))
    return keys


def resolve_principal(
    index: dict[tuple[str, str], T],
    principal_type: str | None,
    principal_id: str | None,
    *,
    by_id: dict[str, T] | None = None,
) -> T | None:
    """Find the agent value behind one runtime principal.

    Order: a ``managed_agent`` principal by agent id, the exact source pair,
    then the base pair of a per-conversation id.

    Args:
        index: Values keyed by ``(session_source_type, session_source_id)``.
        principal_type: Runtime principal type.
        principal_id: Runtime principal id.
        by_id: Values keyed by managed agent id, for ``managed_agent`` principals.

    Returns:
        The matching value, or None.
    """
    if not principal_type or not principal_id:
        return None
    if principal_type == MANAGED_AGENT_PRINCIPAL and by_id is not None:
        found = by_id.get(principal_id)
        if found is not None:
            return found
    found = index.get((principal_type, principal_id))
    if found is None and ":" in principal_id:
        found = index.get((principal_type, base_principal_id(principal_id)))
    return found


def agent_index(agents: Iterable[Any], value: Any = None) -> dict[tuple[str, str], Any]:
    """Index agents by their source pair; ``value`` maps an agent to the stored value.

    Args:
        agents: Managed agent rows.
        value: Optional callable producing the stored value (default: the row).

    Returns:
        Values keyed by ``(session_source_type, session_source_id)``.
    """
    return {
        (row.session_source_type, row.session_source_id): (value(row) if value else row)
        for row in agents
        if getattr(row, "session_source_type", None)
        and getattr(row, "session_source_id", None)
    }
