"""Per-record-class retention settings for one account.

The reason this module exists is a floor, not a default. AI Act Art. 26(6)
asks a deployer to keep automatically generated logs for at least six months,
and DORA asks for record keeping over a comparable horizon. Before this,
audit rows, approvals, runtime sessions and usage rows had no retention job at
all (they lived until the account was purged) and evidence packs had a 30 day
operational window. "Forever" is not a retention policy any more than "30
days" is: the first is a liability nobody chose and the second is shorter than
the obligation.

So an account states, per record class, how long it keeps records, and the
platform refuses to be told anything under six months.

Precedence, most specific first:

1. ``account.meta_data["retention"][<class>]`` when the account sets one
2. ``settings.retention_default_days`` (365, twelve months)

and the result is clamped up to the effective floor, which is
``max(ABSOLUTE_FLOOR_DAYS, settings.retention_floor_days)``. The absolute floor
is a constant here rather than a setting because the regulation is the reason
it exists; the setting can only make a deployment stricter. This is the
mirror image of the account cap in
:mod:`preloop.services.approval_window`, where an account may only tighten a
ceiling.

What this module does NOT govern: how long the encrypted evidence payload is
kept. That stays ``FLOW_EVIDENCE_RETENTION_HOURS`` (30 days by default), an
operational window sized for review, not for archive. The ``evidence`` record
class here governs the ``flow_artifact`` row, its manifest and its digest.
Raising the payload window is a separate, deliberate storage decision, and a
legal hold pins the payload regardless. Said plainly in
``docs/guide/flows/evidence-storage.md`` rather than left to be discovered.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from preloop.config import settings

logger = logging.getLogger(__name__)

#: Account metadata bucket holding per-class retention, in whole days.
RETENTION_KEY = "retention"

#: Six months. Not a setting: this is the obligation the floor exists for.
#: 183 days is the longest six calendar months, so it cannot round short.
ABSOLUTE_FLOOR_DAYS = 183

#: Twelve months, used when neither the account nor the deployment says more.
FALLBACK_DEFAULT_DAYS = 365

#: An account that wants to keep records for longer than this should be
#: exporting them (see the period export), not asking the platform to hold a
#: growing table forever. Twenty years is past any retention obligation we
#: have been shown and still finite.
MAX_RETENTION_DAYS = 7300

#: Audit rows: permission checks, decisions, retention decisions themselves.
CLASS_AUDIT = "audit"
#: Approval requests and their events.
CLASS_APPROVALS = "approvals"
#: Evidence pack rows (manifest, digest, receipt metadata), not the payload.
CLASS_EVIDENCE = "evidence"
#: Runtime sessions and their activity.
CLASS_RUNTIME_SESSIONS = "runtime_sessions"
#: API and gateway usage rows.
CLASS_USAGE = "usage"

RECORD_CLASSES: tuple[str, ...] = (
    CLASS_AUDIT,
    CLASS_APPROVALS,
    CLASS_EVIDENCE,
    CLASS_RUNTIME_SESSIONS,
    CLASS_USAGE,
)

RECORD_CLASS_LABELS: dict[str, str] = {
    CLASS_AUDIT: "Audit log rows",
    CLASS_APPROVALS: "Approval requests and their events",
    CLASS_EVIDENCE: "Evidence pack records (manifest and digest)",
    CLASS_RUNTIME_SESSIONS: "Runtime sessions and session activity",
    CLASS_USAGE: "API and gateway usage rows",
}


class RetentionFloorError(ValueError):
    """Raised when a requested retention is below the effective floor."""

    def __init__(self, record_class: str, requested_days: int, floor_days: int) -> None:
        super().__init__(
            f"retention for {record_class!r} must be at least {floor_days} days "
            f"(six months); {requested_days} was requested"
        )
        self.record_class = record_class
        self.requested_days = requested_days
        self.floor_days = floor_days


@dataclass(frozen=True)
class RetentionSetting:
    """The resolved retention for one record class of one account."""

    record_class: str
    days: int
    #: ``account`` when the account set it, ``default`` otherwise.
    source: str
    #: True when the stored/requested value was raised to meet the floor.
    floored: bool = False

    def describe(self) -> str:
        """Operator-facing sentence naming the setting that produced it."""
        names = {
            "account": "this account's retention setting",
            "default": "the deployment default retention",
        }
        text = f"{self.days}d ({names.get(self.source, self.source)})"
        return f"{text}, raised to the floor" if self.floored else text


def floor_days() -> int:
    """Effective floor: the constant, or a stricter deployment setting."""
    try:
        configured = int(settings.retention_floor_days)
    except (TypeError, ValueError):
        configured = ABSOLUTE_FLOOR_DAYS
    return max(ABSOLUTE_FLOOR_DAYS, configured)


def default_days() -> int:
    """Deployment default, never below the floor."""
    try:
        configured = int(settings.retention_default_days)
    except (TypeError, ValueError):
        configured = FALLBACK_DEFAULT_DAYS
    if configured <= 0:
        configured = FALLBACK_DEFAULT_DAYS
    return max(floor_days(), min(MAX_RETENTION_DAYS, configured))


def _coerce_days(value: Any) -> Optional[int]:
    """Positive int or None. A garbage setting must not decide retention."""
    if value is None or isinstance(value, bool):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


def normalize_retention_store(meta_data: Optional[Mapping[str, Any]]) -> dict[str, int]:
    """Read the account bucket, keeping only known classes and sane values.

    Values are returned as stored. Clamping happens in
    :func:`resolve_retention`, so a caller can still see that a stored value
    is under the floor (and the API can refuse to write one).
    """
    raw = (meta_data or {}).get(RETENTION_KEY)
    if not isinstance(raw, Mapping):
        return {}
    store: dict[str, int] = {}
    for record_class in RECORD_CLASSES:
        days = _coerce_days(raw.get(record_class))
        if days is not None:
            store[record_class] = min(MAX_RETENTION_DAYS, days)
    return store


def resolve_retention(
    meta_data: Optional[Mapping[str, Any]], *, record_class: str
) -> RetentionSetting:
    """Retention for one class: the account's value, floored, else the default."""
    if record_class not in RECORD_CLASSES:
        raise ValueError(f"unknown record class {record_class!r}")
    minimum = floor_days()
    stored = normalize_retention_store(meta_data).get(record_class)
    if stored is None:
        return RetentionSetting(
            record_class=record_class, days=default_days(), source="default"
        )
    days = max(minimum, stored)
    if days != stored:
        # Reachable when the deployment raised its floor after the account
        # chose, or when a row was written by hand. The floor wins; it is the
        # only reason this module exists.
        logger.info(
            "Retention %sd for %s raised to the %sd floor",
            stored,
            record_class,
            days,
        )
    return RetentionSetting(
        record_class=record_class,
        days=days,
        source="account",
        floored=days != stored,
    )


