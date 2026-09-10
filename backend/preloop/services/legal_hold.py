"""Place and release legal holds, and answer "is this held?".

A legal hold is the answer to "counsel says nothing about this incident may be
deleted while the matter is open". Before this, the honest answer in the
evidence storage guide was that ``legal_hold`` is always false, and the
30 day operational evidence window applied to a run under investigation
exactly as it applied to a run nobody would ever look at again.

Two things happen when a hold is placed, in one transaction:

1. a ``legal_hold`` record with the actor and the mandatory reason, and
2. the derived boolean on the held rows, which is what the purge and the
   evidence janitor actually test.

Both the placement and the release are written to the audit log, because a
hold that can be lifted without a trace is a hold whose absence proves
nothing.

Release does not blindly clear the boolean. An evidence pack can be covered by
its own pack-level hold and by a hold on its execution at the same time;
lifting one must leave the other in force, so the flag is recomputed from the
holds that remain active.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log
from preloop.models.crud import legal_hold as crud
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.flow_artifact import FlowArtifact
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.models.legal_hold import (
    HOLD_RESOURCE_APPROVAL,
    HOLD_RESOURCE_EVIDENCE_PACK,
    HOLD_RESOURCE_EXECUTION,
    HOLD_RESOURCE_TYPES,
    LegalHold,
)

logger = logging.getLogger(__name__)

AUDIT_ACTION_PLACED = "legal_hold_placed"
AUDIT_ACTION_RELEASED = "legal_hold_released"

#: A reason is the whole point of the record. One word is not a reason, and a
#: novel pasted into an audit row is a different problem.
MIN_REASON_CHARS = 8
MAX_REASON_CHARS = 2000


class LegalHoldError(ValueError):
    """A hold could not be placed or released as asked."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HoldOutcome:
    """What a place or release call actually changed."""

    hold: LegalHold
    #: Rows whose derived flag this call moved, per table.
    flagged: dict[str, int]


def _clean_reason(reason: Any) -> str:
    text = str(reason or "").strip()
    if len(text) < MIN_REASON_CHARS:
        raise LegalHoldError(
            "reason_required",
            f"a legal hold reason of at least {MIN_REASON_CHARS} characters is "
            "required; the record exists to explain why the data is frozen",
        )
    return text[:MAX_REASON_CHARS]


def _coerce_uuid(resource_id: Any) -> UUID:
    try:
        return UUID(str(resource_id))
    except (TypeError, ValueError) as exc:
        raise LegalHoldError(
            "resource_not_found", "the resource id is not a valid identifier"
        ) from exc


def _require_resource(
    db: Session, *, account_id: Any, resource_type: str, resource_id: str
) -> UUID:
    """Refuse a hold on something this account does not own.

    A hold on a nonexistent id would look active in a listing and freeze
    nothing, which is the failure mode a hold exists to prevent.
    """
    if resource_type not in HOLD_RESOURCE_TYPES:
        raise LegalHoldError(
            "unknown_resource_type",
            f"resource_type must be one of {', '.join(HOLD_RESOURCE_TYPES)}",
        )
    identifier = _coerce_uuid(resource_id)
    model = {
        HOLD_RESOURCE_EXECUTION: FlowExecution,
        HOLD_RESOURCE_APPROVAL: ApprovalRequest,
        HOLD_RESOURCE_EVIDENCE_PACK: FlowArtifact,
    }[resource_type]
    if resource_type == HOLD_RESOURCE_EXECUTION:
        # An execution is owned through its flow, not by a column of its own.
        stmt = select(FlowExecution.id).where(
            FlowExecution.id == identifier, crud.execution_in_account(account_id)
        )
    else:
        stmt = select(model.id).where(
            model.id == identifier, model.account_id == account_id
        )
    if resource_type == HOLD_RESOURCE_EVIDENCE_PACK:
        stmt = stmt.where(FlowArtifact.kind == "evidence")
    if db.execute(stmt).scalar_one_or_none() is None:
        raise LegalHoldError(
            "resource_not_found",
            f"no {resource_type} {resource_id} in this account",
        )
    return identifier


