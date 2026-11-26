"""Baseline config changes a user applies by hand to route an agent via Preloop.

``GET /agents/{id}/manual-setup`` returns the instructions a pending
onboarding stored. Without one, it returns the baseline built here from the
agent kind and the server's public URL. Every secret is the literal
placeholder ``<AGENT_KEY>``: the server never holds an agent key in clear.
"""

from __future__ import annotations

from typing import Any, Optional

from preloop.utils.agent_kind import normalize_agent_kind

AGENT_KEY_PLACEHOLDER = "<AGENT_KEY>"
_BEARER = f"Bearer {AGENT_KEY_PLACEHOLDER}"
_MCP_ONLY_LIMITATION = (
    "This change adds the managed Preloop MCP server only. It does not route "
    "model traffic through the Preloop gateway."
)


def resume_command(display_name: str) -> str:
    """Return the CLI command that validates a manually applied config.

    Args:
        display_name: The managed agent's display name.

    Returns:
        A shell command with the name double-quoted.
    """
    escaped = "".join(f"\\{char}" if char in '"\\$`' else char for char in display_name)
    return f'preloop agents onboard --resume "{escaped}"'


def _file(path: str, file_format: str, merge: dict[str, Any]) -> dict[str, Any]:
    """Return one ``manual_apply`` file entry for a baseline."""
    return {
        "path": path,
        "format": file_format,
        # The server cannot see the user's disk. These are configuration
        # files an installed agent normally already has, so the safe
        # instruction is "merge and keep everything else": telling a user to
        # create the file with exactly this content would invite them to
        # overwrite their settings.
        "exists": True,
        "merge": merge,
        "remove": [],
        "host_source": None,
    }


def _claude_code(base_url: str) -> dict[str, Any]:
    """Return the Claude Code baseline: gateway env plus the MCP server."""
    env = {
        "ANTHROPIC_BASE_URL": f"{base_url}/anthropic",
        "ANTHROPIC_API_KEY": AGENT_KEY_PLACEHOLDER,
    }
    mcp = {
        "mcpServers": {
            "preloop": {
                "type": "http",
                "url": f"{base_url}/mcp/v1",
                "headers": {"Authorization": _BEARER},
            }
        }
    }
    return {
        "files": [
            _file("~/.claude/settings.json", "json", {"env": dict(env)}),
            _file("~/.claude.json", "json", mcp),
        ],
        "env": env,
        "settings_flag": "claude --settings <file>",
        "limitations": [
            "The environment-variable form routes model traffic only. It does "
            "not enable approval hooks or the managed MCP server.",
            "The --settings form applies only to sessions started with the flag.",
        ],
    }


def _codex(base_url: str) -> dict[str, Any]:
    """Return the Codex CLI baseline: the managed MCP server."""
    merge = {
        "mcp_servers": {
            "preloop": {
                "url": f"{base_url}/mcp/v1",
                "http_headers": {"Authorization": _BEARER},
            }
        }
    }
    return {
        "files": [_file("~/.codex/config.toml", "toml", merge)],
        "env": {},
        "settings_flag": None,
        "limitations": [_MCP_ONLY_LIMITATION],
    }


def _cursor(base_url: str) -> dict[str, Any]:
    """Return the Cursor baseline: the managed MCP server."""
    merge = {
        "mcpServers": {
            "preloop": {
                "url": f"{base_url}/mcp/v1",
                "headers": {"Authorization": _BEARER},
            }
        }
    }
    return {
        "files": [_file("~/.cursor/mcp.json", "json", merge)],
        "env": {},
        "settings_flag": None,
        "limitations": [_MCP_ONLY_LIMITATION],
    }


_BASELINES = {"claude_code": _claude_code, "codex": _codex, "cursor": _cursor}


def build_manual_setup_baseline(
    agent_kind: Optional[str], base_url: str
) -> Optional[dict[str, Any]]:
    """Build the baseline ``manual_apply`` instructions for an agent kind.

    Args:
        agent_kind: The managed agent's kind (``claude_code``, ``codex``,
            ``cursor``); other kinds have no baseline.
        base_url: The server's public base URL.

    Returns:
        A ``manual_apply``-shaped object, or ``None`` for an unsupported kind.
    """
    builder = _BASELINES.get(normalize_agent_kind(agent_kind))
    if builder is None:
        return None
    return builder(base_url.strip().rstrip("/"))
