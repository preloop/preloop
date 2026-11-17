"""The hosted Codex tool profile is opt-in and validated before launch."""

import tomllib
from typing import Any

import pytest
from pydantic import ValidationError

from preloop.agents.codex import CodexAgent
from preloop.models.schemas.flow import FlowUpdate


@pytest.mark.parametrize("provider", ["openai", "fixture"])
def test_default_profile_preserves_config(provider: str) -> None:
    """Omission and an explicit default produce identical configuration."""
    agent = CodexAgent({})
    args = ("example/custom", provider, "http://127.0.0.1:1234/v1")
    assert agent._build_codex_auth_config(*args) == agent._build_codex_auth_config(
        *args, codex_tools="default"
    )


@pytest.mark.parametrize("provider", ["openai", "fixture"])
def test_lean_configuration(provider: str) -> None:
    """Lean uses the supported CLI config, including on resumed sessions."""
    agent = CodexAgent({})
    block = agent._build_codex_auth_config(
        "example/custom", provider, "http://127.0.0.1:1234/v1", codex_tools="lean"
    )
    raw = block.split("cat > ~/.codex/config.toml << EOF\n")[1].split("\nEOF")[0]
    config = tomllib.loads(
        raw.replace("$MCP_TOOL_TIMEOUT_SEC", "30")
        .replace("$PRELOOP_CODEX_MCP_AUTH", '"Bearer fixture"')
        .replace("$PRELOOP_CODEX_PROVIDER_AUTH", '"Bearer fixture"')
    )
    assert config["web_search"] == "disabled"
    assert config["agents"]["enabled"] is False
    for flag in ("multi_agent", "multi_agent_v2", "view_image", "goals"):
        assert config["features"][flag] is False
    assert config["tools"]["experimental_request_user_input"]["enabled"] is False
    assert config["tools"]["update_plan"]["enabled"] is False
    assert config["mcp_servers"]["preloop"]["required"] is True


@pytest.mark.parametrize("value", [None, "", "unknown", True, 1, {}, []])
def test_invalid_profile_rejected_on_save(value: Any) -> None:
    """A misspelled or malformed profile cannot silently use defaults."""
    with pytest.raises(ValidationError, match="codex_tools"):
        FlowUpdate(agent_config={"codex_tools": value})


def test_lean_rejects_unverified_cli_override() -> None:
    """A CLI upgrade must verify the opt-in tool switches first."""
    with pytest.raises(ValidationError, match="0.153.4"):
        FlowUpdate(agent_config={"codex_tools": "lean", "codex_cli_version": "1.0.0"})


@pytest.mark.parametrize("profile", ["default", "lean"])
def test_profile_is_not_injected_into_other_config(profile: str) -> None:
    """Validation preserves the caller's complete agent settings."""
    config = {"codex_tools": profile, "sandbox_type": "exec", "native_result": True}
    assert FlowUpdate(agent_config=config).agent_config == config


@pytest.mark.parametrize("profile", ["default", "lean"])
def test_launch_propagates_profile(profile: str) -> None:
    """The shared Docker/Kubernetes script must use the selected profile."""
    agent = CodexAgent({})
    script = agent._build_codex_script(
        {
            "prompt": "Inspect fixture",
            "agent_config": {"codex_tools": profile},
            "allowed_mcp_tools": [],
            "allowed_mcp_servers": [],
        }
    )
    assert ('web_search = "disabled"' in script) is (profile == "lean")
    assert "codex exec resume" in script


def test_launch_revalidates_profile() -> None:
    """Legacy or imported rows also fail before starting an unsupported CLI."""
    with pytest.raises(ValueError, match="codex_tools"):
        CodexAgent({})._build_codex_script(
            {"prompt": "Inspect fixture", "agent_config": {"codex_tools": "invalid"}}
        )


def test_profile_version_tracks_hosted_pin() -> None:
    """A hosted CLI upgrade must revalidate profile controls and wire captures."""
    from preloop.agents.codex_model_catalog import CODEX_CATALOG_VERSION
    from preloop.services.codex_tool_profiles import CODEX_TOOLS_VERSION

    assert CODEX_TOOLS_VERSION == CODEX_CATALOG_VERSION


def test_lean_does_not_enable_a_locked_shell() -> None:
    """A tool profile cannot weaken the flow's existing shell lock."""
    agent = CodexAgent({})
    script = agent._build_codex_script(
        {
            "prompt": "Inspect fixture",
            "agent_config": {"codex_tools": "lean", "sandbox_type": "read-only"},
        }
    )
    assert "--disable shell_tool" in script
    assert "--yolo" not in script
    assert "--disable shell_tool" in (agent.live_nudge_command or "")
