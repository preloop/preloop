"""Tests for HermesPreloopPlugin.verify() runtime validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
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
            with pytest.raises(ValueError, match="Expected Hermes runtime config, got 'other'"):
                plugin.verify()


def test_verify_raises_on_missing_runtime() -> None:
    plugin = HermesPreloopPlugin()
    config = _config()
    block = {}
    with patch.object(plugin, "load_config", return_value=config):
        with patch.object(plugin, "_read_control_block", return_value=block):
            with pytest.raises(ValueError, match="Expected Hermes runtime config, got None"):
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
            with pytest.raises(ValueError, match="preloop.control.control_ws_url is required"):
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
            with pytest.raises(ValueError, match="preloop.control.bearer_token is required"):
                plugin.verify()
