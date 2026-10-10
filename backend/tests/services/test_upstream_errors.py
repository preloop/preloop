"""Unit tests for shared upstream-provider error classification."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import pytest

from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.upstream_errors import (
    ERROR_CLASS_NETWORK,
    ERROR_CLASS_UPSTREAM_AUTH,
    ERROR_CLASS_UPSTREAM_DISCONNECT,
    ERROR_CLASS_UPSTREAM_ERROR,
    ERROR_CLASS_UPSTREAM_OVERLOADED,
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
    ERROR_CLASS_UPSTREAM_RATE_LIMITED,
    ERROR_CLASS_CLIENT_CANCELLED,
    ERROR_CLASS_GATEWAY_TRANSLATION,
    ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED,
    classify_recorded_error,
    classify_upstream_error,
    is_retryable_upstream_failure,
    is_terminal_error_class,
)


class _FakeHTTPError(Exception):
    """Minimal exception shaped like a litellm/httpx provider failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        error_type: Optional[str] = None,
        code: Optional[str] = None,
        retry_after: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        if retry_after is not None:
            self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


class _APIConnectionError(Exception):
    """Name-matched stand-in for litellm.exceptions.APIConnectionError."""


class _MidStreamFallbackError(Exception):
    """Name-matched stand-in for litellm.MidStreamFallbackError."""


class _RateLimitError(Exception):
    """Name-matched stand-in for litellm RateLimitError."""

    def __init__(self, message: str, *, status_code: int = 429) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def test_connection_refused_maps_to_network_503():
    """#116: connection refused must not surface as a generic 500."""
    exc = _APIConnectionError("Connection error. [Errno 111] Connection refused")
    result = classify_upstream_error(exc)
    assert result is not None
    assert result.error_class == ERROR_CLASS_NETWORK
    assert result.status_code == 503
    assert result.terminal is False


def test_midstream_fallback_maps_to_upstream_disconnect():
    """#117: MidStreamFallbackError is a terminal SSE disconnect."""
    exc = _MidStreamFallbackError(
        "litellm.APIConnectionError: peer closed connection without "
        "sending complete message body"
    )
    result = classify_upstream_error(exc)
    assert result is not None
    assert result.error_class == ERROR_CLASS_UPSTREAM_DISCONNECT
    assert result.status_code == 502


def test_overloaded_502_maps_to_upstream_overloaded():
    """#118: OpenAI 'servers are currently overloaded' is not a product failure."""
    exc = _FakeHTTPError(
        "Our servers are currently overloaded. Please try again later.",
        status_code=502,
    )
    result = classify_upstream_error(exc)
    assert result is not None
    assert result.error_class == ERROR_CLASS_UPSTREAM_OVERLOADED
    assert result.status_code == 502


def test_transient_429_maps_to_rate_limited():
    """#114: engine_overloaded_error stays retryable (non-terminal)."""
    exc = _RateLimitError(
        "engine_overloaded_error: The engine is currently overloaded",
    )
    result = classify_upstream_error(exc)
    assert result is not None
    assert result.error_class == ERROR_CLASS_UPSTREAM_RATE_LIMITED
    assert result.status_code == 429
    assert result.terminal is False


def test_google_resource_exhausted_maps_to_rate_limited():
    """Gemini SDK 429s are ResourceExhausted, not openai.RateLimitError."""
    from google.api_core.exceptions import ResourceExhausted

    result = classify_upstream_error(ResourceExhausted("429 Resource exhausted"))
    assert result is not None
    assert result.error_class == ERROR_CLASS_UPSTREAM_RATE_LIMITED
    assert result.status_code == 429
    assert result.terminal is False
    assert is_retryable_upstream_failure(ResourceExhausted("429 Resource exhausted"))


def test_quota_exhausted_429_is_terminal():
    """#114: daily TPD / rate_limit_reached_error must fail fast."""
    exc = _FakeHTTPError(
        "HTTP 429: request reached organization TPD rate limit, "
        "current: 1518025, limit: 1500000",
        status_code=429,
        error_type="rate_limit_reached_error",
        retry_after=600,
    )
    result = classify_upstream_error(exc)
    assert result is not None
    assert result.error_class == ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED
    assert result.status_code == 429
    assert result.terminal is True
    assert result.retry_after_seconds == 600


def test_auth_401_is_terminal_upstream_auth():
    exc = _FakeHTTPError("Invalid API key", status_code=401)
    result = classify_upstream_error(exc)
    assert result is not None
    assert result.error_class == ERROR_CLASS_UPSTREAM_AUTH
    assert result.terminal is True


def test_client_shaped_4xx_returns_none():
    """Validation-style 4xx keep existing passthrough handling."""
    exc = _FakeHTTPError("messages must be a non-empty list", status_code=400)
    assert classify_upstream_error(exc) is None


def test_midstream_provider_unavailable_is_retryable():
    """Founder 502 signature: OpenRouter JSON error injected mid-stream."""
    exc = _MidStreamFallbackError(
        "Upstream provider disconnected mid-stream: APIError: "
        "OpenrouterException - Message: JSON error injected into SSE stream, "
        "Metadata: {'error_type': 'provider_unavailable'}"
    )
    assert is_retryable_upstream_failure(exc) is True


def test_unsupported_parallel_tool_calls_is_not_retryable():
    """glm-5.3 4xx must not be retried; drop_params is the fix, not retry."""
    exc = _FakeHTTPError(
        "zai does not support parameters: ['parallel_tool_calls']",
        status_code=400,
    )
    assert is_retryable_upstream_failure(exc) is False


