"""Remote harness sessions hosted by personal runners (#1482, contract C).

The runner owns the harness process; this module owns the server-side record
and the protocol. A session is a ``RunnerRemoteSession`` row plus a
``RuntimeSession`` (``session_source_type="runner_session"``) that carries
the timeline the console and ``preloop sessions attach`` read.

Delivery to the runner does not depend on which API replica holds its
websocket. Commands are written to the row (``first_prompt``,
``pending_turn``, ``stop_mode``) and :func:`pending_runner_messages` turns
them into ``session_start`` / ``session_turn`` / ``session_stop`` frames on
every heartbeat; a replica that holds the socket also pushes at once. The
runner deduplicates by ``remote_session_id`` and ``turn_id``.

Every step writes an audit row with ``resource_type="runner_session"`` and
the detail keys contract C names. Turn text is never written to the audit
log (only its length) and clone credentials are never stored.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_audit_log,
    crud_runner_remote_session,
    crud_runtime_session,
)
from preloop.models.schemas.runner_session import (
    RUNNER_REJECTED_PREFIX,
    SessionEventMessage,
    SessionStateMessage,
    SessionTurnDoneMessage,
    parse_runner_session_message,
)
from preloop.utils.secret_scrubbing import scrub_structure

logger = logging.getLogger(__name__)

RUNNER_SESSION_SOURCE_TYPE = "runner_session"
AUDIT_RESOURCE_TYPE = "runner_session"

#: Harnesses the runner hosts sessions for in this version (contract C,
#: wave 1). Claude Code and Codex keep their Agent Control sidecars.
RUNNER_SESSION_HARNESSES = frozenset({"copilot_cli"})

DEFAULT_IDLE_TIMEOUT_SECONDS = 1800
MIN_IDLE_TIMEOUT_SECONDS = 60
#: How long an undelivered start or turn waits before it is offered again.
REDELIVERY_AFTER = timedelta(seconds=30)
#: Slack before the server ends an idle session the online runner should
#: already have ended itself.
IDLE_GRACE = timedelta(minutes=5)
MAX_TURN_TEXT_BYTES = 256 * 1024
ACTIVITY_TEXT_LIMIT = 4000

STOP_MODES = frozenset({"graceful", "kill"})

#: Allowed runner-reported transitions. Terminal states accept nothing.
_TRANSITIONS: Dict[str, frozenset[str]] = {
    "requested": frozenset({"starting", "idle", "running", "failed", "ended"}),
    "starting": frozenset({"starting", "idle", "running", "failed", "ended"}),
    "idle": frozenset({"idle", "running", "stopping", "failed", "ended"}),
    "running": frozenset({"running", "idle", "stopping", "failed", "ended"}),
    "stopping": frozenset({"stopping", "failed", "ended"}),
    "ended": frozenset(),
    "failed": frozenset(),
}


class RunnerSessionError(Exception):
    """A request the server refuses before anything reaches the runner."""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _now() -> datetime:
    """Naive UTC, the convention of the timestamp columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _actor_name(user: Any) -> str:
    return str(
        getattr(user, "full_name", None) or getattr(user, "username", None) or ""
    )


def sanitize_workspace(workspace: Mapping[str, Any]) -> Dict[str, Any]:
    """The workspace spec as stored: never a credential (contract D)."""
    return {
        key: workspace[key]
        for key in ("kind", "id", "tracker_id", "repository", "ref")
        if isinstance(workspace.get(key), str)
    }


def audit_details(
    row: models.RunnerRemoteSession,
    runner: Optional[models.FlowRunner],
    **extra: Any,
) -> Dict[str, Any]:
    """The detail keys every ``runner_session.*`` audit row carries."""
    details: Dict[str, Any] = {
        "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
        "runner_id": str(row.runner_id),
        "runner_name": getattr(runner, "name", None),
        "host": getattr(runner, "hostname", None),
        "harness": row.harness,
        "model": row.model,
        "workspace_kind": row.workspace_kind,
        "workspace_label": row.workspace_label,
        "remote_session_id": str(row.id),
        "runtime_session_id": (
            str(row.runtime_session_id) if row.runtime_session_id else None
        ),
    }
    details.update(extra)
    return details


