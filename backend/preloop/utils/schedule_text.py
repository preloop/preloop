"""The one renderer for schedule phrases such as "Daily at 09:00 (Europe/Berlin)".

Two callers need the same words: the pydantic schedule forms in
``preloop.models.schemas.flow`` (``ScheduleConfig.describe()``, which the flow
editor and the schedule preview endpoint show) and the executions list, which
only has the stored ``schedule_config.model_dump()`` dict from the trigger
payload (``preloop.sync.event_normalizer.describe_schedule``). Keeping the
strings here, in a leaf module that imports nothing from ``preloop``, means a
change to a schedule form cannot make the editor and the run list disagree, and
it stays clear of the import cycle a dict-only caller would otherwise create by
importing the models package.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

# Canonical weekday order for weekly schedules (APScheduler abbreviations).
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

WEEKDAY_LABELS: Dict[str, str] = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}


def describe_cron(expr: str, timezone: str) -> str:
    """Phrase for a raw crontab expression."""
    return f"Cron '{expr}' ({timezone})"


def describe_interval(every: int, unit: str) -> str:
    """Phrase for "every N units", singularised at one."""
    unit = str(unit)
    if every == 1 and unit.endswith("s"):
        unit = unit[:-1]
    return f"Every {every} {unit}"


def describe_daily(at: str, timezone: str) -> str:
    """Phrase for a once-a-day schedule."""
    return f"Daily at {at} ({timezone})"


def describe_weekly(days: Iterable[Any], at: str, timezone: str) -> str:
    """Phrase for a weekday schedule, days in the order they are given."""
    labels = ", ".join(WEEKDAY_LABELS.get(str(day), str(day)) for day in days)
    return f"Weekly on {labels} at {at} ({timezone})"


def describe_schedule_config(schedule: Any) -> Optional[str]:
    """Render a stored schedule config dict, or None when it is not one.

    Args:
        schedule: The stored schedule config dict (any of the four forms), or
            the legacy ``{"cron": ...}`` shape.

    Returns:
        The same phrase ``ScheduleConfig.describe()`` returns for that form, or
        None when the config is missing or in a shape this function does not
        know.
    """
    if not isinstance(schedule, dict):
        return None
    timezone = schedule.get("timezone") or "UTC"
    schedule_type = schedule.get("type")
    if schedule_type is None and schedule.get("cron"):
        schedule_type = "cron"
        schedule = {**schedule, "expr": schedule["cron"]}

    if schedule_type == "cron" and schedule.get("expr"):
        return describe_cron(schedule["expr"], timezone)
    if schedule_type == "interval" and schedule.get("every") and schedule.get("unit"):
        return describe_interval(schedule["every"], schedule["unit"])
    if schedule_type == "daily" and schedule.get("at"):
        return describe_daily(schedule["at"], timezone)
    if schedule_type == "weekly" and schedule.get("at") and schedule.get("days"):
        return describe_weekly(schedule["days"], schedule["at"], timezone)
    return None
