"""Durable minute counters which become immutable before chain sealing."""

import json
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import audit_chain

DENIAL_ACTIONS = ("restricted_ci_denied", "agent_permission_denied")


def _current_minute_expression() -> Any:
    """Use the same database wall-clock boundary for writers and sealers."""
    return func.date_trunc("minute", func.timezone("UTC", func.clock_timestamp()))


def closed_denial_windows() -> Any:
    """Exclude the current UTC minute, including from forced zero-lag sealing."""
    minute = _current_minute_expression()
    return or_(
        models.AuditLog.action.not_in(DENIAL_ACTIONS),
        models.AuditLog.timestamp < minute,
    )


def _current_minute(db: Session) -> datetime:
    """Read wall time after obtaining the account lock, never transaction time."""
    return cast(
        datetime,
        db.execute(select(_current_minute_expression())).scalar_one(),
    )


def record_denial(
    db: Session,
    *,
    account_id: UUID,
    principal_type: str,
    principal_id: UUID,
    action: str,
    target: str,
    reason_code: str,
    message: str,
    user_id: UUID | None = None,
) -> models.AuditLog:
    """Count a denial under the same account lock used by the chain sealer.

    Read database wall time *after* locking: a request queued across a minute
    boundary must enter the new bucket, never mutate a sealed old bucket.
    The caller commits on its own audit session, even when business work is
    rolled back. No request bodies or credentials are accepted here.
    """
    if action not in DENIAL_ACTIONS:
        raise ValueError("Unsupported denial audit action")
    audit_chain.get_state(db, account_id=account_id, for_update=True)
    minute = _current_minute(db)
    key = json.dumps(
        [
            str(account_id),
            principal_type,
            str(principal_id),
            action,
            target,
            reason_code,
            message,
            minute.isoformat(),
        ],
        separators=(",", ":"),
    )
    row_id = uuid5(NAMESPACE_URL, "preloop:denial:" + key)
    row = db.get(models.AuditLog, row_id, populate_existing=True)
    if row is None:
        row = models.AuditLog(
            id=row_id,
            account_id=account_id,
            user_id=user_id,
            action=action,
            status="denied",
            resource_type="flow" if action == "restricted_ci_denied" else "tool",
            resource_id=target,
            timestamp=minute,
            details={
                "principal_type": principal_type,
                "principal_id": str(principal_id),
                "target": target,
                "reason_code": reason_code,
                "message": message,
                "count": 1,
                "window_start": minute.isoformat() + "Z",
                "window_end": (minute + timedelta(minutes=1)).isoformat() + "Z",
            },
        )
    else:
        if row.chain_seq is not None:
            raise RuntimeError("Denial window was sealed while still open")
        details = row.details
        if not isinstance(details, dict):
            raise RuntimeError("Denial counter is missing its details")
        row.details = {**details, "count": details["count"] + 1}
    db.add(row)
    db.flush()
    return row
