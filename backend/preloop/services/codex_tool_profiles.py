"""Opt-in tool definitions for the version-pinned hosted Codex runtime."""

from typing import Any

CODEX_TOOLS_VERSION = "0.153.4"
CODEX_TOOL_PROFILES = ("default", "lean")

# Verified against rust-v0.153.4 config_toml.rs, features/src/lib.rs and
# core/src/tools/spec_plan.rs. MCP resource helpers, model-selected apply_patch
# and tool_search have no independent off switch in this release.
LEAN_CONFIG = """web_search = "disabled"
agents.enabled = false
features.multi_agent = false
features.multi_agent_v2 = false
features.goals = false
features.view_image = false
features.sleep_tool = false
tools.experimental_request_user_input.enabled = false
tools.update_plan.enabled = false
"""


def validate_codex_tools_config(config: dict[str, Any]) -> str:
    """Validate a profile without inserting defaults into stored configuration.

    Args:
        config: The flow's agent configuration.

    Returns:
        The selected profile, or the unchanged historical default.

    Raises:
        ValueError: If the profile or its CLI version is unsupported.
    """
    profile = config.get("codex_tools", "default")
    if not isinstance(profile, str) or profile not in CODEX_TOOL_PROFILES:
        raise ValueError('agent_config.codex_tools must be "default" or "lean"')
    if (
        profile == "lean"
        and config.get("codex_cli_version", CODEX_TOOLS_VERSION) != CODEX_TOOLS_VERSION
    ):
        raise ValueError(f"codex_tools lean requires Codex {CODEX_TOOLS_VERSION}")
    return profile


def codex_tools_config(profile: str, cli_version: str) -> str:
    """Render root TOML keys shared by exec, resume and recovery invocations."""
    validate_codex_tools_config(
        {"codex_tools": profile, "codex_cli_version": cli_version}
    )
    return LEAN_CONFIG if profile == "lean" else ""
