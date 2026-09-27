"""Deleting a persistent runner and rotating its token (#841)."""

from datetime import datetime, timezone
from typing import Any, Dict, Tuple
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_flow,
    crud_flow_execution,
)
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.db.session import get_db_session as get_db
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


@pytest.fixture(autouse=True)
def _quiet_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    monkeypatch.setattr(
        runners, "emit_runner_deleted", lambda *args: None, raising=False
    )


def _client(db_session: Session, test_user: models.User) -> TestClient:
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    return TestClient(app)


def _register(client: TestClient, name: str = "build-host") -> Tuple[str, str]:
    response = client.post("/api/v1/runners/register", json={"name": name})
    assert response.status_code == 200, response.text
    body = response.json()
    return body["id"], body["token"]


def _first_frame(client: TestClient, runner_id: str, token: str) -> Dict[str, Any]:
    with client.websocket_connect(
        f"/api/v1/runners/{runner_id}/ws", headers={"x-runner-token": token}
    ) as socket:
        return socket.receive_json()


def _leased_execution(
    db: Session, account_id: UUID, runner_id: UUID
) -> models.FlowExecution:
    flow = crud_flow.create(
        db,
        account_id=account_id,
        flow_in=FlowCreate(
            name=f"flow-{uuid4()}",
            account_id=account_id,
            agent_type="codex",
            agent_config={},
            prompt_template="Implement the issue",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
        ),
    )
    execution = crud_flow_execution.create(
        db, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    execution.runner_id = runner_id
    execution.agent_session_reference = f"runner:{runner_id}:{execution.id}"
    db.add(execution)
    db.commit()
    crud_flow_runner.create_assignment(
        db,
        runner_id=runner_id,
        execution_id=execution.id,
        pending_job={"execution_id": str(execution.id)},
    )
    return execution


def test_deleted_runner_is_rejected_on_the_websocket(
    db_session: Session, test_user: models.User
) -> None:
    with _client(db_session, test_user) as client:
        runner_id, token = _register(client)
        assert _first_frame(client, runner_id, token)["type"] == "hello"

        response = client.delete(f"/api/v1/runners/{runner_id}")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "id": runner_id,
            "deleted": True,
            "halted_execution_ids": [],
        }

        assert _first_frame(client, runner_id, token) == {
            "type": "error",
            "error": "unauthorized",
        }
        assert client.get(f"/api/v1/runners/{runner_id}").status_code == 404
    assert crud_flow_runner.get_fresh(db_session, runner_id=UUID(runner_id)) is None


def test_rotating_the_token_rejects_the_old_one_and_accepts_the_new_one(
    db_session: Session, test_user: models.User
) -> None:
    with _client(db_session, test_user) as client:
        runner_id, old_token = _register(client)

        response = client.post(f"/api/v1/runners/{runner_id}/token")
        assert response.status_code == 200, response.text
        new_token = response.json()["token"]
        assert new_token and new_token != old_token
        assert response.json()["id"] == runner_id

        assert _first_frame(client, runner_id, old_token)["error"] == "unauthorized"
        assert _first_frame(client, runner_id, new_token)["type"] == "hello"

        # The token is returned once: reading the runner never shows it.
        assert "token" not in client.get(f"/api/v1/runners/{runner_id}").json()


def test_a_socket_opened_with_the_old_token_is_closed_by_rotation(
    db_session: Session, test_user: models.User
) -> None:
    """A copied token that is already connected loses its session too."""
    with _client(db_session, test_user) as client:
        runner_id, old_token = _register(client)
        with client.websocket_connect(
            f"/api/v1/runners/{runner_id}/ws", headers={"x-runner-token": old_token}
        ) as socket:
            assert socket.receive_json()["type"] == "hello"
            rotated = client.post(f"/api/v1/runners/{runner_id}/token")
            assert rotated.status_code == 200, rotated.text
            assert socket.receive_json() == {
                "type": "error",
                "error": "Runner token was rotated",
            }


