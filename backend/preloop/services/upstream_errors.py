"""Shared classification of upstream model-provider failures.

One helper (``classify_upstream_error``) turns the zoo of exceptions raised by
upstream backends (litellm, httpx, urllib, raw sockets) into a small, stable
taxonomy that both the streaming and non-streaming gateway paths use for:

- HTTP status mapping (#116: connection refused must not surface as 500),
- SSE terminal error events (#117: mid-stream disconnects),
- ``ApiUsage.error_class`` recording (#118), and
- quota-exhausted vs transient 429 semantics (#114: ``Retry-After`` /
  terminal hints so runtimes can stop retrying hopeless calls).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib import error as urllib_error

# ---------------------------------------------------------------------------
# Taxonomy (stored in ApiUsage.error_class; keep values stable).
# ---------------------------------------------------------------------------
ERROR_CLASS_NETWORK = "network"
ERROR_CLASS_UPSTREAM_OVERLOADED = "upstream_overloaded"
ERROR_CLASS_UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED = "upstream_quota_exhausted"
ERROR_CLASS_UPSTREAM_AUTH = "upstream_auth"
ERROR_CLASS_UPSTREAM_ERROR = "upstream_error"
ERROR_CLASS_UPSTREAM_DISCONNECT = "upstream_disconnect"
ERROR_CLASS_CLIENT_CANCELLED = "client_cancelled"
# The client was already gone when the gateway tried to write the FIRST byte
# of a streaming response, so the response generator never ran. Distinct from
# ``client_cancelled`` (client left part-way through a stream it was reading):
# an abandoned stream usually means something in front of the gateway — a
# proxy read timeout, an ingress hang-up — killed the request, whereas a
# cancellation is normal client behaviour (user hit Ctrl-C, agent moved on).
ERROR_CLASS_STREAM_ABANDONED = "stream_abandoned"


@dataclass(frozen=True)
class UpstreamErrorClass:
    """Classification of an upstream-provider failure.

    Attributes:
        error_class: Stable taxonomy value (see module constants).
        status_code: HTTP status the gateway should return to the client.
        retry_after_seconds: Provider-supplied Retry-After, when available.
        terminal: True when retrying is hopeless (quota exhausted, bad
            upstream credentials) so runtimes should stop retrying.
    """

    error_class: str
    status_code: int
    retry_after_seconds: Optional[int] = None
    terminal: bool = False


# Markers that indicate a 429 is a hard quota/limit exhaustion rather than a
# transient burst (#114). Matched case-insensitively against message+type+code.
_QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "rate_limit_reached",
    "tokens per day",
    "requests per day",
    "tpd",
    "rpd",
    "daily limit",
    "monthly limit",
    "credit balance",
    "billing",
    "quota",
)

# Markers indicating transient provider overload (Anthropic 529
# "overloaded_error", OpenAI "engine is currently overloaded", generic 502
# "servers are currently overloaded" bodies).
_OVERLOAD_MARKERS = (
    "overloaded",
    "engine_overloaded",
    "at capacity",
    "server is busy",
)

# Textual fallbacks for connection-level failures when the exception type is
# opaque (e.g. wrapped by a provider SDK).
_NETWORK_MARKERS = (
    "connection refused",
    "connection reset",
    "connection error",
    "connect error",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "name or service not known",
    "getaddrinfo",
    "remote protocol error",
    "peer closed connection",
)


def _exception_text(exc: Exception) -> str:
    parts = [type(exc).__name__, str(exc)]
    for attr in ("message", "error_type", "code", "litellm_debug_info"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _status_code(exc: Exception) -> Optional[int]:
    raw = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        status = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if status < 100 or status > 599:
        return None
    return status


def _retry_after_seconds(exc: Exception) -> Optional[int]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - headers object may be anything
        return None
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        # HTTP-date Retry-After; not worth parsing for a hint header.
        return None
    return seconds if seconds >= 0 else None


def _is_connection_exception(exc: Exception) -> bool:
    """True for transport-level failures that never got an HTTP response."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, urllib_error.URLError) and not isinstance(
        exc, urllib_error.HTTPError
    ):
        return True
    try:
        import httpx

        if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
            return True
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        pass
    # litellm's APIConnectionError (and its Timeout subclass) — matched by MRO
    # name so this module stays importable without litellm. Substring match
    # tolerates test doubles like ``_APIConnectionError``.
    mro_names = {klass.__name__ for klass in type(exc).__mro__}
    if any(
        "APIConnectionError" in name or "APITimeoutError" in name for name in mro_names
    ):
        return True
    return False


