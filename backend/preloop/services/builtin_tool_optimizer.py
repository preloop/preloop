"""Helpers for account-wide unused-builtin recommendations (#146).

Maps session ``unused_tool_names`` onto Preloop builtins (handling
``mcp__preloop__*`` prefixes without misclassifying external MCP tools),
tiers them against account-wide invocation stats, and expands bare/prefixed
name aliases for replay matching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

# Clients advertise Preloop gateway tools as ``mcp__<server>__<tool>``. Accept
# both the product name and the FastMCP server name used in dynamic_fastmcp.
PRELOOP_MCP_SERVERS = frozenset({"preloop", "preloop-mcp"})

# Account-wide corroboration window for tier-1 disable recommendations.
BUILTIN_USAGE_WINDOW_DAYS = 30


def parse_mcp_tool_name(name: str) -> tuple[Optional[str], str]:
    """Split an ``mcp__server__tool`` name into (server, bare_tool).

    Bare names and non-``mcp__`` names return ``(None, name)``. Unlike a naive
    first-``__`` split, this keeps the server segment intact so
    ``mcp__preloop__get_issue`` yields ``("preloop", "get_issue")``.

    Args:
        name: Advertised or invoked tool name.

    Returns:
        Tuple of optional server segment and bare tool name.
    """
    if not name.startswith("mcp__"):
        return (None, name)
    parts = name.split("__", 2)
    if len(parts) == 3 and parts[1] and parts[2]:
        return (parts[1], parts[2])
    return (None, name)


def expand_tool_name_match_keys(name: str) -> set[str]:
    """Return bare + Preloop-prefixed aliases that should match ``name``.

    External MCP prefixes (e.g. ``mcp__github__search``) are not expanded to
    bare names, so they never collide with Preloop builtins of the same bare
    name.

    Args:
        name: A tool name in bare or ``mcp__…`` form.

    Returns:
        Set of equivalent names for matching.
    """
    keys = {name}
    server, bare = parse_mcp_tool_name(name)
    if server is None:
        for alias in PRELOOP_MCP_SERVERS:
            keys.add(f"mcp__{alias}__{name}")
        return keys
    if server in PRELOOP_MCP_SERVERS:
        keys.add(bare)
        for alias in PRELOOP_MCP_SERVERS:
            keys.add(f"mcp__{alias}__{bare}")
    return keys


def tool_name_in_removed(definition_name: str, removed: set[str]) -> bool:
    """Return whether a tool definition name matches a removed-name set.

    Accepts builtin names in both bare and Preloop-prefixed forms.

    Args:
        definition_name: Name from a request tool definition.
        removed: Candidate ``removed_tool_names``.

    Returns:
        True when the definition should be stripped.
    """
    if not definition_name or not removed:
        return False
    def_keys = expand_tool_name_match_keys(definition_name)
    for candidate in removed:
        if def_keys & expand_tool_name_match_keys(candidate):
            return True
    return False


def resolve_builtin_tool_name(
    unused_name: str, *, builtin_names: set[str]
) -> Optional[str]:
    """Map an unused advertised name onto a Preloop builtin bare name.

    Prefixed names only resolve when the server segment is a Preloop gateway
    alias. Bare names resolve when they appear in ``builtin_names``.

    Args:
        unused_name: Entry from ``unused_tool_names``.
        builtin_names: Catalog of builtin bare names.

    Returns:
        Bare builtin name, or ``None`` when the name is not a Preloop builtin.
    """
    server, bare = parse_mcp_tool_name(unused_name)
    if server is not None and server not in PRELOOP_MCP_SERVERS:
        return None
    if bare in builtin_names:
        return bare
    return None


@dataclass(frozen=True)
class UnusedBuiltinPartition:
    """Tiered split of unused tools for optimizer suggestions.

    Attributes:
        tier1_raw_names: Unused advertised names with zero account-wide
            invocations (eligible for account-wide disable).
        tier1_builtin_names: Deduped bare builtin names for tier 1.
        scope_raw_names: Remaining unused names (non-builtins, or builtins
            still invoked elsewhere) for agent-scoped ``scope_tools``.
        tier1_session_tokens: Resend-inclusive session tokens for tier-1 names.
        scope_session_tokens: Resend-inclusive session tokens for scope names.
    """

    tier1_raw_names: list[str]
    tier1_builtin_names: list[str]
    scope_raw_names: list[str]
    tier1_session_tokens: int
    scope_session_tokens: int


def partition_unused_tools(
    unused_tool_names: Sequence[str],
    *,
    builtin_names: set[str],
    unused_tool_tokens: Mapping[str, int],
    invocation_counts: Mapping[str, int],
) -> UnusedBuiltinPartition:
    """Split unused tools into account-wide disable vs agent-scoped scope.

    Tier 1 requires zero account-wide invocations for the bare builtin name
    over the stats window. Tier 2 (invoked elsewhere) and non-builtins stay on
    the agent-scoped path.

    Args:
        unused_tool_names: Advertised-but-unused names from the session.
        builtin_names: Catalog of builtin bare names.
        unused_tool_tokens: Per-advertised-name resend-inclusive token totals.
        invocation_counts: Account-wide invocation counts keyed by bare name.

    Returns:
        Partition with carved token totals that sum to the unused total.
    """
    tier1_raw: list[str] = []
    tier1_builtins: list[str] = []
    seen_builtins: set[str] = set()
    scope_raw: list[str] = []
    tier1_tokens = 0
    scope_tokens = 0

    for raw_name in unused_tool_names:
        tokens = int(unused_tool_tokens.get(raw_name, 0))
        builtin = resolve_builtin_tool_name(raw_name, builtin_names=builtin_names)
        if builtin is None:
            scope_raw.append(raw_name)
            scope_tokens += tokens
            continue
        if int(invocation_counts.get(builtin, 0)) > 0:
            scope_raw.append(raw_name)
            scope_tokens += tokens
            continue
        tier1_raw.append(raw_name)
        tier1_tokens += tokens
        if builtin not in seen_builtins:
            seen_builtins.add(builtin)
            tier1_builtins.append(builtin)

    return UnusedBuiltinPartition(
        tier1_raw_names=tier1_raw,
        tier1_builtin_names=tier1_builtins,
        scope_raw_names=scope_raw,
        tier1_session_tokens=tier1_tokens,
        scope_session_tokens=scope_tokens,
    )


def invocation_counts_by_tool(
    usage_rows: Iterable[object],
) -> dict[str, int]:
    """Build bare-name → invocation_count from GatewayUsageByTool-like rows.

    Args:
        usage_rows: Rows with ``tool_name`` and ``invocation_count``.

    Returns:
        Mapping of bare tool name to account-wide invocation count.
    """
    counts: dict[str, int] = {}
    for row in usage_rows:
        tool_name = getattr(row, "tool_name", None)
        if not tool_name:
            continue
        _server, bare = parse_mcp_tool_name(str(tool_name))
        counts[bare] = counts.get(bare, 0) + int(
            getattr(row, "invocation_count", 0) or 0
        )
    return counts
