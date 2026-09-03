"""Bounded gateway retries for transient 502 / mid-stream disconnect."""

from types import SimpleNamespace
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
