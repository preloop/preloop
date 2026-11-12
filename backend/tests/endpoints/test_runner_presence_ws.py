"""Presence over the runner websocket and in routing (#1506)."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.services.runner_presence import OFFLINE_AFTER
from preloop.services.runner_service import runner_console_payload


@pytest.fixture
def ws_runner(db_session: Session, test_user: models.User) -> models.FlowRunner:
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "registered_by_user_id": test_user.id,
            "name": "laptop",
            "labels": ["laptop"],
            "token_hash": "test-token",
            "status": "online",
            "concurrency": 1,
        },
    )
    db_session.commit()
    return runner


def _socket(monkeypatch, runner, messages, sent):
    queued = list(messages)

    async def receive_json() -> Dict[str, Any]:
        if not queued:
            raise WebSocketDisconnect()
        return queued.pop(0)

    async def send_json(payload: Dict[str, Any]) -> None:
        sent.append(payload)

    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock(side_effect=send_json)
    websocket.receive_json = AsyncMock(side_effect=receive_json)
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    emitted: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        runners,
        "emit_runner_updated",
        lambda row, db=None: emitted.append(runner_console_payload(row)),
    )
    return websocket, emitted


def _fresh(db_session: Session, runner_id) -> models.FlowRunner:
    db_session.expire_all()
    return crud_flow_runner.get(db_session, id=runner_id)


@pytest.mark.asyncio
async def test_going_to_sleep_then_disconnect_is_asleep(
    db_session: Session, ws_runner: models.FlowRunner, monkeypatch
) -> None:
    sent: List[Dict[str, Any]] = []
    websocket, emitted = _socket(
        monkeypatch,
        ws_runner,
        [{"type": "heartbeat", "concurrency": 1}, {"type": "going_to_sleep"}],
        sent,
    )
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    assert sent[-1] == {"type": "ack"}
    assert not any(m.get("type") == "error" for m in sent)
    saved = _fresh(db_session, ws_runner.id)
    assert saved.sleep_signaled_at is not None
    assert saved.presence_state == "asleep"
    assert runner_console_payload(saved)["presence"] == "asleep"
    # The account websocket saw the transition into asleep.
    assert "asleep" in [e["presence"] for e in emitted]


@pytest.mark.asyncio
async def test_disconnect_without_sleep_is_offline_and_reconnect_wakes(
    db_session: Session, ws_runner: models.FlowRunner, monkeypatch
) -> None:
    sent: List[Dict[str, Any]] = []
    websocket, emitted = _socket(
        monkeypatch,
        ws_runner,
        [{"type": "heartbeat", "concurrency": 1, "os_idle_seconds": 900}],
        sent,
    )
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    saved = _fresh(db_session, ws_runner.id)
    assert saved.os_idle_seconds == 900
    assert saved.presence_state == "offline"
    assert [e["presence"] for e in emitted][-1] == "offline"
    assert "idle" in [e["presence"] for e in emitted]

    crud_flow_runner.set_presence_signals(db_session, saved, sleeping=True)
    websocket, _ = _socket(
        monkeypatch, saved, [{"type": "heartbeat", "concurrency": 1}], []
    )
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    assert _fresh(db_session, ws_runner.id).sleep_signaled_at is None


def test_silent_drop_reads_offline_after_timeout(
    db_session: Session, ws_runner: models.FlowRunner
) -> None:
    """Pulled network: no disconnect event, the heartbeat just stops."""
    ws_runner.last_heartbeat = (
        datetime.now(timezone.utc) - OFFLINE_AFTER - timedelta(seconds=5)
    )
    ws_runner.presence_state = "online"
    db_session.commit()
    payload = runner_console_payload(_fresh(db_session, ws_runner.id))
    assert payload["presence"] == "offline"
    assert payload["presence_since"] == ws_runner.last_heartbeat.isoformat()


def test_routing_skips_asleep_and_offline(
    db_session: Session, ws_runner: models.FlowRunner
) -> None:
    def matched() -> List[Any]:
        return [
            row.id
            for row in crud_flow_runner.find_matching(
                db_session, account_id=ws_runner.account_id, pool="laptop"
            )
        ]

    ws_runner.last_heartbeat = datetime.now(timezone.utc)
    db_session.commit()
    assert matched() == [ws_runner.id]

    crud_flow_runner.set_presence_signals(db_session, ws_runner, sleeping=True)
    assert matched() == []

    crud_flow_runner.set_presence_signals(db_session, ws_runner, sleeping=False)
    ws_runner.last_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    assert matched() == []


def test_claim_free_slot_refuses_asleep_runner(
    db_session: Session, ws_runner: models.FlowRunner
) -> None:
    ws_runner.last_heartbeat = datetime.now(timezone.utc)
    db_session.commit()
    assert crud_flow_runner.claim_free_slot(db_session, runner_id=ws_runner.id)
    db_session.rollback()
    crud_flow_runner.set_presence_signals(db_session, ws_runner, sleeping=True)
    assert crud_flow_runner.claim_free_slot(db_session, runner_id=ws_runner.id) is None


def test_quiet_heartbeat_signals_do_not_write(
    db_session: Session, ws_runner: models.FlowRunner, monkeypatch
) -> None:
    commits = MagicMock(wraps=db_session.commit)
    monkeypatch.setattr(db_session, "commit", commits)
    crud_flow_runner.set_presence_signals(db_session, ws_runner, sleeping=False)
    assert commits.call_count == 0
    crud_flow_runner.set_presence_signals(db_session, ws_runner, os_idle_seconds=30)
    assert commits.call_count == 1
    crud_flow_runner.set_presence_signals(db_session, ws_runner, os_idle_seconds=30)
    assert commits.call_count == 1


def test_schema_presence_literal_matches_service_states() -> None:
    from typing import get_args

    from preloop.models.schemas.flow_runner import RunnerPresenceState
    from preloop.services.runner_presence import PRESENCE_STATES

    assert set(get_args(RunnerPresenceState)) == set(PRESENCE_STATES)


def test_rest_runner_response_carries_presence(
    db_session: Session, ws_runner: models.FlowRunner, test_user: models.User
) -> None:
    """The runners API (`_to_response`) exposes presence and presence_since."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from preloop.api.auth import get_current_active_user
    from preloop.models.db.session import get_db_session as get_db

    ws_runner.last_heartbeat = datetime.now(timezone.utc)
    db_session.commit()
    crud_flow_runner.set_presence_signals(db_session, ws_runner, sleeping=True)
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    with TestClient(app) as client:
        detail = client.get(f"/api/v1/runners/{ws_runner.id}")
        listed = client.get("/api/v1/runners")
    assert detail.status_code == 200, detail.text
    row = next(r for r in listed.json() if r["id"] == str(ws_runner.id))
    for body in (detail.json(), row):
        assert body["presence"] == "asleep"
        assert body["presence_since"] is not None