def test_auth_401_is_not_retryable():
    exc = _FakeHTTPError("Invalid API key", status_code=401)
    assert is_retryable_upstream_failure(exc) is False


def test_quota_exhausted_is_not_retryable():
    exc = _FakeHTTPError(
        "HTTP 429: request reached organization TPD rate limit",
        status_code=429,
        error_type="rate_limit_reached_error",
    )
    assert is_retryable_upstream_failure(exc) is False


def test_opaque_failure_maps_to_upstream_error_502():
    exc = Exception("upstream exploded")
    result = classify_upstream_error(exc)
    assert result is not None
    assert result.error_class == ERROR_CLASS_UPSTREAM_ERROR
    assert result.status_code == 502


def test_opaque_generic_exception_is_not_retryable():
    """#109: first-chunk ``Exception`` is a 502 to the client, not a retry."""
    rejected = Exception("upstream rejected the request")
    assert is_retryable_upstream_failure(rejected) is False
    assert is_retryable_upstream_failure(Exception("upstream exploded")) is False


def test_actual_502_status_is_retryable():
    """A real provider 502 (not a mapped opaque error) is retried."""
    exc = _FakeHTTPError("bad gateway", status_code=502)
    assert is_retryable_upstream_failure(exc) is True


def test_mapped_upstream_error_502_is_not_retryable():
    """#109: presentation 502 on a mapped opaque error is not a retry."""
    exc = ModelGatewayAPIError(
        provider="openai",
        status_code=502,
        message="upstream exploded",
        error_class=ERROR_CLASS_UPSTREAM_ERROR,
    )
    assert is_retryable_upstream_failure(exc) is False


@pytest.mark.parametrize(
    "status_code,detail,expected",
    [
        (200, None, None),
        (499, "client disconnected", ERROR_CLASS_CLIENT_CANCELLED),
        (
            429,
            "organization TPD rate limit",
            ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
        ),
        (429, "slow down", ERROR_CLASS_UPSTREAM_RATE_LIMITED),
        (
            502,
            "Our servers are currently overloaded",
            ERROR_CLASS_UPSTREAM_OVERLOADED,
        ),
        (503, "connection refused", ERROR_CLASS_NETWORK),
        (
            502,
            "Upstream provider disconnected mid-stream: peer closed connection",
            ERROR_CLASS_UPSTREAM_DISCONNECT,
        ),
        (502, "something else", ERROR_CLASS_UPSTREAM_ERROR),
        (401, "bad key", ERROR_CLASS_UPSTREAM_AUTH),
    ],
)
def test_classify_recorded_error(status_code, detail, expected):
    assert classify_recorded_error(status_code, detail) == expected


def test_hosted_tariff_refusal_is_terminal_not_an_upstream_hiccup() -> None:
    """The gateway's own 503 refusal must not read as a provider outage.

    Three agent runs in a row each retried five times against a hosted model
    the deployment had simply never priced.
    """
    refusal = ModelGatewayAPIError(
        provider="openai",
        status_code=503,
        message=(
            "Hosted model google/gemini-3.8-flash has no operator tariff; "
            "use your own provider key or pick another model."
        ),
        code="hosted_tariff_unconfigured",
    )
    classified = classify_upstream_error(refusal)
    assert classified is not None
    assert classified.error_class == ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED
    assert classified.terminal is True
    assert is_retryable_upstream_failure(refusal) is False
    assert is_terminal_error_class(ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED) is True
    # Recorded rows carry only status plus detail; the sentence still decides.
    assert (
        classify_recorded_error(503, refusal.message)
        == ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED
    )
    # An ordinary 503 is still a transient overload.
    assert is_terminal_error_class(ERROR_CLASS_UPSTREAM_OVERLOADED) is False


def test_gateway_translation_error_is_terminal_in_the_shared_taxonomy() -> None:
    """A recorded 500 must not look transient once translation failed.

    The class is terminal inside the gateway retry loop. Callers that only
    have a status and a detail string use this taxonomy instead.
    """
    assert is_terminal_error_class(ERROR_CLASS_GATEWAY_TRANSLATION) is True
    sentence = (
        "Gateway could not translate this request for the "
        "configured model: unhashable type: 'dict'"
    )
    assert classify_recorded_error(500, sentence) == ERROR_CLASS_GATEWAY_TRANSLATION
    assert (
        classify_recorded_error(500, ERROR_CLASS_GATEWAY_TRANSLATION)
        == ERROR_CLASS_GATEWAY_TRANSLATION
    )
    assert (
        classify_recorded_error(500, "Internal server error")
        == ERROR_CLASS_UPSTREAM_ERROR
    )


@pytest.mark.parametrize(
    "detail",
    [
        "Model gateway budget exceeded: account monthly limit reached",
        "Execution budget exceeded: execution token ceiling reached",
        "Preloop trial limit for hosted model reached. Please configure your own "
        "OpenAI/Anthropic API key.",
    ],
)
@pytest.mark.parametrize("status_code", [403, 429])
def test_recorded_budget_denial_is_not_an_upstream_class(status_code, detail):
    """The gateway's budget 429 (#1447) is never an upstream rate limit."""
    from preloop.services.upstream_errors import (
        classify_recorded_error,
        is_preloop_budget_denial_detail,
    )

    assert is_preloop_budget_denial_detail(detail)
    assert classify_recorded_error(status_code, detail) == "budget_exceeded"


def test_recorded_plain_429_is_still_an_upstream_rate_limit():
    from preloop.services.upstream_errors import classify_recorded_error

    assert classify_recorded_error(429, "slow down") == "upstream_rate_limited"
    assert (
        classify_recorded_error(429, "You exceeded your current quota")
        == "upstream_quota_exhausted"
    )
