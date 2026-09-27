"""Bound the model stream idle wait and name a run that timed out on it.

A custom-provider Codex call waits ``stream_idle_timeout_ms`` for the next
byte of a streamed response before it treats the stream as dropped, then
reconnects up to ``stream_max_retries`` times inside the same call. With the
old fixed 600 second wait, one silent stream could spend most of a 900 or
1800 second flow budget, and the execution only said that it timed out
(issue #872).

This module owns two things:

* The per-flow bound, ``agent_config.stream_idle_timeout_seconds``, and the
  rule that keeps it inside the flow's own timeout budget.
* Reading a timed-out run's output for evidence that the model stream was
  still silent when the budget ran out, so the execution can say so.

The evidence strings were captured from the pinned Codex CLI (0.153.4)
against a local server that accepts the request and never sends a byte:

.. code-block:: text

    WARN codex_core::responses_retry: stream disconnected - retrying sampling
    request (1/2 in 212ms)... retries=1 max_retries=2 sampling_error=stream
    disconnected before completion: idle timeout waiting for SSE
    ERROR: Reconnecting... 1/2
    ERROR: stream disconnected before completion: idle timeout waiting for SSE

The ``WARN`` line only appears when ``RUST_LOG`` enables
``codex_core::responses_retry``, which the Codex script does. Without it, the
``Reconnecting`` lines carry no reason and the idle reason is only printed
once every retry is spent.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: Key inside ``agent_config`` that sets the per-flow bound.
STREAM_IDLE_TIMEOUT_CONFIG_KEY = "stream_idle_timeout_seconds"
#: The wait every custom-provider Codex run used before this was configurable.
STREAM_IDLE_TIMEOUT_DEFAULT_SECONDS = 600
#: Below this a slow first token from a live reasoning model reads as a drop.
STREAM_IDLE_TIMEOUT_MIN_SECONDS = 30
#: Above this the bound stops being a bound.
STREAM_IDLE_TIMEOUT_MAX_SECONDS = 3600

#: ``failure_category`` for a run that timed out on a silent model stream.
#: Mirrors ``FAILURE_CATEGORY_MODEL_STREAM_IDLE`` in flow_failure_category.
STALL_REASON_MODEL_STREAM_IDLE = "model_stream_idle"

#: Key the timed-out execution's ``result`` carries the stall under.
STREAM_STALL_RESULT_KEY = "stream_stall"

#: Line the Codex script prints with the bound it wrote into config.toml.
STREAM_IDLE_TIMEOUT_LOG_PREFIX = "PRELOOP_STREAM_IDLE_TIMEOUT_SECONDS="

#: Sentence the timeout message carries when the stall is the cause. The
#: failure-category classifier keys off it, ahead of the plain timeout rule.
STALL_MESSAGE_MARKER = "while waiting on a silent model stream"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_IDLE_REASON_RE = re.compile(
    r"idle timeout waiting for (?:sse|websocket)", re.IGNORECASE
)
_RETRY_LINE_RE = re.compile(r"retrying sampling request", re.IGNORECASE)
# Headers codex exec prints when the model actually produced something: an
# agent message, a command, reasoning, or the per-turn usage footer. One of
# these after the last idle signal means the stream recovered.
_MODEL_ACTIVITY_LINES = frozenset({"codex", "exec", "thinking", "tokens used"})
_BOUND_LINE_RE = re.compile(
    r"^" + re.escape(STREAM_IDLE_TIMEOUT_LOG_PREFIX) + r"(\d+)$"
)
_MAX_SIGNAL_CHARS = 300


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


@dataclass(frozen=True)
class StreamStall:
    """Evidence that a run was waiting on a silent model stream.

    Attributes:
        idle_reconnects: Times Codex reconnected after an idle stream.
        retries_exhausted: Codex spent every reconnect on an idle stream
            and gave up on that call.
        stream_idle_timeout_seconds: The bound the run was launched with,
            when its log recorded it.
        last_signal: The last idle line, truncated.
    """

    idle_reconnects: int
    retries_exhausted: bool
    stream_idle_timeout_seconds: Optional[int]
    last_signal: str

    def as_result(self) -> dict[str, Any]:
        """Structured form stored under ``result.stream_stall``."""
        return {
            "reason": STALL_REASON_MODEL_STREAM_IDLE,
            "idle_reconnects": self.idle_reconnects,
            "retries_exhausted": self.retries_exhausted,
            "stream_idle_timeout_seconds": self.stream_idle_timeout_seconds,
            "last_signal": self.last_signal,
        }

    def timeout_message(self, seconds: int, budget_label: str) -> str:
        """Failure message for a run whose budget ran out on this stall.

        Args:
            seconds: The budget that expired.
            budget_label: Which budget it was, e.g. "this flow's timeout
                budget".

        Returns:
            Operator-facing message. Carries STALL_MESSAGE_MARKER.
        """
        wait = (
            f"{self.stream_idle_timeout_seconds} seconds"
            if self.stream_idle_timeout_seconds
            else "the stream idle timeout"
        )
        if self.idle_reconnects == 0:
            # Only the terminal line was seen (no retry lines in the log).
            reconnects = "Codex gave up on the call after the stream sent nothing"
        else:
            times = (
                "once" if self.idle_reconnects == 1 else f"{self.idle_reconnects} times"
            )
            reconnects = f"Codex reconnected {times} after the stream sent nothing"
            if self.retries_exhausted:
                reconnects += ", then gave up on the call"
        return (
            f"Execution timed out after {seconds} seconds ({budget_label}) "
            f"{STALL_MESSAGE_MARKER}. The model provider sent nothing for "
            f"{wait} at a time. {reconnects}. Lower "
            f"agent_config.{STREAM_IDLE_TIMEOUT_CONFIG_KEY} to give up on a "
            "silent stream sooner, or use a different model or provider. "
            "Raising timeout_seconds only helps if the provider answers."
        )


def _clean(line: Any) -> str:
    return _ANSI_RE.sub("", str(line)).strip()


def detect_stream_stall(lines: Iterable[Any]) -> Optional[StreamStall]:
    """Return the stall a timed-out run was in, or None.

    A run counts as stalled when its output has a stream idle signal and the
    model produced nothing after the last one. A run that stalled, recovered,
    and then ran out of time doing real work is not a stall.

    Args:
        lines: The run's agent output, oldest first.

    Returns:
        The stall evidence, or None when the log does not show one.
    """
    idle_reconnects = 0
    retries_exhausted = False
    bound: Optional[int] = None
    last_signal = ""
    active_since_signal = False
    for raw in lines:
        line = _clean(raw)
        if not line:
            continue
        bound_match = _BOUND_LINE_RE.match(line)
        if bound_match:
            bound = int(bound_match.group(1))
            continue
        if _IDLE_REASON_RE.search(line):
            if _RETRY_LINE_RE.search(line):
                idle_reconnects += 1
            else:
                retries_exhausted = True
            last_signal = line[:_MAX_SIGNAL_CHARS]
            active_since_signal = False
            continue
        if line.lower() in _MODEL_ACTIVITY_LINES:
            active_since_signal = True
            # Exhaustion belongs to the call that ended; a later call that
            # produced output starts clean.
            retries_exhausted = False
            idle_reconnects = 0
    if not last_signal or active_since_signal:
        return None
    return StreamStall(
        idle_reconnects=idle_reconnects,
        retries_exhausted=retries_exhausted,
        stream_idle_timeout_seconds=bound,
        last_signal=last_signal,
    )
