"""Compute builtin tools newly unlocked by registering a tracker.

Used by POST /trackers so the frontend can prompt the user to review
context-tax impact of tools that just became advertised.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from preloop.api.endpoints.tools import BUILTIN_TOOLS


def _is_effectively_enabled(
    tool: Mapping[str, object],
    enabled_by_name: Mapping[str, bool],
) -> bool:
    """Return whether a builtin would be advertised as enabled.

    Explicit ToolConfiguration.is_enabled=False disables the tool. Missing
    config falls back to default_enabled (True when unset).
    """
    name = str(tool["name"])
    if name in enabled_by_name:
        return bool(enabled_by_name[name])
    return bool(tool.get("default_enabled", True))


def supported_tracker_builtin_names(
    *,
    has_tracker: bool,
    tracker_types: Iterable[str],
    enabled_by_name: Optional[Mapping[str, bool]] = None,
) -> Set[str]:
    """Return tracker-gated builtins that are supported and effectively enabled.

    Mirrors the has_tracker / required_tracker_types filter in
    dynamic_fastmcp.list_tools, then drops tools that are effectively disabled.
    """
    types = set(tracker_types)
    enabled = enabled_by_name or {}
    names: Set[str] = set()

    for tool in BUILTIN_TOOLS:
        if not tool.get("requires_tracker", False):
            continue
        if not has_tracker:
            continue
        required_types = tool.get("required_tracker_types") or []
        if required_types and not any(t in types for t in required_types):
            continue
        if not _is_effectively_enabled(tool, enabled):
            continue
        names.add(str(tool["name"]))

    return names


def unlocked_tool_names_after_tracker(
    *,
    had_tracker: bool,
    types_before: Sequence[str],
    types_after: Sequence[str],
    enabled_by_name: Optional[Mapping[str, bool]] = None,
) -> List[str]:
    """Diff supported+enabled tracker builtins before vs after a tracker insert.

    Args:
        had_tracker: Whether the account had any tracker before the insert.
        types_before: Tracker type strings present before the insert.
        types_after: Tracker type strings present after the insert.
        enabled_by_name: Map of builtin tool name -> is_enabled from
            ToolConfiguration rows (only builtins with explicit rows needed).

    Returns:
        Sorted list of newly unlocked tool names. Empty when the new tracker
        does not expand the supported set (e.g. second tracker of same type).
    """
    before = supported_tracker_builtin_names(
        has_tracker=had_tracker,
        tracker_types=types_before,
        enabled_by_name=enabled_by_name,
    )
    # After insert there is always at least one tracker.
    after = supported_tracker_builtin_names(
        has_tracker=True,
        tracker_types=types_after,
        enabled_by_name=enabled_by_name,
    )
    return sorted(after - before)


def enabled_map_from_configs(
    configs: Iterable[object],
) -> Dict[str, bool]:
    """Build tool_name -> is_enabled for builtin ToolConfiguration rows."""
    result: Dict[str, bool] = {}
    for config in configs:
        tool_source = getattr(config, "tool_source", None)
        tool_name = getattr(config, "tool_name", None)
        if tool_source != "builtin" or not tool_name:
            continue
        result[str(tool_name)] = bool(getattr(config, "is_enabled", True))
    return result
