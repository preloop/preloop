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
    assert err.code == ERROR_CLASS_UPSTREAM_DISCONNECT
    assert err.status_code == 502
    assert "peer closed connection" in err.message


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


def test_midstream_sse_preserves_connection_reset_detail():
    """CI regression: neighboring suites assert the original fault text survives.

    Exception("upstream connection reset") is classified as network then remapped
    to upstream_disconnect; the SSE body must keep the detail string.
    """
    service = _service()
    exc = Exception("upstream connection reset")

    openai_frame = service._openai_stream_error_event(exc)
    assert "upstream_disconnect" in openai_frame
    assert "upstream connection reset" in openai_frame

    responses_frame = service._responses_stream_error_event(exc)
    assert '"code": "upstream_disconnect"' in responses_frame or (
        '"code":"upstream_disconnect"' in responses_frame
    )
    assert "upstream connection reset" in responses_frame

    anthropic_frame = service._anthropic_stream_error_event(exc)
    assert "upstream_disconnect" in anthropic_frame
    assert "upstream connection reset" in anthropic_frame


# ---------------------------------------------------------------------------
# Surfacing the upstream provider's real error message (scrubbed).
# ---------------------------------------------------------------------------

_OPENROUTER_BLOB = (
    "litellm.NotFoundError: litellm.NotFoundError: OpenrouterException - "
    '{"error":{"message":'
    '"No allowed providers are available for the selected model.",'
    '"code":404,"metadata":{"requested_providers":["alibaba"],'
    '"available_providers":["openai","deepinfra","together","fireworks",'
    '"novita","hyperbolic","nebius","parasail","baseten","cerebras",'
    '"groq","sambanova","lepton","avian","kluster","targon","inferencenet",'
    '"mancer","featherless","chutes"]}},"user_id":"user_x"}'
)


def _openrouter_not_found_error() -> Exception:
    import litellm

    return litellm.NotFoundError(
        message=_OPENROUTER_BLOB,
        model="openrouter/qwen/qwen3.8-max",
        llm_provider="openrouter",
    )


def test_normalize_openrouter_not_found_surfaces_provider_message():
    """Founder case: the OpenRouter sentence must reach the user, not a blob."""
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai", _openrouter_not_found_error()
    )
    assert err.status_code == 404
    assert "No allowed providers" in err.message
    # No litellm wrapping, no metadata dump, no internal identifiers.
    assert "litellm.NotFoundError" not in err.message
    assert "OpenrouterException" not in err.message
    assert "available_providers" not in err.message
    assert "user_x" not in err.message
    assert "https://" not in err.message
    payload = err.to_payload()
    assert "No allowed providers" in payload["error"]["message"]


def test_normalize_openrouter_not_found_provider_detail_is_short_hint():
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai", _openrouter_not_found_error()
    )
    payload = err.to_payload()
    detail = payload["error"].get("provider_detail")
    assert detail is not None
    assert "openrouter" in detail
    assert "requested_providers: alibaba" in detail
    # A hint, not a metadata dump.
    assert "available_providers" not in detail
    assert len(detail) <= 200


def test_normalize_upstream_error_scrubs_secrets_from_message():
    """Upstream messages can echo keys and credentialed URLs; scrub them."""
    import litellm

    blob = (
        "litellm.NotFoundError: OpenrouterException - request to "
        "https://user:supersecrettoken123@openrouter.ai/api/v1/chat failed "
        "with api key sk-abcdefghijklmnopqrstuvwxyz123456"
    )
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai",
        litellm.NotFoundError(
            message=blob, model="openrouter/x", llm_provider="openrouter"
        ),
    )
    assert "supersecrettoken123" not in err.message
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in err.message
    assert "[REDACTED]" in err.message


def test_normalize_upstream_error_caps_message_length():
    long_message = "litellm.APIError: BoomException - " + ("x" * 5000)
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai",
        _FakeHTTPError(long_message, status_code=404),
    )
    assert len(err.message) <= 500


def test_normalize_plain_message_unchanged():
    """Messages with no litellm wrapping pass through as before."""
    err = OpenAIGatewayService._normalize_upstream_error(
        "openai", _FakeHTTPError("model not found", status_code=404)
    )
    assert err.message == "model not found"
    assert err.to_payload()["error"].get("provider_detail") is None


