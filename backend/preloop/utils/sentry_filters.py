"""Shared Sentry/GlitchTip filters for benign operational noise."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

# Only automatic SDK captures made synchronously inside an attempt owned by the
# gateway are noise: the gateway handles, classifies and records their outcome.
_GATEWAY_UPSTREAM_CALL: ContextVar[bool] = ContextVar(
    "gateway_upstream_call", default=False
)


@contextmanager
def gateway_upstream_call() -> Iterator[None]:
    """Scope automatic SDK error filtering to one gateway-owned attempt."""
    token = _GATEWAY_UPSTREAM_CALL.set(True)
    try:
        yield
    finally:
        _GATEWAY_UPSTREAM_CALL.reset(token)


_BENIGN_LOG_PHRASES = (
    "unclosed client session",
    "unclosed connector",
    "issue data missing from payload",
    "could not find stripe price for plan",
    "error finding stripe price",
)

_TRANSIENT_DB_PHRASES = (
    "ssl connection has been closed",
    "connection reset by peer",
    "server closed the connection unexpectedly",
    "connection already closed",
)


def _event_message(event: dict[str, Any]) -> str:
    logentry = event.get("logentry") or {}
    message = (
        event.get("message") or logentry.get("formatted") or logentry.get("message")
    )
    return str(message or "")


def _exception_values(event: dict[str, Any]) -> list[dict[str, Any]]:
    values = (event.get("exception") or {}).get("values") or []
    return values if isinstance(values, list) else []


def _is_benign_exception(exc_type: Any, exc_value: BaseException | None) -> bool:
    if exc_type is None:
        return False

    type_name = getattr(exc_type, "__name__", str(exc_type))
    message = str(exc_value or "").lower()

    if type_name in {"WebSocketDisconnect", "ClientDisconnected"}:
        return True

    if type_name in {"OperationalError", "InterfaceError", "PendingRollbackError"}:
        return any(phrase in message for phrase in _TRANSIENT_DB_PHRASES)

    if type_name in {"ResourceWarning"}:
        return any(phrase in message for phrase in _BENIGN_LOG_PHRASES[:2])

    return False


def should_drop_sentry_event(
    event: dict[str, Any],
    hint: Optional[dict[str, Any]] = None,
) -> bool:
    """Return True when an event should be kept out of GlitchTip."""
    hint = hint or {}

    if "exc_info" in hint:
        exc_type, exc_value, _ = hint["exc_info"]
        if _is_benign_exception(exc_type, exc_value):
            return True

    if _GATEWAY_UPSTREAM_CALL.get():
        # A mechanism/name string alone is not evidence of an upstream failure.
        # Keep local SDK/adapter bugs and every call outside this owned context.
        from openai import APIConnectionError, APIStatusError

        exc_info = hint.get("exc_info")
        exc_value = exc_info[1] if exc_info else None
        if isinstance(exc_value, (APIConnectionError, APIStatusError)) and any(
            (entry.get("mechanism") or {}).get("type") == "openai"
            for entry in _exception_values(event)
        ):
            return True

    message = _event_message(event).lower()
    if any(phrase in message for phrase in _BENIGN_LOG_PHRASES):
        return True

    for entry in _exception_values(event):
        exc_type_name = entry.get("type") or ""
        exc_value = (entry.get("value") or "").lower()
        if exc_type_name in {"WebSocketDisconnect", "ClientDisconnected"}:
            return True
        if exc_type_name in {"OperationalError", "InterfaceError"}:
            if any(phrase in exc_value for phrase in _TRANSIENT_DB_PHRASES):
                return True
        if exc_type_name == "ResourceWarning" and any(
            phrase in exc_value for phrase in _BENIGN_LOG_PHRASES[:2]
        ):
            return True

    return False


def sentry_before_send(
    event: dict[str, Any],
    hint: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Drop benign events before they reach GlitchTip."""
    if should_drop_sentry_event(event, hint):
        return None
    return event