@pytest.mark.asyncio
async def test_sleep_holds_a_remote_session_and_wake_resumes_it(
    db_session: Session, ws_runner: models.FlowRunner, test_user, monkeypatch
) -> None:
    """#1509 over the websocket: going_to_sleep, a held turn, then a wake."""
    from preloop.services import runner_sessions as svc

    row = svc.request_session(
        db_session,
        runner=ws_runner,
        actor=test_user,
        harness="copilot_cli",
        model=None,
        workspace={"kind": "authorized_directory", "id": "dir_1"},
    )
    svc.apply_runner_session_message(
        db_session,
        ws_runner,
        {"type": "session_state", "remote_session_id": str(row.id), "state": "idle"},
    )
    websocket, _ = _socket(
        monkeypatch,
        ws_runner,
        [{"type": "heartbeat", "concurrency": 1}, {"type": "going_to_sleep"}],
        [],
    )
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    db_session.expire_all()
    row = db_session.get(models.RunnerRemoteSession, row.id)
    assert row.state == "runner_away"
    turn_id = svc.queue_turn(db_session, row, actor=test_user, text="go on")

    sent: List[Dict[str, Any]] = []
    saved = _fresh(db_session, ws_runner.id)
    websocket, _ = _socket(
        monkeypatch, saved, [{"type": "heartbeat", "concurrency": 1}], sent
    )
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    db_session.expire_all()
    row = db_session.get(models.RunnerRemoteSession, row.id)
    assert row.state == "idle"
    turns = [m for m in sent if m.get("type") == "session_turn"]
    assert [m["turn_id"] for m in turns] == [turn_id]
