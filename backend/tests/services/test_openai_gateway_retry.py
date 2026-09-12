"""Bounded gateway retries for transient 502 / mid-stream disconnect."""

from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import (
    OpenAIGatewayService,
    _UPSTREAM_RETRY_AFTER_CAP_SECONDS,
    _upstream_retry_delay_seconds,
)


class _MidStreamFallbackError(Exception):
    """Name-matched stand-in for litellm.exceptions.MidStreamFallbackError."""

    def __init__(self, message: str, *, is_pre_first_chunk: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = 502
        self.error_type = "provider_unavailable"
        self.is_pre_first_chunk = is_pre_first_chunk
        self.generated_content = ""


class _UnsupportedParamsError(Exception):
    def __init__(self) -> None:
        message = "zai does not support parameters: ['parallel_tool_calls']"
        super().__init__(message)
        self.message = message
        self.status_code = 400


class _TransientRateLimitError(Exception):
    """Non-terminal 429 with a provider Retry-After header."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.message = "rate limited"
        self.status_code = 429
        self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


_FOUNDER_502 = (
    "Upstream provider disconnected mid-stream: APIError: OpenrouterException "
    "- Message: JSON error injected into SSE stream, "
    "Metadata: {'error_type': 'provider_unavailable'}"
)


def _service_and_model():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    upstream_backend = MagicMock()
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=upstream_backend
    )
    ai_model = SimpleNamespace(
        provider_name="openai",
        model_identifier="gpt-5",
        api_endpoint=None,
        meta_data={},
    )
    return service, ai_model, upstream_backend


def _call(service, ai_model):
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="sk-test")
        )
        return service._call_litellm(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload={},
            provider="openai",
        )


def _open_stream(service, ai_model):
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="sk-test")
        )
        return service._open_upstream_stream(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload={},
            provider="openai",
        )


def test_call_litellm_retries_midstream_502_then_succeeds():
    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = [
        _MidStreamFallbackError(_FOUNDER_502),
        {"ok": True},
    ]
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry"):
        result = _call(service, ai_model)
    assert result == {"ok": True}
    assert backend.completion.call_count == 2


def test_call_litellm_does_not_retry_unsupported_params_400():
    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = _UnsupportedParamsError()
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep:
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            _call(service, ai_model)
    assert exc_info.value.status_code == 400
    assert backend.completion.call_count == 1
    sleep.assert_not_called()


def test_call_litellm_exhausts_retries_on_persistent_502():
    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = _MidStreamFallbackError(_FOUNDER_502)
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry"):
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            _call(service, ai_model)
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_class == "upstream_disconnect"
    assert backend.completion.call_count == 3


def test_open_upstream_stream_does_not_retry_generic_first_chunk_error():
    """Opaque first-chunk failures stay 502; retrying them can become HTTP 200."""
    service, ai_model, backend = _service_and_model()

    def _failing_stream():
        raise Exception("upstream rejected the tools payload")
        yield  # pragma: no cover

    backend.completion.return_value = _failing_stream()
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep:
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            _open_stream(service, ai_model)
    assert exc_info.value.status_code == 502
    assert backend.completion.call_count == 1
    sleep.assert_not_called()


def test_open_upstream_stream_retries_first_chunk_disconnect():
    service, ai_model, backend = _service_and_model()

    class _FailThenOk:
        def __init__(self) -> None:
            self.calls = 0

        def __iter__(self):
            self.calls += 1
            if self.calls == 1:
                raise _MidStreamFallbackError(_FOUNDER_502)
            return iter([{"delta": "hi"}])

    stream = _FailThenOk()
    backend.completion.return_value = stream
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry"):
        prefetched = _open_stream(service, ai_model)
    assert list(prefetched) == [{"delta": "hi"}]
    assert backend.completion.call_count == 2


def test_retry_delay_honors_capped_retry_after():
    """Backoff is raised to Retry-After, but a large hint stays bounded."""
    with patch("preloop.services.openai_gateway.random.uniform", return_value=0.0):
        assert _upstream_retry_delay_seconds(0) == 0.2
        assert _upstream_retry_delay_seconds(0, retry_after_seconds=5) == 5.0
        assert (
            _upstream_retry_delay_seconds(0, retry_after_seconds=60)
            == _UPSTREAM_RETRY_AFTER_CAP_SECONDS
        )
        assert _upstream_retry_delay_seconds(1, retry_after_seconds=0) == 0.4


def test_call_litellm_honors_retry_after_on_transient_429():
    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = [
        _TransientRateLimitError(5),
        {"ok": True},
    ]
    with (
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep,
        patch("preloop.services.openai_gateway.random.uniform", return_value=0.0),
    ):
        result = _call(service, ai_model)
    assert result == {"ok": True}
    assert backend.completion.call_count == 2
    sleep.assert_called_once_with(5.0)


def test_call_litellm_caps_large_retry_after_on_transient_429():
    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = [
        _TransientRateLimitError(600),
        {"ok": True},
    ]
    with (
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep,
        patch("preloop.services.openai_gateway.random.uniform", return_value=0.0),
    ):
        result = _call(service, ai_model)
    assert result == {"ok": True}
    sleep.assert_called_once_with(_UPSTREAM_RETRY_AFTER_CAP_SECONDS)


def test_retry_count_is_zero_when_the_call_works_first_time():
    service, ai_model, backend = _service_and_model()
    backend.completion.return_value = {"ok": True}
    _call(service, ai_model)
    assert service._last_upstream_retry_count == 0


def test_retry_count_is_recorded_for_a_rescued_call():
    """A run rescued from a flaky provider must be visible, not just slow."""
    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = [
        _MidStreamFallbackError(_FOUNDER_502),
        {"ok": True},
    ]
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry"):
        _call(service, ai_model)
    assert service._last_upstream_retry_count == 1


def test_retry_count_is_recorded_when_retries_are_exhausted():
    """The failing case is exactly where the retry count matters most."""
    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = _MidStreamFallbackError(_FOUNDER_502)
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry"):
        with pytest.raises(ModelGatewayAPIError):
            _call(service, ai_model)
    assert service._last_upstream_retry_count == 2


def test_retry_count_is_not_charged_to_a_later_request():
    """Consume-and-clear: a stale count must not leak onto the next call."""
    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = [
        _MidStreamFallbackError(_FOUNDER_502),
        {"ok": True},
    ]
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry"):
        _call(service, ai_model)
    assert service._last_upstream_retry_count == 1

    # Simulate the usage row being written (which consumes the counter).
    service._last_upstream_retry_count = 0

    backend.completion.side_effect = None
    backend.completion.return_value = {"ok": True}
    _call(service, ai_model)
    assert service._last_upstream_retry_count == 0


def test_stale_retry_count_is_cleared_when_a_request_starts():
    """A request that died before its usage row must not tax the next one.

    ``_record_gateway_request`` consumes and clears the count, but a terminal
    upstream error can escape the handler before the row is written. The next
    request re-arms the counter itself, so the leak cannot survive it.
    """
    service, _ai_model, _backend = _service_and_model()
    service._last_upstream_retry_count = 3  # previous request never recorded

    with pytest.raises(ModelGatewayAPIError):
        # Rejected right after the request starts; no upstream call at all.
        service.create_chat_completion({"model": "gpt-5", "stream": True})

    assert service._last_upstream_retry_count == 0


def test_stale_retry_count_is_cleared_when_a_stream_request_starts():
    """Same for the streaming entry point."""
    service, _ai_model, _backend = _service_and_model()
    service._last_upstream_retry_count = 3

    with pytest.raises(ModelGatewayAPIError):
        # Fails while resolving the model, still after the reset.
        service.stream_chat_completion({"model": "gpt-5", "messages": []})

    assert service._last_upstream_retry_count == 0


def test_every_request_entry_point_rearms_the_retry_count():
    """A new entry point that forgets the reset re-opens the leak."""
    import inspect

    for name in (
        "create_chat_completion",
        "create_response",
        "create_message",
        "stream_chat_completion",
        "stream_response",
        "stream_message",
    ):
        source = inspect.getsource(getattr(OpenAIGatewayService, name))
        assert "_begin_request_accounting()" in source, name


def test_attempt_budget_is_configurable(monkeypatch):
    """Operators can widen or disable the retry budget without a deploy."""
    service, ai_model, backend = _service_and_model()
    monkeypatch.setattr(
        "preloop.services.openai_gateway.settings."
        "model_gateway_upstream_retry_max_attempts",
        1,
        raising=False,
    )
    backend.completion.side_effect = _MidStreamFallbackError(_FOUNDER_502)
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep:
        with pytest.raises(ModelGatewayAPIError):
            _call(service, ai_model)
    assert backend.completion.call_count == 1
    sleep.assert_not_called()


def test_backoff_base_is_configurable(monkeypatch):
    monkeypatch.setattr(
        "preloop.services.openai_gateway.settings."
        "model_gateway_upstream_retry_base_seconds",
        1.0,
        raising=False,
    )
    with patch("preloop.services.openai_gateway.random.uniform", return_value=0.0):
        assert _upstream_retry_delay_seconds(0) == 1.0
        assert _upstream_retry_delay_seconds(1) == 2.0


def test_retry_after_cap_is_configurable(monkeypatch):
    monkeypatch.setattr(
        "preloop.services.openai_gateway.settings."
        "model_gateway_upstream_retry_after_cap_seconds",
        2.0,
        raising=False,
    )
    with patch("preloop.services.openai_gateway.random.uniform", return_value=0.0):
        assert _upstream_retry_delay_seconds(0, retry_after_seconds=600) == 2.0


@pytest.mark.parametrize("fail_on_iter", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
def test_prefetch_failure_closes_stream_before_retry(
    fail_on_iter: bool, wrapped: bool
) -> None:
    service, ai_model, backend = _service_and_model()
    closed = []

    class FailedStream:
        def __iter__(self) -> Iterator[Any]:
            if fail_on_iter:
                raise _MidStreamFallbackError(_FOUNDER_502)
            return self

        def __next__(self) -> Any:
            raise _MidStreamFallbackError(_FOUNDER_502)

        def close(self) -> None:
            closed.append(True)

    failed = FailedStream()
    if wrapped:

        class Wrapper:
            completion_stream = failed

            def __iter__(self) -> Iterator[Any]:
                return iter(self.completion_stream)

        failed = Wrapper()

    def completion(**kwargs: Any) -> Any:
        if backend.completion.call_count == 1:
            return failed
        assert closed == [True], "failed connection must close before the retry"
        return iter([{"delta": "ok"}])

    backend.completion.side_effect = completion
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry"):
        stream = _open_stream(service, ai_model)
    assert list(stream) == [{"delta": "ok"}]
    assert backend.completion.call_count == 2


def test_cleanup_failure_preserves_original_upstream_error() -> None:
    service, ai_model, backend = _service_and_model()
    stream = MagicMock()
    stream.__iter__.side_effect = _UnsupportedParamsError()
    stream.close.side_effect = RuntimeError("cleanup failed")
    backend.completion.return_value = stream
    with pytest.raises(ModelGatewayAPIError) as exc_info:
        _open_stream(service, ai_model)
    assert exc_info.value.status_code == 400
    stream.close.assert_called_once()
    assert backend.completion.call_count == 1


@pytest.mark.parametrize(
    "first_chunk",
    [
        {"delta": "already generated"},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"path":'}}
                        ]
                    }
                }
            ]
        },
    ],
)
def test_disconnect_after_prefetched_chunk_never_retries(
    first_chunk: dict[str, Any],
) -> None:
    service, ai_model, backend = _service_and_model()

    def partial_stream() -> Iterator[dict[str, Any]]:
        yield first_chunk
        raise _MidStreamFallbackError(_FOUNDER_502, is_pre_first_chunk=False)

    backend.completion.return_value = partial_stream()
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep:
        stream = _open_stream(service, ai_model)
        assert next(stream) == first_chunk
        with pytest.raises(_MidStreamFallbackError):
            next(stream)
    assert backend.completion.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("status", [400, 401, 403, 422, 500, 503])
def test_gateway_owns_actual_sdk_retry_budget(status: int) -> None:
    """Exercise LiteLLM and the real OpenAI SDK against an offline transport."""
    import httpx
    from litellm.llms.openai.openai import OpenAIChatCompletion

    from preloop.services.openai_gateway import LiteLLMModelGatewayBackend

    service, ai_model, _ = _service_and_model()
    service.upstream_backend = LiteLLMModelGatewayBackend()
    ai_model.api_endpoint = "https://retry-budget.example.com/v1"
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "provider failure"}})

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch.object(
            OpenAIChatCompletion, "_get_sync_http_client", return_value=client
        ),
        patch.object(
            OpenAIChatCompletion, "get_cached_openai_client", return_value=None
        ),
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry"),
        patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert"),
    ):
        with pytest.raises(ModelGatewayAPIError):
            _call(service, ai_model)
    assert len(calls) == (3 if status >= 500 else 1)


@pytest.mark.parametrize("stream", [False, True])
def test_native_responses_retains_retry_after_until_success(stream: bool) -> None:
    import httpx

    service, ai_model, _ = _service_and_model()
    failed = httpx.Response(
        500,
        headers={"retry-after": "3"},
        json={"error": {"message": "Internal server error", "type": "api_error"}},
    )
    success = httpx.Response(200, json={"id": "resp_test", "output": []})
    client = MagicMock()
    (client.send if stream else client.post).side_effect = [failed, success]
    with (
        patch.object(
            service,
            "_prepare_openai_responses_passthrough",
            return_value=("https://example.com/responses", {}, {}),
        ),
        patch(
            "preloop.services.openai_gateway._openai_passthrough_http_client",
            return_value=client,
        ),
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep,
        patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert") as alert,
    ):
        operation = (
            service._open_openai_responses_passthrough_stream
            if stream
            else service._create_openai_responses_passthrough
        )
        result = operation(ai_model, {})
    assert result.raw is success if stream else result == success.json()
    sleep.assert_called_once_with(3.0)
    alert.assert_not_called()
    assert service._last_upstream_retry_count == 1
    assert failed.is_closed


@pytest.mark.parametrize(
    "message",
    [
        "This model does not support chat completions; use Responses",
        "unsupported protocol",
        "unsupported endpoint",
        "invalid_request_error",
    ],
)
def test_explicit_capability_failure_does_not_retry_even_on_500(message: str) -> None:
    import httpx
    from openai import InternalServerError

    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = InternalServerError(
        message,
        response=httpx.Response(
            500, request=httpx.Request("POST", "https://example.com")
        ),
        body=None,
    )
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep:
        with pytest.raises(ModelGatewayAPIError) as failure:
            _call(service, ai_model)
    assert backend.completion.call_count == 1
    sleep.assert_not_called()
    if "invalid_request" not in message:
        assert failure.value.error_class == "upstream_protocol"
        assert failure.value.status_code == 400
        assert failure.value.response_headers()["X-Preloop-Retry-Terminal"] == "true"


@pytest.mark.parametrize("stream", [False, True])
def test_native_responses_network_failure_recovers(stream: bool) -> None:
    import httpx

    service, ai_model, _ = _service_and_model()
    client = MagicMock()
    success = httpx.Response(200, json={"id": "resp_test", "output": []})
    operation = client.send if stream else client.post
    operation.side_effect = [httpx.ConnectError("disconnected"), success]
    with (
        patch.object(
            service,
            "_prepare_openai_responses_passthrough",
            return_value=("https://example.com/responses", {}, {}),
        ),
        patch(
            "preloop.services.openai_gateway._openai_passthrough_http_client",
            return_value=client,
        ),
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry"),
    ):
        method = (
            service._open_openai_responses_passthrough_stream
            if stream
            else service._create_openai_responses_passthrough
        )
        method(ai_model, {})
    assert operation.call_count == 2
    assert service._last_upstream_retry_count == 1


def test_real_sentry_openai_integration_filters_only_owned_failures() -> None:
    """The automatic SDK capture occurs before LiteLLM/gateway exception handlers."""
    import httpx
    import sentry_sdk
    from litellm.llms.openai.openai import OpenAIChatCompletion
    from openai import InternalServerError, OpenAI
    from sentry_sdk.integrations.openai import OpenAIIntegration

    from preloop.services.openai_gateway import LiteLLMModelGatewayBackend
    from preloop.utils.sentry_filters import sentry_before_send, gateway_upstream_call

    captures = []
    delivered = []

    def before_send(event: dict[str, Any], hint: dict[str, Any]) -> Any:
        result = sentry_before_send(event, hint)
        captures.append((event, result))
        return result

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "provider failure"}})

    service, ai_model, _ = _service_and_model()
    service.upstream_backend = LiteLLMModelGatewayBackend()
    ai_model.api_endpoint = "https://sentry-budget.example.com/v1"
    with (
        sentry_sdk.init(
            dsn="https://public@example.com/1",
            default_integrations=False,
            integrations=[OpenAIIntegration()],
            before_send=before_send,
            transport=lambda event: delivered.append(event),
        ),
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch.object(
            OpenAIChatCompletion, "_get_sync_http_client", return_value=client
        ),
        patch.object(
            OpenAIChatCompletion, "get_cached_openai_client", return_value=None
        ),
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry"),
        patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert"),
    ):
        with pytest.raises(ModelGatewayAPIError) as failure:
            _call(service, ai_model)
        assert failure.value.error_class == "upstream_error"
        assert service._last_upstream_retry_count == 2
        assert len(captures) == 3
        assert all(result is None for _, result in captures)
        assert delivered == []

        # The same SDK exception outside the owned call remains visible.
        sdk = OpenAI(
            api_key="sk-test",
            base_url=ai_model.api_endpoint,
            http_client=client,
            max_retries=0,
        )
        with pytest.raises(InternalServerError):
            sdk.chat.completions.create(
                model="gpt-5", messages=[{"role": "user", "content": "hi"}]
            )
        assert captures[-1][1] is not None
        assert len(delivered) == 1

        # A local application failure in the context must still be captured.
        with gateway_upstream_call():
            sentry_sdk.capture_exception(RuntimeError("local bug"))
        assert len(delivered) == 2


@pytest.mark.parametrize("stream", [False, True])
def test_native_responses_final_failure_alerts_once_with_model(stream: bool) -> None:
    import httpx

    service, ai_model, _ = _service_and_model()
    client = MagicMock()
    responses = [
        httpx.Response(500, json={"error": {"message": "provider failure"}})
        for _ in range(3)
    ]
    operation = client.send if stream else client.post
    operation.side_effect = responses
    with (
        patch.object(
            service,
            "_prepare_openai_responses_passthrough",
            return_value=("https://example.com/responses", {}, {}),
        ),
        patch(
            "preloop.services.openai_gateway._openai_passthrough_http_client",
            return_value=client,
        ),
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry"),
        patch(
            "preloop.services.openai_gateway.reserve_gateway_5xx_alert",
            return_value=(True, 0),
        ) as reserve,
        patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert") as alert,
    ):
        method = (
            service._open_openai_responses_passthrough_stream
            if stream
            else service._create_openai_responses_passthrough
        )
        with pytest.raises(ModelGatewayAPIError) as failure:
            method(ai_model, {})
    assert operation.call_count == 3
    assert failure.value.status_code == 502
    assert failure.value.error_class == "upstream_error"
    assert service._last_upstream_retry_count == 2
    assert all(response.is_closed for response in responses)
    reserve.assert_called_once()
    alert.assert_called_once()
    assert "Upstream model: gpt-5" in alert.call_args.kwargs["message"]


def test_gateway_cancellation_is_not_retried_or_normalized() -> None:
    import asyncio

    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = asyncio.CancelledError()
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep:
        with pytest.raises(asyncio.CancelledError):
            _call(service, ai_model)
    backend.completion.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "failure_type", ["ConnectError", "ReadError", "ReadTimeout", "RemoteProtocolError"]
)
def test_transport_failure_recovers_before_output(failure_type: str) -> None:
    import httpx

    service, ai_model, backend = _service_and_model()
    backend.completion.side_effect = [
        getattr(httpx, failure_type)("provider disconnected"),
        {"ok": True},
    ]
    with patch("preloop.services.openai_gateway._sleep_before_upstream_retry"):
        assert _call(service, ai_model) == {"ok": True}
    assert backend.completion.call_count == 2
    assert service._last_upstream_retry_count == 1
