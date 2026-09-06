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
                "token_hash": "old-token",
                "capabilities": {},
            },
        )
        body["runner_id"] = str(existing.id)
    with TestClient(app) as client:
        response = client.post("/api/v1/runners/register", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["capabilities"] == {"host_exec_profiles": body["host_exec_profiles"]}
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
