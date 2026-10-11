"""Runner-hosted remote sessions: server state machine and protocol (#1482)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud import crud_account_halt, crud_runner_remote_session
from preloop.services import runner_sessions as svc
from preloop.services.agent_control_dispatch import resolve_session_control_mode
from preloop.services.kill_switch import invalidate_kill_switch_cache

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "personal_runners"
DETAIL_KEYS = {
    "actor_user_id",
    "runner_id",
    "runner_name",
    "host",
    "harness",
    "model",
    "workspace_kind",
    "workspace_label",
}


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def runner(db_session: Session, test_user: models.User) -> models.FlowRunner:
    row = models.FlowRunner(
        account_id=test_user.account_id,
        registered_by_user_id=test_user.id,
        name="Jane laptop",
        hostname="jane-laptop.example.com",
        token_hash="test-token-hash",
        status="online",
        last_heartbeat=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _start(
    db: Session, runner: models.FlowRunner, user: models.User, **kwargs: Any
) -> models.RunnerRemoteSession:
    params: Dict[str, Any] = dict(
        runner=runner,
        actor=user,
        harness="copilot_cli",
        model="auto",
        workspace={
            "kind": "authorized_directory",
            "id": "dir_9f2c",
            "credential": {"username": "x", "token": "never-stored"},
        },
        workspace_label="ims",
        first_prompt=None,
    )
    params.update(kwargs)
    return svc.request_session(db, **params)


def _audit_rows(db: Session, row: models.RunnerRemoteSession) -> List[models.AuditLog]:
    return (
        db.query(models.AuditLog)
        .filter(models.AuditLog.resource_id == str(row.id))
        .order_by(models.AuditLog.timestamp)
        .all()
    )


def _actions(db: Session, row: models.RunnerRemoteSession) -> List[str]:
    return [a.action for a in _audit_rows(db, row)]


def _state(row: models.RunnerRemoteSession, state: str, **extra: Any) -> dict:
    return {
        "type": "session_state",
        "remote_session_id": str(row.id),
        "state": state,
        **extra,
    }


def _halt(db: Session, user: models.User, active: bool = True) -> None:
    crud_account_halt.transition_scopes(
        db,
        account_id=user.account_id,
        scopes=["flows", "tools"],
        active=active,
        user_id=user.id,
        reason="test",
    )
    invalidate_kill_switch_cache(user.account_id)


def test_request_creates_runtime_session_row_and_audit(db_session, runner, test_user):
    row = _start(db_session, runner, test_user, first_prompt="hello")

    assert row.state == "requested"
    assert "credential" not in row.workspace
    assert "never-stored" not in json.dumps(row.workspace)
    runtime = db_session.get(models.RuntimeSession, row.runtime_session_id)
    assert runtime.session_source_type == "runner_session"
    assert runtime.session_source_id == str(row.id)
    assert runtime.runtime_principal_type == "user"
    assert runtime.runtime_principal_id == str(test_user.id)
    assert runtime.cwd == "ims"
    [audit] = _audit_rows(db_session, row)
    assert audit.action == "runner_session.start_requested"
    assert audit.resource_type == "runner_session"
    assert audit.user_id == test_user.id
    assert DETAIL_KEYS <= set(audit.details)
    assert audit.details["runner_name"] == "Jane laptop"
    assert audit.details["host"] == "jane-laptop.example.com"


def test_kill_switch_refuses_new_sessions(db_session, runner, test_user):
    _halt(db_session, test_user)
    try:
        with pytest.raises(svc.RunnerSessionError) as err:
            _start(db_session, runner, test_user)
        assert err.value.code == "killed_by_kill_switch"
        audit = (
            db_session.query(models.AuditLog)
            .filter(models.AuditLog.action == "runner_session.rejected")
            .one()
        )
        assert audit.details["reason"] == "killed_by_kill_switch"
        assert audit.status == "denied"
    finally:
        _halt(db_session, test_user, active=False)


def test_unsupported_harness_is_refused(db_session, runner, test_user):
    with pytest.raises(svc.RunnerSessionError) as err:
        _start(db_session, runner, test_user, harness="cursor_cli")
    assert err.value.code == "harness_not_supported"


def test_start_frame_matches_contract_fixture_shape(db_session, runner, test_user):
    row = _start(db_session, runner, test_user, first_prompt="hi")
    fixture = json.loads((FIXTURES / "session_start.json").read_text())
    frame = svc.build_start_message(db_session, row)
    assert set(fixture) <= set(frame)
    assert frame["actor"]["user_id"] == str(test_user.id)
    with_cred = svc.build_start_message(
        db_session, row, credential={"username": "u", "token": "t", "expires_at": "x"}
    )
    assert with_cred["workspace"]["credential"]["token"] == "t"
    db_session.refresh(row)
    assert "credential" not in row.workspace


def test_pending_delivery_start_then_redelivery(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    now = _naive_now()
    first = svc.pending_runner_messages(db_session, runner, now=now)
    assert [m["type"] for m in first] == ["session_start"]
    assert svc.pending_runner_messages(db_session, runner, now=now) == []
    later = svc.pending_runner_messages(
        db_session, runner, now=now + svc.REDELIVERY_AFTER + timedelta(seconds=1)
    )
    assert [m["remote_session_id"] for m in later] == [str(row.id)]


def test_state_machine_start_turns_and_end(db_session, runner, test_user):
    row = _start(db_session, runner, test_user, first_prompt="first")
    svc.pending_runner_messages(db_session, runner)
    svc.apply_runner_session_message(db_session, runner, _state(row, "starting"))
    svc.apply_runner_session_message(
        db_session, runner, _state(row, "idle", harness_session_id="hs-1")
    )
    db_session.refresh(row)
    assert row.state == "idle"
    assert row.harness_session_id == "hs-1"
    assert row.first_prompt is None
    assert row.started_at is not None
    assert row.active_turn_id == "first"
    # The first prompt counts as the turn in flight.
    with pytest.raises(svc.RunnerSessionError) as busy:
        svc.queue_turn(db_session, row, actor=test_user, text="second")
    assert busy.value.code == "turn_in_progress"
    svc.apply_runner_session_message(
        db_session,
        runner,
        {
            "type": "session_turn_done",
            "remote_session_id": str(row.id),
            "turn_id": "first",
            "status": "ok",
            "usage": {"premium_requests": 1},
        },
    )
    turn_id = svc.queue_turn(db_session, row, actor=test_user, text="run the tests")
    frames = svc.pending_runner_messages(db_session, runner)
    assert frames == [
        {
            "type": "session_turn",
            "remote_session_id": str(row.id),
            "turn_id": turn_id,
            "text": "run the tests",
        }
    ]
    with pytest.raises(svc.RunnerSessionError):
        svc.queue_turn(db_session, row, actor=test_user, text="third")
    svc.apply_runner_session_message(db_session, runner, _state(row, "running"))
    svc.apply_runner_session_message(
        db_session,
        runner,
        {
            "type": "session_turn_done",
            "remote_session_id": str(row.id),
            "turn_id": turn_id,
            "status": "ok",
        },
    )
    db_session.refresh(row)
    assert row.pending_turn is None and row.active_turn_id is None

    svc.request_stop(db_session, row, actor_user_id=test_user.id, mode="graceful")
    assert svc.pending_runner_messages(db_session, runner) == [
        {"type": "session_stop", "remote_session_id": str(row.id), "mode": "graceful"}
    ]
    svc.apply_runner_session_message(
        db_session, runner, _state(row, "ended", end_reason="stopped_by_actor")
    )
    db_session.refresh(row)
    assert row.state == "ended" and row.end_reason == "stopped_by_actor"
    runtime = db_session.get(models.RuntimeSession, row.runtime_session_id)
    assert runtime.ended_at is not None
    assert _actions(db_session, row) == [
        "runner_session.start_requested",
        "runner_session.started",
        "runner_session.turn_sent",
        "runner_session.stop_requested",
        "runner_session.ended",
    ]
    for audit in _audit_rows(db_session, row):
        assert DETAIL_KEYS <= set(audit.details), audit.action
    turn_audit = next(
        a for a in _audit_rows(db_session, row) if a.action.endswith("turn_sent")
    )
    assert turn_audit.details["text_length"] == len("run the tests")
    assert "run the tests" not in json.dumps(turn_audit.details)


def test_runner_rejection_is_audited_and_ends(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    fixture = json.loads((FIXTURES / "session_state_rejected.json").read_text())
    fixture["remote_session_id"] = str(row.id)
    fixture["detail"] = "remote sessions are not enabled"
    svc.apply_runner_session_message(db_session, runner, fixture)
    db_session.refresh(row)
    assert row.state == "failed"
    assert row.end_reason == f"runner_rejected:{fixture['error_code']}"
    assert row.error_detail == "remote sessions are not enabled"
    actions = _actions(db_session, row)
    assert actions[-2:] == ["runner_session.rejected", "runner_session.ended"]
    rejected = _audit_rows(db_session, row)[-2]
    assert rejected.details["reason"] == fixture["error_code"]


def test_events_become_scrubbed_activity(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    svc.apply_runner_session_message(
        db_session,
        runner,
        {
            "type": "session_event",
            "remote_session_id": str(row.id),
            "turn_id": "t1",
            "seq": 0,
            "kind": "tool_call",
            "payload": {
                "event": "tool.execution_start",
                "data": {
                    "toolName": "shell",
                    "command": "curl -H 'Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789'",
                },
            },
        },
    )
    activity = (
        db_session.query(models.RuntimeSessionActivity)
        .filter(
            models.RuntimeSessionActivity.runtime_session_id == row.runtime_session_id,
            models.RuntimeSessionActivity.activity_type == "tool_call",
        )
        .one()
    )
    assert activity.tool_name == "shell"
    assert activity.server_name == "copilot_cli"
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(
        activity.metadata_
    )


def test_invalid_transition_and_foreign_runner_are_ignored(
    db_session, runner, test_user
):
    row = _start(db_session, runner, test_user)
    svc.apply_runner_session_message(db_session, runner, _state(row, "stopping"))
    db_session.refresh(row)
    assert row.state == "requested"

    other = models.FlowRunner(
        account_id=test_user.account_id,
        registered_by_user_id=test_user.id,
        name="other",
        token_hash="other-hash",
        status="online",
    )
    db_session.add(other)
    db_session.flush()
    svc.apply_runner_session_message(
        db_session, other, _state(row, "ended", end_reason="harness_exited")
    )
    db_session.refresh(row)
    assert row.is_live


def test_reconnect_report_for_server_ended_session_gets_stop(
    db_session, runner, test_user
):
    row = _start(db_session, runner, test_user)
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    svc.end_session(db_session, row, "runner_offline")
    replies = svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    assert replies == [
        {"type": "session_stop", "remote_session_id": str(row.id), "mode": "kill"}
    ]


def test_sweeper_offline_runner_after_idle_timeout(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    runner.last_heartbeat = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add(runner)
    db_session.commit()
    now = _naive_now()
    # Within the idle timeout a reconnecting runner resumes the session.
    assert svc.sweep_runner_sessions(db_session, now=now + timedelta(minutes=10)) == 0
    assert svc.sweep_runner_sessions(db_session, now=now + timedelta(minutes=31)) == 1
    db_session.refresh(row)
    assert row.state == "ended" and row.end_reason == "runner_offline"
    assert _actions(db_session, row)[-1] == "runner_session.ended"


def test_sweeper_online_runner_gets_grace(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    now = _naive_now()
    assert svc.sweep_runner_sessions(db_session, now=now + timedelta(minutes=31)) == 0
    assert svc.sweep_runner_sessions(db_session, now=now + timedelta(minutes=36)) == 1
    db_session.refresh(row)
    assert row.end_reason == "idle_timeout"


def test_kill_switch_stops_live_sessions(db_session, runner, test_user):
    unsent = _start(db_session, runner, test_user)
    live = _start(db_session, runner, test_user)
    svc.pending_runner_messages(db_session, runner)
    db_session.refresh(unsent)
    svc.apply_runner_session_message(db_session, runner, _state(live, "idle"))
    # Both starts went out above; make one look unsent to cover the path
    # that ends a session without a round trip to the runner.
    unsent.start_sent_at = None
    db_session.add(unsent)
    db_session.commit()

    assert svc.end_sessions_for_kill_switch(db_session, test_user.account_id) == 2
    db_session.refresh(unsent)
    db_session.refresh(live)
    assert unsent.state == "ended" and unsent.end_reason == "killed_by_kill_switch"
    assert live.stop_mode == "kill"
    frames = svc.pending_runner_messages(db_session, runner)
    assert frames == [
        {"type": "session_stop", "remote_session_id": str(live.id), "mode": "kill"}
    ]
    svc.apply_runner_session_message(
        db_session, runner, _state(live, "ended", end_reason="stopped_by_actor")
    )
    db_session.refresh(live)
    assert live.end_reason == "killed_by_kill_switch"
    assert svc.end_sessions_for_kill_switch(db_session, test_user.account_id) == 0


def test_control_mode_for_runner_sessions(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    runtime = db_session.get(models.RuntimeSession, row.runtime_session_id)

    decision = resolve_session_control_mode(
        db_session, account_id=str(test_user.account_id), session=runtime
    )
    assert decision.mode == "command"
    assert decision.kind == "runner_session"
    assert decision.send_path == f"/api/v1/runner-sessions/{runtime.id}/turns"

    runner.last_heartbeat = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add(runner)
    db_session.commit()
    offline = resolve_session_control_mode(
        db_session, account_id=str(test_user.account_id), session=runtime
    )
    assert (offline.mode, offline.reason_code) == ("note", "control_offline")

    svc.end_session(db_session, row, "stopped_by_actor")
    db_session.refresh(runtime)
    ended = resolve_session_control_mode(
        db_session, account_id=str(test_user.account_id), session=runtime
    )
    assert (ended.mode, ended.reason_code) == ("note", "session_ended")


def test_sessions_enabled_inventory_changes_are_audited(db_session, runner):
    def inventory(enabled: bool) -> dict:
        return {"entries": [{"harness": "copilot_cli", "sessions_enabled": enabled}]}

    assert svc.audit_sessions_enabled_changes(
        db_session, runner, None, inventory(True)
    ) == ["runner.sessions_enabled"]
    assert (
        svc.audit_sessions_enabled_changes(
            db_session, runner, inventory(True), inventory(True)
        )
        == []
    )
    assert svc.audit_sessions_enabled_changes(
        db_session, runner, inventory(True), inventory(False)
    ) == ["runner.sessions_disabled"]


@pytest.mark.asyncio
async def test_runner_ws_applies_session_frames_and_delivers_turns(
    db_session, runner, test_user, monkeypatch
):
    row = _start(db_session, runner, test_user)
    turn_frames: List[Dict[str, Any]] = []
    sent: List[Dict[str, Any]] = []
    incoming = [_state(row, "starting"), _state(row, "idle", harness_session_id="h")]

    async def receive_json() -> Dict[str, Any]:
        if not incoming:
            raise WebSocketDisconnect()
        frame = incoming.pop(0)
        if frame["state"] == "idle":
            # The operator sends a turn while the runner starts up.
            db_session.refresh(row)
            row.state = "starting"
            turn_frames.append({"turn_id": "queued"})
            row.pending_turn = {"turn_id": "queued", "text": "hello"}
            db_session.add(row)
            db_session.commit()
        return frame

    async def send_json(payload: Dict[str, Any]) -> None:
        sent.append(payload)

    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock(side_effect=send_json)
    websocket.receive_json = AsyncMock(side_effect=receive_json)
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())

    await runners.runner_ws(websocket, runner.id, db_session)

    types = [m["type"] for m in sent]
    assert types[0] == "hello"
    assert "session_start" in types
    assert {
        "type": "session_turn",
        "remote_session_id": str(row.id),
        "turn_id": "queued",
        "text": "hello",
    } in sent
    db_session.refresh(row)
    assert row.state == "idle" and row.harness_session_id == "h"
    assert not any(m.get("type") == "error" for m in sent)


def test_kill_switch_activation_endpoint_stops_sessions(
    db_session, runner, test_user, monkeypatch
):
    import inspect

    from preloop.api.endpoints import kill_switch as endpoint
    from preloop.schemas.kill_switch import KillSwitchActivateRequest

    row = _start(db_session, runner, test_user)
    svc.pending_runner_messages(db_session, runner)
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    monkeypatch.setattr(endpoint, "_ensure_toggle_authorized", lambda *a: None)
    activate = inspect.unwrap(endpoint.activate_kill_switch)
    try:
        activate(
            KillSwitchActivateRequest(scopes=["gateway"], reason="test"),
            current_user=test_user,
            db=db_session,
        )
        db_session.refresh(row)
        assert row.stop_mode is None, "a gateway-only halt leaves sessions alone"
        activate(
            KillSwitchActivateRequest(scopes=["tools"], reason="test"),
            current_user=test_user,
            db=db_session,
        )
        db_session.refresh(row)
        assert row.stop_mode == "kill"
        assert row.stop_reason == "killed_by_kill_switch"
    finally:
        _halt(db_session, test_user, active=False)
        crud_account_halt.transition_scopes(
            db_session,
            account_id=test_user.account_id,
            scopes=["gateway"],
            active=False,
            user_id=test_user.id,
        )
        invalidate_kill_switch_cache(test_user.account_id)


def _turn_done(row: models.RunnerRemoteSession, turn_id: str) -> dict:
    return {
        "type": "session_turn_done",
        "remote_session_id": str(row.id),
        "turn_id": turn_id,
        "status": "ok",
    }


def _turn_done_activities(db: Session, row: models.RunnerRemoteSession) -> int:
    return (
        db.query(models.RuntimeSessionActivity)
        .filter(
            models.RuntimeSessionActivity.runtime_session_id == row.runtime_session_id,
            models.RuntimeSessionActivity.activity_type == "turn_done",
        )
        .count()
    )


def test_restart_before_completion_frame_is_delivered(db_session, runner, test_user):
    """The runner restarts after the first turn finished but before its
    session_turn_done reached the server. On reconnect it re-reports the
    session and replays the last result; the session takes turns again."""
    row = _start(db_session, runner, test_user, first_prompt="first")
    svc.pending_runner_messages(db_session, runner)
    svc.apply_runner_session_message(db_session, runner, _state(row, "starting"))
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    svc.apply_runner_session_message(db_session, runner, _state(row, "running"))
    # ... completion lost here; the runner restarts and reconnects:
    svc.apply_runner_session_message(db_session, runner, _turn_done(row, "first"))
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    db_session.refresh(row)
    assert row.active_turn_id is None
    turn_id = svc.queue_turn(db_session, row, actor=test_user, text="next")

    # A second reconnect replays the same old result: nothing changes.
    svc.apply_runner_session_message(db_session, runner, _turn_done(row, "first"))
    db_session.refresh(row)
    assert row.pending_turn == {"turn_id": turn_id, "text": "next"}
    assert _turn_done_activities(db_session, row) == 1


def test_lost_completion_of_a_queued_turn_is_released_by_redelivery_reply(
    db_session, runner, test_user
):
    row = _start(db_session, runner, test_user)
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    turn_id = svc.queue_turn(db_session, row, actor=test_user, text="go")
    now = _naive_now()
    svc.pending_runner_messages(db_session, runner, now=now)
    svc.apply_runner_session_message(db_session, runner, _state(row, "running"))
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    # The result was lost; the turn is offered again after the delay and the
    # runner answers with the cached result instead of running it again.
    again = svc.pending_runner_messages(
        db_session, runner, now=now + svc.REDELIVERY_AFTER + timedelta(seconds=1)
    )
    assert [m["turn_id"] for m in again] == [turn_id]
    svc.apply_runner_session_message(db_session, runner, _turn_done(row, turn_id))
    db_session.refresh(row)
    assert row.pending_turn is None and row.active_turn_id is None
    svc.queue_turn(db_session, row, actor=test_user, text="and the next")


def test_turn_done_for_unknown_turn_is_ignored(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    svc.apply_runner_session_message(db_session, runner, _turn_done(row, "stale"))
    assert _turn_done_activities(db_session, row) == 0


def test_send_path_is_keyed_by_runtime_session_id(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    assert svc.runner_session_turns_path(row.runtime_session_id) == (
        f"/api/v1/runner-sessions/{row.runtime_session_id}/turns"
    )
    found = crud_runner_remote_session.get_by_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=row.runtime_session_id,
    )
    assert found is not None and found.id == row.id
    assert row.runtime_session_id != row.id


def test_first_turn_result_before_any_state_frame(db_session, runner, test_user):
    """A runner that crashed before the server saw idle/running replays
    turn_done("first") while the row is still starting: it still releases
    the first prompt, and the state frame after it does not re-block."""
    row = _start(db_session, runner, test_user, first_prompt="first")
    svc.pending_runner_messages(db_session, runner)
    svc.apply_runner_session_message(db_session, runner, _state(row, "starting"))
    svc.apply_runner_session_message(db_session, runner, _turn_done(row, "first"))
    svc.apply_runner_session_message(db_session, runner, _state(row, "idle"))
    db_session.refresh(row)
    assert row.first_prompt is None and row.active_turn_id is None
    assert _turn_done_activities(db_session, row) == 1
    svc.queue_turn(db_session, row, actor=test_user, text="next")