def resolve_all(meta_data: Optional[Mapping[str, Any]]) -> dict[str, RetentionSetting]:
    """Resolved retention for every record class."""
    return {
        record_class: resolve_retention(meta_data, record_class=record_class)
        for record_class in RECORD_CLASSES
    }


def validate_retention_request(values: Mapping[str, Any]) -> dict[str, int]:
    """Sanitize an API payload, refusing anything under the floor.

    A value of ``None`` clears the account's setting for that class (it falls
    back to the deployment default, which is itself at or above the floor).
    Unknown classes are refused rather than ignored: silently dropping a key
    the caller named would let an operator believe they had set something.
    """
    minimum = floor_days()
    cleaned: dict[str, int] = {}
    for key, value in values.items():
        if key not in RECORD_CLASSES:
            raise ValueError(f"unknown record class {key!r}")
        if value is None:
            continue
        days = _coerce_days(value)
        if days is None:
            raise ValueError(f"retention for {key!r} must be a positive number of days")
        if days < minimum:
            raise RetentionFloorError(key, days, minimum)
        cleaned[key] = min(MAX_RETENTION_DAYS, days)
    return cleaned


def set_retention(
    meta_data: Optional[Mapping[str, Any]],
    *,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Return account metadata with the retention bucket replaced.

    ``values`` is the full desired state: a class the caller omits or sets to
    ``None`` goes back to the deployment default. Every other metadata key is
    left untouched, because this bucket shares ``meta_data`` with subject
    governance and the approval window cap.
    """
    cleaned = validate_retention_request(values)
    updated = deepcopy(dict(meta_data or {}))
    if cleaned:
        updated[RETENTION_KEY] = cleaned
    else:
        updated.pop(RETENTION_KEY, None)
    return updated
