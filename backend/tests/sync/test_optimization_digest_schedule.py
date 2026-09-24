"""The cost digest fires on Monday 09:00 UTC, never on process start."""

from datetime import datetime, timezone

from preloop.sync.cli.scheduler_commands import optimization_digest_trigger


def test_digest_trigger_is_next_monday_not_startup() -> None:
    """A Thursday afternoon start waits until Monday, and does not fire now."""
    trigger = optimization_digest_trigger()
    thursday = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)

    nxt = trigger.get_next_fire_time(None, thursday)

    assert nxt is not None
    assert nxt.weekday() == 0
    assert nxt.hour == 9
    assert nxt.minute == 0
    assert nxt > thursday
    assert nxt.date().isoformat() == "2026-09-28"


def test_digest_trigger_skips_a_monday_that_already_passed() -> None:
    """Restarting after Monday 09:00 waits until the following Monday."""
    trigger = optimization_digest_trigger()
    after_fire = datetime(2026, 9, 28, 9, 10, tzinfo=timezone.utc)

    nxt = trigger.get_next_fire_time(None, after_fire)

    assert nxt is not None
    assert nxt.date().isoformat() == "2026-10-05"
    assert nxt.hour == 9
