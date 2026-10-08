"""Scoped recovery artifact persistence and retention transactions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, defer

from preloop.models import models


def _lock_for_put(
    db: Session,
    *,
    account_id: UUID,
    execution_id: UUID,
    require_execution_open: bool,
) -> None:
    """Take the execution then account locks every artifact write uses."""
    from preloop.models.crud import crud_flow_execution

    # Execution row first so a terminal close cannot commit during this PUT.
    crud_flow_execution.lock_for_artifact_put(
        db,
        execution_id=execution_id,
        require_open=require_execution_open,
    )
    # Serialize quota checks without blocking child audit/usage foreign keys.
    # Account identity does not change, so NO KEY UPDATE is sufficient.
    db.query(models.Account).filter(models.Account.id == account_id).with_for_update(
        key_share=True
    ).one()


def workspace_state(metadata: Any) -> tuple[str, tuple[str, ...]] | None:
    """The identity of a workspace capture: file digest plus repository heads.

    None when the checkpoint metadata does not carry a file digest, so a
    capture without one is never treated as a duplicate.
    """
    if not isinstance(metadata, dict):
        return None
    digest = metadata.get("file_state_sha256")
    if not isinstance(digest, str) or not digest:
        return None
    repositories = metadata.get("repositories")
    heads = tuple(
        str(repo.get("head_sha") or "")
        for repo in (repositories if isinstance(repositories, list) else [])
        if isinstance(repo, dict)
    )
    return digest, heads


def reuse_identical_workspace(
    db: Session,
    *,
    account_id: UUID,
    flow_id: UUID,
    thread_id: str,
    execution_id: UUID,
    metadata: dict[str, Any],
    expires_at: datetime,
    require_execution_open: bool = True,
) -> models.FlowArtifact | None:
    """Return the newest identical workspace snapshot, its expiry extended.

    A run captures its workspace periodically and again before publication;
    on a clean review checkout both captures hold the same files at the same
    commit. When the newest available workspace artifact of this execution
    and thread has the same ``file_state_sha256`` and the same repository
    ``head_sha`` list, nothing new is stored and the existing row's expiry
    moves to ``expires_at`` (never earlier). The scope stays inside one
    execution because ``latest`` looks recovery snapshots up by execution.
    Returns None, with the locks still held, when a new row must be stored.
    """
    state = workspace_state(metadata)
    if state is None:
        return None
    _lock_for_put(
        db,
        account_id=account_id,
        execution_id=execution_id,
        require_execution_open=require_execution_open,
    )
    found = (
        db.query(
            models.FlowArtifact,
            models.FlowArtifact.ciphertext.isnot(None).label("has_payload"),
        )
        # The payload is tens of MB; only whether it is still present matters.
        .options(defer(models.FlowArtifact.ciphertext))
        .filter(
            models.FlowArtifact.account_id == account_id,
            models.FlowArtifact.flow_id == flow_id,
            models.FlowArtifact.thread_id == thread_id,
            models.FlowArtifact.execution_id == execution_id,
            models.FlowArtifact.kind == "workspace",
        )
        .order_by(
            models.FlowArtifact.created_at.desc(),
            models.FlowArtifact.updated_at.desc(),
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    newest, has_payload = found if found is not None else (None, False)
    if (
        newest is None
        or newest.availability != "available"
        or not has_payload
        or workspace_state((newest.manifest or {}).get("metadata")) != state
    ):
        # Keep the locks: the caller's store() runs in this same transaction,
        # so no concurrent capture can slip in between the check and insert.
        return None
    if newest.expires_at is None or newest.expires_at < expires_at:
        newest.expires_at = expires_at
    newest.updated_at = datetime.now()
    db.commit()
    db.refresh(newest)
    return newest


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
    _lock_for_put(
        db,
        account_id=values["account_id"],
        execution_id=values["execution_id"],
        require_execution_open=require_execution_open,
    )
    now = datetime.now()
    values.setdefault("created_at", now)
    values.setdefault("updated_at", now)
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
