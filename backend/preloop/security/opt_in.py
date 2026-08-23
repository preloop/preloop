"""Opt-in helpers for built-in MCP servers started inside agent sandboxes.

Default allowlists are empty: ordinary agents pay no extra MCP process or
tool-schema cost. Flows that need Preloop HTTP tools or the local
``repo-audit`` stdio server must list them explicitly.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

REPO_AUDIT_SERVER = "repo-audit"
PRELOOP_MCP_SERVERS = frozenset({"preloop-mcp", "preloop"})

REPO_AUDIT_TOOLS = (
    "secret_history_scan",
    "repo_hygiene_walk",
    "ci_workflow_audit",
    "upstream_divergence",
)

# Pinned scanner versions for runner images. Absence is recorded, never treated
# as a clean secrets walk. A gitleaks count of 0 is not "met".
RECOMMENDED_GITLEAKS_VERSION = "8.24.3"


def _as_tool_dicts(
    allowed_mcp_tools: Optional[Iterable[Any]],
) -> List[Mapping[str, Any]]:
    """Normalize allow-list entries to mappings."""
    tools: List[Mapping[str, Any]] = []
    if not allowed_mcp_tools:
        return tools
    for tool in allowed_mcp_tools:
        if isinstance(tool, str):
            tools.append({"name": tool})
        elif isinstance(tool, dict):
            tools.append(tool)
    return tools


def _tool_server_name(tool: Mapping[str, Any]) -> str:
    return str(tool.get("server_name") or "").strip()


def _tool_name(tool: Mapping[str, Any]) -> str:
    return str(tool.get("tool_name") or tool.get("name") or "").strip()


def wants_preloop_mcp(
    allowed_mcp_servers: Optional[Iterable[str]] = None,
    allowed_mcp_tools: Optional[Iterable[Any]] = None,
) -> bool:
    """Return True when the flow opted into the Preloop HTTP MCP server.

    A tool entry with no ``server_name`` (legacy preset shape) is treated as
    a Preloop builtin tool.
    """
    servers = {str(s).strip() for s in (allowed_mcp_servers or []) if str(s).strip()}
    if servers & PRELOOP_MCP_SERVERS:
        return True
    for tool in _as_tool_dicts(allowed_mcp_tools):
        name = _tool_name(tool)
        if not name:
            continue
        server = _tool_server_name(tool)
        if server in PRELOOP_MCP_SERVERS or server == "":
            if name not in REPO_AUDIT_TOOLS:
                return True
    return False


def wants_repo_audit(
    allowed_mcp_servers: Optional[Iterable[str]] = None,
    allowed_mcp_tools: Optional[Iterable[Any]] = None,
) -> bool:
    """Return True when the flow opted into the local repo-audit stdio server."""
    servers = {str(s).strip() for s in (allowed_mcp_servers or []) if str(s).strip()}
    if REPO_AUDIT_SERVER in servers:
        return True
    for tool in _as_tool_dicts(allowed_mcp_tools):
        if _tool_server_name(tool) == REPO_AUDIT_SERVER and _tool_name(tool):
            return True
        if _tool_name(tool) in REPO_AUDIT_TOOLS:
            return True
    return False


def repo_audit_tool_names(
    allowed_mcp_tools: Optional[Iterable[Any]] = None,
) -> List[str]:
    """Return the repo-audit tool names the flow listed, or all if server-only."""
    names: List[str] = []
    for tool in _as_tool_dicts(allowed_mcp_tools):
        name = _tool_name(tool)
        if name in REPO_AUDIT_TOOLS:
            names.append(name)
    # Server opted in with no tool filter => expose the full family.
    if not names:
        return list(REPO_AUDIT_TOOLS)
    # Preserve catalog order, drop dupes.
    seen = set()
    ordered: List[str] = []
    for name in REPO_AUDIT_TOOLS:
        if name in names and name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def mcp_allowlists_from_context(
    execution_context: Optional[Mapping[str, Any]] = None,
) -> tuple[List[str], List[Dict[str, Any]]]:
    """Read flow MCP allowlists from an agent execution context."""
    ctx = execution_context or {}
    servers = [str(s) for s in (ctx.get("allowed_mcp_servers") or [])]
    tools = [t for t in (ctx.get("allowed_mcp_tools") or [])]
    return servers, tools