def _stamp_evidence_receipts(
    db: Session, *, account_id: Any, execution_ids: Sequence[Any], held: bool
) -> int:
    """Mirror the hold onto the persisted evidence receipt of each execution.

    ``GET .../evidence-status`` answers from ``flow_execution.evidence_receipt``
    without touching ciphertext, and it downgrades an ``available`` receipt to
    ``expired`` once ``expires_at`` has passed. A held pack whose bytes the
    janitor was told to leave alone would otherwise poll as expired and
    download fine, which is exactly the contradiction the receipt exists to
    avoid.
    """
    if not execution_ids:
        return 0
    rows = (
        db.execute(
            select(FlowExecution).where(
                crud.execution_in_account(account_id),
                FlowExecution.id.in_(list(execution_ids)),
            )
        )
        .scalars()
        .all()
    )
    stamped = 0
    for execution in rows:
        receipt = execution.evidence_receipt
        if not isinstance(receipt, dict):
            continue
        if bool(receipt.get("legal_hold")) == held:
            continue
        updated = dict(receipt)
        updated["legal_hold"] = held
        execution.evidence_receipt = updated
        db.add(execution)
        stamped += 1
    return stamped


def _apply_flags(
    db: Session,
    *,
    account_id: Any,
    resource_type: str,
    resource_id: UUID,
    held: bool,
) -> dict[str, int]:
    """Set (or recompute) the derived flags for one hold's resource."""
    flagged: dict[str, int] = {}
    if resource_type == HOLD_RESOURCE_EXECUTION:
        flagged["flow_execution"] = crud.set_execution_flag(
            db, account_id=account_id, execution_id=resource_id, held=held
        )
        flagged["evidence_receipt"] = _stamp_evidence_receipts(
            db, account_id=account_id, execution_ids=[resource_id], held=held
        )
        if held:
            flagged["flow_artifact"] = crud.set_execution_evidence_flags(
                db, account_id=account_id, execution_id=resource_id, held=True
            )
        else:
            # Clearing: a pack that carries its own active pack-level hold
            # keeps its flag. Two overlapping holds, one released, still one
            # frozen pack.
            still_held = crud.active_resource_ids(
                db, account_id=account_id, resource_type=HOLD_RESOURCE_EVIDENCE_PACK
            )
            cleared = 0
            for artifact_id in crud.execution_artifact_ids(
                db, account_id=account_id, execution_id=resource_id
            ):
                if str(artifact_id) in still_held:
                    continue
                cleared += crud.set_artifact_flag(
                    db, account_id=account_id, artifact_id=artifact_id, held=False
                )
            flagged["flow_artifact"] = cleared
    elif resource_type == HOLD_RESOURCE_APPROVAL:
        flagged["approval_request"] = crud.set_approval_flag(
            db, account_id=account_id, approval_id=resource_id, held=held
        )
    else:
        execution_id = db.execute(
            select(FlowArtifact.execution_id).where(
                FlowArtifact.id == resource_id,
                FlowArtifact.account_id == account_id,
            )
        ).scalar_one_or_none()
        if not held:
            # The pack may still sit under an execution-level hold.
            covering = crud.active_resource_ids(
                db, account_id=account_id, resource_type=HOLD_RESOURCE_EXECUTION
            )
            if execution_id is not None and str(execution_id) in covering:
                return {"flow_artifact": 0}
        flagged["flow_artifact"] = crud.set_artifact_flag(
            db, account_id=account_id, artifact_id=resource_id, held=held
        )
        flagged["evidence_receipt"] = _stamp_evidence_receipts(
            db,
            account_id=account_id,
            execution_ids=[execution_id] if execution_id is not None else [],
            held=held,
        )
    return flagged


