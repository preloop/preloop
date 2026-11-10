"""Remote sessions while the hosting runner is asleep or offline (#1509).

A lid close cannot be exercised here. The presence signals stand in for it:
``going_to_sleep`` (``sleep_signaled_at``) for a host that announces sleep
(Linux), and a dropped socket with no signal for macOS and Windows, which
show as ``offline``.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_runner
from preloop.models.crud.runner_notice import recent
from preloop.services import runner_sessions as svc
from preloop.services.agent_control_dispatch import resolve_session_control_mode
from preloop.services.runner_session_api_service import RunnerRemoteSessionService


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def runner(db_session: Session, test_user: models.User) -> models.FlowRunner:
    row = models.FlowRunner(
        account_id=test_user.account_id,
        registered_by_user_id=test_user.id,
        name="Jane laptop",
        hostname="jane-laptop.example.com",
        token_hash="away-token-hash",
        status="online",
        last_heartbeat=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _started(
    db: Session, runner: models.FlowRunner, user: models.User, state: str = "idle"
) -> models.RunnerRemoteSession:
    row = svc.request_session(
        db,
        runner=runner,
        actor=user,
        harness="copilot_cli",
        model="auto",
        workspace={"kind": "authorized_directory", "id": "dir_9f2c"},
        workspace_label="ims",
    )
    svc.apply_runner_session_message(
        db,
        runner,
        {
            "type": "session_state",
            "remote_session_id": str(row.id),
            "state": state,
            "harness_session_id": "harness-session-1",
        },
    )
    db.refresh(row)
    return row


def _sleep(db: Session, runner: models.FlowRunner) -> None:
    """``going_to_sleep``, then the socket drops (a host that announces it)."""
    crud_flow_runner.set_presence_signals(db, runner, sleeping=True)
    runner.status = "offline"
    db.add(runner)
    db.commit()


def _drop(db: Session, runner: models.FlowRunner) -> None:
    """The socket drops with no signal (macOS and Windows lid close)."""
    runner.status = "offline"
    runner.last_heartbeat = datetime.now(timezone.utc)
    db.add(runner)
    db.commit()


def _wake(db: Session, runner: models.FlowRunner) -> None:
    """The runner reconnects and heartbeats."""
    runner.status = "online"
    runner.last_heartbeat = datetime.now(timezone.utc)
    db.add(runner)
    crud_flow_runner.set_presence_signals(db, runner, sleeping=False)
    db.commit()


def _actions(db: Session, row: models.RunnerRemoteSession) -> List[str]:
    return [
        a.action
        for a in db.query(models.AuditLog)
        .filter(models.AuditLog.resource_id == str(row.id))
        .order_by(models.AuditLog.timestamp)
        .all()
    ]


def _states(db: Session, row: models.RunnerRemoteSession) -> List[Any]:
    return [
        a.status
        for a in db.query(models.RuntimeSessionActivity)
        .filter(
            models.RuntimeSessionActivity.runtime_session_id == row.runtime_session_id,
            models.RuntimeSessionActivity.activity_type == "session_state",
        )
        .order_by(models.RuntimeSessionActivity.timestamp)
        .all()
    ]


@pytest.mark.parametrize("leave", [_sleep, _drop])
def test_going_away_marks_session_and_holds_the_turn(
    db_session, runner, test_user, leave
) -> None:
    row = _started(db_session, runner, test_user)
    leave(db_session, runner)
    assert svc.mark_sessions_away(db_session, runner) == 1
    db_session.refresh(row)
    assert row.state == "runner_away" and row.is_live
    assert "runner_session.runner_away" in _actions(db_session, row)
    # A turn sent while away is held on the row, not delivered and not lost.
    turn_id = svc.queue_turn(db_session, row, actor=test_user, text="next step")
    db_session.refresh(row)
    assert row.pending_turn == {"turn_id": turn_id, "text": "next step"}
    assert svc.pending_runner_messages(db_session, runner) == []
    db_session.refresh(row)
    assert row.state == "runner_away", "an away runner gets no frames"


def test_mark_away_is_idempotent_and_skips_reachable_runner(
    db_session, runner, test_user
) -> None:
    row = _started(db_session, runner, test_user)
    assert svc.mark_sessions_away(db_session, runner) == 0
    _sleep(db_session, runner)
    assert svc.mark_sessions_away(db_session, runner) == 1
    assert svc.mark_sessions_away(db_session, runner) == 0
    assert _actions(db_session, row).count("runner_session.runner_away") == 1


def test_reconnect_inside_window_resumes_and_delivers_held_turn(
    db_session, runner, test_user
) -> None:
    row = _started(db_session, runner, test_user)
    _sleep(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    turn_id = svc.queue_turn(db_session, row, actor=test_user, text="continue")
    _wake(db_session, runner)
    frames = svc.pending_runner_messages(db_session, runner)
    db_session.refresh(row)
    assert row.state == "idle"
    assert frames == [
        {
            "type": "session_turn",
            "remote_session_id": str(row.id),
            "turn_id": turn_id,
            "text": "continue",
        }
    ]
    assert row.harness_session_id == "harness-session-1", "context is kept"
    assert _actions(db_session, row)[-2:] == [
        "runner_session.turn_sent",
        "runner_session.resumed",
    ]
    assert _states(db_session, row)[-2:] == ["runner_away", "resumed"]


def test_state_frame_from_reconnected_runner_resumes(
    db_session, runner, test_user
) -> None:
    row = _started(db_session, runner, test_user, state="running")
    _drop(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    _wake(db_session, runner)
    svc.apply_runner_session_message(
        db_session,
        runner,
        {"type": "session_state", "remote_session_id": str(row.id), "state": "running"},
    )
    db_session.refresh(row)
    assert row.state == "running"
    assert "runner_session.resumed" in _actions(db_session, row)


@pytest.mark.parametrize("leave", [_sleep, _drop])
def test_after_the_window_the_session_ends_with_audit_and_notice(
    db_session, runner, test_user, leave
) -> None:
    row = _started(db_session, runner, test_user)
    leave(db_session, runner)
    now = _naive_now()
    assert svc.sweep_runner_sessions(db_session, now=now + timedelta(minutes=5)) == 0
    db_session.refresh(row)
    assert row.state == "runner_away"
    window = timedelta(seconds=row.idle_timeout_seconds)
    assert (
        svc.sweep_runner_sessions(db_session, now=now + window + timedelta(minutes=1))
        == 1
    )
    db_session.refresh(row)
    assert (row.state, row.end_reason) == ("ended", "runner_away_timeout")
    ended = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.resource_id == str(row.id),
            models.AuditLog.action == "runner_session.ended",
        )
        .one()
    )
    assert ended.details["end_reason"] == "runner_away_timeout"
    runtime = db_session.get(models.RuntimeSession, row.runtime_session_id)
    assert runtime.ended_at is not None
    notices = recent(
        db_session, account_id=test_user.account_id, owner_user_id=test_user.id
    )
    assert notices and notices[0]["title"] == "Remote session ended"
    assert str(row.runtime_session_id) in notices[0]["href"]
    host_notices = recent(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        host_only=True,
    )
    assert all(n["title"] != "Remote session ended" for n in host_notices)


def test_window_counts_from_when_the_runner_went_away(
    db_session, runner, test_user
) -> None:
    """An old last activity does not shorten the window after a fresh sleep."""
    row = _started(db_session, runner, test_user)
    row.last_activity_at = _naive_now() - timedelta(hours=3)
    db_session.add(row)
    db_session.commit()
    _sleep(db_session, runner)
    now = _naive_now()
    assert svc.sweep_runner_sessions(db_session, now=now + timedelta(minutes=1)) == 0
    db_session.refresh(row)
    assert row.state == "runner_away"
    away = svc.runner_away_window(row, runner, now=now)
    assert away is not None and away["presence"] == "asleep"
    assert away["resume_until"] - away["away_since"] == timedelta(
        seconds=svc.DEFAULT_IDLE_TIMEOUT_SECONDS
    )


def test_sweep_resumes_a_session_whose_runner_came_back(
    db_session, runner, test_user
) -> None:
    row = _started(db_session, runner, test_user)
    _drop(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    _wake(db_session, runner)
    row.last_activity_at = _naive_now() - timedelta(minutes=5)
    db_session.add(row)
    db_session.commit()
    assert svc.sweep_runner_sessions(db_session) == 0
    db_session.refresh(row)
    assert row.state == "idle"


def test_stop_requested_while_away_goes_out_on_reconnect(
    db_session, runner, test_user
) -> None:
    row = _started(db_session, runner, test_user)
    _sleep(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    svc.request_stop(db_session, row, actor_user_id=test_user.id)
    _wake(db_session, runner)
    frames = svc.pending_runner_messages(db_session, runner)
    assert frames == [
        {"type": "session_stop", "remote_session_id": str(row.id), "mode": "graceful"}
    ]


def test_api_state_names_presence_and_resume_deadline(
    db_session, runner, test_user
) -> None:
    row = _started(db_session, runner, test_user)
    service = RunnerRemoteSessionService()
    assert (
        service.get(db_session, session_id=str(row.runtime_session_id)).resume_until
        is None
    )
    _sleep(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    record = service.get(db_session, session_id=str(row.runtime_session_id))
    assert record.state == "runner_away"
    assert record.runner_presence == "asleep"
    assert record.resume_until is not None and record.resume_until.tzinfo is not None
    assert record.resume_until - record.away_since == timedelta(
        seconds=row.idle_timeout_seconds
    )
    listed = service.list_for_runner(db_session, runner_id=str(runner.id), limit=5)
    assert listed[0].runner_presence == "asleep"
    runtime = db_session.get(models.RuntimeSession, row.runtime_session_id)
    control = resolve_session_control_mode(
        db_session, account_id=str(test_user.account_id), session=runtime
    )
    assert (control.mode, control.reason_code) == ("command", "runner_away")
    assert "asleep" in control.reason


def test_live_event_carries_the_away_state(
    db_session, runner, test_user, monkeypatch
) -> None:
    row = _started(db_session, runner, test_user)
    sent: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "preloop.services.account_realtime.emit_account_event", sent.append
    )
    _sleep(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    away = [
        event["payload"]
        for event in sent
        if event["payload"].get("status") == "runner_away"
    ]
    assert away, sent
    assert away[0]["metadata"]["presence"] == "asleep"
    assert away[0]["metadata"]["resume_until"]
    assert away[0]["runtime_session_id"] == str(row.runtime_session_id)


def _expire(db: Session, row: models.RunnerRemoteSession) -> None:
    """Move the stored away instant back past the resume window."""
    row.away_since = _naive_now() - timedelta(seconds=row.idle_timeout_seconds + 60)
    db.add(row)
    db.commit()


def test_turn_after_the_deadline_is_refused_not_dropped(
    db_session, runner, test_user
) -> None:
    """Review on #2277: a turn past resume_until must not be held and lost."""
    row = _started(db_session, runner, test_user)
    _sleep(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    _expire(db_session, row)
    runtime = db_session.get(models.RuntimeSession, row.runtime_session_id)
    control = resolve_session_control_mode(
        db_session, account_id=str(test_user.account_id), session=runtime
    )
    assert (control.mode, control.reason_code) == ("note", "session_ended")
    with pytest.raises(svc.RunnerSessionError) as exc:
        svc.queue_turn(db_session, row, actor=test_user, text="too late")
    assert exc.value.code == "session_ended"
    db_session.refresh(row)
    assert (row.state, row.end_reason) == ("ended", "runner_away_timeout")
    assert row.pending_turn is None


def test_reconnect_after_the_deadline_ends_instead_of_resuming(
    db_session, runner, test_user
) -> None:
    """Review on #2277: the window is hard even before the next sweep."""
    row = _started(db_session, runner, test_user)
    _sleep(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    svc.queue_turn(db_session, row, actor=test_user, text="held")
    _expire(db_session, row)
    _wake(db_session, runner)
    frames = svc.pending_runner_messages(db_session, runner)
    db_session.refresh(row)
    assert (row.state, row.end_reason) == ("ended", "runner_away_timeout")
    assert not any(f.get("type") == "session_turn" for f in frames)
    assert "runner_session.resumed" not in _actions(db_session, row)


def test_resume_clears_the_stored_away_instant(db_session, runner, test_user) -> None:
    row = _started(db_session, runner, test_user)
    _sleep(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    db_session.refresh(row)
    assert row.away_since is not None
    _wake(db_session, runner)
    svc.pending_runner_messages(db_session, runner)
    db_session.refresh(row)
    assert row.state == "idle" and row.away_since is None


def test_timeout_notice_without_a_workspace_label(
    db_session, runner, test_user
) -> None:
    row = _started(db_session, runner, test_user)
    row.workspace_label = None
    db_session.add(row)
    db_session.commit()
    _sleep(db_session, runner)
    svc.mark_sessions_away(db_session, runner)
    _expire(db_session, row)
    svc.sweep_runner_sessions(db_session, now=_naive_now() + timedelta(minutes=2))
    notices = recent(
        db_session, account_id=test_user.account_id, owner_user_id=test_user.id
    )
    assert notices and "None" not in notices[0]["message"]
    assert notices[0]["message"].endswith("copilot_cli session ended.")
