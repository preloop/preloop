"""Read the human estimate of a tracker issue, for cost comparisons.

The per-issue cost report puts the AI cost of a ticket next to the estimate
the team gave it. The estimate is only ever read from the tracker, never
derived: no hours-per-day or points-to-hours conversion, no default. When the
tracker says nothing, the estimate is empty.

Where an estimate comes from, in order (a part that is found stops the
search for that part):

* Jira ``timeoriginalestimate`` (seconds, "Original Estimate"), or
  ``timetracking.originalEstimateSeconds``: hours.
* GitLab ``time_estimate`` (webhook) or ``time_stats.time_estimate`` (API):
  hours.
* A configured field (``points_field``), for example a Jira story points
  custom field (``customfield_10016``) or GitLab ``weight``: points.
* A configured label prefix: ``hours_label_prefix`` (``estimate:`` reads
  ``estimate: 4h``) and ``points_label_prefix`` (``points:`` reads
  ``points: 3``). This is how GitHub issues carry an estimate.

Configuration lives on the tracker, under ``meta_data.issue_estimate``::

    {"points_field": "customfield_10016",
     "hours_label_prefix": "estimate:",
     "points_label_prefix": "points:"}

A native time estimate of zero seconds is how GitLab says "not set", so zero
seconds reads as no estimate on every tracker.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

#: Largest value that fits ``Numeric(10, 2)``.
MAX_ESTIMATE = Decimal("99999999.99")

_HOURS_LABEL = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)?\s*$", re.IGNORECASE
)
_POINTS_LABEL = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:p|pt|pts|point|points|sp)?\s*$", re.IGNORECASE
)
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


@dataclass(frozen=True)
class Estimate:
    """An issue estimate; a part is None when the tracker did not state it.

    Attributes:
        hours: Estimated hours.
        hours_source: Where ``hours`` was read, such as
            ``jira:timeoriginalestimate`` or ``label:estimate:``.
        points: Estimated points.
        points_source: Where ``points`` was read.
    """

    hours: Optional[Decimal] = None
    hours_source: Optional[str] = None
    points: Optional[Decimal] = None
    points_source: Optional[str] = None

    @property
    def empty(self) -> bool:
        """Whether neither part was found."""
        return self.hours is None and self.points is None


@dataclass(frozen=True)
class EstimateConfig:
    """Per-tracker estimate configuration (``meta_data.issue_estimate``)."""

    points_field: Optional[str] = None
    hours_label_prefix: Optional[str] = None
    points_label_prefix: Optional[str] = None


def estimate_config(raw: Any) -> EstimateConfig:
    """Parse a tracker's ``meta_data.issue_estimate``; ignore bad values.

    Args:
        raw: The configured dict, or anything else for no configuration.

    Returns:
        The configuration.
    """
    if not isinstance(raw, dict):
        return EstimateConfig()

    def text(key: str) -> Optional[str]:
        value = raw.get(key)
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value[:64] or None

    points_field = text("points_field")
    if points_field and not _FIELD_NAME.match(points_field):
        points_field = None
    return EstimateConfig(
        points_field=points_field,
        hours_label_prefix=text("hours_label_prefix"),
        points_label_prefix=text("points_label_prefix"),
    )


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        # Jira select-style custom fields carry the number as ``value``.
        return _decimal(value.get("value"))
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or number > MAX_ESTIMATE:
        return None
    return number.quantize(Decimal("0.01"))


def _hours_from_seconds(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return _decimal(round(seconds / 3600.0, 2))


def label_names(labels: Any) -> list[str]:
    """Label names from a tracker payload or a synced issue.

    Accepts plain strings, GitHub ``{"name": ...}`` and GitLab
    ``{"title": ...}`` objects.

    Args:
        labels: A list of labels, or anything else for none.

    Returns:
        The names, in order.
    """
    if not isinstance(labels, (list, tuple)):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            label = label.get("name") or label.get("title")
        if isinstance(label, str) and label.strip():
            names.append(label.strip())
    return names


def _from_labels(
    labels: Iterable[str], prefix: Optional[str], pattern: re.Pattern[str]
) -> Optional[Decimal]:
    """The one value labels with ``prefix`` state; None if none or conflicting."""
    if not prefix:
        return None
    wanted = prefix.lower()
    values: set[Decimal] = set()
    for label in labels:
        if not label.lower().startswith(wanted):
            continue
        match = pattern.match(label[len(prefix) :])
        if match is None:
            continue
        value = _decimal(match.group(1))
        if value is not None:
            values.add(value)
    # Two labels that disagree are not an estimate; picking one would be a guess.
    return values.pop() if len(values) == 1 else None


def _native_hours(
    tracker_type: str, fields: dict[str, Any]
) -> tuple[Optional[Decimal], Optional[str]]:
    if tracker_type == "jira":
        hours = _hours_from_seconds(fields.get("timeoriginalestimate"))
        if hours is not None:
            return hours, "jira:timeoriginalestimate"
        tracking = fields.get("timetracking")
        if isinstance(tracking, dict):
            hours = _hours_from_seconds(tracking.get("originalEstimateSeconds"))
            if hours is not None:
                return hours, "jira:timeoriginalestimate"
    if tracker_type == "gitlab":
        hours = _hours_from_seconds(fields.get("time_estimate"))
        if hours is None and isinstance(fields.get("time_stats"), dict):
            hours = _hours_from_seconds(fields["time_stats"].get("time_estimate"))
        if hours is not None:
            return hours, "gitlab:time_estimate"
    return None, None


def read_estimate(
    *,
    tracker_type: str,
    fields: Optional[dict[str, Any]],
    labels: Iterable[str],
    config: EstimateConfig,
) -> Estimate:
    """Read an issue's estimate from its tracker fields and labels.

    Args:
        tracker_type: ``jira``, ``github``, ``gitlab`` or another type.
        fields: Raw issue fields (Jira ``issue.fields``, a GitLab issue, or
            the ``estimate_fields`` the tracker sync stored).
        labels: Label names.
        config: The tracker's estimate configuration.

    Returns:
        The estimate; parts the tracker does not state are None.
    """
    tracker_type = (tracker_type or "").lower()
    fields = fields if isinstance(fields, dict) else {}
    labels = list(labels)
    hours, hours_source = _native_hours(tracker_type, fields)
    if hours is None:
        hours = _from_labels(labels, config.hours_label_prefix, _HOURS_LABEL)
        if hours is not None:
            hours_source = f"label:{config.hours_label_prefix}"
    points: Optional[Decimal] = None
    points_source: Optional[str] = None
    if config.points_field:
        points = _decimal(fields.get(config.points_field))
        if points is not None:
            points_source = f"{tracker_type or 'tracker'}:{config.points_field}"
    if points is None:
        points = _from_labels(labels, config.points_label_prefix, _POINTS_LABEL)
        if points is not None:
            points_source = f"label:{config.points_label_prefix}"
    return Estimate(
        hours=hours,
        hours_source=hours_source,
        points=points,
        points_source=points_source,
    )


#: Raw fields the tracker sync keeps on ``Issue.meta_data["estimate_fields"]``.
SYNCED_ESTIMATE_FIELDS = ("timeoriginalestimate", "time_estimate", "weight")


def synced_estimate_fields(issue_data: Any) -> dict[str, Any]:
    """The native estimate fields of a raw issue, for ``Issue.meta_data``.

    Only the raw values are kept; ``read_estimate`` interprets them with the
    tracker's current configuration.

    Args:
        issue_data: A raw tracker issue (Jira with ``fields``, GitLab
            attributes with ``time_stats``).

    Returns:
        The present fields; empty when the issue carries none.
    """
    if not isinstance(issue_data, dict):
        return {}
    sources: list[dict[str, Any]] = [issue_data]
    if isinstance(issue_data.get("fields"), dict):
        sources.insert(0, issue_data["fields"])
    stored: dict[str, Any] = {}
    for source in sources:
        for key in SYNCED_ESTIMATE_FIELDS:
            value = source.get(key)
            if (
                key not in stored
                and isinstance(value, (int, float, str))
                and not isinstance(value, bool)
            ):
                stored[key] = value
        stats = source.get("time_stats")
        if "time_estimate" not in stored and isinstance(stats, dict):
            value = stats.get("time_estimate")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                stored["time_estimate"] = value
    return stored
