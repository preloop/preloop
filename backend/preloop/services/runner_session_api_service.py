"""Runner session service for the HTTP API (#1483 seam, #1482 backend).

The routes in ``preloop.api.endpoints.runner_sessions`` authorize, validate,
mint the clone credential and audit. This adapter persists through
``preloop.services.runner_sessions`` and delivers frames to the runner. The
routes own the audit trail, so the service functions are called with
``audit=False``.

Threading: every database read and write happens in the sync ``persist_*``
methods, which the routes call on the threadpool. The async methods, awaited
on the event loop where the runner socket lives, only send the frames the
sync step prepared.

The clone credential (#1484) only ever travels in the ``session_start`` frame
sent over the runner socket held by this process. It is never stored, so a
start that is delivered later by a heartbeat (another replica holds the
socket, or the send failed) carries no credential and the runner refuses the
checkout with ``checkout_failed``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_runner, crud_runner_remote_session
from preloop.services import runner_sessions as sessions

logger = logging.getLogger(__name__)


def _error(code: str, message: str) -> Exception:
    from preloop.api.endpoints.runner_sessions import RunnerSessionError

    return RunnerSessionError(code, message)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def to_record(
    row: models.RunnerRemoteSession, runner: Optional[models.FlowRunner] = None
) -> Any:
    """The endpoint's view of one stored session.

    Args:
        row: The session.
        runner: Its runner, for the ``runner_away`` window (#1509).
    """
    from preloop.api.endpoints.runner_sessions import RemoteSessionRecord

    workspace = dict(row.workspace or {})
    workspace.setdefault("label", row.workspace_label)
    away = (
        sessions.runner_away_window(row, runner)
        if row.state == sessions.RUNNER_AWAY
        else None
    )
    return RemoteSessionRecord(
        session_id=str(row.runtime_session_id or row.id),
        remote_session_id=str(row.id),
        runner_id=str(row.runner_id),
        account_id=str(row.account_id),
        actor_user_id=str(row.actor_user_id) if row.actor_user_id else None,
        harness=row.harness,
        model=row.model,
        state=row.state,
        workspace=workspace,
        turn_in_progress=bool(
            row.pending_turn or row.active_turn_id or row.first_prompt
        ),
        started_at=row.started_at or row.requested_at,
        last_activity_at=row.last_activity_at,
        end_reason=row.end_reason,
        runner_presence=away["presence"] if away else None,
        away_since=_aware(away["away_since"]) if away else None,
        resume_until=_aware(away["resume_until"]) if away else None,
    )


def _runner_socket(runner_id: Any) -> Any:
    from preloop.api.endpoints.runners import _live

    return _live.get(str(runner_id))


async def _send(runner_id: str, frames: List[Dict[str, Any]]) -> None:
    """Send prepared frames on the local runner socket, best effort."""
    ws = _runner_socket(runner_id)
    if ws is None:
        return
    try:
        for frame in frames:
            await ws.send_json(frame)
    except Exception:  # noqa: BLE001 - the heartbeat path redelivers
        logger.info("Immediate session delivery to runner %s failed", runner_id)
    finally:
        frames.clear()


class RunnerRemoteSessionService:
    """Implements ``RunnerSessionService`` on ``runner_remote_sessions``."""

    def count_active(self, db: Session, *, runner_id: str) -> int:
        return len(
            crud_runner_remote_session.list_live_for_runner(db, runner_id=runner_id)
        )

    def list_for_runner(self, db: Session, *, runner_id: str, limit: int) -> List[Any]:
        rows = crud_runner_remote_session.list_for_runner(
            db, runner_id=runner_id, limit=limit
        )
        runner = crud_flow_runner.get(db, id=runner_id) if rows else None
        return [to_record(row, runner) for row in rows]

    def get(self, db: Session, *, session_id: str) -> Optional[Any]:
        row = crud_runner_remote_session.get_by_reference(db, session_ref=session_id)
        if row is None:
            return None
        runner = (
            crud_flow_runner.get(db, id=row.runner_id)
            if row.state == sessions.RUNNER_AWAY
            else None
        )
        return to_record(row, runner)

    def _row(self, db: Session, record: Any) -> models.RunnerRemoteSession:
        row = crud_runner_remote_session.get_for_account(
            db,
            account_id=record.account_id,
            remote_session_id=record.remote_session_id,
        )
        if row is None:
            raise _error("session_ended", "The session no longer exists.")
        return row

    # -- sync steps (threadpool) -------------------------------------------

    def persist_start(self, db: Session, plan: Any) -> None:
        """Reserve the slot atomically, store the session, build the frame."""
        limit = int((plan.limits or {}).get("max_concurrent") or 0)
        # The runner row lock serializes concurrent starts on one runner, so
        # the count and the insert cannot interleave.
        crud_runner_remote_session.lock_runner(db, runner_id=plan.runner.id)
        runner = crud_flow_runner.get(db, id=plan.runner.id)
        if runner is not None and runner.paused:
            db.rollback()
            raise _error(
                "runner_paused", "Resume the runner before starting a session."
            )
        if limit and self.count_active(db, runner_id=str(plan.runner.id)) >= limit:
            db.commit()
            raise _error(
                "max_concurrent_reached",
                "This runner already has the maximum number of remote sessions.",
            )
        try:
            row = sessions.request_session(
                db,
                runner=plan.runner,
                actor=plan.actor,
                harness=plan.harness,
                model=plan.model,
                workspace=plan.workspace,
                workspace_label=plan.workspace.get("label"),
                first_prompt=plan.first_prompt,
                title=plan.title,
                idle_timeout_seconds=(plan.limits or {}).get("idle_timeout_seconds"),
                session_id=plan.remote_session_id,
                audit=False,
            )
        except sessions.RunnerSessionError as exc:
            db.commit()
            raise _error(exc.code, str(exc)) from None
        record = to_record(row)
        if _runner_socket(row.runner_id) is not None:
            # Marked before the send: a heartbeat must not deliver a
            # credential-less copy of this start first.
            record.outgoing.append(
                sessions.build_start_message(db, row, credential=plan.credential)
            )
            row.start_sent_at = datetime.now(timezone.utc)
            db.add(row)
            db.commit()
        plan.record = record

    def persist_turn(
        self, db: Session, record: Any, *, turn_id: str, text: str
    ) -> None:
        row = self._row(db, record)
        try:
            sessions.queue_turn(
                db, row, actor=None, text=text, turn_id=turn_id, audit=False
            )
        except sessions.RunnerSessionError as exc:
            raise _error(exc.code, str(exc)) from None
        record.outgoing.extend(self._pending(db, row))

    def persist_stop(self, db: Session, record: Any, *, mode: str) -> Any:
        row = self._row(db, record)
        sessions.request_stop(db, row, actor_user_id=None, mode=mode, audit=False)
        frames = self._pending(db, row)
        db.refresh(row)
        updated = to_record(row)
        updated.outgoing.extend(frames)
        return updated

    def _pending(
        self, db: Session, row: models.RunnerRemoteSession
    ) -> List[Dict[str, Any]]:
        """Frames for this runner, marked sent, when this process holds it."""
        if _runner_socket(row.runner_id) is None:
            return []
        runner = crud_flow_runner.get(db, id=row.runner_id)
        return sessions.pending_runner_messages(db, runner) if runner else []

    # -- async steps (event loop): socket sends only -------------------------

    async def start(self, db: Session, plan: Any) -> Any:
        record = plan.record
        if record is None:
            raise _error("remote_sessions_unavailable", "The start was not prepared.")
        await _send(record.runner_id, record.outgoing)
        return record

    async def send_turn(
        self, db: Session, record: Any, *, turn_id: str, text: str
    ) -> None:
        await _send(record.runner_id, record.outgoing)

    async def stop(self, db: Session, record: Any, *, mode: str) -> Any:
        await _send(record.runner_id, record.outgoing)
        return record
