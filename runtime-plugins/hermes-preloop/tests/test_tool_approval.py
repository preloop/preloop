"""Tests for the Hermes native tool-call approval gate."""

from __future__ import annotations

import asyncio
import inspect
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
        raise OSError("connection refused")

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
        raise OSError("connection refused")

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
    assert registered == [("pre_tool_call", instance.pre_tool_call_sync)]


# ---------------------------------------------------------------------------
# Synchronous hook bridge.
#
# Hermes invokes plugin hooks SYNCHRONOUSLY -- ``hermes_cli/plugins.py`` does
# ``ret = cb(**kwargs)`` with no ``await`` -- and then discards any result that
# is not a dict (``if not isinstance(result, dict): continue``). An ``async
# def`` callback therefore returns a coroutine that Hermes drops on the floor,
# and the tool call proceeds ungated. These tests drive the registered callback
# the way Hermes actually does.
# ---------------------------------------------------------------------------


def _registered_hook(instance: HermesPreloopPlugin) -> Any:
    """Return the callback the plugin hands Hermes for ``pre_tool_call``."""
    registered: dict[str, Any] = {}

    class Ctx:
        def register_hook(self, event: str, cb: Any) -> None:
            registered[event] = cb

    instance.register(Ctx())
    return registered["pre_tool_call"]


def _invoke_like_hermes(cb: Any, **kwargs: Any) -> Any:
    """Call a hook the way ``hermes_cli.plugins.invoke_hook`` does.

    Hermes passes every field as a keyword argument, never positionally, and
    does not await the return value.
    """
    kwargs.setdefault("args", {})
    kwargs.setdefault("task_id", "")
    kwargs.setdefault("session_id", "")
    kwargs.setdefault("tool_call_id", "")
    kwargs.setdefault("turn_id", "")
    kwargs.setdefault("api_request_id", "")
    kwargs.setdefault("middleware_trace", [])
    kwargs.setdefault("telemetry_schema_version", 1)
    return cb(**kwargs)


def _hermes_directive(result: Any) -> str | None:
    """Apply Hermes' result filter from ``_get_pre_tool_call_directive_details``."""
    if not isinstance(result, dict):
        return None
    action = result.get("action")
    if action not in ("block", "approve"):
        return None
    message = result.get("message")
    if action == "block" and not (isinstance(message, str) and message):
        return None
    return str(action)


def test_sync_hook_returns_dict_block_to_a_synchronous_caller() -> None:
    """A deny must reach Hermes as a real dict, not an un-awaited coroutine."""
    instance = _plugin()

    async def fake_request(*_: Any, **__: Any) -> dict[str, Any]:
        return {"decision": "deny", "reason": "Operator rejected rm -rf"}

    instance._request_decision = fake_request  # type: ignore[assignment]

    result = _invoke_like_hermes(
        _registered_hook(instance),
        tool_name="terminal",
        args={"command": "rm -rf /"},
    )

    assert isinstance(result, dict), f"Hermes discards non-dict results, got {result!r}"
    assert result["action"] == "block"
    assert result["message"] == "Operator rejected rm -rf"
    # And Hermes' own filter must accept it.
    assert _hermes_directive(result) == "block"


def test_sync_hook_allows_when_backend_allows() -> None:
    instance = _plugin()

    async def fake_request(*_: Any, **__: Any) -> dict[str, Any]:
        return {"decision": "allow"}

    instance._request_decision = fake_request  # type: ignore[assignment]

    result = _invoke_like_hermes(_registered_hook(instance), tool_name="terminal")
    assert result is None


def test_sync_hook_fails_closed_on_unreachable_control_plane() -> None:
    instance = _plugin()

    async def boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise OSError("connection refused")

    instance._request_decision = boom  # type: ignore[assignment]

    result = _invoke_like_hermes(_registered_hook(instance), tool_name="terminal")
    assert _hermes_directive(result) == "block"
    assert "unavailable" in result["message"]