def _mro_contains(exc: Exception, fragment: str) -> bool:
    """True when any class name in ``exc``'s MRO contains ``fragment``."""
    return any(fragment in klass.__name__ for klass in type(exc).__mro__)


def _contains(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def classify_upstream_error(exc: Exception) -> Optional[UpstreamErrorClass]:
    """Classify an exception raised while calling an upstream model provider.

    Args:
        exc: The exception raised by the upstream backend.

    Returns:
        An :class:`UpstreamErrorClass` when the failure is provider-side (or
        network-side), or ``None`` when the exception looks like a
        client-shaped error (4xx validation and similar) that should keep its
        existing passthrough handling.
    """
    text = _exception_text(exc)
    status = _status_code(exc)
    retry_after = _retry_after_seconds(exc)

    # Provider dropped an in-flight stream; litellm raises this when a
    # fallback would be needed mid-stream (#117).
    if _mro_contains(exc, "MidStreamFallbackError"):
        return UpstreamErrorClass(
            error_class=ERROR_CLASS_UPSTREAM_DISCONNECT,
            status_code=502,
            retry_after_seconds=retry_after,
        )

    if _is_connection_exception(exc):
        return UpstreamErrorClass(
            error_class=ERROR_CLASS_NETWORK,
            status_code=503,
            retry_after_seconds=retry_after,
        )

    if status == 429 or _mro_contains(exc, "RateLimitError"):
        if _contains(text, _QUOTA_MARKERS):
            return UpstreamErrorClass(
                error_class=ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
                status_code=429,
                retry_after_seconds=retry_after,
                terminal=True,
            )
        return UpstreamErrorClass(
            error_class=ERROR_CLASS_UPSTREAM_RATE_LIMITED,
            status_code=429,
            retry_after_seconds=retry_after,
        )

    if status == 401:
        return UpstreamErrorClass(
            error_class=ERROR_CLASS_UPSTREAM_AUTH,
            status_code=401,
            terminal=True,
        )

    if status in (502, 503, 529) or (
        status in (None, 500) and _contains(text, _OVERLOAD_MARKERS)
    ):
        if status in (503, 529) or _contains(text, _OVERLOAD_MARKERS):
            return UpstreamErrorClass(
                error_class=ERROR_CLASS_UPSTREAM_OVERLOADED,
                status_code=status if status in (502, 503, 529) else 502,
                retry_after_seconds=retry_after,
            )
        return UpstreamErrorClass(
            error_class=ERROR_CLASS_UPSTREAM_ERROR,
            status_code=502,
            retry_after_seconds=retry_after,
        )

    if status is not None and 400 <= status < 500:
        # Client-shaped upstream error (validation, not-found, permission…):
        # keep the existing passthrough mapping.
        return None

    if status is None and _contains(text, _NETWORK_MARKERS):
        return UpstreamErrorClass(
            error_class=ERROR_CLASS_NETWORK,
            status_code=503,
            retry_after_seconds=retry_after,
        )

    # Remaining 5xx and completely opaque failures: provider-side error. Map
    # to 502 so provider failures are never presented as gateway 500s (#116).
    return UpstreamErrorClass(
        error_class=ERROR_CLASS_UPSTREAM_ERROR,
        status_code=502,
        retry_after_seconds=retry_after,
    )


# Detail markers that indicate a mid-stream drop rather than a pre-stream
# transport failure. Used only by ``classify_recorded_error`` (no exception
# object available there).
_DISCONNECT_DETAIL_MARKERS = (
    "disconnected mid-stream",
    "midstream",
    "mid-stream",
    "incomplete chunked read",
    "peer closed connection",
)

# Transient provider faults the gateway should retry (bounded). Includes the
# OpenRouter mid-stream signature: MidStreamFallbackError wrapping
# provider_unavailable / upstream_disconnect / HTTP 502.
_RETRYABLE_MARKERS = (
    "provider_unavailable",
    "upstream_disconnect",
    "disconnected mid-stream",
    "json error injected into sse stream",
    "midstreamfallbackerror",
)

# Client / model-capability errors. Retrying these cannot succeed and burns
# quota (glm-5.3 parallel_tool_calls, auth, bad params).
_NON_RETRYABLE_MARKERS = (
    "does not support parameters",
    "unsupported parameter",
    "unsupported_parameter",
    "parallel_tool_calls",
    "invalid_request",
    "invalid api key",
    "incorrect api key",
    "authentication",
)

_RETRYABLE_ERROR_CLASSES = frozenset(
    {
        ERROR_CLASS_NETWORK,
        ERROR_CLASS_UPSTREAM_DISCONNECT,
        ERROR_CLASS_UPSTREAM_OVERLOADED,
        ERROR_CLASS_UPSTREAM_ERROR,
        ERROR_CLASS_UPSTREAM_RATE_LIMITED,
    }
)

_NON_RETRYABLE_ERROR_CLASSES = frozenset(
    {
        ERROR_CLASS_UPSTREAM_AUTH,
        ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
        ERROR_CLASS_CLIENT_CANCELLED,
        ERROR_CLASS_STREAM_ABANDONED,
    }
)

_RETRYABLE_STATUS_CODES = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
)


