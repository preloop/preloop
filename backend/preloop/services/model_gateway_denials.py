"""One status contract for model gateway budget denials (#1447).

A budget hard limit is a spend condition, not an authorization failure, so
every gateway router (``/openai/v1``, ``/anthropic/v1``, ``/gemini/v1beta``)
answers it with ``429`` and a provider-shaped body:

* Anthropic: ``error.type`` ``billing_error``.
* OpenAI: ``error.type`` and ``error.code`` ``insufficient_quota``, plus
  ``error.preloop_code`` carrying the Preloop machine code
  (``budget_limit_exceeded`` or ``execution_budget_exceeded``).
* Gemini: ``error.status`` ``RESOURCE_EXHAUSTED``.

Headers are ``retry-after`` (integer seconds to the budget window reset,
3600 when unknown) and ``x-should-retry: false`` so SDKs do not hot-loop.
Policy denials (kill switch, model allowlist, model authorization, content
policy) stay ``403``.
"""

from __future__ import annotations

from typing import Any, Optional

from preloop.services.model_gateway_errors import GatewayProvider, ModelGatewayAPIError

BUDGET_DENIAL_STATUS_CODE = 429
#: ``Retry-After`` when the budget window reset is unknown.
DEFAULT_BUDGET_RETRY_AFTER_SECONDS = 3600
BUDGET_LIMIT_EXCEEDED_CODE = "budget_limit_exceeded"
EXECUTION_BUDGET_EXCEEDED_CODE = "execution_budget_exceeded"
BUDGET_DENIAL_CODES = frozenset(
    {BUDGET_LIMIT_EXCEEDED_CODE, EXECUTION_BUDGET_EXCEEDED_CODE}
)

_BUDGET_ERROR_TYPES: dict[str, str] = {
    "anthropic": "billing_error",
    "openai": "insufficient_quota",
    "gemini": "RESOURCE_EXHAUSTED",
}

#: Message fragments of every budget denial the gateway has ever produced.
#: Kept so ``is_budget_denial`` also recognises errors raised by older
#: extension enforcers that still build a plain ``403``.
_BUDGET_MESSAGE_MARKERS = (
    "budget exceeded",
    "budget hard limit exceeded",
    "limit for hosted model reached",
    "limit for hosted models reached",
    "budget enforcement requires pricing information",
)


class BudgetDenialError(ModelGatewayAPIError):
    """A ``429`` budget denial rendered in the active provider's format.

    ``code`` keeps the Preloop machine code internally (usage rows, audit
    and trusted upstream mapping read it); the OpenAI body renders
    ``insufficient_quota`` in ``code`` and the machine code in
    ``preloop_code``.
    """

    budget_reset_seconds: Optional[int] = None

    @property
    def preloop_code(self) -> str:
        """The Preloop machine code (``budget_limit_exceeded`` by default)."""
        return self.code or BUDGET_LIMIT_EXCEEDED_CODE

    def to_payload(self) -> dict[str, Any]:
        """Provider-native body; OpenAI gets ``insufficient_quota`` codes."""
        payload = super().to_payload()
        if self.provider == "openai":
            payload["error"]["code"] = "insufficient_quota"
            payload["error"]["preloop_code"] = self.preloop_code
        return payload

    def response_headers(self) -> dict[str, str]:
        """``retry-after`` (integer seconds) and ``x-should-retry: false``."""
        headers: dict[str, str] = {
            "retry-after": str(
                int(self.retry_after_seconds or DEFAULT_BUDGET_RETRY_AFTER_SECONDS)
            ),
        }
        headers.update(getattr(self, "extra_response_headers", None) or {})
        headers["x-should-retry"] = "false"
        return headers


def budget_denial_error(
    provider: GatewayProvider,
    reason: Optional[str],
    message: str,
    reset_seconds: Optional[int] = None,
) -> BudgetDenialError:
    """Build the ``429`` budget denial for ``provider``.

    Args:
        provider: Gateway router format to render (``openai``, ``anthropic``
            or ``gemini``).
        reason: Preloop machine code, ``budget_limit_exceeded`` (default when
            ``None``) or ``execution_budget_exceeded``.
        message: Human readable denial. Callers keep the historical
            ``Model gateway budget exceeded: ...`` text so text matchers work.
        reset_seconds: Seconds until the budget window resets; ``None`` or a
            non-positive value renders the 3600 second default.

    Returns:
        The provider-shaped error; raise it from the service layer.
    """
    retry_after = (
        max(1, int(reset_seconds))
        if reset_seconds is not None and reset_seconds > 0
        else DEFAULT_BUDGET_RETRY_AFTER_SECONDS
    )
    error = BudgetDenialError(
        provider=provider,
        status_code=BUDGET_DENIAL_STATUS_CODE,
        message=message,
        error_type=_BUDGET_ERROR_TYPES[provider],
        code=reason or BUDGET_LIMIT_EXCEEDED_CODE,
        retry_after_seconds=retry_after,
        terminal=True,
    )
    error.budget_reset_seconds = retry_after
    return error


def is_budget_denial(exc: ModelGatewayAPIError) -> bool:
    """Whether a gateway error is a Preloop budget denial.

    Accepts the ``429`` shape built by :func:`budget_denial_error` and the
    legacy ``403`` shape (extension enforcers that predate #1447).
    """
    if isinstance(exc, BudgetDenialError):
        return True
    if exc.status_code not in (403, BUDGET_DENIAL_STATUS_CODE):
        return False
    if exc.code in BUDGET_DENIAL_CODES:
        return True
    if exc.status_code != 403:
        # A plain 429 is a rate limit unless it carries a budget code.
        return False
    message = (exc.message or "").lower()
    return any(marker in message for marker in _BUDGET_MESSAGE_MARKERS)


def reraise_as_budget_denial(
    exc: ModelGatewayAPIError, provider: GatewayProvider
) -> ModelGatewayAPIError:
    """Re-render a budget denial from any source in the ``429`` contract.

    Non-budget errors are returned unchanged.
    """
    if not is_budget_denial(exc):
        return exc
    return budget_denial_error(
        provider,
        exc.code if exc.code in BUDGET_DENIAL_CODES else None,
        exc.message,
        getattr(exc, "budget_reset_seconds", None) or exc.retry_after_seconds,
    )
