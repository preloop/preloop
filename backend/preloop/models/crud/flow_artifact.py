"""Scoped recovery artifact persistence and retention transactions."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from preloop.models import models

QUOTA_EXCEEDED = "artifact_quota_exceeded"


# Name fixed by the #1339 spec; the stable code, not the class, is the contract.
class ArtifactQuotaExceeded(ValueError):  # noqa: N818
    """Admission refused: retained plus incoming ciphertext exceeds the quota.

    ``str(exc)`` stays the stable code ``artifact_quota_exceeded`` so every
    ``except ValueError`` caller keeps working; the byte totals ride alongside
    for the 422 body, the audit row and the runner marker (#1339).
    """

    def __init__(
        self, *, retained_bytes: int, quota_bytes: int, incoming_bytes: int
    ) -> None:
        super().__init__(QUOTA_EXCEEDED)
        self.retained_bytes = int(retained_bytes)
        self.quota_bytes = int(quota_bytes)
        self.incoming_bytes = int(incoming_bytes)

    def numbers(self) -> dict[str, int]:
        """The three byte totals, without any artifact identity."""
        return {
            "retained_bytes": self.retained_bytes,
            "quota_bytes": self.quota_bytes,
            "incoming_bytes": self.incoming_bytes,
        }


def _retained_bytes_expr() -> Any:
    """The retained-ciphertext aggregate shared by admission and usage."""
    return func.coalesce(func.sum(func.octet_length(models.FlowArtifact.ciphertext)), 0)


def usage(
    db: Session, *, account_id: UUID, now: datetime | None = None
) -> dict[str, Any]:
    """Account retained flow-artifact bytes, as admission counts them.

    ``retained_bytes`` is the same aggregate ``store`` compares against the
    quota. ``by_kind`` gives bytes and row counts per kind (rows whose payload
    was already cleared count as rows with zero bytes).
    ``expired_pending_cleanup`` counts rows past ``expires_at`` whose
    ciphertext the janitor has not cleared yet: those bytes still count.
    ``next_expiry_at`` is the earliest ``expires_at`` among available rows,
    so a caller can see when space frees.
    """
    now = now or datetime.now(UTC)
    rows = (
        db.query(
            models.FlowArtifact.kind,
            _retained_bytes_expr(),
            func.count(models.FlowArtifact.id),
        )
        .filter(models.FlowArtifact.account_id == account_id)
        .group_by(models.FlowArtifact.kind)
        .all()
    )
    by_kind = {
        str(kind): {"bytes": int(size), "count": int(count)}
        for kind, size, count in rows
    }
    pending = (
        db.query(func.count(models.FlowArtifact.id))
        .filter(
            models.FlowArtifact.account_id == account_id,
            models.FlowArtifact.expires_at <= now,
            models.FlowArtifact.ciphertext.isnot(None),
        )
        .scalar()
    )
    next_expiry = (
        db.query(func.min(models.FlowArtifact.expires_at))
        .filter(
            models.FlowArtifact.account_id == account_id,
            models.FlowArtifact.availability == "available",
            models.FlowArtifact.ciphertext.isnot(None),
        )
        .scalar()
    )
    return {
        "retained_bytes": sum(entry["bytes"] for entry in by_kind.values()),
        "by_kind": by_kind,
        "expired_pending_cleanup": int(pending or 0),
        "next_expiry_at": next_expiry,
    }


def store(
    db: Session,
    *,
    values: dict[str, Any],
    quota_bytes: int,
    require_execution_open: bool = True,
) -> models.FlowArtifact:
    """Serialize account writes and enforce retained ciphertext quota.

    External capability PUTs pass ``require_execution_open=True``. Controller
    retention after a terminal failure passes False so recovery artifacts can
    still commit.
    """
    from preloop.models.crud import crud_flow_execution

    # Execution row first so a terminal close cannot commit during this PUT.
    crud_flow_execution.lock_for_artifact_put(
        db,
        execution_id=values["execution_id"],
        require_open=require_execution_open,
    )
    now = datetime.now()
    values.setdefault("created_at", now)
    values.setdefault("updated_at", now)
    # Serialize quota checks without blocking child audit/usage foreign keys.
    # Account identity does not change, so NO KEY UPDATE is sufficient.
    db.query(models.Account).filter(
        models.Account.id == values["account_id"]
    ).with_for_update(key_share=True).one()
    size = (
        db.query(_retained_bytes_expr())
        .filter(models.FlowArtifact.account_id == values["account_id"])
        .scalar()
    )
    incoming = len(values["ciphertext"])
    if size + incoming > quota_bytes:
        raise ArtifactQuotaExceeded(
            retained_bytes=size, quota_bytes=quota_bytes, incoming_bytes=incoming
        )
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
        .order_by(
            models.FlowArtifact.created_at.desc(),
            models.FlowArtifact.updated_at.desc(),
        )
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
    """Release expired unleased payloads while retaining honest availability.

    A row under a legal hold is skipped whatever its ``expires_at`` says. The
    hold has to block payload expiry, not only deletion: an evidence pack a
    regulator may ask for has to still be downloadable, and "the record row
    survived but the bytes are gone" is not what anyone means by a hold.
    """
    count = (
        db.query(models.FlowArtifact)
        .filter(
            models.FlowArtifact.expires_at <= now,
            models.FlowArtifact.legal_hold.is_(False),
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
