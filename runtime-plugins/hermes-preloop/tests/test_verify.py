"""Tests for HermesPreloopPlugin.verify() runtime validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Make the standalone plugin package importable without installation.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(SRC))

from preloop_hermes_plugin.plugin import HermesPreloopPlugin  # noqa: E402
from preloop.integrations.agent_control import AgentControlConfig  # noqa: E402


def _config() -> AgentControlConfig:
    return AgentControlConfig(
        control_ws_url="wss://staging.preloop.ai/api/v1/agents/control/ws",
        bearer_token="agt_test",
        runtime_principal_id="hermes-1",
    )


def test_verify_passes_when_runtime_matches() -> None:
    plugin = HermesPreloopPlugin()
    config = _config()
    block = {"runtime": "hermes"}
    with patch.object(plugin, "load_config", return_value=config):
        with patch.object(plugin, "_read_control_block", return_value=block):
            plugin.verify()


def test_verify_raises_on_runtime_mismatch() -> None:
    plugin = HermesPreloopPlugin()
    config = _config()
    block = {"runtime": "other"}
    with patch.object(plugin, "load_config", return_value=config):
        with patch.object(plugin, "_read_control_block", return_value=block):
            with pytest.raises(
                ValueError, match="Expected Hermes runtime config, got 'other'"
            ):
                plugin.verify()


def test_verify_raises_on_missing_runtime() -> None:
    plugin = HermesPreloopPlugin()
    config = _config()
    block = {}
    with patch.object(plugin, "load_config", return_value=config):
        with patch.object(plugin, "_read_control_block", return_value=block):
            with pytest.raises(
                ValueError, match="Expected Hermes runtime config, got None"
            ):
                plugin.verify()


def test_verify_raises_on_missing_control_ws_url() -> None:
    plugin = HermesPreloopPlugin()
    config = AgentControlConfig(
        control_ws_url="",
        bearer_token="agt_test",
        runtime_principal_id="hermes-1",
    )
    block = {"runtime": "hermes"}
    with patch.object(plugin, "load_config", return_value=config):
        with patch.object(plugin, "_read_control_block", return_value=block):
            with pytest.raises(
                ValueError, match="preloop.control.control_ws_url is required"
            ):
                plugin.verify()


def test_verify_raises_on_missing_bearer_token() -> None:
    plugin = HermesPreloopPlugin()
    config = AgentControlConfig(
        control_ws_url="wss://staging.preloop.ai/api/v1/agents/control/ws",
        bearer_token="",
        runtime_principal_id="hermes-1",
    )
    block = {"runtime": "hermes"}
    with patch.object(plugin, "load_config", return_value=config):
        with patch.object(plugin, "_read_control_block", return_value=block):
            with pytest.raises(
                ValueError, match="preloop.control.bearer_token is required"
            ):
                plugin.verify()


def test_verify_required_url_message_includes_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / ".hermes" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("preloop: {}\n")
    plugin = HermesPreloopPlugin()
    config = AgentControlConfig(
        control_ws_url="",
        bearer_token="agt_test",
        runtime_principal_id="hermes-1",
    )
    with patch.object(plugin, "load_config", return_value=config):
        with patch.object(
            plugin, "_read_control_block", return_value={"runtime": "hermes"}
        ):
            with pytest.raises(ValueError) as exc_info:
                plugin.verify()
    msg = str(exc_info.value)
    assert "preloop.control.control_ws_url is required" in msg
    assert str(config_path) in msg
    assert "HERMES_HOME=<unset>" in msg
    assert f"HOME={tmp_path}" in msg


def test_verify_required_token_message_includes_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    config_path = tmp_path / ".hermes" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("preloop: {}\n")
    plugin = HermesPreloopPlugin()
    config = AgentControlConfig(
        control_ws_url="wss://example.preloop.ai/api/v1/agents/control/ws",
        bearer_token="",
        runtime_principal_id="hermes-1",
    )
    with patch.object(plugin, "load_config", return_value=config):
        with patch.object(
            plugin, "_read_control_block", return_value={"runtime": "hermes"}
        ):
            with pytest.raises(ValueError) as exc_info:
                plugin.verify()
    msg = str(exc_info.value)
    assert "preloop.control.bearer_token is required" in msg
    assert str(config_path) in msg
    assert "HERMES_HOME=<unset>" in msg
