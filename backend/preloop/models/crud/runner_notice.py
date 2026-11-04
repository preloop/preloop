"""Owner-addressed runner notices backed by the existing audit trail."""

from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from preloop.models import models
from preloop.models.crud import crud_audit_log


def record(
    db: Session,
    *,
    runner: Any,
    actor: Any,
    title: str,
    message: str,
    href: str,
    host: bool = True,
    commit: bool = True,
) -> None:
    """Persist one notice without exposing it to other account members."""
    owner = runner.registered_by_user_id
    if owner is None:
        return
    crud_audit_log.log_action(
        db,
        account_id=runner.account_id,
        user_id=actor.id,
        action="runner.owner_notice",
        resource_type="runner",
        resource_id=str(runner.id),
        status="success",
        commit=commit,
        details={
            "owner_user_id": str(owner),
            "actor_user_id": str(actor.id),
            "runner_id": str(runner.id),
            "title": title,
            "message": message,
            "href": href,
            "host": host,
        },
    )


def recent(
    db: Session,
    *,
    account_id: Any,
    owner_user_id: Any,
    runner_id: Any = None,
    host_only: bool = False,
) -> list[dict[str, Any]]:
    """Latest 50 notices in 24 hours, scoped to their intended owner."""
    query = select(models.AuditLog).where(
        models.AuditLog.account_id == account_id,
        models.AuditLog.action == "runner.owner_notice",
        models.AuditLog.details["owner_user_id"].astext == str(owner_user_id),
        models.AuditLog.timestamp >= datetime.now(timezone.utc) - timedelta(days=1),
    )
    if runner_id is not None:
        query = query.where(models.AuditLog.resource_id == str(runner_id))
    if host_only:
        query = query.where(models.AuditLog.details["host"].astext == "true")
    rows = db.scalars(
        query.order_by(
            models.AuditLog.timestamp.desc(), models.AuditLog.id.desc()
        ).limit(50)
    )
    return [
        {
            "id": str(row.id),
            "title": row.details["title"],
            "message": row.details["message"],
            "href": row.details["href"],
            "created_at": row.timestamp.isoformat(),
        }
        for row in rows
    ]
