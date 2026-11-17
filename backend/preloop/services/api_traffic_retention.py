"""Retention for REST traffic rollups and problem events."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_traffic

#: Problem events are kept at least this long, whatever the setting says.
MIN_EVENT_RETENTION_DAYS = 7
DEFAULT_EVENT_RETENTION_DAYS = 30
DEFAULT_ROLLUP_RETENTION_DAYS = 90


def _days(name: str, default: int, minimum: int = 1) -> int:
    """Read a retention setting in days, never below ``minimum``."""
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        value = default
    return max(minimum, value)


def prune_api_traffic(db: Session, *, now: Optional[datetime] = None) -> dict[str, int]:
    """Delete traffic rows older than their retention, in bounded batches.

    ``API_TRAFFIC_EVENT_RETENTION_DAYS`` (default 30, minimum 7) and
    ``API_TRAFFIC_ROLLUP_RETENTION_DAYS`` (default 90) set the windows.

    Args:
        db: Database session.
        now: Current time; defaults to now.

    Returns:
        Rows deleted per table.
    """
    moment = now or datetime.now(timezone.utc)
    events_days = _days(
        "API_TRAFFIC_EVENT_RETENTION_DAYS",
        DEFAULT_EVENT_RETENTION_DAYS,
        MIN_EVENT_RETENTION_DAYS,
    )
    rollup_days = _days(
        "API_TRAFFIC_ROLLUP_RETENTION_DAYS", DEFAULT_ROLLUP_RETENTION_DAYS
    )
    return crud_api_traffic.prune(
        db,
        events_before=moment - timedelta(days=events_days),
        rollups_before=moment - timedelta(days=rollup_days),
    )
