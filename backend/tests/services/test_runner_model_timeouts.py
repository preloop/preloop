"""Runner liveness must not impose a model thinking deadline."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.services import runner_model_proxy as proxy
from preloop.services.model_gateway_errors import ModelGatewayAPIError


def model() -> SimpleNamespace:
    """Build a synthetic registered model."""
    return SimpleNamespace(
        id="model-1",
        account_id="account-1",
        runner_id="runner-1",
        api_endpoint="http://localhost:11434/v1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "silent", "total", "legacy"])
async def test_request_deadlines(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    """Use scaled limits to exercise the real pending-frame wait and cleanup."""
    monkeypatch.setattr(proxy.settings, "runner_model_idle_seconds", 0.04)
    monkeypatch.setattr(proxy.settings, "runner_model_silent_seconds", 0.16)
    monkeypatch.setattr(proxy.settings, "runner_model_total_seconds", 0.3)
    socket = SimpleNamespace(send_json=AsyncMock())
    connection = proxy.RunnerModelConnection(socket, "account-1", "runner-1")
    monkeypatch.setattr(connection, "_authorized", lambda raw: True)
    request = {
        "operation": "start",
        "request_id": "one",
        "base_url": "http://localhost:11434/v1",
        "timeouts": {"total_seconds": 0.09 if outcome == "total" else 0.3},
    }
    message = SimpleNamespace(
        data=json.dumps(request).encode(), reply="inbox", respond=AsyncMock()
    )

    async def runner() -> None:
        while not socket.send_json.called:
            await asyncio.sleep(0)
        if outcome == "legacy":
            return
        for _ in range(6 if outcome == "success" else 30):
            await asyncio.sleep(0.015)
            connection.response({"request_id": "one", "keepalive": True})
        connection.response({"request_id": "one", "data": "ok", "done": True})

    task = asyncio.create_task(runner())
    try:
        await connection._request(message)
        reply = json.loads(message.respond.call_args.args[0])
        if outcome == "success":
            assert reply["data"] == "ok"
        else:
            assert reply["error"] == "runner_proxy_timeout"
            assert reply["timeout"] == ("idle" if outcome == "legacy" else outcome)
            assert socket.send_json.call_args.args[0]["operation"] == "cancel"
        assert not connection.active
        assert not connection.pending
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await connection.close()


def test_limits_sent_and_total_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request advertises settings and never outlives its total budget."""
    stream = proxy.RunnerModelStream(model(), {}, total_seconds=120)
    try:
        assert stream.start_frame["timeouts"] == {
            "idle_seconds": 60,
            "silent_seconds": 600,
            "total_seconds": 120,
        }
        monkeypatch.setattr(stream, "started", stream.started - 121)
        with pytest.raises(ModelGatewayAPIError, match="model request exceeded 120 s"):
            stream.read()
        assert stream.closed
    finally:
        stream.close()


@pytest.mark.parametrize(
    "kind, expected",
    [
        (
            "silent",
            "local model sent no data for 600 s (RUNNER_MODEL_SILENT_SECONDS=600)",
        ),
        ("total", "model request exceeded 3600 s"),
        ("idle", "runner sent no frames for 60 s (RUNNER_MODEL_IDLE_SECONDS=60)"),
    ],
)
def test_timeout_errors_name_limit(
    monkeypatch: pytest.MonkeyPatch, kind: str, expected: str
) -> None:
    """Errors retain their existing code while explaining the fired limit."""
    stream = proxy.RunnerModelStream(model(), {})
    monkeypatch.setattr(
        stream,
        "_rpc",
        AsyncMock(
            return_value={
                "status": 504,
                "error": "runner_proxy_timeout",
                "timeout": kind,
                "done": True,
            }
        ),
    )
    with pytest.raises(ModelGatewayAPIError) as error:
        stream.read()
    assert expected in str(error.value)
    assert error.value.code == "runner_proxy_timeout"
    assert stream.closed and not stream.thread.is_alive()