def is_retryable_upstream_failure(exc: Exception) -> bool:
    """Whether the gateway should retry this upstream failure.

    Retries transient 502 / provider_unavailable / upstream_disconnect /
    MidStreamFallbackError / network / overload. Does not retry 4xx
    validation, auth, quota, or unsupported-parameter errors
    (``parallel_tool_calls``).

    Args:
        exc: Raw LiteLLM/httpx exception or a mapped ``ModelGatewayAPIError``.

    Returns:
        True when another bounded attempt could plausibly succeed.
    """
    text = _exception_text(exc)
    if _contains(text, _NON_RETRYABLE_MARKERS):
        return False

    if bool(getattr(exc, "terminal", False)):
        return False

    error_class = getattr(exc, "error_class", None)
    if error_class in _NON_RETRYABLE_ERROR_CLASSES:
        return False

    classified = classify_upstream_error(exc)
    if classified is not None:
        if classified.terminal:
            return False
        if classified.error_class in _NON_RETRYABLE_ERROR_CLASSES:
            return False
        if classified.error_class in _RETRYABLE_ERROR_CLASSES:
            return True
        if classified.status_code in _RETRYABLE_STATUS_CODES:
            return True

    if error_class in _RETRYABLE_ERROR_CLASSES:
        return True

    status = _status_code(exc)
    if status in _RETRYABLE_STATUS_CODES:
        return True
    if status is not None and 400 <= status < 500:
        return False

    return _contains(text, _RETRYABLE_MARKERS)


def classify_recorded_error(
    status_code: int, error_detail: Optional[str]
) -> Optional[str]:
    """Best-effort ``error_class`` for usage rows recorded from status+detail.

    Used by ``_record_gateway_request`` when no richer classification was
    attached to the exception (#118). Returns ``None`` for successes and for
    failures that are not upstream-related (validation, budget denials…).

    Note: without the original exception, this helper cannot always tell
    ``upstream_disconnect`` from a generic ``upstream_error``. Streaming
    callers should pass ``error_class`` explicitly from ``_stream_error`` /
    ``_normalize_upstream_error``; this fallback only inspects the detail
    string for disconnect-shaped phrasing.
    """
    if status_code < 400:
        return None
    text = (error_detail or "").lower()
    if status_code == 499:
        return ERROR_CLASS_CLIENT_CANCELLED
    if status_code == 429:
        if _contains(text, _QUOTA_MARKERS):
            return ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED
        return ERROR_CLASS_UPSTREAM_RATE_LIMITED
    if status_code == 401:
        return ERROR_CLASS_UPSTREAM_AUTH
    if status_code in (502, 503, 529):
        if _contains(text, _DISCONNECT_DETAIL_MARKERS):
            return ERROR_CLASS_UPSTREAM_DISCONNECT
        if _contains(text, _NETWORK_MARKERS):
            return ERROR_CLASS_NETWORK
        if status_code in (503, 529) or _contains(text, _OVERLOAD_MARKERS):
            return ERROR_CLASS_UPSTREAM_OVERLOADED
        return ERROR_CLASS_UPSTREAM_ERROR
    if status_code >= 500:
        return ERROR_CLASS_UPSTREAM_ERROR
    return None
