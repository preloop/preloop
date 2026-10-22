"""Resolve media time independently for each recording, without clamping.

Browser occurred_at is the agent clock; activity timestamps for model/tool
rows are the server clock. Only server-to-agent conversion uses median skew.
This pure resolver is also used by evidence exports.
"""

from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import media_sync as crud_media_sync
from preloop.schemas.media_timing import (
    MediaSyncMapping,
    MediaSyncOut,
    MediaTiming,
    RecordingSync,
)


def _time(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def resolve_recordings(
    recordings: Iterable[Any], activities: Iterable[Any]
) -> MediaSyncOut:
    """Apply exact, same-clock, then corrected server-time precedence.

    An invalid higher-trust offset stays unmapped; it never falls through
    to a less trustworthy rule. Unknown duration is bounded by the player
    once the actual media duration is available.
    """
    rows = list(activities)
    samples = []
    for row in rows:
        meta = row.metadata_ or {}
        if (
            row.activity_type != "browser_step"
            or (meta.get("clock") or "agent") != "agent"
        ):
            continue
        occurred, recorded = (
            _time(meta.get("occurred_at")),
            _time(meta.get("recorded_at")),
        )
        if occurred is not None and recorded is not None:
            samples.append((recorded - occurred).total_seconds() * 1000)
    enough = len(samples) >= 3
    skew_ms = round(median(samples)) if enough else 0
    results = []
    for artifact in recordings:
        result = RecordingSync(
            artifact_id=str(artifact.id),
            skew_ms=skew_ms,
            mappings=[],
            unmapped_activity_ids=[],
        )
        results.append(result)
        if artifact.media_timing is None or artifact.availability != "available":
            result.unmapped_activity_ids = [str(row.id) for row in rows]
            continue
        timing = MediaTiming.model_validate(artifact.media_timing)
        exact = {
            step.source_step_id: step.offset_ms for step in timing.step_offsets or []
        }
        for row in rows:
            meta = row.metadata_ or {}
            step_id = meta.get("source_step_id")
            occurred = _time(meta.get("occurred_at"))
            browser = row.activity_type == "browser_step"
            approximate = False
            if browser and step_id in exact:
                offset, rule = exact[step_id], "exact"
            elif (
                browser
                and occurred is not None
                and timing.clock == (meta.get("clock") or "agent")
            ):
                offset = round((occurred - timing.started_at).total_seconds() * 1000)
                rule = "occurred_at"
            else:
                stamp = _time(meta.get("recorded_at") if browser else row.timestamp)
                if stamp is None:
                    result.unmapped_activity_ids.append(str(row.id))
                    continue
                correction = skew_ms if timing.clock == "agent" else 0
                offset = (
                    round((stamp - timing.started_at).total_seconds() * 1000)
                    - correction
                )
                rule = "server_skew"
                approximate = not enough
            if offset < 0 or (
                timing.duration_ms is not None and offset > timing.duration_ms
            ):
                result.unmapped_activity_ids.append(str(row.id))
                continue
            result.mappings.append(
                MediaSyncMapping(
                    activity_id=str(row.id),
                    activity_type=row.activity_type,
                    api_usage_id=meta.get("api_usage_id"),
                    offset_ms=offset,
                    rule=rule,
                    approximate=approximate,
                )
            )
        result.mappings.sort(
            key=lambda mapping: (mapping.offset_ms, mapping.activity_id)
        )
    return MediaSyncOut(recordings=results)


def session_media_sync(
    db: Session, *, account_id: UUID, session_id: UUID
) -> MediaSyncOut:
    """Resolve an authorized session using metadata-only CRUD reads."""
    return resolve_recordings(
        crud_media_sync.recording_rows(
            db, account_id=account_id, session_id=session_id
        ),
        crud_media_sync.timing_rows(db, account_id=account_id, session_id=session_id),
    )