def test_sync_hook_fails_closed_on_timeout() -> None:
    """A hung control plane must block, and must not hang the agent forever."""
    instance = _plugin()

    async def hang(*_: Any, **__: Any) -> dict[str, Any]:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    instance._request_decision = hang  # type: ignore[assignment]
    # Keep the test fast: shrink the bridge deadline rather than waiting ~5min.
    instance._bridge_timeout_seconds = 0.25

    started = time.monotonic()
    result = _invoke_like_hermes(_registered_hook(instance), tool_name="terminal")
    elapsed = time.monotonic() - started

    assert _hermes_directive(result) == "block"
    assert elapsed < 10, f"bridge hung the agent for {elapsed:.1f}s"


def test_sync_hook_fails_closed_on_unexpected_exception() -> None:
    """A bug anywhere under the hook must block, never silently allow."""
    instance = _plugin()

    def explode(*_: Any, **__: Any) -> Any:
        raise ValueError("malformed config")

    instance._check_permission = explode  # type: ignore[assignment]

    result = _invoke_like_hermes(_registered_hook(instance), tool_name="terminal")
    assert _hermes_directive(result) == "block"


def test_sync_hook_fails_closed_on_malformed_response() -> None:
    """A non-mapping backend response must not be read as an allow."""
    instance = _plugin()

    async def garbage(*_: Any, **__: Any) -> Any:
        return "not a mapping"

    instance._request_decision = garbage  # type: ignore[assignment]

    result = _invoke_like_hermes(_registered_hook(instance), tool_name="terminal")
    assert _hermes_directive(result) == "block"


def test_sync_hook_honors_fail_open_override() -> None:
    instance = _plugin({"fail_open": True})

    async def boom(*_: Any, **__: Any) -> dict[str, Any]:
        raise OSError("connection refused")

    instance._request_decision = boom  # type: ignore[assignment]

    assert _invoke_like_hermes(_registered_hook(instance), tool_name="terminal") is None


def test_sync_hook_works_with_a_running_event_loop_in_the_caller() -> None:
    """The bridge must not depend on the calling thread being loop-free.

    ``asyncio.run()`` raises when a loop is already running in the thread, and
    Hermes embeds this sync agent loop inside async hosts (gateway,
    acp_adapter). The hook must still return a decision.
    """
    instance = _plugin()

    async def fake_request(*_: Any, **__: Any) -> dict[str, Any]:
        return {"decision": "deny", "reason": "nope"}

    instance._request_decision = fake_request  # type: ignore[assignment]
    hook = _registered_hook(instance)

    async def driver() -> Any:
        # Called from inside a running loop, exactly like the gateway path.
        return _invoke_like_hermes(hook, tool_name="terminal")

    result = asyncio.run(driver())
    assert _hermes_directive(result) == "block"


def test_sync_hook_is_concurrency_safe_across_worker_threads() -> None:
    """Hermes executes tool calls in parallel worker threads."""
    instance = _plugin()

    async def fake_request(*_: Any, **__: Any) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        return {"decision": "deny", "reason": "nope"}

    instance._request_decision = fake_request  # type: ignore[assignment]
    hook = _registered_hook(instance)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: _invoke_like_hermes(hook, tool_name="terminal"), range(16)
            )
        )

    assert all(_hermes_directive(r) == "block" for r in results)


def test_sync_hook_fails_closed_on_unreadable_config() -> None:
    """A missing/broken ``preloop.control`` block must not mean "allow".

    ``_load_control_settings`` raises for an absent or malformed config. Hermes
    swallows hook exceptions and proceeds, so the plugin must convert that into
    a block itself.
    """
    instance = HermesPreloopPlugin(config_path=Path("/nonexistent/hermes.yaml"))

    result = _invoke_like_hermes(_registered_hook(instance), tool_name="terminal")
    assert _hermes_directive(result) == "block"


