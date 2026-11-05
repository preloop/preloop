"""Registration, hello and unregister for one-shot CI runners."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud.flow_runner import ONLINE_HEARTBEAT_TTL, crud_flow_runner
from preloop.models.db.session import get_db_session as get_db


def _client(db_session: Session, test_user: models.User) -> TestClient:
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    return TestClient(app)


def test_register_marks_the_row_ephemeral(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    with _client(db_session, test_user) as client:
        response = client.post(
            "/api/v1/runners/register",
            json={"name": "ci-job-1", "labels": ["ci-gha-42"], "ephemeral": True},
        )
    assert response.status_code == 200, response.text
    assert response.json()["ephemeral"] is True
    saved = crud_flow_runner.get_fresh(
        db_session, runner_id=UUID(response.json()["id"])
    )
    assert saved is not None and saved.ephemeral is True


def test_register_reaps_a_previous_job_that_never_unregistered(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed CI job leaves a row; the next registration clears it."""
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    abandoned = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "ci-job-killed",
            "token_hash": f"hash-{uuid4()}",
            "ephemeral": True,
            "status": "online",
            "last_heartbeat": datetime.now(timezone.utc)
            - ONLINE_HEARTBEAT_TTL
            - timedelta(seconds=5),
        },
    )
    abandoned_id = abandoned.id

    with _client(db_session, test_user) as client:
        response = client.post(
            "/api/v1/runners/register", json={"name": "ci-job-2", "ephemeral": True}
        )
    assert response.status_code == 200, response.text
    assert crud_flow_runner.get_fresh(db_session, runner_id=abandoned_id) is None


def test_register_defaults_to_a_persistent_runner(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    with _client(db_session, test_user) as client:
        response = client.post("/api/v1/runners/register", json={"name": "desk-mac"})
    assert response.status_code == 200, response.text
    assert response.json()["ephemeral"] is False


def _capability_writer(runner: SimpleNamespace):
    """Stand-in for the compare-and-swap that stamps the connection id."""

    def set_publication_capabilities(db, *, capabilities=None, **kwargs) -> bool:
        runner.publication_capabilities = capabilities
        return True

    return set_publication_capabilities


def _record_reported(runner: SimpleNamespace):
    """Stand-in so a MagicMock db still records the declared concurrency."""

    def set_reported_concurrency(db, *, runner, reported, commit=True):
        runner.reported_concurrency = reported
        return runner

    return set_reported_concurrency


def _ws_runner(ephemeral: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        registered_by_user_id=None,
        status="online",
        publication_capabilities=None,
        ephemeral=ephemeral,
        capabilities={},
        assignments=[],
        capacity=1,
        free_slots=1,
        reported_concurrency=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ephemeral", [True, False])
async def test_hello_reports_whether_the_row_is_ephemeral(
    monkeypatch: pytest.MonkeyPatch, ephemeral: bool
) -> None:
    runner = _ws_runner(ephemeral)
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(side_effect=[WebSocketDisconnect()])
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _capability_writer(runner),
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    hello = websocket.send_json.await_args_list[0].args[0]
    assert hello["type"] == "hello"
    assert hello["ephemeral"] is ephemeral


@pytest.mark.asyncio
async def test_heartbeat_upgrades_a_row_the_register_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI re-asserts ephemeral so a resumed row cannot outlive its job."""
    runner = _ws_runner(False)
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[{"type": "heartbeat", "ephemeral": True}, WebSocketDisconnect()]
    )
    mark = MagicMock(return_value=True)
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "mark_ephemeral", mark)
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_reported_concurrency",
        _record_reported(runner),
    )
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _capability_writer(runner),
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    mark.assert_called_once()
    assert mark.call_args.kwargs["runner_id"] == runner.id
    assert runner.reported_concurrency is None


def test_register_resume_without_concurrency_clears_a_stale_report(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-multi-slot CLI reusing a runner_id must not keep four slots."""
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    existing = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "desk-mac",
            "token_hash": f"hash-{uuid4()}",
            "status": "online",
            "reported_concurrency": 4,
        },
    )
    with _client(db_session, test_user) as client:
        response = client.post(
            "/api/v1/runners/register",
            json={"runner_id": str(existing.id), "name": "desk-mac"},
        )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    saved = crud_flow_runner.get_fresh(db_session, runner_id=existing.id)
    assert saved is not None
    assert saved.reported_concurrency is None
    assert saved.capacity == 1


@pytest.mark.asyncio
async def test_heartbeat_without_concurrency_clears_a_stale_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _ws_runner(False)
    runner.reported_concurrency = 4
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[{"type": "heartbeat"}, WebSocketDisconnect()]
    )
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_reported_concurrency",
        _record_reported(runner),
    )
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _capability_writer(runner),
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    assert runner.reported_concurrency is None


@pytest.mark.asyncio
async def test_unregister_deletes_an_ephemeral_row_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _ws_runner(True)
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[{"type": "unregister"}, WebSocketDisconnect()]
    )
    delete = MagicMock(return_value=True)
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(
        runners.crud_flow_runner, "get_fresh", lambda *args, **kwargs: runner
    )
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "delete_ephemeral", delete)
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _capability_writer(runner),
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    delete.assert_called_once()
    # By id, not by account: a sibling CI job's idle row must survive.
    assert delete.call_args.kwargs["runner_id"] == runner.id
    # The runner still gets its acknowledgement before the socket closes.
    assert websocket.send_json.await_args_list[-1].args[0] == {"type": "ack"}


@pytest.mark.asyncio
async def test_unregister_leaves_a_sibling_ci_runner_alone(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a matrix build registers several one-shot runners in one
    account. The first job to finish must delete its own row only."""
    rows = [
        crud_flow_runner.create(
            db_session,
            obj_in={
                "account_id": test_user.account_id,
                "name": name,
                "token_hash": f"hash-{uuid4()}",
                "ephemeral": True,
                "status": "online",
                "last_heartbeat": datetime.now(timezone.utc),
            },
        )
        for name in ("ci-matrix-py311", "ci-matrix-py312")
    ]
    leaving, sibling = rows[0].id, rows[1].id

    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[{"type": "unregister"}, WebSocketDisconnect()]
    )
    websocket.query_params = {"token": "tok"}
    websocket.headers = {}
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    monkeypatch.setattr(
        runners, "_authenticate_runner", lambda db, runner_id, token: rows[0]
    )

    await runners.runner_ws(websocket, leaving, db_session)

    assert crud_flow_runner.get_fresh(db_session, runner_id=leaving) is None
    assert crud_flow_runner.get_fresh(db_session, runner_id=sibling) is not None
