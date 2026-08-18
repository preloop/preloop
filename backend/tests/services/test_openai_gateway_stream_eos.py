"""Client-visible EOS must not wait on gateway usage recording.

The stream generators used to call ``_record_gateway_request`` (DB +
activity bookkeeping) *before* yielding ``message_stop`` / ``[DONE]``.
That held the last SSE event for the duration of recording. These tests
pin the new order: the terminal event is yielded first, then recording
runs on the subsequent pull. Disconnect at that yield still bills via
the ``finally`` abort path.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock, patch

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import (
    OpenAIGatewayService,
    _anthropic_passthrough_http_client,
    _close_anthropic_passthrough_http_client,
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
    """``[DONE]`` is yielded before ``_record_gateway_request`` runs."""
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
        try:
            next(stream)
        except StopIteration:
            pass

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
        for event in stream:
            if _parse_sse_payload(event) == "[DONE]":
                saw_done = True
                elapsed = time.perf_counter() - started
                break
        remainder = list(stream)

    assert saw_done
    assert elapsed < 0.1
    assert recorded == ["record"]
    assert remainder == []


def test_chat_stream_disconnect_after_done_still_records():
    """GeneratorExit at the terminal yield still bills via the abort path."""
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

    mock_abort.assert_called_once()
    assert mock_record.call_args.kwargs["status_code"] == 499


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
        try:
            next(stream)
        except StopIteration:
            pass

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
        try:
            next(stream)
        except StopIteration:
            pass

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

    mock_abort.assert_called_once()
    assert mock_record.call_args.kwargs["status_code"] == 499


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
