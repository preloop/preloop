"""One explicit reporting window for digest sections and summaries.

Every report covers one half-open ``[start, end)`` window of UTC time. A
builder that defaults to "the last seven days" also takes explicit ``start``
and ``end`` bounds, so a caller whose window is not seven days does not have
to be described by a rounding of it.

Combinations that cannot mean one window are rejected instead of guessed at:

* only ``start`` or only ``end``: there is no window without both bounds
* ``start`` at or after ``end``: there is no span to report on
* explicit bounds together with ``now``: ``now`` is what the default window
  is measured back from, so combining them is ambiguous

The bounds are normalized to aware UTC. A naive value keeps the documented
compatibility with the rest of this repository and is read as UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

#: Units the window description is broken down into, longest first.
_WINDOW_UNITS: Tuple[Tuple[str, int], ...] = (
    ("day", 86400),
    ("hour", 3600),
    ("minute", 60),
)


def as_utc(value: datetime) -> datetime:
    """One instant as aware UTC; a naive value is read as UTC.

    Args:
        value: Aware or naive (implicitly UTC) timestamp.

    Returns:
        The same instant as an aware UTC datetime.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_reporting_window(
    *,
    start: Optional[datetime],
    end: Optional[datetime],
    now: Optional[datetime],
    default_window: timedelta,
    what: str = "reporting window",
) -> Tuple[datetime, datetime]:
    """The half-open ``[start, end)`` window a report covers.

    Args:
        start: Explicit window start, or None to use the default window.
        end: Explicit exclusive window end, or None to use the default
            window.
        now: End of the default window; the current time when omitted.
        default_window: Length of the window used without explicit bounds.
        what: Subject named in the error messages.

    Returns:
        ``(start, end)`` as aware UTC, with ``start`` before ``end``.

    Raises:
        ValueError: Only one bound was given, the bounds describe an empty or
            reversed window, explicit bounds were combined with ``now``, or
            ``default_window`` is not positive.
    """
    if (start is None) != (end is None):
        raise ValueError(f"{what}: start and end must be given together")
    if start is not None and end is not None and now is not None:
        raise ValueError(f"{what}: now cannot be combined with start and end")
    if start is None or end is None:
        if default_window <= timedelta(0):
            raise ValueError(f"{what}: the default window must be positive")
        moment = as_utc(now) if now is not None else datetime.now(timezone.utc)
        return moment - default_window, moment
    window_start = as_utc(start)
    window_end = as_utc(end)
    if window_end <= window_start:
        raise ValueError(f"{what}: end must be after start")
    return window_start, window_end


def describe_window_duration(window: timedelta) -> str:
    """A window length as words, e.g. ``7 days`` or ``1 day, 3 hours``.

    Reports the window the caller asked for rather than rounding it to whole
    days, which would describe a two-day-and-three-hour window as two days.

    Args:
        window: Length of the window.

    Returns:
        The length as a short human phrase.
    """
    seconds = max(0, int(window.total_seconds()))
    parts: List[str] = []
    remainder = seconds
    for unit, size in _WINDOW_UNITS:
        value, remainder = divmod(remainder, size)
        if value:
            parts.append(f"{value} {unit}" if value == 1 else f"{value} {unit}s")
    if not parts:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    return ", ".join(parts)
