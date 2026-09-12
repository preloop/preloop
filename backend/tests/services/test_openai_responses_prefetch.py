"""Native Responses must recover before the first body byte, without replay."""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock

import httpx
import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService


class _Body(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes | BaseException) -> None:
        self.chunks = chunks
        self.closed = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def native_stream(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="test-token",
            user=SimpleNamespace(id="user-test", account_id="account-test"),
        ),
    )
    state = SimpleNamespace(
        service=service,
        model=SimpleNamespace(
            provider_name="openai-compatible",
            model_identifier="test-model",
            meta_data={},
        ),
        bodies=[],
        requests=[],
        alert=MagicMock(),
        sleep=MagicMock(),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=state.bodies.pop(0),
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            service,
            "_prepare_openai_responses_passthrough",
            lambda *_args, **_kwargs: (
                "https://provider.example.com/responses",
                {},
                {},
            ),
        )
        monkeypatch.setattr(
            "preloop.services.openai_gateway._openai_passthrough_http_client",
            lambda _model: client,
        )
        monkeypatch.setattr(
            "preloop.services.openai_gateway._sleep_before_upstream_retry", state.sleep
        )
        monkeypatch.setattr(
            "preloop.services.openai_gateway.reserve_gateway_5xx_alert",
            lambda *_args, **_kwargs: (True, 0),
        )
        monkeypatch.setattr(
            "preloop.services.openai_gateway.enqueue_gateway_5xx_alert", state.alert
        )
        monkeypatch.setattr(
            service, "_observe_stream", lambda stream, **_kwargs: stream
        )
        monkeypatch.setattr(
            "preloop.services.openai_gateway.wrap_stream_for_response_policy",
            lambda stream, **_kwargs: stream,
        )
        for method in (
            "_defer_stream_record",
            "_finish_stream_generator",
            "_record_gateway_request",
        ):
            monkeypatch.setattr(service, method, MagicMock())
        yield state


def _open(state: SimpleNamespace) -> Any:
    return state.service._open_openai_responses_passthrough_stream(state.model, {})


def _relay(state: SimpleNamespace, response: Any) -> Iterator[str]:
    return state.service._openai_responses_passthrough_event_stream(
        response,
        ai_model=state.model,
        payload={"model": "test-model"},
        budget_result=None,
        started_at=time.perf_counter(),
    )


@pytest.mark.parametrize(
    "failure",
    [
        httpx.RemoteProtocolError("peer closed connection"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_native_first_body_failure_recovers(
    native_stream: SimpleNamespace, failure: Exception
) -> None:
    failed = _Body(failure)
    # Split both SSE frames and a UTF-8 character across transport reads.
    chunks = [
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"caf\xc3',
        b'\xa9"}\n',
        b'\nevent: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_test","usage":{"input_tokens":2,"output_tokens":1}}}\n\n',
    ]
    successful = _Body(*chunks)
    native_stream.bodies.extend([failed, successful])
    opened = _open(native_stream)
    assert len(native_stream.requests) == 2
    assert failed.closed == 1
    assert native_stream.service._last_upstream_retry_count == 1
    assert "".join(_relay(native_stream, opened)) == b"".join(chunks).decode()
    assert successful.closed == 1
    native_stream.alert.assert_not_called()


def test_native_first_body_exhaustion_is_one_final_failure(
    native_stream: SimpleNamespace,
) -> None:
    failed = [_Body(httpx.ReadError("connection reset")) for _ in range(3)]
    native_stream.bodies.extend(failed)
    with pytest.raises(ModelGatewayAPIError) as failure:
        _open(native_stream)
    assert len(native_stream.requests) == 3
    assert native_stream.service._last_upstream_retry_count == 2
    assert failure.value.error_class == "network"
    assert failure.value.status_code == 503
    assert all(body.closed == 1 for body in failed)
    native_stream.alert.assert_called_once()


@pytest.mark.parametrize(
    "prefix",
    [
        b": keepalive\n\n",
        b'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n',
        b'data: {"type":"response.function_call_arguments.delta","delta":"{"}\n\n',
    ],
)
def test_native_output_is_never_replayed(
    native_stream: SimpleNamespace, prefix: bytes
) -> None:
    body = _Body(prefix, httpx.RemoteProtocolError("peer closed connection"))
    native_stream.bodies.append(body)
    opened = _open(native_stream)
    emitted = list(_relay(native_stream, opened))
    assert emitted[0] == prefix.decode()
    assert "response.failed" in emitted[1] or '"error"' in emitted[1]
    assert emitted[-1] == "data: [DONE]\n\n"
    assert len(native_stream.requests) == 1
    assert body.closed == 1
    native_stream.sleep.assert_not_called()
    native_stream.alert.assert_not_called()


def test_native_empty_transport_chunks_do_not_end_prefetch(
    native_stream: SimpleNamespace,
) -> None:
    failed = _Body(b"", b"", httpx.ReadError("connection reset"))
    successful = _Body(b": keepalive\n\n")
    native_stream.bodies.extend([failed, successful])
    opened = _open(native_stream)
    assert len(native_stream.requests) == 2
    assert "".join(_relay(native_stream, opened)) == ": keepalive\n\n"


def test_native_first_read_cancellation_closes_without_retry(
    native_stream: SimpleNamespace,
) -> None:
    body = _Body(asyncio.CancelledError())
    native_stream.bodies.append(body)
    with pytest.raises(asyncio.CancelledError):
        _open(native_stream)
    assert len(native_stream.requests) == 1
    assert body.closed == 1
    native_stream.sleep.assert_not_called()
    native_stream.alert.assert_not_called()


def test_native_empty_body_is_not_an_empty_success(
    native_stream: SimpleNamespace,
) -> None:
    empty = [_Body() for _ in range(3)]
    native_stream.bodies.extend(empty)
    with pytest.raises(ModelGatewayAPIError) as failure:
        _open(native_stream)
    assert len(native_stream.requests) == 3
    assert failure.value.status_code == 503
    assert failure.value.error_class == "network"
    assert all(body.closed == 1 for body in empty)
    native_stream.alert.assert_called_once()


def test_native_prefetched_response_can_be_closed_without_consuming(
    native_stream: SimpleNamespace,
) -> None:
    body = _Body(b": keepalive\n\n", b"data: untouched\n\n")
    native_stream.bodies.append(body)
    opened = _open(native_stream)
    opened.close()
    assert body.closed == 1
    assert len(native_stream.requests) == 1


def test_native_first_read_cleanup_failure_does_not_replace_cancellation(
    native_stream: SimpleNamespace,
) -> None:
    class BadCleanup(_Body):
        def close(self) -> None:
            super().close()
            raise RuntimeError("cleanup failed")

    body = BadCleanup(asyncio.CancelledError())
    native_stream.bodies.append(body)
    with pytest.raises(asyncio.CancelledError):
        _open(native_stream)
    assert body.closed == 1
    assert len(native_stream.requests) == 1
    native_stream.sleep.assert_not_called()
