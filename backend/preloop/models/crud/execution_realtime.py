"""Shared realtime cursor allocation and authorized lifecycle projections."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models


def allocate_sequence(
    db: Session, execution_id: str, *, lifecycle: bool = False
) -> tuple[int, int]:
    """Atomically allocate a cursor across publishers; caller owns the session.

    A publication failure leaves a detectable hole. This counter does not imply
    durable replay or log persistence. Its independent row avoids locking the
    execution row held by a publisher's business transaction.
    """
    cursor = models.ExecutionRealtimeCursor
    stmt = insert(cursor).values(
        id=UUID(execution_id), seq=1, lifecycle_seq=int(lifecycle)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[cursor.id],
        set_={
            "seq": cursor.seq + 1,
            "lifecycle_seq": cursor.lifecycle_seq + int(lifecycle),
        },
    ).returning(cursor.seq, cursor.lifecycle_seq)
    seq = db.execute(stmt).one()
    db.commit()
    return seq[0], seq[1]


def lifecycle_snapshot(
    db: Session, execution_id: UUID, account_id: UUID
) -> dict | None:
    """Read state and cursor in one statement, scoped to the owning account."""
    execution = models.FlowExecution
    cursor = models.ExecutionRealtimeCursor
    fields = (
        "status",
        "agent_session_reference",
        "start_time",
        "end_time",
        "stop_requested_at",
        "stop_confirmed_at",
    )
    row = db.execute(
        select(
            execution.id,
            execution.flow_id,
            cursor.seq,
            cursor.lifecycle_seq,
            *(getattr(execution, name) for name in fields),
        )
        .join(models.Flow, execution.flow_id == models.Flow.id)
        .outerjoin(cursor, cursor.id == execution.id)
        .where(execution.id == execution_id, models.Flow.account_id == account_id)
    ).first()
    if row is None:
        return None
    values = dict(zip(fields, row[4:], strict=True))
    return {
        "type": "snapshot",
        "execution_id": str(row[0]),
        "flow_id": str(row[1]),
        "seq": row[2] or 0,
        "lifecycle_seq": row[3] or 0,
        "sequence_domain": "execution",
        **{
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in values.items()
        },
    }
