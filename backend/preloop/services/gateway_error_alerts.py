"""Throttled admin alerting for sustained gateway 5xx failures.

Before this module, every gateway 5xx fired ``notify_admins`` from
``OpenAIGatewayService._normalize_upstream_error`` with no rate limit,
deduplication, or backoff: a sustained upstream outage sent one admin
email/Slack/Mattermost message per request. This module caps that to one
notification per ``(provider, status_code)`` key per quiet window and reports
how many alerts were dropped while a window was open.

Design notes:

- **Per-process state, deliberately.** The window is an in-memory timestamp
  guarded by a ``threading.Lock``, so no database or broker is involved and a
  notifier failure cannot turn into a retry storm. With several gateway
  workers the effective rate is ``N x workers`` per key/window; that is
  documented rather than papered over with shared state.
- **Reserve before notifying.** The window is reserved atomically and the
  notifier runs afterwards, outside the lock. A failing notifier therefore
  consumes its window instead of re-arming the alert for the next request.
- **Never raises.** Alert bookkeeping is best-effort: any unexpected error
  falls back to the old behavior (send now) so a bug in the throttle cannot
  silently silence outage alerts.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

#: Quiet period (seconds) between admin alerts for one (provider, status) key.
DEFAULT_ALERT_INTERVAL_SECONDS = 300.0

#: Env var overriding the quiet period; must parse to a finite positive number.
GATEWAY_ERROR_ALERT_INTERVAL_ENV = "GATEWAY_ERROR_ALERT_INTERVAL_SECONDS"

#: Only 5xx notifications use the throttle; anything below is never reserved.
_MIN_THROTTLED_STATUS = 500

_lock = threading.Lock()
_state: dict[Tuple[str, int], "_Window"] = {}

#: Monotonic clock, overridable in tests (``time.monotonic`` in production).
_clock: Callable[[], float] = time.monotonic


@dataclass
class _Window:
    """Quiet window for one (provider, status) alert key.

    Attributes:
        next_allowed_at: Earliest monotonic time (inclusive) at which the
            next alert for this key may be sent.
        suppressed: Number of 5xx alerts dropped while this window was open.
    """

    next_allowed_at: float
    suppressed: int = 0


def _alert_interval_seconds() -> float:
    """Return the configured quiet window, falling back to the default.

    The env var must be a finite positive number of seconds; missing, empty,
    non-numeric, infinite, NaN, zero, and negative values all fall back to
    :data:`DEFAULT_ALERT_INTERVAL_SECONDS`.

    Returns:
        The quiet window length in seconds.
    """
    raw = os.getenv(GATEWAY_ERROR_ALERT_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_ALERT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ALERT_INTERVAL_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_ALERT_INTERVAL_SECONDS
    return value


def reserve_gateway_5xx_alert(
    provider: str, status_code: int, *, now: Optional[float] = None
) -> Tuple[bool, int]:
    """Atomically reserve an admin-alert slot for a gateway 5xx.

    The first error for a ``(provider, status_code)`` key sends immediately
    and opens a quiet window; repeats inside the window are dropped. The
    first error after the window closes sends again and reports how many
    alerts were suppressed while it was open; the counter then resets for the
    new window. Statuses below 500 are never reserved.

    The window is reserved before the notifier runs so a failing notifier
    consumes its slot instead of re-arming the alert.

    Args:
        provider: Gateway provider value used for alert keying (e.g.
            ``"openai"``).
        status_code: Normalized integer HTTP status of the failure.
        now: Monotonic timestamp override for tests. Defaults to the module
            clock (``time.monotonic``).

    Returns:
        A ``(send, suppressed)`` pair: ``send`` is True when the caller
        should emit the notification now, and ``suppressed`` is the number of
        alerts dropped during the previous window (always 0 when ``send`` is
        False, and on the very first alert for a key).
    """
    try:
        if status_code < _MIN_THROTTLED_STATUS:
            return False, 0
        key = (str(provider), int(status_code))
        interval = _alert_interval_seconds()
        current = _clock() if now is None else now

        with _lock:
            window = _state.get(key)
            if window is None or current >= window.next_allowed_at:
                suppressed = window.suppressed if window is not None else 0
                _state[key] = _Window(next_allowed_at=current + interval)
                return True, suppressed
            window.suppressed += 1
            return False, 0
    except Exception:  # noqa: BLE001 - alerting must never break the gateway
        # A bug in the throttle must not silently silence outage alerts: fall
        # back to the pre-throttle behavior (send now) and log a breadcrumb.
        logger.warning(
            "Gateway 5xx alert throttle failed for %s; sending unsuppressed",
            provider,
            exc_info=True,
        )
        return True, 0


def reset_alert_state_for_tests() -> None:
    """Clear the in-process quiet windows (test isolation only)."""
    with _lock:
        _state.clear()
