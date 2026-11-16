"""Persistence for external approval hand-offs and notification recovery."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from preloop.models import models


def workflow_for_delivery(
    db: Session, workflow_id: UUID
) -> models.ApprovalWorkflow | None:
    """Load the workflow attached to a delivery endpoint."""
    return db.get(models.ApprovalWorkflow, workflow_id) if workflow_id else None


def mark_waiting(db: Session, request_id: UUID) -> None:
    """Defer human notifications until the signed delivery has a response."""
    request = db.get(models.ApprovalRequest, request_id)
    request.external_handoff_state = "waiting"
    db.flush()


def finish_handoff(
    db: Session,
    request_id: UUID,
    *,
    handoff: dict[str, str] | None,
    error: str | None = None,
) -> None:
    """Persist validated links and schedule recoverable human notifications."""
    request = db.get(models.ApprovalRequest, request_id)
    if request is None or request.external_handoff_state != "waiting":
        return
    if handoff:
        request.external_reference = handoff["external_reference"]
        request.external_url = handoff["external_url"]
    request.webhook_error = error
    if error is None:
        request.webhook_posted_at = datetime.utcnow()
    request.external_handoff_state = "notify"
    db.flush()


def stop_delivery(db: Session, delivery: models.WebhookDelivery) -> None:
    """A malformed successful response cannot be repaired by automatic retries."""
    delivery.status = "dead"
    db.flush()


def pending_notifications(db: Session) -> list[UUID]:
    """Find bounded work left by a completed webhook, including after a crash."""
    return list(
        db.scalars(
            select(models.ApprovalRequest.id)
            .where(
                models.ApprovalRequest.external_handoff_state == "notify",
            )
            .limit(100)
        )
    )


def notification_request(
    db: Session, request_id: UUID
) -> models.ApprovalRequest | None:
    """Lock a pending notification while its worker prepares delivery."""
    return db.scalars(
        select(models.ApprovalRequest)
        .where(
            models.ApprovalRequest.id == request_id,
            models.ApprovalRequest.external_handoff_state == "notify",
        )
        .options(selectinload(models.ApprovalRequest.approval_workflow))
        .with_for_update(skip_locked=True)
    ).first()


def notifications_finished(db: Session, request_id: UUID) -> None:
    """Mark the human notification pass complete."""
    request = db.get(models.ApprovalRequest, request_id)
    if request is not None:
        request.external_handoff_state = "ready" if request.external_url else "failed"
    db.flush()


def update_fields(
    db: Session, request: models.ApprovalRequest, fields: dict[str, Any]
) -> None:
    """Apply service-validated fields in the caller's decision transaction."""
    for key, value in fields.items():
        setattr(request, key, value)
    db.flush()
