"""HTTP profile registration must survive schema parsing and actual leasing."""

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import runners
from preloop.models import models, schemas
from preloop.models.db.session import get_db_session as get_db
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.services.runner_service import lease_job


@pytest.mark.parametrize("reregister", [False, True])
def test_register_profiles_can_be_selected_for_lease(
    db_session: Session, test_user: models.User, monkeypatch, reregister: bool
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    monkeypatch.setattr(
        "preloop.services.runner_service.emit_runner_updated", lambda *args: None
    )
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    body = {
        "name": "native-registration",
        "flow_execution_consent": "always",
        "host_exec_profiles": [
            {
                "name": "cursor-ask",
                "capabilities": ["host_exec", "cursor_cli"],
                "models": ["team-fast"],
            }
        ],
    }
    if reregister:
        existing = crud_flow_runner.create(
            db_session,
            obj_in={
                "account_id": test_user.account_id,
                "name": "existing",
                "registered_by_user_id": test_user.id,
                "token_hash": "old-token",
                "capabilities": {},
            },
        )
        body["runner_id"] = str(existing.id)
    with TestClient(app) as client:
        response = client.post("/api/v1/runners/register", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["capabilities"] == {
        "host_exec_profiles": body["host_exec_profiles"],
        "docker_available": False,
    }
    runner_id = UUID(data["id"])
    db_session.expire_all()
    saved = crud_flow_runner.get(
        db_session, id=runner_id, account_id=str(test_user.account_id)
    )
    assert saved.capabilities == data["capabilities"]
    flow = crud_flow.create(
        db_session,
        flow_in=schemas.FlowCreate(
            name="Native registration",
            prompt_template="question",
            agent_type="cursor",
            agent_config={"host_exec_profile": "cursor-ask"},
            runner_pool="native-registration",
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, obj_in=schemas.FlowExecutionCreate(flow_id=flow.id)
    )
    payload = {
        "execution_id": str(execution.id),
        "agent_type": "cursor",
        "host_exec_profile": "cursor-ask",
        "completion_protocol": "host_exec",
        "model_identifier": "team-fast",
    }
    unsupported = lease_job(
        db_session,
        account_id=test_user.account_id,
        pool="native-registration",
        execution_id=execution.id,
        payload={**payload, "model_identifier": "unknown"},
    )
    assert unsupported is None
    leased = lease_job(
        db_session,
        account_id=test_user.account_id,
        pool="native-registration",
        execution_id=execution.id,
        payload=payload,
    )
    assert leased is not None and leased.id == runner_id


def _register_client(db_session: Session, test_user: models.User) -> TestClient:
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    return TestClient(app)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "desk-mac", "host_exec_profiles": None},
        {"name": "desk-mac"},
        {"name": "desk-mac", "host_exec_profiles": []},
    ],
)
def test_register_null_missing_or_empty_host_exec_profiles_stores_none(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    with _register_client(db_session, test_user) as client:
        response = client.post("/api/v1/runners/register", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["capabilities"] == {"host_exec_profiles": [], "docker_available": False}
    saved = crud_flow_runner.get(
        db_session, id=UUID(data["id"]), account_id=str(test_user.account_id)
    )
    assert saved is not None
    assert saved.capabilities == {"host_exec_profiles": [], "docker_available": False}


def test_register_one_valid_host_exec_profile_is_stored(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    profile = {
        "name": "cursor-ask",
        "capabilities": ["host_exec", "cursor_cli"],
        "models": ["team-fast"],
    }
    with _register_client(db_session, test_user) as client:
        response = client.post(
            "/api/v1/runners/register",
            json={"name": "desk-mac", "host_exec_profiles": [profile]},
        )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["capabilities"] == {
        "host_exec_profiles": [profile],
        "docker_available": False,
    }
    saved = crud_flow_runner.get(
        db_session, id=UUID(data["id"]), account_id=str(test_user.account_id)
    )
    assert saved is not None
    assert saved.capabilities == {
        "host_exec_profiles": [profile],
        "docker_available": False,
    }


def test_register_copilot_profile_leases_only_copilot_jobs(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    monkeypatch.setattr(
        "preloop.services.runner_service.emit_runner_updated", lambda *args: None
    )
    profiles = [
        {
            "name": "copilot-seat",
            "capabilities": ["host_exec", "copilot_cli", "stdout", "cancel"],
            "models": ["team-default"],
        }
    ]
    with _register_client(db_session, test_user) as client:
        response = client.post(
            "/api/v1/runners/register",
            json={
                "name": "copilot-desk",
                "host_exec_profiles": profiles,
                "flow_execution_consent": "always",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"] == {
        "host_exec_profiles": profiles,
        "docker_available": False,
    }
    runner_id = UUID(response.json()["id"])
    flow = crud_flow.create(
        db_session,
        flow_in=schemas.FlowCreate(
            name="Copilot review",
            prompt_template="review",
            agent_type="copilot",
            agent_config={"host_exec_profile": "copilot-seat"},
            runner_pool="copilot-desk",
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, obj_in=schemas.FlowExecutionCreate(flow_id=flow.id)
    )
    payload = {
        "execution_id": str(execution.id),
        "agent_type": "copilot",
        "host_exec_profile": "copilot-seat",
        "completion_protocol": "host_exec",
        "model_identifier": "team-default",
    }
    as_cursor = lease_job(
        db_session,
        account_id=test_user.account_id,
        pool="copilot-desk",
        execution_id=execution.id,
        payload={**payload, "agent_type": "cursor"},
    )
    assert as_cursor is None
    leased = lease_job(
        db_session,
        account_id=test_user.account_id,
        pool="copilot-desk",
        execution_id=execution.id,
        payload=payload,
    )
    assert leased is not None and leased.id == runner_id


def test_scheduling_preferences_survive_runner_registration(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    with _register_client(db_session, test_user) as client:
        registered = client.post(
            "/api/v1/runners/register",
            json={"name": "workstation", "docker_available": True},
        )
        assert registered.status_code == 200
        runner_id = registered.json()["id"]
        assert registered.json()["accept_untagged"] is False
        assert "docker" in registered.json()["labels"]
        changed = client.patch(
            f"/api/v1/runners/{runner_id}/scheduling",
            json={"paused": True, "accept_untagged": True},
        )
        assert changed.status_code == 200
        assert changed.json()["paused"] is True
        resumed = client.post(
            "/api/v1/runners/register",
            json={
                "name": "workstation",
                "runner_id": runner_id,
                "docker_available": False,
            },
        )
        assert resumed.status_code == 200
        assert resumed.json()["paused"] is True
        assert resumed.json()["accept_untagged"] is True
        assert "docker" not in resumed.json()["labels"]
        assert (
            crud_flow_runner.claim_free_slot(db_session, runner_id=UUID(runner_id))
            is None
        )
