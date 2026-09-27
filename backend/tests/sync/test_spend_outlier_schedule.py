"""Scheduling and task wiring for spend outlier alerts (#960)."""

from datetime import datetime
from unittest.mock import MagicMock

import pytz

from preloop.sync import tasks
from preloop.sync.cli.scheduler_commands import (
    SPEND_OUTLIER_SESSION_CHECK_MINUTES,
    spend_outlier_daily_trigger,
)


def test_daily_pass_runs_after_the_utc_day_closes():
    """00:30 UTC every day, before the Monday 09:00 UTC digest."""
    trigger = spend_outlier_daily_trigger()
    now = datetime(2026, 9, 27, 0, 0, tzinfo=pytz.utc)

    fire = trigger.get_next_fire_time(None, now)

    assert fire == datetime(2026, 9, 27, 0, 30, tzinfo=pytz.utc)
    following = trigger.get_next_fire_time(fire, fire)
    assert following == datetime(2026, 9, 28, 0, 30, tzinfo=pytz.utc)


def test_session_check_is_periodic_not_per_request():
    """The session rule is polled, never evaluated on every token."""
    assert SPEND_OUTLIER_SESSION_CHECK_MINUTES >= 5


def test_tasks_are_dispatchable():
    """A worker pool enumerating subjects must receive both tasks."""
    assert "evaluate_spend_outliers" in tasks.DISPATCHABLE_TASKS
    assert "evaluate_spend_outlier_sessions" in tasks.DISPATCHABLE_TASKS


def test_tasks_close_the_session_and_swallow_errors(monkeypatch):
    """A failing pass is logged, returns None, and still closes the session."""
    db = MagicMock()
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([db]))

    def boom(session, now=None):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("preloop.services.spend_outliers.run_daily_pass", boom)
    monkeypatch.setattr("preloop.services.spend_outliers.run_session_pass", boom)

    assert tasks.evaluate_spend_outliers() is None
    assert tasks.evaluate_spend_outlier_sessions() is None
    assert db.close.call_count == 2


def test_tasks_return_pass_counts(monkeypatch):
    """The task returns what the pass reports."""
    db = MagicMock()
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([db]))
    monkeypatch.setattr(
        "preloop.services.spend_outliers.run_daily_pass",
        lambda session, now=None: {"accounts": 2, "findings": 1},
    )

    assert tasks.evaluate_spend_outliers() == {"accounts": 2, "findings": 1}
