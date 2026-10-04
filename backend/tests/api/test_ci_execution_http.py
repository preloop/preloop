"""Real machine auth and principal-owned projections on execution routes."""

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints import flows
from preloop.api.middleware import ci_auth
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.db.session import get_db_session
from tests.api.test_ci_execution import review_binding
from tests.api.test_ci_principal import ci_resources as create_ci_resources
from tests.api.test_ci_principal import provision


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(flows.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    monkeypatch.setattr(ci_auth, "get_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    return TestClient(app)


def owned_run(db: Session, resources: tuple[Any, ...]) -> tuple[Any, ...]:
    principal, key, token = provision(db, resources)
    context = crud.crud_ci_principal.authenticate(db, token=token)
    execution = crud.crud_ci_execution.create(
        db, context=context, binding=review_binding(context), event={}
    )
    return principal, key, token, context, execution


def test_projection_result_and_rotation(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    principal, key, token, context, execution = owned_run(db_session, ci_resources)
    execution_id = execution.id
    CRUDBase(models.FlowExecution).update(
        db_session,
        db_obj=execution,
        obj_in={
            "status": "SUCCEEDED",
            "resolved_input_prompt": "synthetic private prompt",
            "result": {
                "review": "persisted review",
                "nested": [{"api_key": "synthetic-secret", "score": 1}],
                "_private_publication": {"access_token": "synthetic-secret"},
            },
        },
    )
    headers = {"Authorization": f"Bearer {token}"}
    detail = client.get(f"/api/v1/flows/executions/{execution_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert set(detail.json()) == {
        "id",
        "flow_id",
        "project_id",
        "repository_identifier",
        "pr_number",
        "provider_pr_id",
        "head_sha",
        "status",
        "start_time",
        "end_time",
        "failure_category",
    }
    result = client.get(
        f"/api/v1/flows/executions/{execution_id}/result", headers=headers
    )
    assert result.status_code == 200, result.text
    assert result.json()["result"] == {
        "review": "persisted review",
        "nested": [{"score": 1}],
    }
    assert result.json()["project_id"] == str(context.project_id)
    assert result.json()["head_sha"] == "a" * 40
    assert "synthetic-secret" not in result.text
    _, rotated = crud.crud_ci_principal.rotate(
        db_session,
        actor=ci_resources[0],
        principal_id=principal.id,
        key_id=key.id,
    )
    assert (
        client.get(
            f"/api/v1/flows/executions/{execution_id}", headers=headers
        ).status_code
        == 401
    )
    assert (
        client.get(
            f"/api/v1/flows/executions/{execution_id}",
            headers={"Authorization": f"Bearer {rotated}"},
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "suffix,method", [("", "get"), ("/result", "get"), ("/command", "post")]
)
def test_other_principal_and_null_history_denied_before_bus(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    method: str,
) -> None:
    from preloop.sync.services import event_bus

    _, _, token, context, execution = owned_run(db_session, ci_resources)
    _, _, other_token = provision(db_session, ci_resources)
    null = CRUDBase(models.FlowExecution).create(
        db_session, obj_in={"flow_id": context.flow_id}
    )
    bus = AsyncMock()
    monkeypatch.setattr(event_bus, "get_nats_client", bus)
    for denied_token, execution_id in [
        (other_token, execution.id),
        (token, null.id),
        (token, uuid4()),
    ]:
        response = client.request(
            method,
            f"/api/v1/flows/executions/{execution_id}{suffix}",
            headers={"Authorization": f"Bearer {denied_token}"},
            **({"json": {"command": "stop"}} if method == "post" else {}),
        )
        assert response.status_code == 404, response.text
    bus.assert_not_awaited()


def test_list_owns_before_pagination_and_count(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
) -> None:
    _, _, token, context, own = owned_run(db_session, ci_resources)
    owned_run(db_session, ci_resources)
    CRUDBase(models.FlowExecution).create(
        db_session, obj_in={"flow_id": context.flow_id}
    )
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/v1/flows/executions?limit=1", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["x-total-count"] == "1"
    assert [row["id"] for row in response.json()] == [str(own.id)]
    assert client.get("/api/v1/flows/executions?skip=1", headers=headers).json() == []
    assert (
        client.get(
            f"/api/v1/flows/executions?flow_id={uuid4()}", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/flows/executions?search=secret", headers=headers
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "resume"},
        {"command": "retry"},
        {"command": "stop", "payload": {"runner_pool": "other"}},
        {"command": "stop", "matrix": [{}]},
        {"command": "stop", "_resume": True},
    ],
)
def test_stop_raw_overrides_denied_without_bus(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    from preloop.sync.services import event_bus

    _, _, token, _, execution = owned_run(db_session, ci_resources)
    bus = AsyncMock()
    monkeypatch.setattr(event_bus, "get_nats_client", bus)
    response = client.post(
        f"/api/v1/flows/executions/{execution.id}/command",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422, response.text
    bus.assert_not_awaited()
    assert crud.crud_flow_execution.get(db_session, id=execution.id).status == "PENDING"


def test_missing_result_and_terminal_stop_are_distinct_and_idempotent(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.sync.services import event_bus

    _, _, token, _, execution = owned_run(db_session, ci_resources)
    execution_id = execution.id
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": "SUCCEEDED"}
    )
    bus = AsyncMock()
    monkeypatch.setattr(event_bus, "get_nats_client", bus)
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        client.get(
            f"/api/v1/flows/executions/{execution_id}/result", headers=headers
        ).status_code
        == 404
    )
    for _ in range(2):
        response = client.post(
            f"/api/v1/flows/executions/{execution_id}/command",
            headers=headers,
            json={"command": "stop"},
        )
        assert response.json() == {
            "status": "not_running",
            "execution_status": "SUCCEEDED",
        }
    bus.assert_not_awaited()


def test_stop_rechecks_authority_after_bus_and_stops_owned_run(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.sync.services import event_bus

    principal, key, token, _, execution = owned_run(db_session, ci_resources)
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        f"/api/v1/flows/executions/{execution.id}/command",
        headers=headers,
        json={"command": "stop"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "stopped"}
    stopped = crud.crud_flow_execution.get(db_session, id=execution.id)
    assert stopped.stop_source == "restricted_ci"
    execution = crud.crud_ci_execution.create(
        db_session,
        context=crud.crud_ci_principal.authenticate(db_session, token=token),
        binding=review_binding(
            crud.crud_ci_principal.authenticate(db_session, token=token)
        ),
        event={},
    )
    execution_id = execution.id

    async def revoke_during_bus() -> None:
        crud.crud_ci_principal.revoke_key(
            db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
        )

    monkeypatch.setattr(event_bus, "get_nats_client", revoke_during_bus)
    response = client.post(
        f"/api/v1/flows/executions/{execution_id}/command",
        headers=headers,
        json={"command": "stop"},
    )
    assert response.status_code == 403, response.text
    assert crud.crud_flow_execution.get(db_session, id=execution_id).status == "PENDING"


@pytest.mark.parametrize(
    "extra", ["matrix", "runner_pool", "workspace_files", "_subject", "prompt_template"]
)
def test_trigger_override_denied_before_service(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    extra: str,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    trigger = AsyncMock()
    monkeypatch.setattr(flows, "trigger_ci_review", trigger)
    response = client.post(
        f"/api/v1/flows/{ci_resources[2].id}/trigger",
        headers={"Authorization": f"Bearer {token}"},
        json={"pr_number": 7, "head_sha": "a" * 40, extra: "synthetic-override"},
    )
    assert response.status_code == 422, response.text
    trigger.assert_not_awaited()