def _audit(
    db: Session,
    row: models.RunnerRemoteSession,
    runner: Optional[models.FlowRunner],
    action: str,
    *,
    user_id: Any = None,
    status: str = "success",
    **extra: Any,
) -> None:
    crud_audit_log.log_action(
        db,
        account_id=row.account_id,
        user_id=user_id,
        action=action,
        resource_type=AUDIT_RESOURCE_TYPE,
        resource_id=str(row.id),
        status=status,
        details=audit_details(row, runner, **extra),
        commit=False,
    )


def _runner_for(db: Session, row: models.RunnerRemoteSession) -> Any:
    return db.get(models.FlowRunner, row.runner_id)


def _activity(
    db: Session,
    row: models.RunnerRemoteSession,
    activity_type: str,
    *,
    status: Optional[str] = None,
    summary: Optional[str] = None,
    tool_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    when: Optional[datetime] = None,
) -> None:
    """Append one item to the paired runtime session's timeline."""
    if row.runtime_session_id is None:
        return
    timestamp = when or _now()
    db.add(
        models.RuntimeSessionActivity(
            account_id=row.account_id,
            runtime_session_id=row.runtime_session_id,
            activity_type=activity_type[:50],
            server_name=row.harness,
            tool_name=(tool_name or None) and str(tool_name)[:255],
            status=status,
            summary=summary,
            metadata_=scrub_structure(metadata) if metadata else None,
            timestamp=timestamp,
        )
    )
    runtime = db.get(models.RuntimeSession, row.runtime_session_id)
    if runtime is not None:
        runtime.last_activity_at = timestamp
        db.add(runtime)


def _touch(row: models.RunnerRemoteSession, when: Optional[datetime] = None) -> None:
    row.last_activity_at = when or _now()


def flows_or_tools_halted(db: Session, account_id: Any) -> bool:
    """The account kill switch covers remote sessions (#1485 T10)."""
    from preloop.services.kill_switch import flows_halted, tools_halted

    return flows_halted(db, account_id) or tools_halted(db, account_id)


# ---------------------------------------------------------------------------
# Requests from the HTTP API (#1483 calls these)


def request_session(
    db: Session,
    *,
    runner: models.FlowRunner,
    actor: models.User,
    harness: str,
    model: Optional[str],
    workspace: Mapping[str, Any],
    workspace_label: Optional[str] = None,
    first_prompt: Optional[str] = None,
    title: Optional[str] = None,
    idle_timeout_seconds: Optional[int] = None,
) -> models.RunnerRemoteSession:
    """Record a start request and its runtime session; delivery follows.

    Authorization (who may start a session on this runner) is the caller's
    job (``ACTION_RUNNER_SESSION_START``, #1483). This refuses what no caller
    may do: a halted account, or a harness the runner cannot host.

    Raises:
        RunnerSessionError: ``killed_by_kill_switch`` or
            ``harness_not_supported``.
    """
    stored_workspace = sanitize_workspace(workspace)
    kind = str(stored_workspace.get("kind") or "unknown")
    label = (workspace_label or stored_workspace.get("id") or kind)[:255]
    now = _now()
    idle = DEFAULT_IDLE_TIMEOUT_SECONDS
    if idle_timeout_seconds is not None:
        idle = max(MIN_IDLE_TIMEOUT_SECONDS, min(idle, int(idle_timeout_seconds)))
    row = models.RunnerRemoteSession(
        id=uuid.uuid4(),
        account_id=runner.account_id,
        runner_id=runner.id,
        actor_user_id=actor.id,
        harness=harness,
        model=(model or None) and model[:128],
        workspace=stored_workspace,
        workspace_kind=kind,
        workspace_label=label,
        state="requested",
        idle_timeout_seconds=idle,
        first_prompt=first_prompt or None,
        requested_at=now,
        last_activity_at=now,
    )
    refusal: Optional[RunnerSessionError] = None
    if flows_or_tools_halted(db, runner.account_id):
        refusal = RunnerSessionError(
            "killed_by_kill_switch",
            "The account kill switch is active; remote sessions cannot start.",
        )
    elif harness not in RUNNER_SESSION_HARNESSES:
        refusal = RunnerSessionError(
            "harness_not_supported",
            f"Runner-hosted sessions are not available for {harness}.",
            status_code=422,
        )
    if refusal is not None:
        crud_audit_log.log_action(
            db,
            account_id=runner.account_id,
            user_id=actor.id,
            action="runner_session.rejected",
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=str(row.id),
            status="denied",
            details=audit_details(row, runner, reason=refusal.code),
        )
        raise refusal

    runtime = crud_runtime_session.upsert_by_source(
        db,
        account_id=runner.account_id,
        session_source_type=RUNNER_SESSION_SOURCE_TYPE,
        session_source_id=str(row.id),
        runtime_principal_type="user",
        runtime_principal_id=str(actor.id),
        runtime_principal_name=_actor_name(actor) or None,
        started_at=now,
        last_activity_at=now,
    )
    runtime.title = (title or "").strip()[:255] or f"{harness} on {runner.name}"
    # The label, never the host path: the server does not know the path.
    runtime.cwd = label
    db.add(runtime)
    db.flush()
    row.runtime_session_id = runtime.id
    db.add(row)
    db.flush()
    _audit(db, row, runner, "runner_session.start_requested", user_id=actor.id)
    _activity(
        db,
        row,
        "session_state",
        status="requested",
        summary=f"Session requested by {_actor_name(actor) or actor.id}",
    )
    db.commit()
    db.refresh(row)
    return row


