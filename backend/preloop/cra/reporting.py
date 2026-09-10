"""CRA Article 14 reporting block: the judgement and the clock.

Article 14 obliges a manufacturer to report an *actively exploited*
vulnerability in its product: an early warning within 24 hours of becoming
aware of it, a vulnerability notification within 72 hours, and a final
report within 14 days. The obligation applies from 11 September 2026.

Before this module a CRA run produced ``art14_candidates``, a list of CVE
ids that appear in the CISA KEV catalogue. KEV membership is evidence that
a vulnerability is exploited somewhere; it is not a finding that this
product is affected, and it carries no deadline. An operator reading the
result could see "these components are on KEV" and still not know whether
anything is due in the next 24 hours.

The ``reporting`` block closes that gap deterministically:

* ``actively_exploited`` comes from the exploitation evidence (KEV, or a
  vendor advisory the run actually read).
* ``affected`` comes from VEX or reachability analysis, with the source
  named, and is ``"undetermined"`` when neither answered.
* ``reportable`` is exactly ``actively_exploited AND affected is True``.
* ``deadlines`` is arithmetic on ``discovered_at``, in UTC.

Nothing here decides anything legal, and nothing here files anything.
Preloop does not build a submission client for the ENISA single reporting
platform: the filing decision and the filing itself stay with the
manufacturer. This block tells the operator that the clock is running and
when it stops.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from preloop.cra.schemas import (
    ART14_DEADLINE_KEYS,
    ART14_EARLY_WARNING,
    ART14_FINAL_REPORT,
    ART14_NOTIFICATION,
)

# Tolerance when reconciling a submitted deadline against the computed one.
# Agents serialize timestamps at second precision, so an exact string match
# would reject correct arithmetic over a formatting difference.
DEADLINE_TOLERANCE = timedelta(seconds=1)

_DEADLINE_OFFSETS: Mapping[str, timedelta] = {
    "early_warning_24h": ART14_EARLY_WARNING,
    "notification_72h": ART14_NOTIFICATION,
    "final_report_14d": ART14_FINAL_REPORT,
}


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp into an aware UTC datetime.

    A naive timestamp is read as UTC rather than rejected: the alternative
    is discarding a usable discovery time over a missing "Z", and the
    deadline arithmetic is the point of the field.

    Args:
        value: Candidate timestamp; anything that is not a string is None.

    Returns:
        An aware ``datetime`` in UTC, or ``None`` when unparseable.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """Render an aware datetime as a UTC ISO 8601 string with a Z suffix."""
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def article14_deadlines(discovered_at: Any) -> Optional[dict[str, str]]:
    """Return the three Article 14 deadlines for a discovery time.

    Args:
        discovered_at: ISO 8601 string or aware/naive ``datetime``.

    Returns:
        ``{"early_warning_24h", "notification_72h", "final_report_14d"}`` as
        UTC ISO 8601 strings, or ``None`` when the discovery time is
        unusable. The clock starts at awareness, so every offset is measured
        from the same instant rather than chained one after another.
    """
    start = (
        discovered_at
        if isinstance(discovered_at, datetime)
        else parse_timestamp(discovered_at)
    )
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    return {
        key: format_timestamp(start + offset)
        for key, offset in _DEADLINE_OFFSETS.items()
    }


def earliest_discovery(*candidates: Any) -> Optional[str]:
    """Return the earliest parseable timestamp among the arguments.

    Awareness starts the first time the organisation saw the vulnerability,
    not the first time this particular run saw it, so a candidate that also
    appears in the drift baseline keeps the baseline's older timestamp. A
    later re-run must never restart the clock.
    """
    parsed = [ts for ts in (parse_timestamp(item) for item in candidates) if ts]
    if not parsed:
        return None
    return format_timestamp(min(parsed))


def deadline_mismatches(
    discovered_at: Any, deadlines: Any
) -> list[tuple[str, Optional[str], str]]:
    """Reconcile submitted deadlines against the computed ones.

    Args:
        discovered_at: The candidate's discovery timestamp.
        deadlines: The submitted ``deadlines`` object.

    Returns:
        One ``(key, submitted, expected)`` tuple per key that is missing,
        unparseable, or more than :data:`DEADLINE_TOLERANCE` away from the
        arithmetic. An empty list means the clock is correct.
    """
    expected = article14_deadlines(discovered_at)
    if expected is None:
        return []
    if not isinstance(deadlines, Mapping):
        return [(key, None, value) for key, value in expected.items()]
    problems: list[tuple[str, Optional[str], str]] = []
    for key in ART14_DEADLINE_KEYS:
        want = parse_timestamp(expected[key])
        got_raw = deadlines.get(key)
        got = parse_timestamp(got_raw)
        if want is None:
            continue
        if got is None or abs(got - want) > DEADLINE_TOLERANCE:
            problems.append(
                (
                    key,
                    got_raw if isinstance(got_raw, str) else None,
                    expected[key],
                )
            )
    return problems


def kev_finding_ids(findings: Sequence[Any]) -> list[str]:
    """Ids of findings flagged as KEV-listed, in delivery order."""
    ids: list[str] = []
    for item in findings:
        if not isinstance(item, Mapping):
            continue
        if item.get("kev") is not True:
            continue
        finding_id = item.get("id")
        if isinstance(finding_id, str) and finding_id:
            ids.append(finding_id)
    return ids


def reportable_candidates(reporting: Any) -> list[Mapping[str, Any]]:
    """Candidates the run itself marked reportable.

    Read by the event emitter, so it is deliberately literal: the block says
    ``reportable: true`` or the platform does not emit an event for it.
    """
    if not isinstance(reporting, Mapping):
        return []
    candidates = reporting.get("candidates")
    if not isinstance(candidates, list):
        return []
    out: list[Mapping[str, Any]] = []
    for item in candidates:
        if isinstance(item, Mapping) and item.get("reportable") is True:
            out.append(item)
    return out
