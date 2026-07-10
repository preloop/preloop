"""Tests for the Hermes native tool-call approval gate."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Make the standalone plugin package importable without installation.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from preloop_hermes_plugin.plugin import (  # noqa: E402
    HermesPreloopPlugin,
    _block,
    _permission_check_url,
    _resolve_fail_open,
)
from preloop.integrations.agent_control import AgentControlConfig  # noqa: E402


def _config() -> AgentControlConfig:
    return AgentControlConfig(
        control_ws_url="wss://staging.preloop.ai/api/v1/agents/control/ws",
        bearer_token="agt_test",
        runtime_principal_id="hermes-1",
    )


def _plugin(
    approval: dict[str, Any] | None = None,
) -> HermesPreloopPlugin:
    instance = HermesPreloopPlugin()
    instance._control_settings = (_config(), approval or {})
    return instance


def test_permission_check_url_derives_https_base() -> None:
    url = _permission_check_url("wss://staging.preloop.ai/api/v1/agents/control/ws")
    assert url == "https://staging.preloop.ai/api/v1/agents/permission-check"
    insecure = _permission_check_url("ws://localhost:8000/api/v1/agents/control/ws")
    assert insecure == "http://localhost:8000/api/v1/agents/permission-check"


def test_block_envelope_matches_upstream_hermes_shape() -> None:
    envelope = _block("nope")
    assert envelope["action"] == "block"
    assert envelope["message"] == "nope"
    # Plan-era aliases kept for docs/tests compatibility.
    assert envelope["decision"] == "block"
    assert envelope["reason"] == "nope"


def test_capabilities_advertise_tool_approval() -> None:
    caps = HermesPreloopPlugin().capabilities()
    assert caps.supports_tool_approval is True
    assert caps.to_payload()["tool_approval"] is True


@pytest.mark.asyncio
async def test_allow_decision_returns_none() -> None:
    instance = _plugin()
    captured: dict[str, Any] = {}

    async def fake_request(
        url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers
        return {"decision": "allow", "request_id": "req_1"}

    instance._request_decision = fake_request  # type: ignore[assignment]

    result = await instance.pre_tool_call(
        {
            "hook_event_name": "pre_tool_call",
            "tool_name": "terminal",
            "tool_input": {"command": "ls"},
            "session_id": "sess_1",
            "cwd": "/home/user",
        }
    )

    assert result is None
    assert captured["url"].endswith("/api/v1/agents/permission-check")
    assert captured["body"]["source"] == "hermes"
    assert captured["body"]["tool_name"] == "terminal"
    assert captured["body"]["tool_input"] == {"command": "ls"}
    assert captured["body"]["session_id"] == "sess_1"
    assert captured["body"]["client_decision"] == "ask"
    assert captured["headers"]["Authorization"] == "Bearer agt_test"


@pytest.mark.asyncio
async def test_deny_decision_blocks_with_reason() -> None:
    instance = _plugin()

    async def fake_request(*_: Any, **__: Any) -> dict[str, Any]:
        return {"decision": "deny", "reason": "Operator rejected rm -rf"}

    instance._request_decision = fake_request  # type: ignore[assignment]

    result = await instance.pre_tool_call(
        tool_name="terminal",
        args={"command": "rm -rf /"},
        task_id="sess_2",
    )

    assert result == {
        "action": "block",
        "message": "Operator rejected rm -rf",
        "decision": "block",
        "reason": "Operator rejected rm -rf",
    }


@pytest.mark.asyncio
async def test_positional_tool_name_python_plugin_form() -> None:
    instance = _plugin()
    captured: dict[str, Any] = {}

    async def fake_request(
        url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        captured["body"] = body
        return {"decision": "allow"}

    instance._request_decision = fake_request  # type: ignore[assignment]

    result = await instance.pre_tool_call(
        "terminal",
        args={"command": "pwd"},
        task_id="sess_3",
    )
    assert result is None
    assert captured["body"]["tool_name"] == "terminal"
    assert captured["body"]["tool_input"] == {"command": "pwd"}
    assert captured["body"]["session_id"] == "sess_3"


@pytest.mark.asyncio
async def test_non_tool_event_is_allowed_without_request() -> None:
    instance = _plugin()

    async def fail(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("should not call backend for non-tool events")

    instance._request_decision = fail  # type: ignore[assignment]

    assert await instance.pre_tool_call({"hook_event_name": "pre_llm_call"}) is None


@pytest.mark.asyncio
async def test_network_error_fails_closed_by_default() -> None:
    instance = _plugin()

    async def boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("connection refused")

    instance._request_decision = boom  # type: ignore[assignment]

    result = await instance.pre_tool_call(
        {"tool_name": "terminal", "tool_input": {"command": "ls"}}
    )

    assert result is not None
    assert result["action"] == "block"
    assert result["decision"] == "block"
    assert "unavailable" in result["message"]
    assert "unavailable" in result["reason"]


@pytest.mark.asyncio
async def test_network_error_fails_open_when_configured() -> None:
    instance = _plugin({"fail_open": True})

    async def boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("connection refused")

    instance._request_decision = boom  # type: ignore[assignment]

    result = await instance.pre_tool_call(
        {"tool_name": "terminal", "tool_input": {"command": "ls"}}
    )

    assert result is None


@pytest.mark.asyncio
async def test_disabled_approval_skips_gate() -> None:
    instance = _plugin({"enabled": False})

    async def fail(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("should not call backend when disabled")

    instance._request_decision = fail  # type: ignore[assignment]

    assert (
        await instance.pre_tool_call({"tool_name": "terminal", "tool_input": {}})
        is None
    )


def test_resolve_fail_open_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_TOOL_APPROVAL_FAIL_OPEN", "true")
    assert _resolve_fail_open({"fail_open": False}) is True
    monkeypatch.setenv("PRELOOP_TOOL_APPROVAL_FAIL_OPEN", "0")
    assert _resolve_fail_open({"fail_open": True}) is False
    monkeypatch.delenv("PRELOOP_TOOL_APPROVAL_FAIL_OPEN", raising=False)
    assert _resolve_fail_open({"fail_open": True}) is True


def test_register_wires_pre_tool_call_hook() -> None:
    instance = HermesPreloopPlugin()
    registered: list[tuple[str, Any]] = []

    class Ctx:
        def register_hook(self, event: str, cb: Any) -> None:
            registered.append((event, cb))

    instance.register(Ctx())
    assert registered == [("pre_tool_call", instance.pre_tool_call)]
