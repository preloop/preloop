"""Operator account health: what went wrong for one account in a window.

One cheap, bounded read meant to run daily per account. It returns
categories, counts, ids, route templates and first-party codes only; never
prompts, outputs, raw error text or provider error bodies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account_health, crud_api_traffic

DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=31)


class AccountHealthWindowError(ValueError):
    """The requested window is empty, inverted or too long."""


def resolve_window(
    since: Optional[datetime],
    until: Optional[datetime],
    *,
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Normalise a window to aware UTC and enforce ``MAX_WINDOW``.

    Args:
        since: Window start; defaults to ``until`` minus 24 hours.
        until: Window end; defaults to now.
        now: Current time, for tests.

    Returns:
        ``(since, until)`` in aware UTC.

    Raises:
        AccountHealthWindowError: When ``since`` is not before ``until`` or
            the window is longer than ``MAX_WINDOW``.
    """

    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    end = _utc(until) if until else _utc(now or datetime.now(timezone.utc))
    start = _utc(since) if since else end - DEFAULT_WINDOW
    if start >= end:
        raise AccountHealthWindowError("since must be before until")
    if end - start > MAX_WINDOW:
        raise AccountHealthWindowError("the window may be at most 31 days")
    return start, end


def build_account_health(
    db: Session, *, account_id: UUID, since: datetime, until: datetime
) -> dict[str, Any]:
    """Collect every health signal for one account and window.

    Args:
        db: Database session.
        account_id: The account to report on.
        since: Window start (aware UTC).
        until: Window end (aware UTC).

    Returns:
        A dict matching ``AccountHealthResponse``.
    """
    window = {"account_id": account_id, "since": since, "until": until}
    return {
        "account_id": account_id,
        "since": since,
        "until": until,
        "failed_executions": crud_account_health.failed_executions_by_category(
            db, **window
        ),
        "hollow_successes": crud_account_health.hollow_successes(db, **window),
        "route_problems": crud_api_traffic.route_problems(db, **window),
        "error_responses": crud_api_traffic.error_status_breakdown(db, **window),
        "empty_after_create": crud_api_traffic.empty_after_create(db, **window),
        "gateway_upstream_errors": crud_account_health.gateway_upstream_errors(
            db, **window
        ),
    }