def test_a_socket_on_another_replica_ends_on_its_next_frame_after_rotation(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rotating process cannot reach the socket; the session loop can."""
    with _client(db_session, test_user) as client:
        runner_id, old_token = _register(client)
        with client.websocket_connect(
            f"/api/v1/runners/{runner_id}/ws", headers={"x-runner-token": old_token}
        ) as socket:
            assert socket.receive_json()["type"] == "hello"
            # What another replica sees: the hash changed, nobody evicted us.
            monkeypatch.setattr(runners, "_evict_live_runner", AsyncMock())
            rotated = client.post(f"/api/v1/runners/{runner_id}/token")
            assert rotated.status_code == 200, rotated.text
            socket.send_json({"type": "heartbeat"})
            assert socket.receive_json() == {
                "type": "error",
                "error": "Runner token was rotated",
            }


def test_delete_refuses_a_runner_holding_a_lease_without_force(
    db_session: Session, test_user: models.User
) -> None:
    with _client(db_session, test_user) as client:
        runner_id, token = _register(client)
        execution = _leased_execution(db_session, test_user.account_id, UUID(runner_id))

        response = client.delete(f"/api/v1/runners/{runner_id}")
        assert response.status_code == 409, response.text
        assert "force=true" in response.json()["detail"]

        # Nothing changed: the runner, its lease and its token all survive.
        assert _first_frame(client, runner_id, token)["type"] == "hello"
    assert crud_flow_runner.get_fresh(db_session, runner_id=UUID(runner_id))
    assert crud_flow_runner.get_assignment(
        db_session, runner_id=UUID(runner_id), execution_id=execution.id
    )
    db_session.refresh(execution)
    assert execution.status == "RUNNING"
    assert execution.stop_requested_at is None


def test_force_delete_halts_the_leases_and_deletes_the_runner(
    db_session: Session, test_user: models.User
) -> None:
    with _client(db_session, test_user) as client:
        runner_id, token = _register(client)
        execution = _leased_execution(db_session, test_user.account_id, UUID(runner_id))

        response = client.delete(f"/api/v1/runners/{runner_id}?force=true")
        assert response.status_code == 200, response.text
        assert response.json()["halted_execution_ids"] == [str(execution.id)]

        assert _first_frame(client, runner_id, token)["error"] == "unauthorized"
    assert crud_flow_runner.get_fresh(db_session, runner_id=UUID(runner_id)) is None
    stopped = crud_flow_execution.get(db_session, id=execution.id, refresh=True)
    assert stopped.status == "STOPPED"
    assert stopped.end_time is not None
    assert stopped.stop_source == "runner_deleted"
    assert stopped.stop_requested_at is not None
    # Nobody is left to report termination, so the stop is settled here and
    # the execution monitor does not wait on a runner that cannot connect.
    assert stopped.stop_confirmed_at is not None


def test_force_delete_revokes_the_runtime_key_and_fails_the_publication(
    db_session: Session, test_user: models.User
) -> None:
    """The job may still be running on the host; its credentials must not."""
    with _client(db_session, test_user) as client:
        runner_id, _ = _register(client)
        execution = _leased_execution(db_session, test_user.account_id, UUID(runner_id))
        execution.result = {"_private_publication": {"nonce": "n-1", "phase": "agent"}}
        db_session.add(execution)
        db_session.commit()
        runtime_key, _ = crud_api_key.create_runtime_key(
            db_session,
            name="Leased flow runtime key",
            account_id=test_user.account_id,
            user_id=test_user.id,
            scopes=["mcp:read"],
            context_data={"flow_execution_id": str(execution.id)},
        )

        response = client.delete(f"/api/v1/runners/{runner_id}?force=true")
        assert response.status_code == 200, response.text
    db_session.refresh(runtime_key)
    assert runtime_key.is_active is False
    stopped = crud_flow_execution.get(db_session, id=execution.id, refresh=True)
    assert stopped.result["_private_publication"]["phase"] == "failed"


def test_force_delete_keeps_an_execution_that_already_finished(
    db_session: Session, test_user: models.User
) -> None:
    with _client(db_session, test_user) as client:
        runner_id, _ = _register(client)
        execution = _leased_execution(db_session, test_user.account_id, UUID(runner_id))
        execution.status = "SUCCEEDED"
        execution.end_time = datetime.now(timezone.utc)
        db_session.add(execution)
        db_session.commit()

        response = client.delete(f"/api/v1/runners/{runner_id}?force=true")
        assert response.status_code == 200, response.text
    finished = crud_flow_execution.get(db_session, id=execution.id, refresh=True)
    assert finished.status == "SUCCEEDED"
    assert finished.stop_source is None


def test_delete_and_rotate_do_not_reach_another_accounts_runner(
    db_session: Session, test_user: models.User
) -> None:
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other org", "is_active": True}
    )
    foreign = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": other.id,
            "name": "foreign",
            "token_hash": "foreign-hash",
            "status": "online",
        },
    )
    with _client(db_session, test_user) as client:
        assert client.delete(f"/api/v1/runners/{foreign.id}").status_code == 404
        assert client.post(f"/api/v1/runners/{foreign.id}/token").status_code == 404
    kept = crud_flow_runner.get_fresh(db_session, runner_id=foreign.id)
    assert kept is not None and kept.token_hash == "foreign-hash"


@pytest.mark.parametrize("action", ["delete", "rotate"])
def test_delete_and_rotate_close_the_live_socket(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    with _client(db_session, test_user) as client:
        runner_id, _ = _register(client)
        live = MagicMock()
        live.send_json = AsyncMock()
        live.close = AsyncMock()
        monkeypatch.setitem(runners._live, runner_id, live)

        if action == "delete":
            response = client.delete(f"/api/v1/runners/{runner_id}")
        else:
            response = client.post(f"/api/v1/runners/{runner_id}/token")
        assert response.status_code == 200, response.text

    frames = [call.args[0] for call in live.send_json.await_args_list]
    assert frames[-1]["type"] == "error"
    live.close.assert_awaited_once_with(code=1008)
    assert runner_id not in runners._live
