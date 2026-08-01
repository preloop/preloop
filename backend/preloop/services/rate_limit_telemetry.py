"""Rate-limit telemetry for upstream model providers (#136).

Parses the rate-limit response headers providers attach to gateway upstream
calls (successes and 429s) into a normalized :class:`RateLimitSnapshot` that
the gateway persists on the usage row. Everything stored is an echo of an
observed provider response: raw headers are preserved verbatim in
``RateLimitSnapshot.raw`` and normalized fields are direct parses of those
values. Nothing is estimated or extrapolated.

Header families handled:

- ``Retry-After`` (seconds or HTTP-date), on 429/503 responses.
- ``anthropic-ratelimit-*`` (limit/remaining plus RFC3339 ``-reset``).
- ``x-ratelimit-*`` (OpenAI style: limit/remaining plus duration ``-reset``
  values like ``1s`` / ``6m0s`` / ``12ms``).

Unrecognized ``*ratelimit*`` headers (for example Anthropic's unified
subscription headers, whose semantics are not publicly documented) are kept
in ``raw`` only, so nothing is lost and nothing undocumented is presented as
a normalized fact.

The transient-vs-quota-exhausted subtype classification delegates to the
shared upstream-error taxonomy from #141 (``preloop.services.upstream_errors``)
when that module is available, and otherwise falls back to a minimal local
heuristic labeled as such (``subtype_source: heuristic``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Subtype values stored in meta_data["rate_limit"]["subtype"].
RATE_LIMIT_SUBTYPE_TRANSIENT = "transient"
RATE_LIMIT_SUBTYPE_QUOTA_EXHAUSTED = "quota_exhausted"

# Minimal fallback markers used ONLY when the shared taxonomy from #141 is
# not importable yet. Kept intentionally small; the taxonomy is the source
# of truth once it lands.
_FALLBACK_QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "rate_limit_reached",
    "quota",
    "billing",
    "credit balance",
)

_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")

_DURATION_UNIT_MS = {
    "ms": 1.0,
    "s": 1000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
    "d": 86_400_000.0,
}


@dataclass
class RateLimitSnapshot:
    """Normalized rate-limit signal observed on one upstream response.

    Attributes:
        retry_after_ms: Provider-advised wait from ``Retry-After``.
        requests_limit: Request-count limit for the provider's window.
        requests_remaining: Requests remaining in the window.
        requests_reset_at: Provider-supplied reset timestamp (verbatim
            RFC3339 string), when the provider expresses reset as a time.
        requests_reset_after_ms: Reset expressed as a duration, when the
            provider sends one (OpenAI style).
        tokens_limit: Token limit for the provider's window.
        tokens_remaining: Tokens remaining in the window.
        tokens_reset_at: Token-window reset timestamp (verbatim string).
        tokens_reset_after_ms: Token-window reset duration.
        raw: Every observed rate-limit-related header, verbatim.
    """

    retry_after_ms: Optional[int] = None
    requests_limit: Optional[int] = None
    requests_remaining: Optional[int] = None
    requests_reset_at: Optional[str] = None
    requests_reset_after_ms: Optional[int] = None
    tokens_limit: Optional[int] = None
    tokens_remaining: Optional[int] = None
    tokens_reset_at: Optional[str] = None
    tokens_reset_after_ms: Optional[int] = None
    raw: Dict[str, str] = field(default_factory=dict)

    def has_signal(self) -> bool:
        """Return True when at least one rate-limit header was observed."""
        return bool(self.raw)

    def to_meta(self) -> Dict[str, Any]:
        """Serialize for ``ApiUsage.meta_data["rate_limit"]`` (drops Nones)."""
        payload: Dict[str, Any] = {
            "retry_after_ms": self.retry_after_ms,
            "requests_limit": self.requests_limit,
            "requests_remaining": self.requests_remaining,
            "requests_reset_at": self.requests_reset_at,
            "requests_reset_after_ms": self.requests_reset_after_ms,
            "tokens_limit": self.tokens_limit,
            "tokens_remaining": self.tokens_remaining,
            "tokens_reset_at": self.tokens_reset_at,
            "tokens_reset_after_ms": self.tokens_reset_after_ms,
        }
        meta = {key: value for key, value in payload.items() if value is not None}
        meta["headers"] = dict(self.raw)
        return meta


def _headers_to_dict(headers: Any) -> Optional[Dict[str, str]]:
    """Coerce any headers-like object to a lowercase-keyed str dict."""
    if headers is None:
        return None
    items = None
    if isinstance(headers, dict):
        items = headers.items()
    else:
        items_method = getattr(headers, "items", None)
        if callable(items_method):
            try:
                items = items_method()
            except Exception:  # noqa: BLE001 - headers object may be anything
                return None
    if items is None:
        return None
    result: Dict[str, str] = {}
    try:
        for key, value in items:
            result[str(key).strip().lower()] = str(value).strip()
    except Exception:  # noqa: BLE001 - tolerate exotic mappings
        return None
    return result


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_retry_after_ms(value: Optional[str]) -> Optional[int]:
    """Parse ``Retry-After`` (delta-seconds or HTTP-date) to milliseconds."""
    if value is None:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if seconds < 0:
        return None
    return int(seconds * 1000)


def _parse_duration_ms(value: Optional[str]) -> Optional[int]:
    """Parse OpenAI-style reset durations (``1s``, ``6m0s``, ``12ms``)."""
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    parts = _DURATION_PART_RE.findall(text)
    if not parts:
        return None
    total_ms = 0.0
    for amount, unit in parts:
        total_ms += float(amount) * _DURATION_UNIT_MS[unit]
    return int(total_ms)


def parse_rate_limit_headers(headers: Any) -> Optional[RateLimitSnapshot]:
    """Extract a rate-limit snapshot from upstream response headers.

    Args:
        headers: Any headers-like object (dict, ``httpx.Headers``,
            ``email.message.Message`` from urllib, ...). Multi-valued
            lookups collapse to the last observed value.

    Returns:
        A snapshot when at least one rate-limit-related header is present,
        otherwise None. Never raises.
    """
    normalized = _headers_to_dict(headers)
    if not normalized:
        return None
    raw = {
        key: value
        for key, value in normalized.items()
        if "ratelimit" in key or key == "retry-after"
    }
    if not raw:
        return None

    snapshot = RateLimitSnapshot(raw=raw)
    snapshot.retry_after_ms = _parse_retry_after_ms(normalized.get("retry-after"))

    # Anthropic family: limit/remaining ints, reset as RFC3339 timestamps.
    snapshot.requests_limit = _parse_int(
        normalized.get("anthropic-ratelimit-requests-limit")
    )
    snapshot.requests_remaining = _parse_int(
        normalized.get("anthropic-ratelimit-requests-remaining")
    )
    snapshot.requests_reset_at = normalized.get("anthropic-ratelimit-requests-reset")
    snapshot.tokens_limit = _parse_int(
        normalized.get("anthropic-ratelimit-tokens-limit")
    )
    snapshot.tokens_remaining = _parse_int(
        normalized.get("anthropic-ratelimit-tokens-remaining")
    )
    snapshot.tokens_reset_at = normalized.get("anthropic-ratelimit-tokens-reset")

    # OpenAI family: limit/remaining ints, reset as durations.
    if snapshot.requests_limit is None:
        snapshot.requests_limit = _parse_int(
            normalized.get("x-ratelimit-limit-requests")
        )
    if snapshot.requests_remaining is None:
        snapshot.requests_remaining = _parse_int(
            normalized.get("x-ratelimit-remaining-requests")
        )
    snapshot.requests_reset_after_ms = _parse_duration_ms(
        normalized.get("x-ratelimit-reset-requests")
    )
    if snapshot.tokens_limit is None:
        snapshot.tokens_limit = _parse_int(normalized.get("x-ratelimit-limit-tokens"))
    if snapshot.tokens_remaining is None:
        snapshot.tokens_remaining = _parse_int(
            normalized.get("x-ratelimit-remaining-tokens")
        )
    snapshot.tokens_reset_after_ms = _parse_duration_ms(
        normalized.get("x-ratelimit-reset-tokens")
    )
    return snapshot


def headers_from_exception(exc: Exception) -> Any:
    """Best-effort headers extraction from an upstream-backend exception.

    Probes the shapes used by urllib (``HTTPError.headers``) and the
    OpenAI/LiteLLM SDK exceptions (``exc.response.headers``). Returns None
    when no headers-like object is found; never raises.

    Args:
        exc: The exception raised by an upstream backend call.

    Returns:
        A headers-like object suitable for :func:`parse_rate_limit_headers`,
        or None.
    """
    direct = getattr(exc, "headers", None)
    if direct is not None:
        return direct
    response = getattr(exc, "response", None)
    return getattr(response, "headers", None)


def headers_from_litellm_response(response: Any) -> Any:
    """Best-effort provider headers from a LiteLLM success response.

    LiteLLM exposes provider response headers under
    ``response._hidden_params["additional_headers"]``, prefixing pass-through
    provider headers with ``llm_provider-``. The prefix is stripped so the
    result parses like a native provider header map. Returns None when the
    shape is absent; never raises.

    Args:
        response: The object returned by ``litellm.completion``.

    Returns:
        A plain header dict, or None.
    """
    hidden = getattr(response, "_hidden_params", None)
    if not isinstance(hidden, dict):
        return None
    additional = hidden.get("additional_headers")
    if not isinstance(additional, dict):
        return None
    headers: Dict[str, str] = {}
    for key, value in additional.items():
        name = str(key).strip().lower()
        if name.startswith("llm_provider-"):
            name = name[len("llm_provider-") :]
        headers[name] = str(value)
    return headers or None


def classify_rate_limit_subtype(
    status_code: int, error_detail: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Classify a 429 as transient overload vs quota exhaustion.

    Delegates to the shared upstream-error taxonomy (#141) when available so
    the two features never disagree; otherwise applies a minimal quota-marker
    heuristic and says so via the returned source.

    Args:
        status_code: The recorded gateway status code.
        error_detail: The recorded upstream error message, if any.

    Returns:
        Tuple of (subtype, subtype_source). Both are None for non-429 rows.
        Source is ``"taxonomy"`` (shared #141 classifier) or ``"heuristic"``
        (local fallback).
    """
    if status_code != 429:
        return None, None
    # Telemetry classification must never break request recording: any
    # failure importing or running the shared taxonomy (#141) falls back to
    # the local heuristic.
    try:
        # Optional dependency: lands with PR #141. The ignore covers both the
        # not-yet-merged state (module absent) and the merged state.
        from preloop.services.upstream_errors import (  # type: ignore[import-untyped,import-not-found]
            ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
            classify_recorded_error,
        )

        error_class = classify_recorded_error(status_code, error_detail)
        if error_class == ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED:
            return RATE_LIMIT_SUBTYPE_QUOTA_EXHAUSTED, "taxonomy"
        return RATE_LIMIT_SUBTYPE_TRANSIENT, "taxonomy"
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - defensive: taxonomy bug must not crash
        logger.debug(
            "classify_recorded_error raised; falling back to heuristic",
            exc_info=True,
        )
    text = (error_detail or "").lower()
    if any(marker in text for marker in _FALLBACK_QUOTA_MARKERS):
        return RATE_LIMIT_SUBTYPE_QUOTA_EXHAUSTED, "heuristic"
    return RATE_LIMIT_SUBTYPE_TRANSIENT, "heuristic"