@pytest.mark.asyncio
async def test_async_path_fails_closed_on_malformed_yaml(tmp_path: Path) -> None:
    """Malformed YAML must fail closed on the awaitable path, not raise."""
    config = tmp_path / "hermes.yaml"
    config.write_text("preloop: { control: [unterminated\n", encoding="utf-8")
    instance = HermesPreloopPlugin(config_path=config)

    result = await instance.pre_tool_call(
        {"tool_name": "terminal", "tool_input": {"command": "ls"}}
    )

    assert result is not None
    assert result["action"] == "block"
    assert "configuration invalid" in result["message"]


def test_unreadable_config_never_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An environment override never turns a broken credential/config into allow."""
    monkeypatch.setenv("PRELOOP_TOOL_APPROVAL_FAIL_OPEN", "true")
    instance = HermesPreloopPlugin(config_path=Path("/nonexistent/hermes.yaml"))

    assert (
        _hermes_directive(
            _invoke_like_hermes(_registered_hook(instance), tool_name="terminal")
        )
        == "block"
    )


def test_async_implementation_is_still_awaitable() -> None:
    """The async path stays intact for other callers and existing tests."""
    assert inspect.iscoroutinefunction(HermesPreloopPlugin.pre_tool_call)
    assert not inspect.iscoroutinefunction(HermesPreloopPlugin.pre_tool_call_sync)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{}, {"decision": "ask"}, {"decision": 1}])
async def test_unknown_decision_fails_closed(data: dict[str, Any]) -> None:
    instance = _plugin()

    async def fake_request(*_: Any) -> dict[str, Any]:
        return data

    instance._request_decision = fake_request  # type: ignore[assignment]
    result = await instance.pre_tool_call(tool_name="terminal", args={})
    assert result is not None
    assert result["action"] == "block"


@pytest.mark.asyncio
async def test_expiry_deny_is_terminal_even_with_fail_open() -> None:
    instance = _plugin({"fail_open": True})

    async def fake_request(*_: Any) -> dict[str, Any]:
        return {"decision": "deny", "timed_out": True, "reason": "Approval expired"}

    instance._request_decision = fake_request  # type: ignore[assignment]
    result = await instance.pre_tool_call(tool_name="terminal", args={})
    assert result is not None
    assert result["action"] == "block"


def test_workflow_wait_budget_has_http_and_bridge_headroom() -> None:
    assert _plugin()._permission_timeout_seconds() == 86415
    assert _plugin()._approval_bridge_timeout_seconds() == 86430
    assert _plugin({"timeout_seconds": 1800})._permission_timeout_seconds() == 1815
    assert _plugin({"timeout_seconds": 1800})._approval_bridge_timeout_seconds() == 1830


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 29, 86401, 30.5, "300", True, None])
async def test_invalid_workflow_wait_budget_blocks_without_request(value: Any) -> None:
    instance = _plugin({"timeout_seconds": value})
    called = False

    async def fake_request(*_: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"decision": "allow"}

    instance._request_decision = fake_request  # type: ignore[assignment]
    result = await instance.pre_tool_call(tool_name="terminal", args={})
    assert result is not None
    assert result["action"] == "block"
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_real_http_waits_for_synthetic_approval(decision: str) -> None:
    from aiohttp import web

    received = asyncio.Event()
    decided = asyncio.Event()

    async def check(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["source"] == "hermes"
        assert body["client_decision"] == "ask"
        received.set()
        await decided.wait()
        return web.json_response(
            {"decision": decision, "request_id": "synthetic-approval"}
        )

    app = web.Application()
    app.router.add_post("/api/v1/agents/permission-check", check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = runner.addresses[0][1]
        instance = _plugin()
        instance._control_settings = (
            AgentControlConfig(
                control_ws_url=f"ws://127.0.0.1:{port}/api/v1/agents/control/ws",
                bearer_token="synthetic-token",
                runtime_principal_id="synthetic-hermes",
            ),
            {},
        )
        pending = asyncio.create_task(
            instance.pre_tool_call(tool_name="terminal", args={"command": "pwd"})
        )
        await asyncio.wait_for(received.wait(), timeout=5)
        assert not pending.done()
        decided.set()
        result = await asyncio.wait_for(pending, timeout=5)
        if decision == "allow":
            assert result is None
        else:
            assert result is not None
            assert result["action"] == "block"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_open", [False, True])
async def test_http_timeout_obeys_explicit_fail_open(fail_open: bool) -> None:
    instance = _plugin({"fail_open": fail_open})

    async def timeout(*_: Any) -> dict[str, Any]:
        raise asyncio.TimeoutError("synthetic HTTP timeout")

    instance._request_decision = timeout  # type: ignore[assignment]
    result = await instance.pre_tool_call(tool_name="terminal", args={})
    if fail_open:
        assert result is None
    else:
        assert result is not None
        assert result["action"] == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
async def test_auth_and_client_errors_never_fail_open(status: int) -> None:
    from aiohttp import ClientResponseError
    from unittest.mock import AsyncMock

    instance = _plugin({"fail_open": True})
    instance._request_decision = AsyncMock(
        side_effect=ClientResponseError(
            request_info=type(
                "RequestInfo", (), {"real_url": "http://127.0.0.1/synthetic"}
            )(),
            history=(),
            status=status,
            message="Synthetic rejection",
        )
    )
    result = await instance.pre_tool_call(tool_name="exec", args={})
    assert result is not None and result["action"] == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"decision": "unknown"},
        {"decision": "allow", "timed_out": True},
        {"decision": "deny", "reason": {"invalid": True}},
        {"decision": "allow", "timed_out": "false"},
    ],
)
async def test_malformed_decision_never_fails_open(body: Any) -> None:
    from unittest.mock import AsyncMock

    instance = _plugin({"fail_open": True})
    instance._request_decision = AsyncMock(return_value=body)
    result = await instance.pre_tool_call(tool_name="exec", args={})
    assert result is not None and result["action"] == "block"


@pytest.mark.parametrize(
    "key,value",
    [("enabled", 0), ("enabled", "false"), ("fail_open", "false"), ("fail_open", 1)],
)
def test_invalid_boolean_config_is_closed(
    key: str, value: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("PRELOOP_TOOL_APPROVAL_FAIL_OPEN", "true")
    instance = _plugin({key: value})
    instance._request_decision = AsyncMock(side_effect=OSError("Synthetic outage"))
    result = instance.pre_tool_call_sync(tool_name="exec", args={})
    assert result is not None and result["action"] == "block"
    instance._request_decision.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,fail_open", [(401, True), (403, True), (503, True), (503, False)]
)
async def test_real_http_classifies_auth_and_availability(
    status: int, fail_open: bool
) -> None:
    from aiohttp import web

    async def check(request: web.Request) -> web.Response:
        return web.json_response({"detail": "Synthetic rejection"}, status=status)

    app = web.Application()
    app.router.add_post("/api/v1/agents/permission-check", check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = runner.addresses[0][1]
        instance = _plugin()
        instance._control_settings = (
            AgentControlConfig(
                control_ws_url=f"ws://127.0.0.1:{port}/api/v1/agents/control/ws",
                bearer_token="synthetic-token",
                runtime_principal_id="synthetic-hermes",
            ),
            {"fail_open": fail_open},
        )
        result = await instance.pre_tool_call(tool_name="terminal", args={})
        assert (result is None) == (status >= 500 and fail_open)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_unexpected_failure_never_fails_open() -> None:
    from unittest.mock import AsyncMock

    instance = _plugin({"fail_open": True})
    instance._request_decision = AsyncMock(
        side_effect=RuntimeError("Synthetic unexpected failure")
    )
    assert (await instance.pre_tool_call(tool_name="exec", args={}))[
        "action"
    ] == "block"
