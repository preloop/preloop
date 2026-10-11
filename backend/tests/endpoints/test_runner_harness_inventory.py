"""Harness inventory on register, heartbeat and the runners API (#1480)."""

import copy
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud import crud_audit_log, crud_user
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.db.session import get_db_session as get_db
from preloop.services.harness_inventory import (
    HARNESS_INVENTORY_CHANGED_ACTION,
    normalize_harness_inventory,
)
from preloop.services.runner_service import runner_console_payload

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "personal_runners"


def _fixture(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _app(db_session: Session, user: models.User, monkeypatch) -> TestClient:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _member(db_session: Session, owner: models.User) -> models.User:
    member = crud_user.create(
        db_session,
        obj_in={
            "account_id": owner.account_id,
            "email": "member@example.com",
            "username": "member",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db_session.flush()
    return member


def _inventory_audits(db_session: Session, runner_id: str) -> List[models.AuditLog]:
    return [
        row
        for row in db_session.query(models.AuditLog)
        .filter(models.AuditLog.resource_id == runner_id)
        .all()
        if row.action == HARNESS_INVENTORY_CHANGED_ACTION
    ]


def test_register_without_inventory_is_inventory_unknown(
    db_session: Session, test_user: models.User, monkeypatch
) -> None:
    with _app(db_session, test_user, monkeypatch) as client:
        response = client.post(
            "/api/v1/runners/register", json=_fixture("register_956_era.json")
        )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["harness_inventory"] is None
    assert data["harness_inventory_updated_at"] is None
    assert data["capabilities"]["host_exec_profiles"][0]["name"] == "copilot"


def test_register_with_inventory_stores_and_audits(
    db_session: Session, test_user: models.User, monkeypatch
) -> None:
    body = _fixture("register_with_inventory.json")
    with _app(db_session, test_user, monkeypatch) as client:
        response = client.post("/api/v1/runners/register", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["harness_inventory"]["schema"] == 1
    assert data["harness_inventory"]["hash"] == body["harness_inventory"]["hash"]
    copilot = data["harness_inventory"]["entries"][0]
    # The owner sees the host details.
    assert copilot["version"] == "1.0.95"
    assert copilot["account_host"] == "github.com"
    assert data["harness_inventory_updated_at"]
    db_session.expire_all()
    saved = crud_flow_runner.get(db_session, id=UUID(data["id"]))
    assert saved.harness_inventory == body["harness_inventory"]
    audits = _inventory_audits(db_session, data["id"])
    assert len(audits) == 1
    assert audits[0].details["hash"] == body["harness_inventory"]["hash"]
    assert audits[0].details["runner_name"] == "build-laptop"


def test_register_with_malformed_inventory_still_registers(
    db_session: Session, test_user: models.User, monkeypatch
) -> None:
    body = _fixture("register_with_inventory.json")
    body["harness_inventory"]["entries"][0]["login_state"] = "token:abc"
    with _app(db_session, test_user, monkeypatch) as client:
        response = client.post("/api/v1/runners/register", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["harness_inventory"] is None


def test_member_view_redacts_version_and_account_host(
    db_session: Session, test_user: models.User, monkeypatch
) -> None:
    with _app(db_session, test_user, monkeypatch) as client:
        runner_id = client.post(
            "/api/v1/runners/register", json=_fixture("register_with_inventory.json")
        ).json()["id"]
    member = _member(db_session, test_user)
    with _app(db_session, member, monkeypatch) as client:
        detail = client.get(f"/api/v1/runners/{runner_id}")
        listed = client.get("/api/v1/runners")
    assert detail.status_code == 200, detail.text
    for inventory in (
        detail.json()["harness_inventory"],
        next(r for r in listed.json() if r["id"] == runner_id)["harness_inventory"],
    ):
        copilot = inventory["entries"][0]
        assert copilot["version"] is None
        assert copilot["account_host"] is None
        assert copilot["login_state"] == "signed_in"
        assert copilot["models"][0]["id"] == "auto"
    with _app(db_session, test_user, monkeypatch) as client:
        owner_view = client.get(f"/api/v1/runners/{runner_id}").json()
    assert owner_view["harness_inventory"]["entries"][0]["version"] == "1.0.95"


def test_console_payload_carries_member_view(
    db_session: Session, test_user: models.User
) -> None:
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "registered_by_user_id": test_user.id,
            "name": "console",
            "token_hash": "t",
        },
    )
    payload = runner_console_payload(runner)
    assert payload["harness_inventory"] is None
    crud_flow_runner.set_harness_inventory(
        db_session,
        runner=runner,
        inventory=_fixture("harness_inventory_copilot_signed_in.json"),
    )
    payload = runner_console_payload(runner)
    entry = payload["harness_inventory"]["entries"][0]
    assert "version" not in entry and "account_host" not in entry
    assert entry["governance"] == "governed"
    assert payload["harness_inventory_updated_at"]


def test_normalize_caps_sizes_and_drops_unknown_harnesses() -> None:
    raw = _fixture("harness_inventory_copilot_signed_in.json")
    copilot = raw["entries"][0]
    copilot["models"] = [{"id": f"m{i}", "source": "static"} for i in range(100)]
    copilot["models"].append({"id": "x" * 500, "source": "static"})
    copilot["capabilities"] = [f"c{i}" for i in range(40)]
    copilot["version"] = "9" * 100
    raw["entries"].extend(
        [copy.deepcopy(raw["entries"][1]) for _ in range(40)]
        + [{"harness": "future_cli", "display_name": "Future"}]
    )
    normalized = normalize_harness_inventory(raw)
    assert normalized is not None
    assert len(normalized["entries"]) == 32
    first = normalized["entries"][0]
    assert len(first["models"]) == 64
    assert all(len(m["id"]) <= 128 for m in first["models"])
    assert len(first["capabilities"]) == 16
    assert "version" not in first
    assert all(e["harness"] != "future_cli" for e in normalized["entries"])


def test_normalize_rejects_garbage() -> None:
    assert normalize_harness_inventory(None) is None
    assert normalize_harness_inventory({"entries": "nope"}) is None
    bad = _fixture("harness_inventory_copilot_signed_in.json")
    bad["hash"] = "md5:1"
    assert normalize_harness_inventory(bad) is None


@pytest.fixture
def ws_runner(db_session: Session, test_user: models.User) -> models.FlowRunner:
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "registered_by_user_id": test_user.id,
            "name": "laptop",
            "hostname": "laptop.example.com",
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
    emitted = MagicMock()
    monkeypatch.setattr(runners, "emit_runner_updated", emitted)
    return websocket, emitted


def _acks(sent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [m for m in sent if m.get("type") == "ack"]


@pytest.mark.asyncio
async def test_heartbeat_hash_unknown_asks_for_inventory(
    db_session: Session, ws_runner: models.FlowRunner, monkeypatch
) -> None:
    sent: List[Dict[str, Any]] = []
    websocket, _ = _socket(
        monkeypatch, ws_runner, [_fixture("heartbeat_hash_only.json")], sent
    )
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    assert _acks(sent)[-1].get("inventory_wanted") is True


@pytest.mark.asyncio
async def test_heartbeat_with_inventory_stores_then_hash_only_is_quiet(
    db_session: Session, ws_runner: models.FlowRunner, monkeypatch
) -> None:
    sent: List[Dict[str, Any]] = []
    websocket, emitted = _socket(
        monkeypatch,
        ws_runner,
        [
            _fixture("heartbeat_with_inventory.json"),
            _fixture("heartbeat_hash_only.json"),
            _fixture("heartbeat_with_inventory.json"),
        ],
        sent,
    )
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    acks = _acks(sent)
    assert len(acks) == 3
    assert not any(ack.get("inventory_wanted") for ack in acks)
    db_session.expire_all()
    saved = crud_flow_runner.get(db_session, id=ws_runner.id)
    expected = _fixture("harness_inventory_copilot_signed_in.json")
    assert saved.harness_inventory == expected
    assert saved.harness_inventory_updated_at is not None
    # Same hash twice: one change, one audit event.
    assert len(_inventory_audits(db_session, str(ws_runner.id))) == 1
    assert emitted.call_count >= 1


@pytest.mark.asyncio
async def test_heartbeat_without_inventory_fields_changes_nothing(
    db_session: Session, ws_runner: models.FlowRunner, monkeypatch
) -> None:
    """A #956-era runner never sends a hash and is never asked."""
    sent: List[Dict[str, Any]] = []
    websocket, _ = _socket(
        monkeypatch, ws_runner, [{"type": "heartbeat", "concurrency": 1}], sent
    )
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    assert "inventory_wanted" not in _acks(sent)[-1]
    db_session.expire_all()
    assert crud_flow_runner.get(db_session, id=ws_runner.id).harness_inventory is None


@pytest.mark.asyncio
async def test_heartbeat_with_invalid_inventory_is_ignored(
    db_session: Session, ws_runner: models.FlowRunner, monkeypatch
) -> None:
    beat = _fixture("heartbeat_with_inventory.json")
    beat["harness_inventory"]["hash"] = "not-a-hash"
    sent: List[Dict[str, Any]] = []
    websocket, _ = _socket(monkeypatch, ws_runner, [beat], sent)
    await runners.runner_ws(websocket, ws_runner.id, db_session)
    assert _acks(sent), "the heartbeat must still be acknowledged"
    db_session.expire_all()
    assert crud_flow_runner.get(db_session, id=ws_runner.id).harness_inventory is None
    assert crud_audit_log is not None


def test_list_resolves_the_admin_check_once_per_request(
    db_session: Session, test_user: models.User, monkeypatch
) -> None:
    with _app(db_session, test_user, monkeypatch) as client:
        for index in range(3):
            body = _fixture("register_with_inventory.json") | {"name": f"r{index}"}
            assert client.post("/api/v1/runners/register", json=body).status_code == 200
    member = _member(db_session, test_user)
    calls: List[str] = []
    from preloop.utils import permissions

    real = permissions.user_holds_permission

    def counting(db, user, name):
        calls.append(name)
        return real(db, user, name)

    monkeypatch.setattr(permissions, "user_holds_permission", counting)
    with _app(db_session, member, monkeypatch) as client:
        listed = client.get("/api/v1/runners")
    assert listed.status_code == 200, listed.text
    with_inventory = [r for r in listed.json() if r["harness_inventory"]]
    assert len(with_inventory) == 3
    assert all(
        r["harness_inventory"]["entries"][0]["version"] is None for r in with_inventory
    )
    assert calls.count("manage_account") == 1