def queue_turn(
    db: Session,
    row: models.RunnerRemoteSession,
    *,
    actor: models.User,
    text: str,
) -> str:
    """Queue one operator turn. One turn at a time per session.

    Raises:
        RunnerSessionError: ``session_ended``, ``turn_in_progress``,
            ``killed_by_kill_switch`` or ``invalid_turn``.
    """
    if not row.is_live or row.stop_mode:
        raise RunnerSessionError("session_ended", "The session has ended.")
    if flows_or_tools_halted(db, row.account_id):
        raise RunnerSessionError(
            "killed_by_kill_switch", "The account kill switch is active."
        )
    if not text.strip() or len(text.encode("utf-8")) > MAX_TURN_TEXT_BYTES:
        raise RunnerSessionError("invalid_turn", "Turn text is empty or too long.", 422)
    if row.pending_turn or row.active_turn_id or row.first_prompt:
        raise RunnerSessionError("turn_in_progress", "A turn is already running.")
    turn_id = str(uuid.uuid4())
    row.pending_turn = {"turn_id": turn_id, "text": text}
    row.pending_turn_sent_at = None
    _touch(row)
    db.add(row)
    runner = _runner_for(db, row)
    _audit(
        db,
        row,
        runner,
        "runner_session.turn_sent",
        user_id=actor.id,
        turn_id=turn_id,
        text_length=len(text),
    )
    _activity(
        db,
        row,
        "operator_turn",
        status="queued",
        summary=text[:ACTIVITY_TEXT_LIMIT],
        metadata={"turn_id": turn_id, "actor_user_id": str(actor.id)},
    )
    db.commit()
    return turn_id


def request_stop(
    db: Session,
    row: models.RunnerRemoteSession,
    *,
    actor_user_id: Any,
    mode: str = "graceful",
    reason: str = "stopped_by_actor",
) -> None:
    """Ask the runner to stop a session. Idempotent.

    A start the runner has not been sent yet ends here without a round trip.
    """
    if not row.is_live:
        return
    mode = mode if mode in STOP_MODES else "graceful"
    runner = _runner_for(db, row)
    if row.stop_mode is None:
        _audit(
            db,
            row,
            runner,
            "runner_session.stop_requested",
            user_id=actor_user_id,
            mode=mode,
            reason=reason,
        )
    row.stop_mode = "kill" if "kill" in (mode, row.stop_mode) else mode
    row.stop_reason = row.stop_reason or reason
    db.add(row)
    if row.state == "requested" and row.start_sent_at is None:
        end_session(db, row, reason, commit=False)
    db.commit()


