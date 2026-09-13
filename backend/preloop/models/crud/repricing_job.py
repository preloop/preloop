"""Account-scoped job persistence and fenced, renewable worker claims."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from preloop.models import models
from .base import CRUDBase

LEASE_SECONDS = 300
MAX_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CRUDRepricingJob(CRUDBase[models.RepricingJob]):
    """Persist outcomes independently of HTTP requests and NATS deliveries."""

    def claim(
        self, db: Session, *, job_id: str, account_id: str
    ) -> models.RepricingJob | None:
        """Atomically claim new or abandoned work, with a retry limit."""
        now = _now()
        changed = (
            db.query(self.model)
            .filter(
                self.model.id == job_id,
                self.model.account_id == account_id,
                self.model.attempts < MAX_ATTEMPTS,
                or_(
                    self.model.status == "queued",
                    and_(
                        self.model.status == "running",
                        self.model.heartbeat_at
                        < now - timedelta(seconds=LEASE_SECONDS),
                    ),
                ),
            )
            .update(
                {
                    "status": "running",
                    "attempts": self.model.attempts + 1,
                    "started_at": now,
                    "heartbeat_at": now,
                    "finished_at": None,
                    "error": None,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        db.expire_all()
        return self.get(db, id=job_id, account_id=account_id) if changed else None

    def fail_exhausted(self, db: Session, *, job_id: str, account_id: str) -> None:
        """Make the last abandoned attempt terminal on redelivery."""
        db.query(self.model).filter(
            self.model.id == job_id,
            self.model.account_id == account_id,
            self.model.status == "running",
            self.model.attempts >= MAX_ATTEMPTS,
            self.model.heartbeat_at < _now() - timedelta(seconds=LEASE_SECONDS),
        ).update(
            {
                "status": "failed",
                "finished_at": _now(),
                "error": "Worker stopped reporting progress. Some usage may already have been repriced. Retry repricing.",
            },
            synchronize_session=False,
        )
        db.commit()
        db.expire_all()

    def heartbeat(
        self, db: Session, *, job_id: str, account_id: str, attempt: int
    ) -> bool:
        """Renew only the current worker's lease."""
        with db.no_autoflush:
            changed = (
                db.query(self.model)
                .filter(
                    self.model.id == job_id,
                    self.model.account_id == account_id,
                    self.model.status == "running",
                    self.model.attempts == attempt,
                )
                .update({"heartbeat_at": _now()}, synchronize_session=False)
            )
        if changed:
            db.commit()
        else:
            db.rollback()
        return bool(changed)

    def finish(
        self,
        db: Session,
        *,
        job_id: str,
        account_id: str,
        attempt: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Fence late workers so they cannot overwrite a newer attempt."""
        with db.no_autoflush:
            changed = (
                db.query(self.model)
                .filter(
                    self.model.id == job_id,
                    self.model.account_id == account_id,
                    self.model.status == "running",
                    self.model.attempts == attempt,
                )
                .update(
                    {
                        "status": "failed" if error else "succeeded",
                        "result": result,
                        "error": error,
                        "finished_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
        if changed:
            db.commit()
        else:
            db.rollback()
        return bool(changed)

    def fail_submission(
        self, db: Session, *, job_id: uuid.UUID, account_id: uuid.UUID
    ) -> None:
        """Record uncertain delivery without overwriting a worker's outcome."""
        db.query(self.model).filter(
            self.model.id == job_id,
            self.model.account_id == account_id,
            self.model.status == "queued",
        ).update(
            {
                "status": "failed",
                "error": "Queue submission was not confirmed. Retry repricing.",
                "finished_at": _now(),
            },
            synchronize_session=False,
        )
        db.commit()


crud_repricing_job = CRUDRepricingJob(models.RepricingJob)
