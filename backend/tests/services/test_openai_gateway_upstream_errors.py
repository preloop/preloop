"""Gateway integration of upstream error classification (#116/#117/#118/#114)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.upstream_errors import (
    ERROR_CLASS_NETWORK,
    ERROR_CLASS_UPSTREAM_DISCONNECT,
    ERROR_CLASS_UPSTREAM_OVERLOADED,
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
    ERROR_CLASS_UPSTREAM_RATE_LIMITED,
)


class _APIConnectionError(Exception):
    """Name-matched stand-in for litellm.exceptions.APIConnectionError."""


class _MidStreamFallbackError(Exception):
    """Name-matched stand-in for litellm.MidStreamFallbackError."""


class _FakeHTTPError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_type: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        if retry_after is not None:
            self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


def _service() -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context)


def test_normalize_connection_refused_returns_503_network():
    """#116: connection refused becomes 503 with a clear upstream message."""
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai",
        _APIConnectionError("Connection error. [Errno 111] Connection refused"),
    )
    assert isinstance(err, ModelGatewayAPIError)
    assert err.status_code == 503
    assert err.error_class == ERROR_CLASS_NETWORK
    assert "Upstream model provider unavailable" in err.message
    assert err.response_headers().get("X-Preloop-Error-Class") == ERROR_CLASS_NETWORK


def test_normalize_overloaded_502_classified():
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai",
        _FakeHTTPError(
            "Our servers are currently overloaded. Please try again later.",
            status_code=502,
        ),
    )
    assert err.status_code == 502
    assert err.error_class == ERROR_CLASS_UPSTREAM_OVERLOADED


def test_normalize_quota_exhausted_is_terminal_with_retry_after():
    """#114 gateway half: quota-exhausted 429 carries terminal + Retry-After."""
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai",
        _FakeHTTPError(
            "request reached organization TPD rate limit, "
            "current: 1518025, limit: 1500000",
            status_code=429,
            error_type="rate_limit_reached_error",
            retry_after=600,
        ),
    )
    assert err.status_code == 429
    assert err.error_class == ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED
    assert err.terminal is True
    assert err.retry_after_seconds == 600
    headers = err.response_headers()
    assert headers["Retry-After"] == "600"
    assert headers["X-Preloop-Retry-Terminal"] == "true"
    assert err.code == "insufficient_quota"


def test_normalize_transient_429_not_terminal():
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai",
        _FakeHTTPError(
            "engine_overloaded_error: The engine is currently overloaded",
            status_code=429,
            error_type="engine_overloaded_error",
        ),
    )
    assert err.error_class == ERROR_CLASS_UPSTREAM_RATE_LIMITED
    assert err.terminal is False
    assert "X-Preloop-Retry-Terminal" not in err.response_headers()


def test_stream_error_remaps_network_to_upstream_disconnect():
    """#117: mid-stream transport failures emit upstream_disconnect."""
    service = _service()
    err = service._stream_error(
        "openai",
        _APIConnectionError("peer closed connection without sending complete message"),
    )
    assert err.error_class == ERROR_CLASS_UPSTREAM_DISCONNECT
    assert err.error_type == "upstream_disconnect"
    assert err.status_code == 502


def test_stream_error_midstream_fallback_is_upstream_disconnect():
    service = _service()
    err = service._stream_error(
        "openai",
        _MidStreamFallbackError("litellm.APIConnectionError: incomplete chunked read"),
    )
    assert err.error_class == ERROR_CLASS_UPSTREAM_DISCONNECT
    payload = err.to_payload()
    assert payload["error"]["type"] == "upstream_disconnect"


def test_openai_stream_error_event_payload_shape():
    service = _service()
    frame = service._openai_stream_error_event(
        _MidStreamFallbackError("peer closed connection")
    )
    assert frame.startswith("data: ")
    assert "upstream_disconnect" in frame
