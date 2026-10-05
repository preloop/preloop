"""Lifecycle and enforcement tests against the real adapter seam."""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from preloop.integrations.agent_control import OperatorCommand
from preloop_nanobot_plugin.cli import PINNED_NANOBOT_SDK, discover, enroll
from preloop_nanobot_plugin.runtime import (
    BoundedProvider,
    GovernedTools,
    NanobotRuntime,
    _session,
    validate_document,
)


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
async def test_outbound_message_content_is_the_reply(tmp_path: Path) -> None:
    """nanobot-ai 0.2 process_direct returns OutboundMessage, not a string."""
    loop = FakeLoop()
    loop._max_messages = 120
    loop.process_direct.return_value = SimpleNamespace(
        content="Completed local fixture."
    )
    runtime = NanobotRuntime(
        validate_document(document()), loop, tmp_path / "state.json"
    )
    result = await runtime.handle_send_message(
        OperatorCommand("one", "hello", session_mode="new")
    )
    assert result.reply_text == "Completed local fixture."
    assert loop._max_messages == 40


@pytest.mark.asyncio
async def test_bounded_provider_stamps_session_on_lazy_client() -> None:
    """0.2 leaves _client empty until _ensure_client; the session header still lands."""
    built = SimpleNamespace(
        with_options=lambda **kwargs: SimpleNamespace(headers=kwargs["default_headers"])
    )
    provider = SimpleNamespace(
        _client=None,
        chat=AsyncMock(
            return_value=SimpleNamespace(
                finish_reason="stop", usage={"total_tokens": 1}
            )
        ),
    )

    async def ensure() -> object:
        provider._client = built
        return built

    provider._ensure_client = ensure
    token = _session.set("sess-1")
    try:
        await BoundedProvider(provider).chat(messages=[])
    finally:
        _session.reset(token)
    assert provider._client.headers["X-Preloop-Session-Id"] == "sess-1"


@pytest.mark.asyncio
async def test_retry_entry_point_stays_inside_the_budget() -> None:
    """0.2 reaches the provider through chat_with_retry."""
    retry = AsyncMock(
        return_value=SimpleNamespace(finish_reason="stop", usage={"total_tokens": 3})
    )
    chat = AsyncMock()
    provider = SimpleNamespace(chat=chat, chat_with_retry=retry)
    await BoundedProvider(provider).chat_with_retry(
        messages=[{"role": "user", "content": "hi"}]
    )
    retry.assert_awaited()
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_budget_estimates_characters() -> None:
    """A 32k token cap still admits the larger 0.2 prompt."""
    chat = AsyncMock(
        return_value=SimpleNamespace(finish_reason="stop", usage={"total_tokens": 10})
    )
    wrapper = BoundedProvider(SimpleNamespace(chat=chat))
    wrapper.remaining_tokens = 32000
    await wrapper.chat(messages=[{"role": "user", "content": "x" * 30000}])
    chat.assert_awaited()
    wrapper.remaining_tokens = 32000
    with pytest.raises(ValueError, match="budget"):
        await wrapper.chat(messages=[{"role": "user", "content": "x" * 130000}])


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

    def factory(doc: dict, state: Path, *, model_base_url: str) -> SimpleNamespace:
        captured.append({**doc, "model_base_url": model_base_url})
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
    split_gateway = {
        **gateway,
        "api_url": "https://example.com",
        "base_url": "https://gateway.example.com/openai/v1",
    }
    await runtime.handle_send_message(
        OperatorCommand(
            "split", "run", metadata={"gateway": split_gateway}, session_mode="new"
        )
    )
    assert captured[-1]["model_base_url"] == split_gateway["base_url"]
    assert (
        captured[-1]["preloop"]["control"]["control_ws_url"]
        == document()["preloop"]["control"]["control_ws_url"]
    )
    for index, bad in enumerate(
        [
            {**split_gateway, "api_url": "https://foreign.example.com"},
            {**split_gateway, "base_url": "http://gateway.example.com/openai/v1"},
            {
                **split_gateway,
                "base_url": "https://user:pass@gateway.example.com/openai/v1",
            },
        ]
    ):
        with pytest.raises(ValueError):
            await runtime.handle_send_message(
                OperatorCommand(f"invalid-{index}", "run", metadata={"gateway": bad})
            )
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


