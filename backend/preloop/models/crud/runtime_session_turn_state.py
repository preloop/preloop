"""Derive ``runtime_session.turn_state`` on write.

One agent state per session, so lists and headers stop guessing "working"
from recency. The state moves on the rows that already describe a turn:

* ``user_prompt`` and ``tool_end`` mean the agent is ``working``;
* a pending approval for the session means ``waiting_for_approval``, and that
  wins over working until the last pending approval is resolved;
* ``turn_end`` and ``waiting_for_input`` mean ``your_turn``;
* ``session_end`` means ``ended``, which nothing re-opens.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import event, func
from sqlalchemy.orm import Session

from preloop.models import models

RuntimeSession = models.RuntimeSession
ApprovalRequest = models.ApprovalRequest

TURN_STATE_WORKING = "working"
TURN_STATE_WAITING_FOR_APPROVAL = "waiting_for_approval"
TURN_STATE_YOUR_TURN = "your_turn"
TURN_STATE_ENDED = "ended"
TURN_STATES = (
    TURN_STATE_WORKING,
    TURN_STATE_WAITING_FOR_APPROVAL,
    TURN_STATE_YOUR_TURN,
    TURN_STATE_ENDED,
)

#: Activity row kind to the state it implies.
ACTIVITY_TURN_STATES = {
    "user_prompt": TURN_STATE_WORKING,
    "tool_end": TURN_STATE_WORKING,
    "turn_end": TURN_STATE_YOUR_TURN,
    "waiting_for_input": TURN_STATE_YOUR_TURN,
    "session_end": TURN_STATE_ENDED,
}

_TERMINAL_APPROVAL_STATUSES = frozenset(
    {"approved", "declined", "expired", "cancelled", "failed"}
)


def next_turn_state(current: Optional[str], activity_type: str) -> Optional[str]:
    """Return the state after one activity row, or ``current`` when unchanged.

    Args:
        current: The session's state before the row.
        activity_type: The new row's kind.

    Returns:
        The new state. ``ended`` is final; a tool end does not clear a pending
        approval, because the approval belongs to a different call.
    """
    implied = ACTIVITY_TURN_STATES.get(activity_type)
    if implied is None or current == TURN_STATE_ENDED:
        return current
    if implied == TURN_STATE_WORKING and current == TURN_STATE_WAITING_FOR_APPROVAL:
        return current
    return implied


def apply_activity(
    session: Optional[RuntimeSession],
    activity_type: str,
    timestamp: Optional[datetime] = None,
    *,
    interrupted: bool = False,
) -> Optional[str]:
    """Move ``session.turn_state`` and the turn bounds for one row (no flush).

    A prompt opens a turn (``turn_started_at``, clears the end); a turn end
    or a wait for input closes it once (``turn_ended_at``, and
    ``turn_interrupted`` for an interrupted Stop).
    """
    if session is None:
        return None
    if session.ended_at is not None and activity_type != "session_end":
        session.turn_state = TURN_STATE_ENDED
        return session.turn_state
    at = _naive_utc(timestamp)
    if activity_type == "user_prompt":
        session.turn_started_at = at
        session.turn_ended_at = None
        session.turn_interrupted = None
    elif activity_type in ("turn_end", "waiting_for_input"):
        if session.turn_ended_at is None:
            session.turn_ended_at = at
        if activity_type == "turn_end":
            session.turn_interrupted = bool(interrupted)
    session.turn_state = next_turn_state(session.turn_state, activity_type)
    return session.turn_state


def _naive_utc(value: Optional[datetime]) -> datetime:
    """The column is a naive UTC timestamp, like ``started_at``."""
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _has_other_pending(db: Session, runtime_session_id: Any, exclude: Any) -> bool:
    query = db.query(func.count(ApprovalRequest.id)).filter(
        ApprovalRequest.runtime_session_id == runtime_session_id,
        ApprovalRequest.status == "pending",
    )
    if exclude is not None:
        query = query.filter(ApprovalRequest.id != exclude)
    return bool(query.scalar())


def release_approval_wait(
    db: Session, runtime_session_ids: Iterable[Any], *, exclude: Any = None
) -> None:
    """Return sessions to ``working`` once no approval is pending for them.

    Called for approvals resolved in bulk (expiry, cancellation), which the
    flush listener cannot see because they are ``UPDATE`` statements.
    """
    with db.no_autoflush:
        for session_id in {sid for sid in runtime_session_ids if sid is not None}:
            session = db.get(RuntimeSession, session_id)
            if session is None or session.turn_state != (
                TURN_STATE_WAITING_FOR_APPROVAL
            ):
                continue
            if not _has_other_pending(db, session_id, exclude):
                session.turn_state = TURN_STATE_WORKING


def _status_changed(obj: Any) -> bool:
    from sqlalchemy import inspect

    history = inspect(obj).attrs.status.history
    return bool(history.added)


@event.listens_for(Session, "before_flush")
def _approval_turn_state(session: Session, flush_context: Any, instances: Any) -> None:
    """Keep ``turn_state`` in step with approvals written through the ORM."""
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, ApprovalRequest) or obj.runtime_session_id is None:
            continue
        if obj not in session.new and not _status_changed(obj):
            continue
        with session.no_autoflush:
            runtime_session = session.get(RuntimeSession, obj.runtime_session_id)
            if runtime_session is None or runtime_session.ended_at is not None:
                continue
            if obj.status == "pending":
                runtime_session.turn_state = TURN_STATE_WAITING_FOR_APPROVAL
            elif obj.status in _TERMINAL_APPROVAL_STATUSES:
                if (
                    runtime_session.turn_state == TURN_STATE_WAITING_FOR_APPROVAL
                    and not _has_other_pending(session, obj.runtime_session_id, obj.id)
                ):
                    runtime_session.turn_state = TURN_STATE_WORKING