def end_session(
    db: Session,
    row: models.RunnerRemoteSession,
    end_reason: str,
    *,
    state: str = "ended",
    commit: bool = True,
) -> None:
    """Make a session terminal on the server and close its runtime session."""
    if not row.is_live:
        return
    now = _now()
    row.state = state
    row.end_reason = end_reason[:128]
    row.ended_at = now
    row.first_prompt = None
    row.pending_turn = None
    row.active_turn_id = None
    db.add(row)
    if row.runtime_session_id is not None:
        runtime = db.get(models.RuntimeSession, row.runtime_session_id)
        if runtime is not None and runtime.ended_at is None:
            runtime.ended_at = now
            db.add(runtime)
    _audit(
        db,
        row,
        _runner_for(db, row),
        "runner_session.ended",
        user_id=None,
        end_reason=end_reason,
    )
    _activity(db, row, "session_state", status=state, summary=f"Ended: {end_reason}")
    if commit:
        db.commit()


def end_sessions_for_kill_switch(db: Session, account_id: Any) -> int:
    """Stop every live session of an account (#1485 T10). Idempotent.

    Returns:
        How many sessions were asked to stop or ended.
    """
    count = 0
    for row in crud_runner_remote_session.list_live_for_account(
        db, account_id=account_id
    ):
        if row.stop_reason == "killed_by_kill_switch":
            continue
        request_stop(
            db,
            row,
            actor_user_id=None,
            mode="kill",
            reason="killed_by_kill_switch",
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Delivery to the runner


def build_start_message(
    db: Session,
    row: models.RunnerRemoteSession,
    *,
    credential: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The ``session_start`` frame. ``credential`` (contract D) is added to
    the frame only, never to the row."""
    actor = db.get(models.User, row.actor_user_id) if row.actor_user_id else None
    workspace = dict(row.workspace or {})
    if credential:
        workspace["credential"] = dict(credential)
    return {
        "type": "session_start",
        "remote_session_id": str(row.id),
        "harness": row.harness,
        "model": row.model,
        "workspace": workspace,
        "first_prompt": row.first_prompt,
        "actor": {
            "user_id": str(row.actor_user_id) if row.actor_user_id else "",
            "display_name": _actor_name(actor) if actor is not None else None,
        },
        "limits": {"idle_timeout_seconds": row.idle_timeout_seconds},
    }


def pending_runner_messages(
    db: Session, runner: models.FlowRunner, *, now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """Frames the runner still has to see, marked as sent.

    Stops are repeated until the runner reports the session over; starts and
    turns are offered again after :data:`REDELIVERY_AFTER`.
    """
    now = now or _now()
    messages: List[Dict[str, Any]] = []
    for row in crud_runner_remote_session.list_live_for_runner(db, runner_id=runner.id):
        if row.stop_mode:
            if row.state == "requested" and row.start_sent_at is None:
                continue
            messages.append(
                {
                    "type": "session_stop",
                    "remote_session_id": str(row.id),
                    "mode": row.stop_mode,
                }
            )
            continue
        if row.state == "requested":
            if row.start_sent_at is None or now - row.start_sent_at > REDELIVERY_AFTER:
                messages.append(build_start_message(db, row))
                row.start_sent_at = now
                db.add(row)
            continue
        turn = row.pending_turn
        if turn and row.state in ("idle", "running"):
            sent = row.pending_turn_sent_at
            if sent is None or (row.state == "idle" and now - sent > REDELIVERY_AFTER):
                messages.append(
                    {
                        "type": "session_turn",
                        "remote_session_id": str(row.id),
                        "turn_id": turn["turn_id"],
                        "text": turn["text"],
                    }
                )
                row.pending_turn_sent_at = now
                db.add(row)
    db.commit()
    return messages


async def push_pending_to_runner(db: Session, runner: models.FlowRunner) -> bool:
    """Send pending frames now when this process holds the runner socket.

    Returns:
        True when the socket was local and every frame was sent. Otherwise
        the next heartbeat on whichever replica holds it delivers them.
    """
    from preloop.api.endpoints.runners import _live

    ws = _live.get(str(runner.id))
    if ws is None:
        return False
    try:
        for message in pending_runner_messages(db, runner):
            await ws.send_json(message)
    except Exception:  # noqa: BLE001 - the heartbeat path retries
        logger.info("Immediate session delivery to runner %s failed", runner.id)
        return False
    return True


# ---------------------------------------------------------------------------
# Frames from the runner


def apply_runner_session_message(
    db: Session, runner: models.FlowRunner, raw: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Apply one runner frame. Returns frames to send back (for example a
    stop for a session the server already ended).

    Frames naming a session of another runner are ignored: a runner can only
    report on its own sessions.
    """
    try:
        message = parse_runner_session_message(dict(raw))
    except ValidationError:
        logger.warning("Invalid session frame from runner %s", runner.id)
        return []
    try:
        remote_id = uuid.UUID(str(message.remote_session_id))
    except ValueError:
        return []
    row = crud_runner_remote_session.get_for_runner(
        db, runner_id=runner.id, remote_session_id=remote_id
    )
    if row is None:
        return []
    replies: List[Dict[str, Any]] = []
    if isinstance(message, SessionStateMessage):
        replies = _apply_state(db, runner, row, message, raw)
    elif isinstance(message, SessionEventMessage):
        _apply_event(db, row, message)
    elif isinstance(message, SessionTurnDoneMessage):
        _apply_turn_done(db, row, message, raw)
    db.commit()
    return replies


def _apply_state(
    db: Session,
    runner: models.FlowRunner,
    row: models.RunnerRemoteSession,
    message: SessionStateMessage,
    raw: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    state = message.state
    if not row.is_live:
        if state not in ("ended", "failed"):
            # The server already ended it (offline sweep, kill switch):
            # the runner must not keep it alive.
            return [
                {
                    "type": "session_stop",
                    "remote_session_id": str(row.id),
                    "mode": "kill",
                }
            ]
        return []
    if state not in _TRANSITIONS.get(row.state, frozenset()):
        logger.info("Ignoring session %s transition %s -> %s", row.id, row.state, state)
        return []
    now = _now()
    _touch(row, now)
    if message.harness_session_id:
        row.harness_session_id = message.harness_session_id
    if state == "failed":
        code = message.error_code or "unknown"
        detail = str(raw.get("detail") or "")[:512] or None
        row.error_detail = detail
        reason = message.end_reason or f"{RUNNER_REJECTED_PREFIX}{code}"
        if row.state in ("requested", "starting"):
            _audit(
                db,
                row,
                runner,
                "runner_session.rejected",
                status="denied",
                reason=code,
                detail=detail,
            )
        end_session(db, row, reason, state="failed", commit=False)
        return []
    if state == "ended":
        reason = row.stop_reason or message.end_reason or "harness_exited"
        end_session(db, row, reason, commit=False)
        return []
    first_start = row.started_at is None and state in ("idle", "running")
    if row.state in ("requested", "starting") and state != "starting":
        if row.first_prompt:
            # The runner runs the first prompt as turn "first".
            row.active_turn_id = "first"
        row.first_prompt = None
    row.state = state
    if first_start:
        row.started_at = now
        _audit(db, row, runner, "runner_session.started")
        _activity(db, row, "session_state", status="started", summary="Session started")
    if state == "running" and row.pending_turn and row.pending_turn_sent_at:
        row.active_turn_id = row.pending_turn.get("turn_id")
    db.add(row)
    return []


def _apply_event(
    db: Session, row: models.RunnerRemoteSession, message: SessionEventMessage
) -> None:
    if not row.is_live:
        return
    payload = scrub_structure(dict(message.payload))
    text = payload.get("text") if isinstance(payload.get("text"), str) else None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tool_name = data.get("toolName") or data.get("tool_name") or data.get("name")
    status = {"tool_call": "requested", "tool_result": "completed"}.get(message.kind)
    _activity(
        db,
        row,
        message.kind,
        status=status,
        summary=(text or "")[:ACTIVITY_TEXT_LIMIT] or None,
        tool_name=tool_name if isinstance(tool_name, str) else None,
        metadata={"turn_id": message.turn_id, "seq": message.seq, "payload": payload},
    )
    _touch(row)
    db.add(row)


def _apply_turn_done(
    db: Session,
    row: models.RunnerRemoteSession,
    message: SessionTurnDoneMessage,
    raw: Mapping[str, Any],
) -> None:
    if row.pending_turn and row.pending_turn.get("turn_id") == message.turn_id:
        row.pending_turn = None
        row.pending_turn_sent_at = None
    if row.active_turn_id == message.turn_id:
        row.active_turn_id = None
    if message.turn_id == "first":
        row.first_prompt = None
    _touch(row)
    db.add(row)
    error_code = raw.get("error_code")
    _activity(
        db,
        row,
        "turn_done",
        status=message.status,
        summary=str(error_code)[:200] if error_code else None,
        metadata={"turn_id": message.turn_id, "usage": dict(message.usage)},
    )


# ---------------------------------------------------------------------------
# Sweeper


def sweep_runner_sessions(db: Session, *, now: Optional[datetime] = None) -> int:
    """End sessions whose runner is gone or that outlived their idle timeout.

    A runner that is offline past the idle timeout ends its sessions with
    ``runner_offline``; a runner that comes back sooner resumes them. An
    online runner ends idle sessions itself; the server only steps in after
    :data:`IDLE_GRACE` more.

    Returns:
        How many sessions ended.
    """
    from preloop.services.runner_service import is_online

    now = now or _now()
    ended = 0
    cutoff = now - timedelta(seconds=MIN_IDLE_TIMEOUT_SECONDS)
    for row in crud_runner_remote_session.list_live_inactive_since(db, cutoff=cutoff):
        idle_for = now - (row.last_activity_at or row.requested_at)
        timeout = timedelta(seconds=row.idle_timeout_seconds)
        if idle_for <= timeout:
            continue
        runner = _runner_for(db, row)
        if runner is None or not is_online(runner):
            end_session(db, row, "runner_offline", commit=False)
            ended += 1
        elif row.state == "idle" and idle_for > timeout + IDLE_GRACE:
            end_session(db, row, "idle_timeout", commit=False)
            ended += 1
    db.commit()
    return ended


# ---------------------------------------------------------------------------
# Host switches reported through the inventory


def audit_sessions_enabled_changes(
    db: Session,
    runner: models.FlowRunner,
    previous: Optional[Mapping[str, Any]],
    current: Optional[Mapping[str, Any]],
) -> List[str]:
    """Audit ``runner.sessions_enabled`` / ``runner.sessions_disabled`` when a
    runner's harness inventory flips ``sessions_enabled`` for a harness.

    The host user made the change on the machine, so the actor is the runner
    owner. Called where the inventory is stored (#1480).

    Returns:
        The actions written.
    """

    def flags(inventory: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
        entries: Iterable[Any] = (inventory or {}).get("entries") or []
        return {
            str(entry.get("harness")): bool(entry.get("sessions_enabled"))
            for entry in entries
            if isinstance(entry, Mapping) and entry.get("harness")
        }

    before, after = flags(previous), flags(current)
    written: List[str] = []
    for harness in sorted(after):
        if after[harness] == before.get(harness, False):
            continue
        action = (
            "runner.sessions_enabled" if after[harness] else "runner.sessions_disabled"
        )
        crud_audit_log.log_action(
            db,
            account_id=runner.account_id,
            user_id=getattr(runner, "registered_by_user_id", None),
            action=action,
            resource_type="runner",
            resource_id=str(runner.id),
            status="success",
            details={
                "actor_user_id": str(getattr(runner, "registered_by_user_id", "")),
                "runner_id": str(runner.id),
                "runner_name": runner.name,
                "host": runner.hostname,
                "harness": harness,
            },
            commit=False,
        )
        written.append(action)
    return written
