"""CRUD for REST traffic rollups and problem events (``api_traffic_*``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models.models.api_traffic import (
    EVENT_KIND_EMPTY_AFTER_CREATE,
    EVENT_KIND_ERROR,
    ApiTrafficEvent,
    ApiTrafficRollup,
)

#: Rows inserted per statement, well under PostgreSQL's bind limit.
_INSERT_CHUNK = 1000


class CRUDApiTraffic:
    """Writes from the traffic recorder, reads for account health, retention."""

    def add_rollups(
        self, db: Session, *, rows: Sequence[dict[str, Any]], commit: bool = True
    ) -> int:
        """Add request counts to their minute rows, creating rows as needed.

        Args:
            db: Database session.
            rows: Column values per rollup key, with ``request_count``,
                ``total_duration_ms`` and ``max_duration_ms`` as deltas.
            commit: Commit after writing.

        Returns:
            Number of rows submitted.
        """
        for start in range(0, len(rows), _INSERT_CHUNK):
            chunk = list(rows[start : start + _INSERT_CHUNK])
            stmt = insert(ApiTrafficRollup).values(chunk)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_api_traffic_rollup_key",
                set_={
                    "request_count": ApiTrafficRollup.request_count
                    + stmt.excluded.request_count,
                    "total_duration_ms": ApiTrafficRollup.total_duration_ms
                    + stmt.excluded.total_duration_ms,
                    "max_duration_ms": func.greatest(
                        ApiTrafficRollup.max_duration_ms,
                        stmt.excluded.max_duration_ms,
                    ),
                    "updated_at": func.now(),
                },
            )
            db.execute(stmt)
        if commit:
            db.commit()
        return len(rows)

    def add_events(
        self, db: Session, *, events: Sequence[dict[str, Any]], commit: bool = True
    ) -> int:
        """Insert problem events.

        Args:
            db: Database session.
            events: Column values per event.
            commit: Commit after writing.

        Returns:
            Number of events inserted.
        """
        for start in range(0, len(events), _INSERT_CHUNK):
            db.execute(
                insert(ApiTrafficEvent).values(
                    list(events[start : start + _INSERT_CHUNK])
                )
            )
        if commit:
            db.commit()
        return len(events)

    def route_problems(
        self,
        db: Session,
        *,
        account_id: UUID,
        since: datetime,
        until: datetime,
        min_count: int = 2,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Routes that answered 4xx or 5xx at least ``min_count`` times.

        Served by ``uq_api_traffic_rollup_key`` (account, bucket leading).

        Returns:
            One entry per (method, route, status class), largest first, with
            the request total for the same route and class for context.
        """
        total = func.sum(ApiTrafficRollup.request_count).label("count")
        rows = db.execute(
            select(
                ApiTrafficRollup.method,
                ApiTrafficRollup.route_template,
                ApiTrafficRollup.status_class,
                total,
                func.min(ApiTrafficRollup.bucket_start).label("first_at"),
                func.max(ApiTrafficRollup.bucket_start).label("last_at"),
            )
            .where(
                ApiTrafficRollup.account_id == account_id,
                ApiTrafficRollup.bucket_start >= since,
                ApiTrafficRollup.bucket_start < until,
                ApiTrafficRollup.status_class >= 4,
            )
            .group_by(
                ApiTrafficRollup.method,
                ApiTrafficRollup.route_template,
                ApiTrafficRollup.status_class,
            )
            .having(total >= min_count)
            .order_by(total.desc())
            .limit(limit)
        ).all()
        return [dict(row._mapping) for row in rows]

    def error_status_breakdown(
        self,
        db: Session,
        *,
        account_id: UUID,
        since: datetime,
        until: datetime,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Problem events grouped by route, status, principal kind and reason.

        Served by ``ix_api_traffic_event_account_time``.
        """
        count = func.count().label("count")
        rows = db.execute(
            select(
                ApiTrafficEvent.method,
                ApiTrafficEvent.route_template,
                ApiTrafficEvent.status_code,
                ApiTrafficEvent.principal_kind,
                ApiTrafficEvent.reason_code,
                count,
                func.min(ApiTrafficEvent.occurred_at).label("first_at"),
                func.max(ApiTrafficEvent.occurred_at).label("last_at"),
            )
            .where(
                ApiTrafficEvent.account_id == account_id,
                ApiTrafficEvent.occurred_at >= since,
                ApiTrafficEvent.occurred_at < until,
                ApiTrafficEvent.kind == EVENT_KIND_ERROR,
            )
            .group_by(
                ApiTrafficEvent.method,
                ApiTrafficEvent.route_template,
                ApiTrafficEvent.status_code,
                ApiTrafficEvent.principal_kind,
                ApiTrafficEvent.reason_code,
            )
            .order_by(count.desc())
            .limit(limit)
        ).all()
        return [dict(row._mapping) for row in rows]

    def empty_after_create(
        self,
        db: Session,
        *,
        account_id: UUID,
        since: datetime,
        until: datetime,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List responses that came back empty right after a create.

        Served by ``ix_api_traffic_event_account_time``.
        """
        count = func.count().label("count")
        rows = db.execute(
            select(
                ApiTrafficEvent.method,
                ApiTrafficEvent.route_template,
                count,
                func.min(ApiTrafficEvent.occurred_at).label("first_at"),
                func.max(ApiTrafficEvent.occurred_at).label("last_at"),
            )
            .where(
                ApiTrafficEvent.account_id == account_id,
                ApiTrafficEvent.occurred_at >= since,
                ApiTrafficEvent.occurred_at < until,
                ApiTrafficEvent.kind == EVENT_KIND_EMPTY_AFTER_CREATE,
            )
            .group_by(ApiTrafficEvent.method, ApiTrafficEvent.route_template)
            .order_by(count.desc())
            .limit(limit)
        ).all()
        return [dict(row._mapping) for row in rows]

    def prune(
        self,
        db: Session,
        *,
        events_before: datetime,
        rollups_before: datetime,
        batch_size: int = 5000,
        max_batches: int = 20,
    ) -> dict[str, int]:
        """Delete expired rows in bounded, committed batches.

        Args:
            db: Database session.
            events_before: Delete problem events older than this.
            rollups_before: Delete rollup rows older than this.
            batch_size: Rows per delete statement.
            max_batches: Statements per table per call.

        Returns:
            Rows deleted per table.
        """
        deleted = {"events": 0, "rollups": 0}
        for name, model, column, cutoff in (
            ("events", ApiTrafficEvent, ApiTrafficEvent.occurred_at, events_before),
            (
                "rollups",
                ApiTrafficRollup,
                ApiTrafficRollup.bucket_start,
                rollups_before,
            ),
        ):
            for _ in range(max_batches):
                ids = select(model.id).where(column < cutoff).limit(batch_size)
                result = db.execute(delete(model).where(model.id.in_(ids)))
                db.commit()
                deleted[name] += result.rowcount or 0
                if (result.rowcount or 0) < batch_size:
                    break
        return deleted

    def latest_event(
        self, db: Session, *, account_id: Optional[UUID]
    ) -> Optional[ApiTrafficEvent]:
        """Newest problem event of an account (tests and diagnostics)."""
        return db.scalars(
            select(ApiTrafficEvent)
            .where(ApiTrafficEvent.account_id == account_id)
            .order_by(ApiTrafficEvent.occurred_at.desc())
            .limit(1)
        ).first()


crud_api_traffic = CRUDApiTraffic()
