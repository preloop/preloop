"""The editor phrase and the executions-list phrase come from one renderer."""

import pytest

from preloop.models.schemas.flow import (
    CronSchedule,
    DailySchedule,
    IntervalSchedule,
    WeeklySchedule,
)
from preloop.sync.event_normalizer import describe_schedule
from preloop.utils.schedule_text import describe_schedule_config

SCHEDULES = [
    CronSchedule(expr="0 9 * * 1", timezone="UTC"),
    IntervalSchedule(every=1, unit="hours"),
    IntervalSchedule(every=6, unit="hours"),
    IntervalSchedule(every=2, unit="days", timezone="Europe/Berlin"),
    DailySchedule(at="09:00", timezone="Europe/Berlin"),
    WeeklySchedule(days=["mon", "wed"], at="18:30", timezone="UTC"),
    WeeklySchedule(days=["sun"], at="00:15", timezone="America/New_York"),
]


@pytest.mark.parametrize("schedule", SCHEDULES, ids=lambda s: s.type)
def test_stored_config_reads_exactly_like_the_editor(schedule):
    """A run's schedule phrase matches the form's own describe(), field for field.

    The executions list only has ``schedule_config.model_dump()``; the editor
    has the pydantic form. If a schedule form ever changes its wording without
    the dict renderer following, this fails.
    """
    assert describe_schedule_config(schedule.model_dump(mode="json")) == (
        schedule.describe()
    )
    assert describe_schedule(schedule.model_dump(mode="json")) == schedule.describe()


def test_every_schedule_form_is_covered():
    """A new schedule form has to be added here, not silently left unrendered."""
    from preloop.models.schemas.flow import ScheduleBase

    covered = {type(s).__name__ for s in SCHEDULES}
    known = {cls.__name__ for cls in ScheduleBase.__subclasses__()}
    assert known == covered, f"schedule forms without a phrase test: {known - covered}"


def test_unknown_shapes_render_nothing():
    """A missing or unrecognised config degrades to no phrase, never a guess."""
    assert describe_schedule_config(None) is None
    assert describe_schedule_config({}) is None
    assert describe_schedule_config({"type": "daily"}) is None
    assert describe_schedule_config({"type": "weekly", "at": "09:00"}) is None
