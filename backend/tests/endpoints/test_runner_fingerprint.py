"""Runner discovery keys persist without widening their visibility."""

from datetime import datetime, timezone

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import runners
from preloop.models import models, schemas
from preloop.models.crud.flow_runner import crud_flow_runner
from tests.endpoints.test_runner_ephemeral_register import (
    _client,
    _ws_runner,
    _capability_writer,
    _record_reported,
)

FINGERPRINT = hmac.new(
    b"a" * 64, b"preloop-workstation:v1:fixed-machine", hashlib.sha256
).hexdigest()


@pytest.mark.parametrize("fingerprint", [None, FINGERPRINT])
@pytest.mark.parametrize("ephemeral", [False, True])
def test_register_and_resume_fingerprint(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    fingerprint: str | None,
    ephemeral: bool,
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    body = {"name": "workstation", "ephemeral": ephemeral}
    if fingerprint is not None:
        body["workstation_fingerprint"] = fingerprint
    with _client(db_session, test_user) as client:
        response = client.post("/api/v1/runners/register", json=body)
        assert response.status_code == 200, response.text
        runner_id = UUID(response.json()["id"])
        expected = None if ephemeral else fingerprint
        assert response.json()["workstation_fingerprint"] == expected
        saved = crud_flow_runner.get_fresh(db_session, runner_id=runner_id)
        assert saved.workstation_fingerprint == expected
        # A new salt is sent on resume, while old CLIs can omit the field.
        body["runner_id"] = str(runner_id)
        body["workstation_fingerprint"] = "b" * 64
        resumed = client.post("/api/v1/runners/register", json=body)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["workstation_fingerprint"] == (
            None if ephemeral else "b" * 64
        )
        body.pop("workstation_fingerprint")
        legacy = client.post("/api/v1/runners/register", json=body)
        assert legacy.status_code == 200
        assert legacy.json()["workstation_fingerprint"] is None


@pytest.mark.parametrize(
    "owner,permission,same_account,visible",
    [
        (True, False, True, True),
        (False, True, True, True),
        (False, False, True, False),
        (False, True, False, False),
        (True, True, False, False),
    ],
)
def test_fingerprint_visibility(
    monkeypatch: pytest.MonkeyPatch,
    owner: bool,
    permission: bool,
    same_account: bool,
    visible: bool,
) -> None:
    runner = models.FlowRunner(
        id=uuid4(),
        account_id=uuid4(),
        registered_by_user_id=uuid4(),
        name="workstation",
        status="offline",
        labels=[],
        capabilities={},
        concurrency=2,
        paused=False,
        accept_untagged=False,
        ephemeral=False,
        workstation_fingerprint=FINGERPRINT,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    viewer = models.User(
        id=runner.registered_by_user_id if owner else uuid4(),
        account_id=runner.account_id if same_account else uuid4(),
    )
    monkeypatch.setattr(runners, "user_holds_permission", lambda *args: permission)
    monkeypatch.setattr(runners.crud_user, "get", lambda *args, **kwargs: None)
    response = runners._to_response(runner, MagicMock(), viewer)
    assert response.workstation_fingerprint == (FINGERPRINT if visible else None)
    assert runners._to_response(runner).workstation_fingerprint is None


@pytest.mark.parametrize("fingerprint", ["x", "a" * 65, "A" * 64, 42])
def test_invalid_registration_fingerprint(fingerprint: object) -> None:
    with pytest.raises(ValueError):
        schemas.RunnerRegisterRequest(workstation_fingerprint=fingerprint)


@pytest.mark.asyncio
@pytest.mark.parametrize("ephemeral", [False, True])
@pytest.mark.parametrize("fingerprint", [None, "bad", "b" * 64])
async def test_heartbeat_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    ephemeral: bool,
    fingerprint: str | None,
) -> None:
    runner = _ws_runner(ephemeral)
    runner.workstation_fingerprint = FINGERPRINT
    message = {"type": "heartbeat"}
    if fingerprint is not None:
        message["workstation_fingerprint"] = fingerprint
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(side_effect=[message, WebSocketDisconnect()])
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _capability_writer(runner),
    )
    monkeypatch.setattr(
        runners.crud_flow_runner, "set_reported_concurrency", _record_reported(runner)
    )

    def update(db: Session, *, db_obj: object, obj_in: dict) -> object:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    monkeypatch.setattr(runners.crud_flow_runner, "update", update)
    await runners.runner_ws(websocket, runner.id, MagicMock())
    expected = (
        None if ephemeral else (fingerprint if fingerprint == "b" * 64 else FINGERPRINT)
    )
    assert runner.workstation_fingerprint == expected


def test_runner_and_discovery_store_equal_keys(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both report paths retain the same account-salted HMAC unchanged."""
    from preloop.models.crud import crud_account, crud_discovered_agent_candidate

    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    account = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(
        db_session, db_obj=account, obj_in={"primary_user_id": test_user.id}
    )
    salt_response = client.get("/api/v1/agents/discovery-salt")
    assert salt_response.status_code == 200, salt_response.text
    salt = salt_response.json()["salt"]
    fingerprint = hmac.new(
        salt.encode(), b"preloop-workstation:v1:fixed-machine", hashlib.sha256
    ).hexdigest()
    registered = client.post(
        "/api/v1/runners/register",
        json={
            "name": "workstation",
            "workstation_fingerprint": fingerprint,
        },
    )
    assert registered.status_code == 200, registered.text
    reported = client.post(
        "/api/v1/agents/discovery-reports",
        json={
            "workstation_fingerprint": fingerprint,
            "os": "linux",
            "candidates": [
                {
                    "agent_kind": "cursor",
                    "config_path_hash": "c" * 64,
                    "mcp_server_count": 0,
                    "enrolled": False,
                }
            ],
        },
    )
    assert reported.status_code == 202, reported.text
    saved = crud_flow_runner.get_fresh(
        db_session, runner_id=UUID(registered.json()["id"])
    )
    candidates = crud_discovered_agent_candidate.list_for_account(
        db_session,
        account_id=test_user.account_id,
    ).items
    assert len(candidates) == 1
    assert (
        saved.workstation_fingerprint
        == candidates[0].workstation_fingerprint
        == fingerprint
    )