# The exact wire text litellm 1.81.13 produces for an invalid Gemini key. It
# was captured by executing `litellm.completion(model="gemini/...")` against
# the real endpoint with a bogus key, not hand-written, so this test tracks
# what the provider actually sends: a Google JSON envelope nested inside the
# "GeminiException - " marker, with the useful sentence three levels down.
_GEMINI_AUTH_BLOB = """litellm.AuthenticationError: GeminiException - {
  "error": {
    "code": 400,
    "message": "API key not valid. Please pass a valid API key.",
    "status": "INVALID_ARGUMENT",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "API_KEY_INVALID",
        "domain": "googleapis.com",
        "metadata": {
          "service": "generativelanguage.googleapis.com"
        }
      },
      {
        "@type": "type.googleapis.com/google.rpc.LocalizedMessage",
        "locale": "en-US",
        "message": "API key not valid. Please pass a valid API key."
      }
    ]
  }
}"""


def test_normalize_gemini_error_lifts_google_envelope():
    """Gemini/Google was the untested provider in the surfacing path.

    Google nests the human sentence inside a JSON envelope that also carries
    type URLs, a reason code and a service domain. The user must get the
    sentence, not the envelope.
    """
    err = OpenAIGatewayService._normalize_upstream_error(
        "gemini", _FakeHTTPError(_GEMINI_AUTH_BLOB, status_code=401)
    )
    assert err.message == "API key not valid. Please pass a valid API key."
    # None of the envelope scaffolding may reach the user.
    assert "GeminiException" not in err.message
    assert "litellm." not in err.message
    assert "type.googleapis.com" not in err.message
    assert "API_KEY_INVALID" not in err.message
    assert "generativelanguage.googleapis.com" not in err.message
    assert err.to_payload()["error"]["message"] == (
        "API key not valid. Please pass a valid API key."
    )


def test_normalize_gemini_quota_error_surfaces_sentence_and_hint():
    """The other common Gemini failure: quota exhaustion mid-incident."""
    blob = (
        "litellm.RateLimitError: GeminiException - "
        '{"error": {"code": 429, "message": "Resource has been exhausted '
        '(e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}'
    )
    err = OpenAIGatewayService._normalize_upstream_error(
        "gemini", _FakeHTTPError(blob, status_code=429)
    )
    assert err.message == "Resource has been exhausted (e.g. check quota)."
    assert "RESOURCE_EXHAUSTED" not in err.message
    # The provider marker is kept on the error object as a short hint...
    assert err.provider_detail == "gemini"
    # ...but deliberately does NOT appear in the Gemini response body, which
    # must stay Google's native {code, message, status} envelope. Only the
    # OpenAI-shaped payload carries provider_detail.
    payload = err.to_payload()
    assert set(payload["error"]) == {"code", "message", "status"}
    assert payload["error"]["code"] == 429


class TestNotifyAdminsTraceScrubbing:
    """Regression: the admin notification in _normalize_upstream_error must
    scrub secrets from the exception trace and cap its length."""

    def test_api_key_in_exception_is_scrubbed_in_notification(self):
        """A synthetic 502 exception whose message contains an API key must
        not survive into the notify_admins payload."""
        from unittest.mock import patch as _patch

        fake_key = "sk-live-SUPERSECRETKEY1234567890abcdef"
        exc = _FakeHTTPError(
            f"Connection to https://api.example.com?key={fake_key} failed: "
            f"auth header Bearer {fake_key}",
            status_code=502,
        )

        captured_calls: list = []

        def _capture_notify(*, subject: str, message: str) -> None:
            captured_calls.append({"subject": subject, "message": message})

        with _patch("preloop.sync.tasks.notify_admins", side_effect=_capture_notify):
            OpenAIGatewayService._normalize_upstream_error("openai", exc)

        assert captured_calls, "notify_admins was never called for a 502 error"
        body = captured_calls[0]["message"]
        assert fake_key not in body, "Raw API key survived into admin notification body"
        assert "[REDACTED]" in body

    def test_long_trace_is_capped_at_400_chars(self):
        """The trace portion of the admin notification must be at most 400 chars."""
        from unittest.mock import patch as _patch

        long_detail = "x" * 2000
        exc = _FakeHTTPError(long_detail, status_code=500)

        captured_calls: list = []

        def _capture_notify(*, subject: str, message: str) -> None:
            captured_calls.append({"subject": subject, "message": message})

        with _patch("preloop.sync.tasks.notify_admins", side_effect=_capture_notify):
            OpenAIGatewayService._normalize_upstream_error("openai", exc)

        assert captured_calls, "notify_admins was never called for a 500 error"
        body = captured_calls[0]["message"]
        # The trace starts after "Trace:\n"; extract it.
        trace_marker = "Trace:\n"
        trace_start = body.index(trace_marker) + len(trace_marker)
        trace_text = body[trace_start:]
        assert len(trace_text) <= 400, (
            f"Trace length {len(trace_text)} exceeds 400-char cap"
        )
