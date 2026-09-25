"""Legal hold persistence: the record rows and the derived enforcement flags.

Every function here is account scoped. A hold that could be placed on, listed
with, or released from another account's records would be worse than no hold
at all, so the account id is a required argument on every call rather than
something a caller may pass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.flow_artifact import FlowArtifact
from preloop.models.models.flow import Flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.models.legal_hold import LegalHold
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.runtime_session_artifact import RuntimeSessionArtifact


def execution_in_account(account_id: Any):
    """Account filter for an execution.

    ``flow_execution`` carries no ``account_id`` of its own: an execution
    belongs to an account through its flow. Every execution-scoped query here
    goes through this so the ownership check cannot be forgotten in one place
    and remembered in another.
    """
    return FlowExecution.flow_id.in_(
        select(Flow.id).where(Flow.account_id == account_id)
    )


def get(db: Session, *, account_id: Any, hold_id: UUID) -> Optional[LegalHold]:
    """One hold, never outside the account that owns it."""
    return db.execute(
        select(LegalHold).where(
            LegalHold.id == hold_id,
            LegalHold.account_id == str(account_id),
        )
    ).scalar_one_or_none()


def get_active(
    db: Session, *, account_id: Any, resource_type: str, resource_id: str
) -> Optional[LegalHold]:
    """The active hold on one resource, if there is one."""
    return db.execute(
        select(LegalHold).where(
            LegalHold.account_id == str(account_id),
            LegalHold.resource_type == resource_type,
            LegalHold.resource_id == str(resource_id),
            LegalHold.released_at.is_(None),
        )
    ).scalar_one_or_none()


def list_for_account(
    db: Session,
    *,
    account_id: Any,
    active_only: bool = False,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
) -> list[LegalHold]:
    """Holds for one account, newest first."""
    stmt = select(LegalHold).where(LegalHold.account_id == str(account_id))
    if active_only:
        stmt = stmt.where(LegalHold.released_at.is_(None))
    if resource_type:
        stmt = stmt.where(LegalHold.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(LegalHold.resource_id == str(resource_id))
    stmt = stmt.order_by(LegalHold.placed_at.desc()).offset(skip).limit(limit)
    return list(db.execute(stmt).scalars().all())


def active_resource_ids(
    db: Session, *, account_id: Any, resource_type: str
) -> set[str]:
    """Resource ids under an active hold, for recomputing derived flags."""
    rows = db.execute(
        select(LegalHold.resource_id).where(
            LegalHold.account_id == str(account_id),
            LegalHold.resource_type == resource_type,
            LegalHold.released_at.is_(None),
        )
    ).scalars()
    return {str(value) for value in rows}


def create(
    db: Session,
    *,
    account_id: Any,
    resource_type: str,
    resource_id: str,
    reason: str,
    placed_by_user_id: Optional[UUID],
    placed_at: datetime,
    commit: bool = False,
) -> LegalHold:
    """Insert the hold record. The caller sets the derived flags."""
    hold = LegalHold(
        account_id=str(account_id),
        resource_type=resource_type,
        resource_id=str(resource_id),
        reason=reason,
        placed_by_user_id=placed_by_user_id,
        placed_at=placed_at,
    )
    db.add(hold)
    if commit:
        db.commit()
        db.refresh(hold)
    else:
        db.flush()
    return hold


def release(
    db: Session,
    *,
    hold: LegalHold,
    released_by_user_id: Optional[UUID],
    released_at: datetime,
    release_reason: str,
    commit: bool = False,
) -> LegalHold:
    """Mark the hold released. The caller recomputes the derived flags."""
    hold.released_at = released_at
    hold.released_by_user_id = released_by_user_id
    hold.release_reason = release_reason
    db.add(hold)
    if commit:
        db.commit()
        db.refresh(hold)
    else:
        db.flush()
    return hold


def set_execution_flag(
    db: Session, *, account_id: Any, execution_id: Any, held: bool
) -> int:
    """Set the flag on one execution. Returns rows touched (0 or 1)."""
    result = db.execute(
        update(FlowExecution)
        .where(
            FlowExecution.id == execution_id,
            execution_in_account(account_id),
        )
        .values(legal_hold=held)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def set_approval_flag(
    db: Session, *, account_id: Any, approval_id: Any, held: bool
) -> int:
    """Set the flag on one approval request."""
    result = db.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.id == approval_id,
            ApprovalRequest.account_id == account_id,
        )
        .values(legal_hold=held)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def set_artifact_flag(
    db: Session, *, account_id: Any, artifact_id: Any, held: bool
) -> int:
    """Set the flag on one evidence pack row."""
    result = db.execute(
        update(FlowArtifact)
        .where(
            FlowArtifact.id == artifact_id,
            FlowArtifact.account_id == account_id,
            FlowArtifact.kind == "evidence",
        )
        .values(legal_hold=held)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def set_runtime_session_flag(
    db: Session, *, account_id: Any, runtime_session_id: Any, held: bool
) -> int:
    """Set the flag on one runtime session.

    The session's activity rows carry no flag of their own: they are tied to
    the session with ``ON DELETE CASCADE``, so a session the purge skips keeps
    its activity.
    """
    result = db.execute(
        update(RuntimeSession)
        .where(
            RuntimeSession.id == runtime_session_id,
            RuntimeSession.account_id == account_id,
        )
        .values(legal_hold=held)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def set_runtime_session_artifact_flags(
    db: Session, *, account_id: Any, runtime_session_id: Any, held: bool
) -> int:
    """Set the flag on every artifact of one runtime session.

    A hold that freezes the session row and lets its screenshots expire would
    keep the record and lose the bytes. The session hold reaches the artifacts.

    Args:
        db: Database session.
        account_id: Account that owns the session.
        runtime_session_id: Session whose artifacts are flagged.
        held: Derived flag value.

    Returns:
        Rows touched.
    """
    result = db.execute(
        update(RuntimeSessionArtifact)
        .where(
            RuntimeSessionArtifact.account_id == account_id,
            RuntimeSessionArtifact.runtime_session_id == runtime_session_id,
        )
        .values(legal_hold=held)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def set_execution_evidence_flags(
    db: Session, *, account_id: Any, execution_id: Any, held: bool
) -> int:
    """Cascade a hold to every evidence pack of one execution.

    Freezing a run and letting its evidence expire on the operational window
    would be a hold in name only, so the execution hold reaches the packs.
    When clearing, packs that carry their own active pack-level hold are left
    alone by the caller (see services/legal_hold.py).
    """
    result = db.execute(
        update(FlowArtifact)
        .where(
            FlowArtifact.account_id == account_id,
            FlowArtifact.execution_id == execution_id,
            FlowArtifact.kind == "evidence",
        )
        .values(legal_hold=held)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def execution_artifact_ids(
    db: Session, *, account_id: Any, execution_id: Any
) -> Sequence[UUID]:
    """Evidence pack ids belonging to one execution."""
    return list(
        db.execute(
            select(FlowArtifact.id).where(
                FlowArtifact.account_id == account_id,
                FlowArtifact.execution_id == execution_id,
                FlowArtifact.kind == "evidence",
            )
        )
        .scalars()
        .all()
    )