@pytest.mark.asyncio
async def test_receipt_completed_replay_restart_changed_input_and_no_secrets(
    tmp_path: Path,
) -> None:
    loop = FakeLoop()
    loop.process_direct.return_value = "sensitive-output-secret"
    state = tmp_path / "state.json"
    runtime = NanobotRuntime(validate_document(document()), loop, state)
    command = OperatorCommand(
        "durable",
        "private-input-secret",
        metadata={"private": "metadata-secret"},
        session_mode="new",
    )
    original = await runtime.handle_send_message(command)
    replay = await runtime.handle_send_message(command)
    assert replay.status == "completed"
    assert replay.session_reference == original.session_reference
    assert replay.reply_text == ""
    assert replay.metadata["receipt_replayed"] is True
    assert loop.process_direct.await_count == 1
    restarted_loop = FakeLoop()
    restarted = NanobotRuntime(runtime.config, restarted_loop, state)
    assert (await restarted.handle_send_message(command)).status == "completed"
    restarted_loop.process_direct.assert_not_awaited()
    with pytest.raises(ValueError, match="different input"):
        await restarted.handle_send_message(
            OperatorCommand("durable", "changed", session_mode="new")
        )
    receipt = state.with_name(state.name + ".receipts.json")
    stored = receipt.read_text()
    for secret in [
        "private-input-secret",
        "metadata-secret",
        "runtime-secret",
        "sensitive-output-secret",
    ]:
        assert secret not in stored
    assert receipt.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_receipt_inflight_duplicate_and_crash_unknown_never_execute(
    tmp_path: Path,
) -> None:
    loop = FakeLoop()
    entered = asyncio.Event()

    async def work(*args: object, **kwargs: object) -> str:
        entered.set()
        await asyncio.Event().wait()
        return ""

    loop.process_direct.side_effect = work
    state = tmp_path / "state.json"
    runtime = NanobotRuntime(validate_document(document()), loop, state)
    command = OperatorCommand("effectful", "work", session_mode="new")
    first = asyncio.create_task(runtime.handle_send_message(command))
    await entered.wait()
    duplicate = await runtime.handle_send_message(command)
    assert duplicate.status == "accepted"
    assert duplicate.metadata["receipt_state"] == "in_progress"
    assert loop.process_direct.await_count == 1
    # A separate runtime seeing the durable pending intent cannot know whether
    # effects completed before an abrupt exit, even while the first still lives.
    other_loop = FakeLoop()
    restarted = NanobotRuntime(runtime.config, other_loop, state)
    unknown = await restarted.handle_send_message(command)
    assert unknown.status == "failed"
    assert unknown.metadata["receipt_state"] == "outcome_unknown"
    other_loop.process_direct.assert_not_awaited()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await runtime.handle_send_message(command)).status == "failed"


@pytest.mark.asyncio
async def test_queue_wait_and_expiry_never_start_effects(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    loop = FakeLoop()
    runtime = NanobotRuntime(
        validate_document(document()), loop, tmp_path / "state.json"
    )
    await runtime.lock.acquire()
    command = OperatorCommand(
        "queued", "work", metadata={"timeout_seconds": 1}, session_mode="new"
    )
    try:
        with pytest.raises(TimeoutError):
            await runtime.handle_send_message(command)
    finally:
        runtime.lock.release()
    loop.process_direct.assert_not_awaited()
    assert (await runtime.handle_send_message(command)).status == "failed"
    expiry = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(TimeoutError, match="expired"):
        await runtime.handle_send_message(
            OperatorCommand(
                "expired", "work", metadata={"expires_at": expiry}, session_mode="new"
            )
        )
    loop.process_direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_abrupt_worker_exit_retains_intent_before_effect(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    state, effect = tmp_path / "state.json", tmp_path / "effect.txt"
    script = """
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace
from preloop.integrations.agent_control import OperatorCommand
from preloop_nanobot_plugin.runtime import NanobotRuntime, validate_document
async def execute(*args, **kwargs):
    Path(sys.argv[2]).write_text("effect performed")
    os._exit(17)
loop = SimpleNamespace(tools=SimpleNamespace(), provider=None,
    sessions=SimpleNamespace(get_or_create=lambda _: SimpleNamespace(messages=[])),
    process_direct=execute)
config = validate_document({'preloop': {'control': {
    'control_ws_url': 'wss://example.com/api/v1/agents/control/ws',
    'bearer_token': 'runtime-secret', 'runtime_principal_id': 'worker-a'}}})
runtime = NanobotRuntime(config, loop, Path(sys.argv[1]))
asyncio.run(runtime.handle_send_message(OperatorCommand('crashed', 'work', session_mode='new')))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(state), str(effect)],
        env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 17, process.stderr
    assert effect.read_text() == "effect performed"
    loop = FakeLoop()
    restarted = NanobotRuntime(validate_document(document()), loop, state)
    replay = await restarted.handle_send_message(
        OperatorCommand("crashed", "work", session_mode="new")
    )
    assert replay.status == "failed"
    assert replay.metadata["receipt_state"] == "outcome_unknown"
    loop.process_direct.assert_not_awaited()


def test_sdk_pin_matches_pyproject_and_manifest() -> None:
    """The install pin, manifest, and verify guard name one SDK release."""
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    pin = next(
        dep
        for dep in pyproject["project"]["dependencies"]
        if dep.startswith("nanobot-ai==")
    )
    manifest = json.loads((root / "preloop-plugin.json").read_text())
    assert pin == f"nanobot-ai=={PINNED_NANOBOT_SDK}"
    assert manifest["sdkVersion"] == pin
