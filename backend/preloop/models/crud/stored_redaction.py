"""Batch reads and locks for the stored-row redaction (#2133).

Every reader is keyset paginated on ``(timestamp column, id)`` so a pass can
stop after any batch and a later pass can continue from the cursor it
returned. Rows are taken ``FOR UPDATE`` so a concurrent writer cannot change
one between the read and the rewrite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence, Tuple

from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.legal_hold import execution_in_account

#: ``(timestamp, id)`` of the last row a batch handled.
Cursor = Optional[Tuple[datetime, Any]]


def _window(
    stmt: Any, column: Any, since: Optional[datetime], until: Optional[datetime]
) -> Any:
    if since is not None:
        stmt = stmt.where(column >= since)
    if until is not None:
        stmt = stmt.where(column < until)
    return stmt


def _after(stmt: Any, column: Any, id_column: Any, cursor: Cursor) -> Any:
    if cursor is None:
        return stmt
    return stmt.where(tuple_(column, id_column) > tuple_(cursor[0], cursor[1]))


def audit_rows_batch(
    db: Session,
    *,
    account_id: Any,
    cursor: Cursor,
    since: Optional[datetime],
    until: Optional[datetime],
    limit: int,
    exclude_actions: Sequence[str] = (),
) -> list[models.AuditLog]:
    """Audit rows that store tool content (``tool_args`` or ``result_preview``).

    Read with ``populate_existing`` for the same reason the sealer does: the
    hash of a rewritten row must be taken over what the database holds.
    """
    table = models.AuditLog
    stmt = select(table).where(
        table.account_id == account_id,
        or_(
            table.details.has_key("tool_args"),  # type: ignore[attr-defined]
            table.details.has_key("result_preview"),  # type: ignore[attr-defined]
        ),
    )
    if exclude_actions:
        stmt = stmt.where(table.action.not_in(list(exclude_actions)))
    stmt = _window(stmt, table.timestamp, since, until)
    stmt = _after(stmt, table.timestamp, table.id, cursor)
    stmt = (
        stmt.order_by(table.timestamp, table.id)
        .limit(max(1, int(limit)))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(db.execute(stmt).scalars().all())


def approval_rows_batch(
    db: Session,
    *,
    account_id: Any,
    cursor: Cursor,
    since: Optional[datetime],
    until: Optional[datetime],
    limit: int,
) -> list[models.ApprovalRequest]:
    """Approval requests in the window, oldest first."""
    table = models.ApprovalRequest
    stmt = select(table).where(table.account_id == account_id)
    stmt = _window(stmt, table.requested_at, since, until)
    stmt = _after(stmt, table.requested_at, table.id, cursor)
    stmt = (
        stmt.order_by(table.requested_at, table.id)
        .limit(max(1, int(limit)))
        .with_for_update()
    )
    return list(db.execute(stmt).scalars().all())


def activity_rows_batch(
    db: Session,
    *,
    account_id: Any,
    cursor: Cursor,
    since: Optional[datetime],
    until: Optional[datetime],
    limit: int,
) -> list[models.RuntimeSessionActivity]:
    """Runtime session timeline rows that carry a summary or metadata."""
    table = models.RuntimeSessionActivity
    stmt = select(table).where(
        table.account_id == account_id,
        or_(table.summary.is_not(None), table.metadata_.is_not(None)),
    )
    stmt = _window(stmt, table.timestamp, since, until)
    stmt = _after(stmt, table.timestamp, table.id, cursor)
    stmt = (
        stmt.order_by(table.timestamp, table.id)
        .limit(max(1, int(limit)))
        .with_for_update()
    )
    return list(db.execute(stmt).scalars().all())


def search_documents_batch(
    db: Session,
    *,
    account_id: Any,
    cursor: Cursor,
    since: Optional[datetime],
    until: Optional[datetime],
    limit: int,
) -> list[models.SessionSearchDocument]:
    """Session search index chunks with content, oldest first."""
    table = models.SessionSearchDocument
    stmt = select(table).where(table.account_id == account_id, table.content != "")
    stmt = _window(stmt, table.occurred_at, since, until)
    stmt = _after(stmt, table.occurred_at, table.id, cursor)
    stmt = (
        stmt.order_by(table.occurred_at, table.id)
        .limit(max(1, int(limit)))
        .with_for_update()
    )
    return list(db.execute(stmt).scalars().all())


def held_ids(db: Session, *, account_id: Any) -> dict[str, set[str]]:
    """Ids under legal hold: runtime sessions, executions and approvals."""
    sessions = db.execute(
        select(models.RuntimeSession.id).where(
            models.RuntimeSession.account_id == account_id,
            models.RuntimeSession.legal_hold.is_(True),
        )
    ).scalars()
    executions: Any = db.execute(
        select(models.FlowExecution.id).where(
            execution_in_account(account_id),
            models.FlowExecution.legal_hold.is_(True),
        )
    ).scalars()
    approvals = db.execute(
        select(models.ApprovalRequest.id).where(
            models.ApprovalRequest.account_id == account_id,
            models.ApprovalRequest.legal_hold.is_(True),
        )
    ).scalars()
    return {
        "runtime_session": {str(value) for value in sessions},
        "execution": {str(value) for value in executions},
        "approval": {str(value) for value in approvals},
    }
