"""Scoped recovery artifact persistence and retention transactions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from preloop.models import models


def store(
    db: Session, *, values: dict[str, Any], quota_bytes: int
) -> models.FlowArtifact:
    """Serialize account writes and enforce retained ciphertext quota."""
    db.query(models.Account).filter(
        models.Account.id == values["account_id"]
    ).with_for_update().one()
    size = (
        db.query(
            func.coalesce(
                func.sum(func.octet_length(models.FlowArtifact.ciphertext)), 0
            )
        )
        .filter(models.FlowArtifact.account_id == values["account_id"])
        .scalar()
    )
    if size + len(values["ciphertext"]) > quota_bytes:
        raise ValueError("artifact_quota_exceeded")
    artifact = models.FlowArtifact(**values)
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


def get(
    db: Session, *, artifact_id: UUID, account_id: UUID, flow_id: UUID, thread_id: str
) -> models.FlowArtifact | None:
    """Never return an artifact outside the exact authorized thread."""
    return (
        db.query(models.FlowArtifact)
        .filter(
            models.FlowArtifact.id == artifact_id,
            models.FlowArtifact.account_id == account_id,
            models.FlowArtifact.flow_id == flow_id,
            models.FlowArtifact.thread_id == thread_id,
        )
        .first()
    )


def latest(
    db: Session,
    *,
    account_id: UUID,
    flow_id: UUID,
    thread_id: str,
    execution_id: UUID,
    kind: str,
) -> models.FlowArtifact | None:
    """Return the most recent fully committed checkpoint, including loss metadata."""
    return (
        db.query(models.FlowArtifact)
        .filter(
            models.FlowArtifact.account_id == account_id,
            models.FlowArtifact.flow_id == flow_id,
            models.FlowArtifact.thread_id == thread_id,
            models.FlowArtifact.execution_id == execution_id,
            models.FlowArtifact.kind == kind,
        )
        .order_by(models.FlowArtifact.created_at.desc())
        .first()
    )


def lease(
    db: Session, *, artifact: models.FlowArtifact, until: datetime
) -> models.FlowArtifact:
    """Renew the artifact lease in a transaction shared with cleanup."""
    row = (
        db.query(models.FlowArtifact)
        .filter(models.FlowArtifact.id == artifact.id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    if row.ciphertext is None:
        raise ValueError("artifact_expired")
    row.lease_until = until
    db.commit()
    db.refresh(row)
    return row


def cleanup(db: Session, *, now: datetime) -> int:
    """Release expired unleased payloads while retaining honest availability."""
    count = (
        db.query(models.FlowArtifact)
        .filter(
            models.FlowArtifact.expires_at <= now,
            or_(
                models.FlowArtifact.lease_until.is_(None),
                models.FlowArtifact.lease_until <= now,
            ),
            models.FlowArtifact.ciphertext.isnot(None),
        )
        .update(
            {
                models.FlowArtifact.ciphertext: None,
                models.FlowArtifact.availability: "expired",
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return count


def rollback(db: Session) -> None:
    """Release the upload transaction after validation or quota rejection."""
    db.rollback()
