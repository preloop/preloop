"""Runners advertise authorized session directories without their paths (#1484).

The advertisement (id, label, mode, harnesses) is stored on the runner row
with register and heartbeat, a heartbeat that carries only host profiles
keeps it, and a runner that sent a path has it dropped before storage.
"""

from __future__ import annotations

from typing import Tuple
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.db.session import get_db_session as get_db

DIRECTORIES = [
    {"id": "dir_9f2c", "label": "ims", "mode": "write", "harnesses": "all"},
    {
        "id": "dir_aa11",
        "label": "docs",
        "mode": "read_only",
        "harnesses": ["copilot_cli"],
    },
]
PROFILES = [
    {
        "name": "copilot",
        "capabilities": ["host_exec", "copilot_cli", "stdout", "cancel"],
        "models": ["auto"],
    }
]


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


def _register(client: TestClient, **extra: object) -> Tuple[str, str, dict]:
    response = client.post("/api/v1/runners/register", json={"name": "laptop", **extra})
    assert response.status_code == 200, response.text
    body = response.json()
    return body["id"], body["token"], body


def test_register_stores_the_advertisement_without_paths(
    db_session: Session, test_user: models.User
) -> None:
    with_paths = [dict(entry, path="/home/jane/ims") for entry in DIRECTORIES]
    with _client(db_session, test_user) as client:
        runner_id, _token, body = _register(
            client, host_exec_profiles=PROFILES, authorized_directories=with_paths
        )
    assert body["capabilities"] == {
        "host_exec_profiles": PROFILES,
        "authorized_directories": DIRECTORIES,
    }
    saved = crud_flow_runner.get(
        db_session, id=UUID(runner_id), account_id=str(test_user.account_id)
    )
    assert saved is not None
    assert saved.capabilities["authorized_directories"] == DIRECTORIES
    assert "/home/jane" not in str(saved.capabilities)


def test_register_without_the_field_keeps_old_runners_working(
    db_session: Session, test_user: models.User
) -> None:
    with _client(db_session, test_user) as client:
        _runner_id, _token, body = _register(client, host_exec_profiles=PROFILES)
    assert body["capabilities"] == {"host_exec_profiles": PROFILES}


def test_heartbeat_updates_directories_and_profiles_independently(
    db_session: Session, test_user: models.User
) -> None:
    with _client(db_session, test_user) as client:
        runner_id, token, _body = _register(
            client, host_exec_profiles=PROFILES, authorized_directories=DIRECTORIES
        )
        with client.websocket_connect(
            f"/api/v1/runners/{runner_id}/ws", headers={"x-runner-token": token}
        ) as socket:
            assert socket.receive_json()["type"] == "hello"
            # A #956-era heartbeat (profiles only) keeps the directories.
            socket.send_json({"type": "heartbeat", "host_exec_profiles": PROFILES})
            assert socket.receive_json()["type"] == "ack"
            db_session.expire_all()
            saved = crud_flow_runner.get(db_session, id=UUID(runner_id))
            assert saved.capabilities["authorized_directories"] == DIRECTORIES
            assert saved.capabilities["host_exec_profiles"] == PROFILES
            # The operator removed one directory: only that list changes.
            socket.send_json(
                {"type": "heartbeat", "authorized_directories": DIRECTORIES[:1]}
            )
            assert socket.receive_json()["type"] == "ack"
            db_session.expire_all()
            saved = crud_flow_runner.get(db_session, id=UUID(runner_id))
            assert saved.capabilities["authorized_directories"] == DIRECTORIES[:1]
            assert saved.capabilities["host_exec_profiles"] == PROFILES
            # Garbage is bounded away, never stored verbatim.
            socket.send_json(
                {
                    "type": "heartbeat",
                    "authorized_directories": [
                        {"id": "../x", "mode": "write"},
                        {"id": "dir_ok", "mode": "rw"},
                        {"id": "dir_ok", "mode": "write", "path": "C:\\Users\\jane"},
                    ],
                }
            )
            assert socket.receive_json()["type"] == "ack"
            db_session.expire_all()
            saved = crud_flow_runner.get(db_session, id=UUID(runner_id))
            assert saved.capabilities["authorized_directories"] == [
                {"id": "dir_ok", "label": "dir_ok", "mode": "write", "harnesses": "all"}
            ]
            assert "jane" not in str(saved.capabilities)
