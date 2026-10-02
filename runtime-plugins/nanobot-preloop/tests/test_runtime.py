"""Lifecycle and enforcement tests against the real adapter seam."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from preloop.integrations.agent_control import OperatorCommand
from preloop_nanobot_plugin.runtime import (
    BoundedProvider,
    GovernedTools,
    NanobotRuntime,
    validate_document,
)
from preloop_nanobot_plugin.cli import discover, enroll


def document() -> dict:
    return {
        "preloop": {
            "control": {
                "control_ws_url": "wss://example.com/api/v1/agents/control/ws",
                "bearer_token": "runtime-secret",
                "runtime_principal_id": "worker-a",
            }
        }
    }


class FakeLoop:
    def __init__(self) -> None:
        self.tools = SimpleNamespace(execute=AsyncMock(return_value="executed"))
        self.sessions = SimpleNamespace(
            get_or_create=lambda _: SimpleNamespace(messages=[])
        )
        self.provider = None
        self.process_direct = AsyncMock(return_value="reply")


@pytest.mark.asyncio
async def test_new_resume_restart_and_foreign_denial(tmp_path: Path) -> None:
    loop = FakeLoop()
    runtime = NanobotRuntime(
        validate_document(document()), loop, tmp_path / "state.json"
    )
    first = await runtime.handle_send_message(
        OperatorCommand("one", "hello", session_mode="new")
    )
    resumed = await runtime.handle_send_message(
        OperatorCommand(
            "two",
            "again",
            session_mode="existing",
            session_reference=first.session_reference,
        )
    )
    assert resumed.session_reference == first.session_reference
    restart = NanobotRuntime(runtime.config, FakeLoop(), runtime.state)
    assert first.session_reference in restart.sessions
    with pytest.raises(ValueError, match="not owned"):
        await runtime.handle_send_message(
            OperatorCommand(
                "three", "no", session_mode="existing", session_reference="foreign"
            )
        )
    other = document()
    other["preloop"]["control"]["runtime_principal_id"] = "worker-b"
    with pytest.raises(ValueError, match="another principal"):
        NanobotRuntime(validate_document(other), FakeLoop(), runtime.state)


@pytest.mark.asyncio
async def test_timeout_and_command_owned_cancellation(tmp_path: Path) -> None:
    loop = FakeLoop()
    entered = asyncio.Event()

    async def blocked(*args: object, **kwargs: object) -> str:
        entered.set()
        await asyncio.Event().wait()
        return ""

    loop.process_direct.side_effect = blocked
    runtime = NanobotRuntime(
        validate_document(document()), loop, tmp_path / "state.json"
    )
    task = asyncio.create_task(
        runtime.handle_send_message(
            OperatorCommand("owned", "wait", session_mode="new")
        )
    )
    await entered.wait()
    with pytest.raises(ValueError, match="owned"):
        await runtime.interrupt("foreign")
    await runtime.handle_send_message(
        OperatorCommand(
            "stop", "stop", metadata={"target_command_id": "owned"}, interrupt=True
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime.active
    with pytest.raises(TimeoutError):
        await runtime.handle_send_message(
            OperatorCommand("timeout", "wait", metadata={"timeout_seconds": 1})
        )


@pytest.mark.parametrize("value", ["", "token\nforged"])
def test_invalid_credentials(value: str) -> None:
    doc = document()
    doc["preloop"]["control"]["bearer_token"] = value
    with pytest.raises(ValueError):
        validate_document(doc)


@pytest.mark.asyncio
async def test_tool_escaping_and_unowned_execution_blocked() -> None:
    native = SimpleNamespace(execute=AsyncMock())
    tools = GovernedTools(native, validate_document(document()))
    assert "disabled" in await tools.execute("spawn", {})
    assert "unowned" in await tools.execute("exec", {})
    native.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_failure_and_budget_enforced() -> None:
    provider = SimpleNamespace(
        chat=AsyncMock(return_value=SimpleNamespace(finish_reason="error", usage={}))
    )
    wrapper = BoundedProvider(provider)
    with pytest.raises(RuntimeError):
        await wrapper.chat(messages=[])
    wrapper.remaining_tokens = 0
    with pytest.raises(ValueError, match="budget"):
        await wrapper.chat(messages=[])


@pytest.mark.asyncio
async def test_enrollment_failures_never_write_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "preloop.json"
    with pytest.raises(ValueError, match="HTTPS"):
        await enroll(path, "http://example.com", "secret")
    with pytest.raises(ValueError, match="credential"):
        await enroll(path, "https://example.com", "")
    assert not path.exists()
    monkeypatch.setenv("NANOBOT_HOME", str(tmp_path))
    assert discover() == path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,allowed",
    [
        ({"decision": "allow"}, True),
        ({"decision": "deny"}, False),
        ({"decision": "allow", "timed_out": True}, False),
        ({"decision": "allow", "timed_out": "false"}, False),
        ({"decision": "allow", "reason": 1}, False),
        ([], False),
        ({"decision": "ask"}, False),
    ],
)
async def test_permission_decisions_enforced(
    monkeypatch: pytest.MonkeyPatch, decision: object, allowed: bool
) -> None:
    from preloop_nanobot_plugin import runtime as module

    class Response:
        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def raise_for_status(self) -> None:
            pass

        async def json(self) -> object:
            return decision

    class Client(Response):
        def __init__(self, **kwargs: object) -> None:
            pass

        def post(self, *args: object, **kwargs: object) -> Response:
            assert kwargs["json"]["session_id"] == "owned-session"
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Client)
    native = SimpleNamespace(execute=AsyncMock(return_value="executed"))
    tools = GovernedTools(native, validate_document(document()))
    token = module._session.set("owned-session")
    try:
        result = await tools.execute("exec", {"command": "echo example"})
    finally:
        module._session.reset(token)
    assert (result == "executed") is allowed
    assert native.execute.await_count == int(allowed)


@pytest.mark.asyncio
async def test_execution_gateway_credentials_scoped_to_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop_nanobot_plugin import runtime as module

    loop = FakeLoop()
    loop.workspace = tmp_path
    scoped_loop = FakeLoop()
    captured = []

    def factory(doc: dict, state: Path) -> SimpleNamespace:
        captured.append(doc)
        return SimpleNamespace(loop=scoped_loop)

    monkeypatch.setattr(module, "build_runtime", factory)
    runtime = NanobotRuntime(
        validate_document(document()), loop, tmp_path / "state.json"
    )
    gateway = {
        "api_key": "execution-secret",
        "base_url": "https://example.com/openai/v1",
        "model": "deepseek-chat",
    }
    result = await runtime.handle_send_message(
        OperatorCommand(
            "owned",
            "run",
            metadata={"gateway": gateway, "run_limits": {"max_usd": 1}},
            session_mode="new",
        )
    )
    assert result.status == "completed"
    assert captured[0]["preloop"]["control"]["bearer_token"] == "execution-secret"
    assert runtime.loop is loop
    assert scoped_loop.process_direct.await_count == 1
    gateway["base_url"] = "https://other.example.com/openai/v1"
    with pytest.raises(ValueError, match="enrolled instance"):
        await runtime.handle_send_message(
            OperatorCommand("bad", "run", metadata={"gateway": gateway})
        )


@pytest.mark.asyncio
async def test_enrollment_mints_runtime_token_and_exclusive_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop_nanobot_plugin import cli

    class Response:
        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def raise_for_status(self) -> None:
            pass

        async def json(self) -> dict:
            return {
                "token": "scoped-runtime-secret",
                "managed_agent_id": "synthetic-agent",
            }

    class Client(Response):
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["headers"]["Authorization"] == "Bearer user-secret"

        def post(self, url: str, **kwargs: object) -> Response:
            assert kwargs["json"]["session_source_type"] == "nanobot"
            return Response()

    monkeypatch.setattr(cli.aiohttp, "ClientSession", Client)
    path = tmp_path / "preloop.json"
    await enroll(path, "https://example.com", "user-secret")
    assert path.stat().st_mode & 0o777 == 0o600
    assert "user-secret" not in path.read_text()
    assert "scoped-runtime-secret" in path.read_text()
    with pytest.raises(ValueError, match="exists"):
        await enroll(path, "https://example.com", "user-secret")


@pytest.mark.asyncio
async def test_control_receives_interrupt_while_original_command_runs(
    tmp_path: Path,
) -> None:
    from preloop_nanobot_plugin.runtime import ConcurrentControlClient

    loop = FakeLoop()
    entered = asyncio.Event()

    async def wait(*args: object, **kwargs: object) -> str:
        entered.set()
        await asyncio.Event().wait()
        return ""

    loop.process_direct.side_effect = wait
    runtime = NanobotRuntime(
        validate_document(document()), loop, tmp_path / "state.json"
    )
    client = ConcurrentControlClient(runtime)
    socket = SimpleNamespace(send_json=AsyncMock())
    await client._handle_text_message(
        socket,
        {
            "type": "command",
            "name": "send_message",
            "message_id": "first",
            "payload": {"text": "run", "session_mode": "new"},
        },
    )
    await entered.wait()
    await client._handle_text_message(
        socket,
        {
            "type": "command",
            "name": "send_message",
            "message_id": "stop",
            "payload": {
                "text": "stop",
                "interrupt": True,
                "metadata": {"target_command_id": "first"},
            },
        },
    )
    await asyncio.gather(*list(client.commands))
    envelopes = [call.args[0] for call in socket.send_json.await_args_list]
    assert any(
        item["message_id"] == "stop" and item["payload"]["status"] == "accepted"
        for item in envelopes
    )
    assert any(
        item["message_id"] == "first" and item["payload"]["reason"] == "cancelled"
        for item in envelopes
    )
    assert not runtime.active
