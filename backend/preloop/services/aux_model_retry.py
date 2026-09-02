"""Bounded Retry-After-aware retry for aux (non-gateway) model calls.

The model gateway already retries transient 429 / overload / network faults.
Auxiliary call sites (approval summaries, session summaries, policy
generation, agent-name extraction) talk to providers through litellm
directly and historically let a single transient 429 propagate raw — or
burn the capped one-shot system-default fallback.

This helper reuses ``classify_upstream_error`` / ``is_retryable_upstream_failure``
so aux paths share the gateway taxonomy instead of growing a parallel one.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional, TypeVar

from preloop.services.model_gateway_errors import (
    GatewayProvider,
    ModelGatewayAPIError,
)
from preloop.services.upstream_errors import (
    ERROR_CLASS_UPSTREAM_RATE_LIMITED,
    UpstreamErrorClass,
    classify_upstream_error,
    is_retryable_upstream_failure,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# One retry = two total attempts. Aux paths sit on short request deadlines
# (approval summaries, name extraction), so this stays tighter than the
# gateway's retry budget.
AUX_RETRY_COUNT = 1
AUX_BASE_BACKOFF_SECONDS = 0.5
AUX_RETRY_AFTER_CAP_SECONDS = 4.0

# ModelGatewayAPIError.provider is a closed literal used for the error
# envelope shape. Unknown/custom aux providers fall back to openai.
_GATEWAY_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "gemini"})


def _as_gateway_provider(provider: Optional[str]) -> GatewayProvider:
    """Map an AIModel.provider_name onto the gateway envelope literal."""

    name = (provider or "").strip().lower()
    if name in _GATEWAY_PROVIDERS:
        return name  # type: ignore[return-value]
    return "openai"


def _backoff_seconds(classified: Optional[UpstreamErrorClass], attempt: int) -> float:
    """Exponential backoff, raised to the provider Retry-After, capped at 4s."""

    delay = AUX_BASE_BACKOFF_SECONDS * float(1 << attempt)
    if classified is not None and classified.retry_after_seconds is not None:
        delay = max(delay, float(classified.retry_after_seconds))
    return min(delay, AUX_RETRY_AFTER_CAP_SECONDS)


def _to_gateway_error(
    exc: Exception, *, provider: Optional[str] = None
) -> ModelGatewayAPIError:
    """Surface an exhausted transient as a classified gateway error.

    The raw provider exception is chained as ``__cause__`` so GlitchTip
    still has the original body, but callers see a stable error_class
    instead of ``RateLimitError: Error code: 429 - ...``.
    """

    classified = classify_upstream_error(exc)
    error_class = (
        classified.error_class
        if classified is not None
        else ERROR_CLASS_UPSTREAM_RATE_LIMITED
    )
    status_code = classified.status_code if classified is not None else 429
    retry_after = classified.retry_after_seconds if classified is not None else None
    terminal = classified.terminal if classified is not None else False
    return ModelGatewayAPIError(
        provider=_as_gateway_provider(provider),
        status_code=status_code,
        message=str(exc),
        error_class=error_class,
        retry_after_seconds=retry_after,
        terminal=terminal,
    )


def call_with_aux_retry(
    fn: Callable[[], T],
    *,
    operation_name: str = "aux_model",
    provider: Optional[str] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> T:
    """Call ``fn``, retrying once on a transient upstream fault.

    Terminal quota / auth failures fail fast. Exhausted transients are
    re-raised as :class:`ModelGatewayAPIError` with the raw exception
    chained as ``__cause__``.

    ``provider`` is the model's ``provider_name`` so the classified error
    envelope matches Anthropic/Gemini aux calls instead of always looking
    like OpenAI.

    ``sleep`` is resolved at call time so tests can patch
    ``preloop.services.aux_model_retry.time.sleep``.
    """

    sleeper = time.sleep if sleep is None else sleep
    last_exc: Optional[Exception] = None

    for attempt in range(AUX_RETRY_COUNT + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            classified = classify_upstream_error(exc)
            if classified is not None and classified.terminal:
                raise
            if not is_retryable_upstream_failure(exc):
                raise
            if attempt >= AUX_RETRY_COUNT:
                break
            delay = _backoff_seconds(classified, attempt)
            logger.info(
                "Retrying %s after %.2fs (%s)",
                operation_name,
                delay,
                type(exc).__name__,
            )
            sleeper(delay)

    assert last_exc is not None
    raise _to_gateway_error(last_exc, provider=provider) from last_exc