def place_hold(
    db: Session,
    *,
    account_id: Any,
    resource_type: str,
    resource_id: str,
    reason: str,
    user_id: Optional[UUID] = None,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> HoldOutcome:
    """Freeze one resource. Record, flags and audit row in one transaction."""
    cleaned_reason = _clean_reason(reason)
    identifier = _require_resource(
        db,
        account_id=account_id,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    existing = crud.get_active(
        db,
        account_id=account_id,
        resource_type=resource_type,
        resource_id=str(identifier),
    )
    if existing is not None:
        raise LegalHoldError(
            "already_held",
            f"{resource_type} {identifier} is already under an active legal hold",
        )
    stamp = now or datetime.now(UTC)
    hold = crud.create(
        db,
        account_id=account_id,
        resource_type=resource_type,
        resource_id=str(identifier),
        reason=cleaned_reason,
        placed_by_user_id=user_id,
        placed_at=stamp,
    )
    flagged = _apply_flags(
        db,
        account_id=account_id,
        resource_type=resource_type,
        resource_id=identifier,
        held=True,
    )
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action=AUDIT_ACTION_PLACED,
        resource_type=resource_type,
        resource_id=str(identifier),
        status="success",
        details={
            "hold_id": str(hold.id),
            "reason": cleaned_reason,
            "flagged": flagged,
        },
        commit=False,
    )
    if commit:
        db.commit()
        db.refresh(hold)
    logger.info(
        "Legal hold placed on %s %s (%s rows flagged)",
        resource_type,
        identifier,
        sum(flagged.values()),
    )
    return HoldOutcome(hold=hold, flagged=flagged)


def release_hold(
    db: Session,
    *,
    account_id: Any,
    hold_id: UUID,
    reason: str,
    user_id: Optional[UUID] = None,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> HoldOutcome:
    """Lift one hold, recording who lifted it and why."""
    cleaned_reason = _clean_reason(reason)
    hold = crud.get(db, account_id=account_id, hold_id=hold_id)
    if hold is None:
        raise LegalHoldError("hold_not_found", f"no legal hold {hold_id}")
    if hold.released_at is not None:
        raise LegalHoldError(
            "already_released", f"legal hold {hold_id} was already released"
        )
    stamp = now or datetime.now(UTC)
    crud.release(
        db,
        hold=hold,
        released_by_user_id=user_id,
        released_at=stamp,
        release_reason=cleaned_reason,
    )
    # The record is released first so the recompute below sees the remaining
    # active holds, not this one.
    flagged = _apply_flags(
        db,
        account_id=account_id,
        resource_type=hold.resource_type,
        resource_id=_coerce_uuid(hold.resource_id),
        held=False,
    )
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action=AUDIT_ACTION_RELEASED,
        resource_type=hold.resource_type,
        resource_id=str(hold.resource_id),
        status="success",
        details={
            "hold_id": str(hold.id),
            "reason": cleaned_reason,
            "placed_reason": hold.reason,
            "flagged": flagged,
        },
        commit=False,
    )
    if commit:
        db.commit()
        db.refresh(hold)
    logger.info(
        "Legal hold %s released on %s %s",
        hold_id,
        hold.resource_type,
        hold.resource_id,
    )
    return HoldOutcome(hold=hold, flagged=flagged)


def hold_summary(hold: LegalHold) -> dict[str, Any]:
    """Serializable view of one hold for API responses and exports."""
    return {
        "id": str(hold.id),
        "resource_type": hold.resource_type,
        "resource_id": hold.resource_id,
        "reason": hold.reason,
        "placed_by_user_id": (
            str(hold.placed_by_user_id) if hold.placed_by_user_id else None
        ),
        "placed_at": hold.placed_at.isoformat() if hold.placed_at else None,
        "released_by_user_id": (
            str(hold.released_by_user_id) if hold.released_by_user_id else None
        ),
        "released_at": hold.released_at.isoformat() if hold.released_at else None,
        "release_reason": hold.release_reason,
        "active": hold.released_at is None,
    }