def test_gateway_caps_total_to_flow_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the execution budget before releasing the gateway DB session."""
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext
    from preloop.services.openai_gateway import OpenAIGatewayService

    auth = ModelGatewayAuthContext(
        token="test", user=SimpleNamespace(id="u", account_id="a")
    )
    service = OpenAIGatewayService(MagicMock(), auth, upstream_backend=MagicMock())
    monkeypatch.setattr(proxy, "runner_model_flow_budget", lambda *args: 123)
    dispatch = MagicMock()
    monkeypatch.setattr(proxy, "runner_completion", dispatch)
    monkeypatch.setattr(
        service,
        "_optimize_request_context",
        lambda **kw: (kw["messages"], kw["payload"]),
    )
    local = model()
    local.endpoint_type = "runner"
    local.model_identifier = "example"
    service._call_litellm(local, messages=[], payload={}, provider="openai")
    assert dispatch.call_args.kwargs["total_seconds"] == 123


@pytest.mark.parametrize(
    "spent, consumed, expected", [(100, 0, 200), (100, 120, 80), (400, 0, 0)]
)
def test_flow_remaining_budget(
    monkeypatch: pytest.MonkeyPatch, spent: int, consumed: int, expected: int
) -> None:
    """Launch time and time consumed before a park count against the flow."""
    from preloop.models.crud import crud_flow, crud_flow_execution

    now = datetime.now(timezone.utc)
    execution = SimpleNamespace(
        id="execution-1",
        flow_id="flow-1",
        status="RUNNING",
        launch_requested_at=now - timedelta(seconds=spent),
        start_time=now,
        trigger_event_details={"_answers": {"consumed_seconds": consumed}},
    )
    monkeypatch.setattr(crud_flow_execution, "get", lambda *args, **kw: execution)
    monkeypatch.setattr(
        crud_flow,
        "get",
        lambda *args, **kw: SimpleNamespace(account_id="a", timeout_seconds=300),
    )
    auth = SimpleNamespace(
        account_id="a",
        api_key=SimpleNamespace(context_data={"flow_execution_id": "execution-1"}),
    )
    result = proxy.runner_model_flow_budget(MagicMock(), auth)
    assert result == pytest.approx(expected, abs=0.1)


@pytest.mark.asyncio
async def test_active_slot_survives_old_total_and_expires_without_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slot sweep uses each request's deadline, including abandoned pulls."""
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(proxy, "time", SimpleNamespace(monotonic=lambda: clock.now))
    socket = SimpleNamespace(send_json=AsyncMock())
    connection = proxy.RunnerModelConnection(socket, "a", "r")
    monkeypatch.setattr(connection, "_authorized", lambda raw: True)

    async def send(raw: dict[str, Any]) -> None:
        if raw["operation"] != "cancel":
            connection.response({"request_id": "one", "data": "ok"})

    socket.send_json.side_effect = send

    async def request(operation: str) -> None:
        await connection._request(
            SimpleNamespace(
                data=json.dumps(
                    {
                        "request_id": "one",
                        "operation": operation,
                        "base_url": "http://localhost:11434/v1",
                    }
                ).encode(),
                reply="inbox",
                respond=AsyncMock(),
            )
        )

    await request("start")
    clock.now += 601
    await request("next")
    assert "one" in connection.active
    await connection.close()

    monkeypatch.setattr(proxy.settings, "runner_model_total_seconds", 0.04)
    connection = proxy.RunnerModelConnection(socket, "a", "r")
    monkeypatch.setattr(connection, "_authorized", lambda raw: True)
    await request("start")
    assert "one" in connection.active
    await asyncio.sleep(0.08)
    assert not connection.active
    assert socket.send_json.call_args.args[0]["operation"] == "cancel"
    await connection.close()


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented variables reach validated settings without a CLI flag."""
    from preloop.config import Settings
    from pydantic import ValidationError

    monkeypatch.setenv("RUNNER_MODEL_IDLE_SECONDS", "90")
    monkeypatch.setenv("RUNNER_MODEL_SILENT_SECONDS", "1200")
    monkeypatch.setenv("RUNNER_MODEL_TOTAL_SECONDS", "7200")
    required = {
        name: getattr(proxy.settings, name)
        for name in ("database", "security", "server")
    }
    settings = Settings(_env_file=None, **required)
    assert (
        settings.runner_model_idle_seconds,
        settings.runner_model_silent_seconds,
        settings.runner_model_total_seconds,
    ) == (90, 1200, 7200)
    for value in ("0", "-1", "nan", "inf"):
        monkeypatch.setenv("RUNNER_MODEL_SILENT_SECONDS", value)
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **required)
