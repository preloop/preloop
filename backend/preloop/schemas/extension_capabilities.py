"""What an extension can do, derived deterministically from reported metadata.

Each capability is a plain statement backed by a collected fact: a declared
tool, a hook event, a bundled script, an MCP transport. Nothing is scored and
nothing is guessed: when the facts are missing the answer is ``unknown``.
"""

from typing import Any, Literal

from pydantic import BaseModel

CapabilityKey = Literal[
    "shell",
    "files_read",
    "files_write",
    "network",
    "hooks",
    "mcp",
    "scripts",
    "unknown",
]
CAPABILITY_KEYS: tuple[str, ...] = (
    "shell",
    "files_read",
    "files_write",
    "network",
    "hooks",
    "mcp",
    "scripts",
    "unknown",
)

READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell", "Shell"})


class ExtensionCapability(BaseModel):
    """One plain-words capability and its filter key."""

    key: CapabilityKey
    label: str


def _tool_base(tool: str) -> str:
    """Return the tool name without a rule, so ``Bash(git:*)`` is ``Bash``."""
    return tool.split("(", 1)[0].strip()


def _events(events: list[str]) -> str:
    """Join hook event names for a label."""
    return ", ".join(events)


def extension_capabilities(
    kind: str, details: dict[str, Any] | None
) -> list[ExtensionCapability]:
    """Derive the capability list for one extension's reported details.

    Args:
        kind: The extension kind (skill, command, plugin, hook, mcp_server).
        details: The stored details object, or None when none was reported.

    Returns:
        Capabilities in a stable order; a single ``unknown`` entry when the
        report carries no facts to derive from.
    """
    d = details or {}
    caps: dict[str, str] = {}
    known = False
    partly_unknown = False
    if kind in {"skill", "command", "plugin"}:
        tools = {_tool_base(tool) for tool in d.get("allowed_tools") or []}
        known = bool(d.get("tools_declared")) or bool(tools)
        if tools & SHELL_TOOLS:
            caps["shell"] = "Runs shell commands"
        if tools & READ_TOOLS:
            caps["files_read"] = "Reads files"
        if tools & WRITE_TOOLS:
            caps["files_write"] = "Writes files"
        if tools & NETWORK_TOOLS:
            caps["network"] = "Network access"
        if any(tool.startswith("mcp__") for tool in tools):
            caps["mcp"] = "Uses MCP tools"
    if kind == "hook":
        known = True
        caps["shell"] = "Runs shell commands"
    elif kind == "plugin":
        bundles = d.get("bundles")
        known = known or bundles is not None
        components = (
            sum(bundles.get(part) or 0 for part in ("skills", "commands", "agents"))
            if bundles
            else 0
        )
        partly_unknown = components > 0 and not d.get("tools_declared")
        if bundles and bundles.get("hooks"):
            caps["shell"] = "Runs shell commands"
        if bundles and bundles.get("mcp_servers"):
            caps["mcp"] = "Adds MCP tools"
    elif kind == "mcp_server":
        transport = d.get("transport") or ""
        if transport:
            known = True
            caps["mcp"] = "Adds MCP tools"
        if transport == "stdio":
            program = d.get("command") or ""
            caps["shell"] = (
                f"Runs a local program ({program})"
                if program
                else "Runs a local program"
            )
        elif transport in {"http", "sse"}:
            host = d.get("url_host") or ""
            caps["network"] = f"Network access ({host})" if host else "Network access"
    events: list[str] = list(d.get("hook_events") or [])
    if kind == "hook":
        matchers = d.get("hook_matchers") or []
        if any(
            command.get("script")
            for matcher in matchers
            for command in matcher.get("commands") or []
        ):
            caps["scripts"] = "Executes scripts"
    if events:
        known = True
        caps["hooks"] = f"Registers hooks on {_events(events)}"
        caps.setdefault("shell", "Runs shell commands")
    if (d.get("script_count") or 0) > 0:
        known = True
        caps["scripts"] = "Executes bundled scripts"
    if not known:
        return [ExtensionCapability(key="unknown", label="Unknown")]
    if partly_unknown:
        caps["unknown"] = "Some components declare no tools"
    elif not caps and kind in {"skill", "command"}:
        caps["unknown"] = "Declares no tools"
    elif not caps:
        return [ExtensionCapability(key="unknown", label="Unknown")]
    return [
        ExtensionCapability(key=key, label=caps[key])  # type: ignore[arg-type]
        for key in CAPABILITY_KEYS
        if key in caps
    ]


def capability_keys(kind: str, details: dict[str, Any] | None) -> list[str]:
    """Return only the keys, as stored for filtering."""
    return [cap.key for cap in extension_capabilities(kind, details)]
