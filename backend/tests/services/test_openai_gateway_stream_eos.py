"""Client-visible EOS must not wait on gateway usage recording.

Yielding ``message_stop`` / ``[DONE]`` before ``_record_gateway_request`` is
not enough: Starlette still pulls the generator again before sending
``more_body=False``, and that pull used to run recording. These tests pin
that the terminal event is yielded with recording only stashed, and that
``flush_deferred_stream_record`` (the ASGI complete hook) writes the row.
Disconnect at that yield still bills as a completed 200 with captured
usage rather than 499/partial.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

from preloop.services.gateway_streaming import GatewayStreamingResponse
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
import preloop.services.openai_gateway as openai_gateway
from preloop.services.openai_gateway import (
    _GATEWAY_STARTED_EMIT_MAX_PENDING,
    OpenAIGatewayService,
    _anthropic_passthrough_http_client,
    _close_anthropic_passthrough_http_client,
    _emit_account_event_nonblocking,
)


def _parse_sse_payload(event: str):
    data_line = next(line for line in event.splitlines() if line.startswith("data: "))
    payload = data_line.removeprefix("data: ")
    if payload == "[DONE]":
        return payload
    return json.loads(payload)


def _service() -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context)


def _openai_model() -> SimpleNamespace:
    return SimpleNamespace(id="model-1", provider_name="openai")


def _chat_upstream() -> list[dict]:
    return [
        {"choices": [{"delta": {"content": "Hello"}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 1,
                "total_tokens": 4,
            },
        },
    ]


def _responses_upstream() -> list[dict]:
    return [
        {"choices": [{"delta": {"content": "Hello"}}]},
        {
            "choices": [{"delta": {"content": " world"}}],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        },
    ]


def _anthropic_upstream() -> list[dict]:
    return [
        {
            "id": "msg_123",
            "choices": [{"index": 0, "delta": {"content": "Hello"}}],
        },
        {
            "id": "msg_123",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "total_tokens": 6,
            },
        },
    ]


@contextmanager
def _stream_patches(
    service: OpenAIGatewayService, upstream: list[dict]
) -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                service, "_resolve_requested_model", return_value=_openai_model()
            )
        )
        stack.enter_context(patch.object(service, "_check_budget", return_value=None))
        stack.enter_context(
            patch.object(service, "_call_litellm", return_value=iter(upstream))
        )
        stack.enter_context(patch.object(service, "_emit_gateway_request_started"))
        stack.enter_context(
            patch.object(
                service, "_anthropic_oauth_passthrough_token", return_value=None
            )
        )
        yield


def test_chat_stream_emits_done_before_recording():
    """``[DONE]`` is yielded with recording only stashed, not run."""
    service = _service()
    order: list[str] = []

    def _record(*_args, **_kwargs) -> None:
        order.append("record")

    with (
        _stream_patches(service, _chat_upstream()),
        patch.object(service, "_record_gateway_request", side_effect=_record),
    ):
        stream = service.stream_chat_completion(
            {
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
        )
        for event in stream:
            if _parse_sse_payload(event) == "[DONE]":
                order.append("done")
                break
        remainder = list(stream)
        assert order == ["done"]
        assert remainder == []
        service.flush_deferred_stream_record()

    assert order == ["done", "record"]


def test_chat_stream_emits_done_even_if_recording_is_slow():
    """A slow record must not delay the client-visible ``[DONE]``."""
    service = _service()
    recorded: list[str] = []

    def _slow_record(*_args, **_kwargs) -> None:
        time.sleep(0.15)
        recorded.append("record")

    with (
        _stream_patches(service, _chat_upstream()),
        patch.object(service, "_record_gateway_request", side_effect=_slow_record),
    ):
        stream = service.stream_chat_completion(
            {
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
        )
        started = time.perf_counter()
        saw_done = False
        elapsed = 0.0
        for event in stream:
            if _parse_sse_payload(event) == "[DONE]":
                saw_done = True
                elapsed = time.perf_counter() - started
                break
        remainder = list(stream)
        assert saw_done
        assert elapsed < 0.1
        assert recorded == []
        assert remainder == []
        service.flush_deferred_stream_record()

    assert recorded == ["record"]


def test_chat_stream_disconnect_after_done_still_records():
    """GeneratorExit at the terminal yield records 200 with captured usage."""
    service = _service()

    with (
        _stream_patches(service, _chat_upstream()),
        patch.object(service, "_record_gateway_request") as mock_record,
        patch.object(
            service, "_record_stream_abort", wraps=service._record_stream_abort
        ) as mock_abort,
    ):
        stream = service.stream_chat_completion(
            {
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
        )
        for event in stream:
            if _parse_sse_payload(event) == "[DONE]":
                stream.close()
                break

    mock_abort.assert_not_called()
    assert mock_record.call_args.kwargs["status_code"] == 200
    assert mock_record.call_args.kwargs.get("usage_source") is None
    assert mock_record.call_args.kwargs.get("error_class") is None
    usage = mock_record.call_args.kwargs["upstream_response"]["usage"]
    assert usage["prompt_tokens"] == 3
    assert usage["completion_tokens"] == 1


def test_chat_stream_disconnect_before_done_records_abort():
    """A mid-stream disconnect still records 499/partial."""
    service = _service()

    with (
        _stream_patches(service, _chat_upstream()),
        patch.object(service, "_record_gateway_request") as mock_record,
        patch.object(
            service, "_record_stream_abort", wraps=service._record_stream_abort
        ) as mock_abort,
    ):
        stream = service.stream_chat_completion(
            {
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
        )
        next(stream)
        stream.close()

    mock_abort.assert_called_once()
    assert mock_abort.call_args.kwargs["stream_completed"] is False
    assert mock_record.call_args.kwargs["status_code"] == 499
    assert (
        mock_record.call_args.kwargs["error_detail"]
        == "client disconnected before stream completion"
    )


def test_responses_stream_emits_done_before_recording():
    service = _service()
    order: list[str] = []

    def _record(*_args, **_kwargs) -> None:
        order.append("record")

    with (
        _stream_patches(service, _responses_upstream()),
        patch.object(service, "_record_gateway_request", side_effect=_record),
    ):
        stream = service.stream_response(
            {"model": "openai/gpt-5", "input": "Hi", "stream": True}
        )
        for event in stream:
            if _parse_sse_payload(event) == "[DONE]":
                order.append("done")
                break
        remainder = list(stream)
        assert order == ["done"]
        assert remainder == []
        service.flush_deferred_stream_record()

    assert order == ["done", "record"]


def test_anthropic_stream_emits_message_stop_before_recording():
    service = _service()
    order: list[str] = []

    def _record(*_args, **_kwargs) -> None:
        order.append("record")

    with (
        _stream_patches(service, _anthropic_upstream()),
        patch.object(service, "_record_gateway_request", side_effect=_record),
    ):
        stream = service.stream_message(
            {
                "model": "anthropic/claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 64,
                "stream": True,
            }
        )
        for event in stream:
            if "event: message_stop" in event:
                order.append("message_stop")
                break
        remainder = list(stream)
        assert order == ["message_stop"]
        assert remainder == []
        service.flush_deferred_stream_record()

    assert order == ["message_stop", "record"]


def test_anthropic_stream_disconnect_after_stop_still_records():
    service = _service()

    with (
        _stream_patches(service, _anthropic_upstream()),
        patch.object(service, "_record_gateway_request") as mock_record,
        patch.object(
            service, "_record_stream_abort", wraps=service._record_stream_abort
        ) as mock_abort,
    ):
        stream = service.stream_message(
            {
                "model": "anthropic/claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 64,
                "stream": True,
            }
        )
        for event in stream:
            if "event: message_stop" in event:
                stream.close()
                break

    mock_abort.assert_not_called()
    assert mock_record.call_args.kwargs["status_code"] == 200
    assert mock_record.call_args.kwargs.get("usage_source") is None
    assert mock_record.call_args.kwargs.get("error_class") is None


def test_anthropic_passthrough_http_client_is_reused():
    """OAuth passthrough must reuse one process-level httpx client."""
    _close_anthropic_passthrough_http_client()
    first = _anthropic_passthrough_http_client()
    second = _anthropic_passthrough_http_client()
    try:
        assert first is second
        assert not first.is_closed
    finally:
        _close_anthropic_passthrough_http_client()


def test_oauth_passthrough_complete_posts_via_shared_client():
    """Non-streaming OAuth passthrough must use the process-level httpx client."""
    service = _service()
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.headers = {}
    upstream.json.return_value = {"id": "msg_1", "type": "message"}
    client = MagicMock()
    client.post.return_value = upstream

    with patch(
        "preloop.services.openai_gateway._anthropic_passthrough_http_client",
        return_value=client,
    ):
        payload = service._anthropic_oauth_passthrough_complete(
            url="https://api.anthropic.com/v1/messages",
            headers={"authorization": "Bearer token"},
            body={"model": "claude-sonnet-4-5", "max_tokens": 16},
        )

    client.post.assert_called_once()
    assert client.post.call_args.args[0] == "https://api.anthropic.com/v1/messages"
    assert payload == {"id": "msg_1", "type": "message"}
    upstream.close.assert_called_once()


def test_started_emit_drops_when_queue_is_full():
    """Started-event telemetry must not queue unbounded work."""
    with (
        patch(
            "preloop.services.openai_gateway.asyncio.get_running_loop",
            side_effect=RuntimeError,
        ),
        patch.object(
            openai_gateway,
            "_GATEWAY_STARTED_EMIT_PENDING",
            _GATEWAY_STARTED_EMIT_MAX_PENDING,
        ),
        patch.object(
            openai_gateway._GATEWAY_STARTED_EMIT_EXECUTOR, "submit"
        ) as mock_submit,
        patch.object(openai_gateway, "emit_account_event") as mock_emit,
    ):
        _emit_account_event_nonblocking({"type": "test"})

    mock_submit.assert_not_called()
    mock_emit.assert_not_called()


def test_gateway_streaming_response_records_after_body_flush() -> None:
    """ASGI ``more_body=False`` is sent before deferred usage recording."""
    order: list[str] = []

    def _gen() -> Iterator[str]:
        yield "data: hi\n\n"
        yield "data: [DONE]\n\n"
        order.append("generator_exhausted")

    def _on_complete() -> None:
        order.append("record")

    response = GatewayStreamingResponse(
        _gen(),
        media_type="text/event-stream",
        on_complete=_on_complete,
    )
    sent: list[dict[str, Any]] = []

    async def _send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message.get("type") == "http.response.body" and not message.get(
            "more_body", True
        ):
            order.append("body_flush")

    asyncio.run(response.stream_response(_send))

    assert order == ["generator_exhausted", "body_flush", "record"]
    assert sent[-1]["more_body"] is False
