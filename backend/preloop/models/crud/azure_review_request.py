"""Serialized, durable Azure review requests and bounded label discovery."""

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import monotonic
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from sqlalchemy import String, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from preloop.models import models

QUIET_SECONDS = 15
MAX_COMMENT_SIGNALS = 256
RETENTION_DAYS = 90


class CRUDAzureReviewRequest:
    """Own Azure request persistence; callers never issue database statements."""

    @asynccontextmanager
    async def serialized(
        self, db: Session, tracker_id: Any, repository_id: str, number: int
    ) -> AsyncIterator[None]:
        """Hold a PR lock across reads and dispatch commits, including pushes."""
        key = int.from_bytes(
            sha256(
                f"azure-review:{tracker_id}:{repository_id}:{number}".encode()
            ).digest()[:8],
            "big",
            signed=True,
        )
        bind = db.get_bind()
        engine = bind.engine if isinstance(bind, Connection) else bind
        with engine.connect() as connection:
            acquired = False
            try:
                deadline = monotonic() + 10
                while not connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ):
                    if monotonic() >= deadline:
                        raise RuntimeError("azure_pull_request_busy")
                    await asyncio.sleep(0.05)
                acquired = True
                db.expire_all()
                yield
            finally:
                try:
                    if acquired:
                        connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                        )
                except BaseException:
                    connection.invalidate()
                    raise

    def enqueue(
        self,
        db: Session,
        *,
        flow: models.Flow,
        event: dict[str, Any],
        now: datetime | None = None,
    ) -> bool:
        """Accept a signal once and replace the sole pending follow-up."""
        payload = event["payload"]
        request = payload["azure_review_request"]
        repo = str(payload["repository"]["id"])
        number = int(payload["pull_request"]["number"])
        db.execute(
            insert(models.AzureReviewRequest)
            .values(
                id=uuid4(),
                flow_id=flow.id,
                tracker_id=UUID(event["tracker_id"]),
                repository_id=repo,
                number=number,
                revision=0,
                event={},
                signals={},
            )
            .on_conflict_do_nothing(constraint="uq_azure_review_request")
        )
        row = db.scalars(
            select(models.AzureReviewRequest)
            .where(
                models.AzureReviewRequest.flow_id == flow.id,
                models.AzureReviewRequest.repository_id == repo,
                models.AzureReviewRequest.number == number,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        signals = copy.deepcopy(row.signals)
        key = str(request["signal"])
        if request["kind"] == "label":
            if signals.get("label") == key:
                db.commit()
                return False
            signals["label"] = key
        else:
            seen = signals.setdefault("comments", [])
            if key in seen:
                db.commit()
                return False
            seen.append(key)
            signals["comments"] = seen[-MAX_COMMENT_SIGNALS:]
        row.signals = signals
        row.revision += 1
        row.event = copy.deepcopy(event)
        row.due_at = (now or datetime.now(timezone.utc)) + timedelta(
            seconds=QUIET_SECONDS
        )
        db.commit()
        return True

    def observe_labels(
        self,
        db: Session,
        *,
        tracker_id: Any,
        repository_id: str,
        number: int,
        present: set[str],
    ) -> None:
        """Rearm removed labels so a later attachment can request another run."""
        rows = db.scalars(
            select(models.AzureReviewRequest)
            .where(
                models.AzureReviewRequest.tracker_id == tracker_id,
                models.AzureReviewRequest.repository_id == repository_id,
                models.AzureReviewRequest.number == number,
            )
            .with_for_update()
        ).all()
        for row in rows:
            # A label still observed on an open PR must not expire and rearm.
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            if row.signals.get("label") not in present:
                row.signals = {**row.signals, "label": None}
        db.commit()

    def claim_due(self, db: Session, *, now: datetime, limit: int = 5) -> list[UUID]:
        """Lease a bounded batch, without racing another scheduler tick."""
        rows = db.scalars(
            select(models.AzureReviewRequest)
            .where(
                models.AzureReviewRequest.due_at <= now,
                or_(
                    models.AzureReviewRequest.lease_until.is_(None),
                    models.AzureReviewRequest.lease_until <= now,
                ),
            )
            .order_by(models.AzureReviewRequest.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.lease_until = now + timedelta(minutes=2)
        result = [row.id for row in rows]
        db.commit()
        return result

    def get(self, db: Session, row_id: UUID) -> models.AzureReviewRequest | None:
        """Reload a leased request after acquiring its PR lock."""
        return db.get(models.AzureReviewRequest, row_id, populate_existing=True)

    def finish(
        self, db: Session, *, row_id: UUID, revision: int, done: bool, now: datetime
    ) -> None:
        """Clear only the dispatched revision; keep newer signals pending."""
        row = db.get(
            models.AzureReviewRequest,
            row_id,
            populate_existing=True,
            with_for_update=True,
        )
        if row and row.revision == revision:
            row.due_at = None if done else now + timedelta(seconds=QUIET_SECONDS)
            row.lease_until = None
            db.commit()

    def purge_stale(self, db: Session, *, now: datetime, limit: int = 100) -> int:
        """Delete a bounded batch of idle bookkeeping after 90 unobserved days.

        Pending requests and live leases are retained. Open PR label polls
        refresh updated_at, so a label awaiting manual removal stays deduped.
        """
        rows = db.scalars(
            select(models.AzureReviewRequest)
            .where(
                models.AzureReviewRequest.updated_at
                < (now - timedelta(days=RETENTION_DAYS)).replace(tzinfo=None),
                models.AzureReviewRequest.due_at.is_(None),
                or_(
                    models.AzureReviewRequest.lease_until.is_(None),
                    models.AzureReviewRequest.lease_until <= now,
                ),
            )
            .order_by(models.AzureReviewRequest.updated_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)

    def claim_projects(
        self, db: Session, *, now: datetime, limit: int = 2
    ) -> list[tuple[UUID, int]]:
        """Rotate through imported Azure repositories that have enabled PR flows.

        The existing project metadata holds the polling cursor. Each claim
        advances its timestamp before I/O, so failing projects cannot starve
        other repositories. Scope is checked by the caller before API access.
        """
        last_poll = models.Project.meta_data["azure_review_poll_at"].as_string()
        has_flow = (
            select(models.Flow.id)
            .where(
                models.Flow.trigger_event_source == cast(models.Tracker.id, String),
                models.Flow.is_enabled.is_(True),
                models.Flow.trigger_event_types.any("pull_request_updated"),
            )
            .exists()
        )
        rows = db.scalars(
            select(models.Project)
            .join(models.Organization)
            .join(models.Tracker)
            .where(
                models.Tracker.tracker_type == "azure_devops",
                models.Tracker.is_active.is_(True),
                models.Tracker.is_deleted.is_(False),
                models.Project.is_active.is_(True),
                models.Organization.is_active.is_(True),
                has_flow,
                or_(
                    last_poll.is_(None),
                    last_poll < (now - timedelta(seconds=30)).isoformat(),
                ),
            )
            .order_by(func.coalesce(last_poll, ""), models.Project.id)
            .limit(limit)
            .with_for_update(of=models.Project, skip_locked=True)
            .execution_options(populate_existing=True)
        ).all()
        result = []
        for row in rows:
            data = dict(row.meta_data or {})
            result.append((row.id, int(data.get("azure_review_poll_page", 1))))
            row.meta_data = {**data, "azure_review_poll_at": now.isoformat()}
        db.commit()
        return result

    def advance_page(self, db: Session, *, project_id: UUID, page: int) -> None:
        """Persist the next bounded page of open PRs for this repository."""
        project = db.get(
            models.Project, project_id, populate_existing=True, with_for_update=True
        )
        if project:
            project.meta_data = {
                **dict(project.meta_data or {}),
                "azure_review_poll_page": page,
            }
            db.commit()


crud_azure_review_request = CRUDAzureReviewRequest()
