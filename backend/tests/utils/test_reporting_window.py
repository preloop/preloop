"""One reporting window: the bounds a digest section is allowed to be given."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from preloop.utils.reporting_window import (
    describe_window_duration,
    resolve_reporting_window,
)

WEEK = timedelta(days=7)
END = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
OFFSET = timezone(timedelta(hours=-5))


def _resolve(**overrides):
    values = {
        "start": None,
        "end": None,
        "now": None,
        "default_window": WEEK,
        "what": "digest window",
    }
    values.update(overrides)
    return resolve_reporting_window(**values)


def test_the_default_window_is_the_seven_days_before_now() -> None:
    assert _resolve(now=END) == (END - WEEK, END)


def test_the_default_window_ends_now_when_no_clock_is_given() -> None:
    window_start, window_end = _resolve()

    assert window_end - window_start == WEEK
    assert window_end.tzinfo is timezone.utc


def test_explicit_bounds_are_used_as_they_are() -> None:
    start = END - timedelta(days=2)

    assert _resolve(start=start, end=END) == (start, END)


@pytest.mark.parametrize(
    "bounds",
    [
        (END - timedelta(days=2), END),
        (
            (END - timedelta(days=2)).astimezone(OFFSET),
            END.astimezone(OFFSET),
        ),
        (
            (END - timedelta(days=2)).replace(tzinfo=None),
            END.replace(tzinfo=None),
        ),
    ],
    ids=["utc", "offset", "naive"],
)
def test_every_spelling_of_one_window_resolves_to_one_window(bounds) -> None:
    start, end = bounds

    window_start, window_end = _resolve(start=start, end=end)

    assert (window_start, window_end) == (END - timedelta(days=2), END)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start": END - timedelta(days=1)}, "start and end must be given together"),
        ({"end": END}, "start and end must be given together"),
        (
            {"start": END, "end": END - timedelta(days=1)},
            "end must be after start",
        ),
        ({"start": END, "end": END}, "end must be after start"),
        (
            {"start": END - timedelta(days=1), "end": END, "now": END},
            "now cannot be combined with start and end",
        ),
        ({"now": END, "default_window": timedelta(0)}, "must be positive"),
    ],
    ids=["no-end", "no-start", "reversed", "empty", "now-and-bounds", "no-default"],
)
def test_a_window_that_cannot_be_reported_is_refused(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        _resolve(**kwargs)


def test_the_error_names_the_window_that_was_asked_for() -> None:
    with pytest.raises(ValueError, match="spend outlier digest window"):
        resolve_reporting_window(
            start=END,
            end=END,
            now=None,
            default_window=WEEK,
            what="spend outlier digest window",
        )


@pytest.mark.parametrize(
    ("window", "phrase"),
    [
        (timedelta(days=7), "7 days"),
        (timedelta(days=1), "1 day"),
        (timedelta(days=1, hours=3), "1 day, 3 hours"),
        (timedelta(hours=1), "1 hour"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(seconds=1), "1 second"),
        (timedelta(seconds=30), "30 seconds"),
        (timedelta(days=2, hours=3, minutes=5), "2 days, 3 hours, 5 minutes"),
    ],
    ids=[
        "week",
        "day",
        "day-and-hours",
        "hour",
        "minute",
        "second",
        "seconds",
        "all-units",
    ],
)
def test_a_window_is_described_as_it_is(window, phrase) -> None:
    """A window that is not whole days is never rounded to whole days."""
    assert describe_window_duration(window) == phrase
