"""Bound the model stream idle wait for flow runs.

A custom-provider Codex call waits ``stream_idle_timeout_ms`` for the next
byte of a streamed response before it treats the stream as dropped, then
reconnects up to ``stream_max_retries`` times inside the same call. With the
old fixed 600 second wait, one silent stream could spend most of a 900 or
1800 second flow budget (issue #872).

This module owns the per-flow bound,
``agent_config.stream_idle_timeout_seconds``, and the rule that keeps it
inside the flow's own timeout budget.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

#: Key inside ``agent_config`` that sets the per-flow bound.
STREAM_IDLE_TIMEOUT_CONFIG_KEY = "stream_idle_timeout_seconds"
#: The wait every custom-provider Codex run used before this was configurable.
STREAM_IDLE_TIMEOUT_DEFAULT_SECONDS = 600
#: Below this a slow first token from a live reasoning model reads as a drop.
STREAM_IDLE_TIMEOUT_MIN_SECONDS = 30
#: Above this the bound stops being a bound.
STREAM_IDLE_TIMEOUT_MAX_SECONDS = 3600

#: Line the Codex script prints with the bound it wrote into config.toml.
STREAM_IDLE_TIMEOUT_LOG_PREFIX = "PRELOOP_STREAM_IDLE_TIMEOUT_SECONDS="


def _as_seconds(value: Any) -> Optional[int]:
    """Return ``value`` as whole seconds, or None when it is not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def validate_stream_idle_timeout(value: Any) -> None:
    """Reject a configured bound that is not whole seconds in range.

    Args:
        value: ``agent_config.stream_idle_timeout_seconds`` as submitted.

    Raises:
        ValueError: When the value is not an integer within
            [STREAM_IDLE_TIMEOUT_MIN_SECONDS, STREAM_IDLE_TIMEOUT_MAX_SECONDS].
    """
    seconds = _as_seconds(value)
    if (
        seconds is None
        or seconds < STREAM_IDLE_TIMEOUT_MIN_SECONDS
        or seconds > STREAM_IDLE_TIMEOUT_MAX_SECONDS
    ):
        raise ValueError(
            f"agent_config.{STREAM_IDLE_TIMEOUT_CONFIG_KEY} must be a whole "
            f"number of seconds between {STREAM_IDLE_TIMEOUT_MIN_SECONDS} and "
            f"{STREAM_IDLE_TIMEOUT_MAX_SECONDS}"
        )


def resolve_stream_idle_timeout_seconds(
    agent_config: Any, flow_timeout_seconds: Any = None
) -> int:
    """Return the stream idle wait this run should use.

    The flow's ``agent_config.stream_idle_timeout_seconds`` wins; without it
    the previous 600 second wait applies. Either way the wait is capped at
    half the run's timeout budget. A wait as long as the whole budget can
    never fire: the run is stopped first, Codex never reconnects, and the log
    never names the stall. Half leaves room for at least one reconnect. The
    cap only changes budgets under 1200 seconds when nothing is configured.

    Args:
        agent_config: The flow's ``agent_config`` (any shape).
        flow_timeout_seconds: Wall-clock budget of this run, when known.

    Returns:
        Whole seconds, never below STREAM_IDLE_TIMEOUT_MIN_SECONDS.
    """
    seconds = STREAM_IDLE_TIMEOUT_DEFAULT_SECONDS
    configured = (
        agent_config.get(STREAM_IDLE_TIMEOUT_CONFIG_KEY)
        if isinstance(agent_config, Mapping)
        else None
    )
    if configured is not None:
        try:
            validate_stream_idle_timeout(configured)
            seconds = int(configured)
        except ValueError:
            logger.warning(
                "Ignoring agent_config.%s=%r; using %ss",
                STREAM_IDLE_TIMEOUT_CONFIG_KEY,
                configured,
                seconds,
            )
    budget = _as_seconds(flow_timeout_seconds)
    if budget is not None and budget > 0:
        seconds = min(seconds, budget // 2)
    return max(STREAM_IDLE_TIMEOUT_MIN_SECONDS, seconds)

