"""Security and transport contracts for runner-bound models."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.services.runner_model_proxy import normalize_local_base_url
from preloop.models.crud import crud_ai_model


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:1234/v1/",
        "http://[::1]:8000/v1",
    ],
)
def test_loopback_endpoint(url: str) -> None:
    assert normalize_local_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:11434/v1",
        "http://192.168.1.2:8000/v1",
        "http://localhost/v1",
        "http://user:pass@localhost:8000/v1",
        "http://localhost:8000/v1?x=1",
        "http://localhost:8000/api",
        "http://localhost:8000/v1#fragment",
        "https://localhost:8000/v1",
        "http://127.1:8000/v1",
        "http://localhost.:8000/v1",
    ],
)
def test_reject_non_allowlisted_endpoint(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_local_base_url(url)


def test_crud_rejects_foreign_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from preloop.models.crud.flow_runner import crud_flow_runner

    monkeypatch.setattr(crud_flow_runner, "get", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="Runner not found"):
        crud_ai_model._validate_runner_endpoint(
            MagicMock(),
            {
                "endpoint_type": "runner",
                "runner_id": uuid4(),
                "api_endpoint": "http://localhost:11434/v1",
                "provider_name": "openai",
            },
            account_id=uuid4(),
        )


def test_crud_revalidates_partial_endpoint_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = SimpleNamespace(
        endpoint_type="runner",
        runner_id=uuid4(),
        api_endpoint="http://localhost:11434/v1",
        provider_name="openai",
        account_id=uuid4(),
        api_key=None,
        credentials_secret_id=None,
    )
    with pytest.raises(ValueError):
        crud_ai_model._validate_runner_endpoint(
            MagicMock(),
            {"api_endpoint": "http://example.com:8000/v1"},
            account_id=existing.account_id,
            existing=existing,
        )


@pytest.mark.asyncio
async def test_socket_responses_are_scoped_and_disconnect_is_immediate() -> None:
    import asyncio
    from preloop.services.runner_model_proxy import RunnerModelConnection

    connection = RunnerModelConnection(MagicMock(), str(uuid4()), str(uuid4()))
    future = asyncio.get_running_loop().create_future()
    connection.pending["request-1"] = future
    connection.response({"request_id": "unknown", "data": "ignored"})
    assert not future.done()
    await connection.close()
    assert (await future)["error"] == "runner_offline"


@pytest.mark.asyncio
async def test_response_frame_limit() -> None:
    import asyncio
    from preloop.services.runner_model_proxy import (
        RunnerModelConnection,
        MAX_FRAME_BYTES,
    )

    connection = RunnerModelConnection(MagicMock(), str(uuid4()), str(uuid4()))
    future = asyncio.get_running_loop().create_future()
    connection.pending["request-1"] = future
    connection.response(
        {"request_id": "request-1", "data": "x" * (MAX_FRAME_BYTES + 2048)}
    )
    assert (await future)["error"] == "runner_frame_too_large"


def test_gateway_dispatch_stays_after_optimization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext
    from preloop.services.openai_gateway import OpenAIGatewayService
    from preloop.services import runner_model_proxy

    auth = ModelGatewayAuthContext(
        token="test", user=SimpleNamespace(id=uuid4(), account_id=uuid4())
    )
    service = OpenAIGatewayService(MagicMock(), auth, upstream_backend=MagicMock())
    model = SimpleNamespace(
        endpoint_type="runner",
        runner_id=uuid4(),
        model_identifier="example-model",
        provider_name="openai",
    )
    messages = [{"role": "user", "content": "Hello"}]
    optimized = [{"role": "user", "content": "Optimized"}]
    monkeypatch.setattr(
        service,
        "_optimize_request_context",
        lambda **kwargs: (optimized, kwargs["payload"]),
    )
    dispatch = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(runner_model_proxy, "runner_completion", dispatch)
    result = service._call_litellm(
        model,
        messages=messages,
        payload={
            "model": "public-alias",
            "temperature": 0.2,
            "api_key": "must-not-cross",
        },
        stream=True,
        provider="openai",
    )
    assert result == {"ok": True}
    body = dispatch.call_args.args[1]
    assert body["model"] == "example-model"
    assert body["messages"] == optimized
    assert "api_key" not in body
    assert body["stream_options"]["include_usage"] is True
    service.upstream_backend.completion.assert_not_called()


def test_offline_runner_never_uses_direct_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from preloop.services import runner_model_proxy
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    async def offline(*args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError("broker offline")

    monkeypatch.setattr(runner_model_proxy.nats, "connect", offline)
    model = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        runner_id=uuid4(),
        api_endpoint="http://localhost:11434/v1",
    )
    with pytest.raises(ModelGatewayAPIError) as error:
        runner_model_proxy.runner_completion(model, {"model": "example"}, stream=False)
    assert error.value.status_code == 503
    assert error.value.code == "runner_offline"


def test_unconsumed_stream_can_be_closed_without_starting_worker() -> None:
    from preloop.services.runner_model_proxy import RunnerModelStream

    model = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        runner_id=uuid4(),
        api_endpoint="http://localhost:11434/v1",
    )
    stream = RunnerModelStream(model, {"model": "example"})
    assert not stream.thread.is_alive()
    stream.close()
    stream.close()
    assert stream.loop.is_closed()


@pytest.mark.asyncio
async def test_concurrent_authorizations_reserve_slots_and_disconnect_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import json
    import threading
    from unittest.mock import AsyncMock
    from preloop.services.runner_model_proxy import RunnerModelConnection

    gate = threading.Event()
    connection = RunnerModelConnection(MagicMock(), str(uuid4()), str(uuid4()))
    monkeypatch.setattr(connection, "_authorized", lambda raw: gate.wait(timeout=3))
    messages = [
        SimpleNamespace(
            data=json.dumps(
                {
                    "request_id": str(index),
                    "operation": "start",
                    "base_url": "http://localhost:11434/v1",
                }
            ).encode(),
            reply="inbox",
            respond=AsyncMock(),
        )
        for index in range(5)
    ]
    try:
        for message in messages:
            task = asyncio.create_task(connection._request(message))
            connection.tasks.add(task)
        await asyncio.sleep(0.05)
        assert len(connection.active) == 4
        assert (
            json.loads(messages[4].respond.call_args.args[0])["error"] == "runner_busy"
        )
        await connection.close()
        assert not connection.active
        for message in messages[:4]:
            assert (
                json.loads(message.respond.call_args.args[0])["error"]
                == "runner_offline"
            )
        assert all(task.done() for task in connection.tasks)
    finally:
        gate.set()


def test_stream_frames_preserve_deltas_and_close_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from unittest.mock import AsyncMock
    from preloop.services.runner_model_proxy import RunnerModelStream

    model = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        runner_id=uuid4(),
        api_endpoint="http://localhost:11434/v1",
    )
    stream = RunnerModelStream(model, {"model": "example", "stream": True})
    rpc = AsyncMock(
        side_effect=[
            {
                "status": 200,
                "data": "data: "
                + json.dumps(
                    {
                        "id": "example-completion",
                        "model": "example",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "Hello"},
                                "finish_reason": None,
                            }
                        ],
                    }
                ),
            },
            {"status": 200, "data": "data: [DONE]", "done": True},
        ]
    )
    monkeypatch.setattr(stream, "_rpc", rpc)
    chunks = list(stream)
    assert chunks[0].choices[0].delta.content == "Hello"
    assert [call.args[0]["operation"] for call in rpc.call_args_list] == [
        "start",
        "next",
    ]
    assert stream.closed and not stream.thread.is_alive()


def test_request_size_limit_precedes_transport_allocation() -> None:
    from preloop.services.runner_model_proxy import RunnerModelStream, MAX_REQUEST_BYTES
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    model = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        runner_id=uuid4(),
        api_endpoint="http://localhost:11434/v1",
    )
    with pytest.raises(ModelGatewayAPIError) as error:
        RunnerModelStream(model, {"messages": "x" * MAX_REQUEST_BYTES})
    assert error.value.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"status": True},
        {"status": "200"},
        {"data": {}},
        {"done": "false"},
        {"error": {}},
        {"error": "x" * 129},
    ],
)
async def test_malformed_runner_frames_fail_closed(fields: dict[str, object]) -> None:
    import asyncio
    from preloop.services.runner_model_proxy import RunnerModelConnection

    connection = RunnerModelConnection(MagicMock(), str(uuid4()), str(uuid4()))
    future = asyncio.get_running_loop().create_future()
    connection.pending["request-1"] = future
    connection.response({"request_id": "request-1", **fields})
    assert (await future)["status"] == 502
    assert (await future)["error"] == "runner_proxy_error"


def test_invalid_upstream_json_closes_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock
    from preloop.services.runner_model_proxy import RunnerModelStream
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    model = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        runner_id=uuid4(),
        api_endpoint="http://localhost:11434/v1",
    )
    stream = RunnerModelStream(model, {"model": "example", "stream": True})
    monkeypatch.setattr(
        stream, "_rpc", AsyncMock(return_value={"data": "data: invalid JSON"})
    )
    with pytest.raises(ModelGatewayAPIError) as error:
        next(stream)
    assert error.value.status_code == 502
    assert stream.closed and not stream.thread.is_alive()


def test_request_start_rechecks_socket_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.models.crud.flow_runner import crud_flow_runner
    from preloop.models.db import session
    from preloop.services.runner_model_proxy import RunnerModelConnection

    connection = RunnerModelConnection(
        MagicMock(),
        str(uuid4()),
        str(uuid4()),
        credential_hash="old-hash",
        connection_id="connection-1",
    )
    monkeypatch.setattr(session, "get_db_session", lambda: iter([MagicMock()]))
    monkeypatch.setattr(
        crud_flow_runner,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            token_hash="new-hash",
            publication_capabilities={"connection_id": "connection-1"},
        ),
    )
    with pytest.raises(ConnectionError, match="no longer current"):
        connection._authorized({})
