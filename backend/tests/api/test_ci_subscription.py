"""Independent restricted callback management contract on synthetic resources."""

from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.api.endpoints import event_webhooks
from preloop.api.middleware import ci_auth
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.db.session import get_db_session
from preloop.schemas.ci_principal import CiAction
from tests.api.test_ci_principal import ci_resources as create_ci_resources
from tests.api.test_ci_principal import provision

BASE = "/api/v1/event-webhooks/endpoints"
FINISHED = "flow.execution.finished"


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(event_webhooks.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    monkeypatch.setattr(ci_auth, "get_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    return TestClient(app)


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_endpoint(client: TestClient, token: str) -> dict[str, Any]:
    response = client.post(
        BASE, json={"url": "https://example.com/completed"}, headers=headers(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "schema_name", ["CiSubscriptionCreate", "CiSubscriptionUpdate"]
)
@pytest.mark.parametrize(
    "event_types", [[], [FINISHED, FINISHED], [FINISHED, "approval.requested"], None]
)
def test_completion_filter_rejects_broad_empty_duplicate_or_null(
    schema_name: str, event_types: Any
) -> None:
    from preloop.schemas import ci_subscription

    schema = getattr(ci_subscription, schema_name)
    with pytest.raises(ValidationError):
        schema.model_validate(
            {"url": "https://example.com/completed", "event_types": event_types}
        )


@pytest.mark.parametrize(
    "schema_name", ["CiSubscriptionCreate", "CiSubscriptionUpdate"]
)
@pytest.mark.parametrize(
    "field",
    [
        "ci_principal_id",
        "initiating_ci_key_id",
        "ci_subscription_binding",
        "account_id",
        "project_id",
        "flow_id",
        "source",
        "secret",
        "secret_encrypted",
        "approval_workflow_id",
        "created_by_user_id",
    ],
)
def test_raw_control_fields_rejected(schema_name: str, field: str) -> None:
    from preloop.schemas import ci_subscription

    with pytest.raises(ValidationError):
        getattr(ci_subscription, schema_name).model_validate(
            {"url": "https://example.com/completed", field: str(uuid4())}
        )


def test_http_ownership_rotation_and_secret_once(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    principal, key, token = provision(db_session, ci_resources)
    _, _, foreign_token = provision(db_session, ci_resources)
    own = create_endpoint(client, token)
    assert own["event_types"] == [FINISHED]
    assert own["secret"]
    endpoint_id = own["id"]
    human = CRUDBase(models.WebhookEndpoint).create(
        db_session,
        obj_in={
            "account_id": principal.account_id,
            "url": "https://example.com/human",
            "secret_encrypted": "fixture-ciphertext",
            "event_types": [],
        },
    )
    listed = client.get(BASE, headers=headers(token))
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [endpoint_id]
    assert "secret" not in listed.json()[0]
    for denied_id in (endpoint_id, str(human.id), str(uuid4())):
        denied_token = foreign_token if denied_id == endpoint_id else token
        for method, suffix, body in [
            ("PATCH", "", {"description": "unauthorized"}),
            ("DELETE", "", None),
            ("POST", "/secret/rotate", None),
        ]:
            response = client.request(
                method,
                f"{BASE}/{denied_id}{suffix}",
                headers=headers(denied_token),
                json=body,
            )
            assert response.status_code == 404, response.text
    _, rotated = crud.crud_ci_principal.rotate(
        db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
    )
    assert client.get(BASE, headers=headers(token)).status_code == 401
    response = client.patch(
        f"{BASE}/{endpoint_id}",
        headers=headers(rotated),
        json={"description": "updated", "active": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active"] is False
    assert "secret" not in response.json()
    secret_response = client.post(
        f"{BASE}/{endpoint_id}/secret/rotate", headers=headers(rotated)
    )
    assert secret_response.status_code == 200, secret_response.text
    assert secret_response.json()["secret"] != own["secret"]
    assert client.delete(
        f"{BASE}/{endpoint_id}", headers=headers(rotated)
    ).status_code in (200, 204)
    assert client.get(BASE, headers=headers(rotated)).json() == []


@pytest.mark.parametrize(
    "method,suffix,action",
    [
        ("GET", "", CiAction.READ_SUBSCRIPTION),
        ("POST", "", CiAction.CREATE_SUBSCRIPTION),
        ("PATCH", "/owned", CiAction.UPDATE_SUBSCRIPTION),
        ("DELETE", "/owned", CiAction.DELETE_SUBSCRIPTION),
        ("POST", "/owned/secret/rotate", CiAction.ROTATE_SUBSCRIPTION_SECRET),
    ],
)
def test_management_requires_specific_action(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    method: str,
    suffix: str,
    action: CiAction,
) -> None:
    principal, _, token = provision(db_session, ci_resources)
    own = create_endpoint(client, token)
    grant = ci_resources[3].model_copy(
        update={"actions": tuple(item for item in CiAction if item != action)}
    )
    crud.crud_ci_principal.change(
        db_session, actor=ci_resources[0], principal_id=principal.id, grant=grant
    )
    response = client.request(
        method,
        BASE + suffix.replace("owned", own["id"]),
        headers=headers(token),
        json={"url": "https://example.com/changed"},
    )
    assert response.status_code == 403, response.text
    assert (
        CRUDBase(models.WebhookEndpoint).get(db_session, id=own["id"]).url == own["url"]
    )


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize(
    "override", ["source", "account_id", "ci_subscription_binding", "secret_encrypted"]
)
def test_http_raw_control_rejection_precedes_mutation(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    method: str,
    override: str,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    own = create_endpoint(client, token)
    path = BASE if method == "POST" else f"{BASE}/{own['id']}"
    response = client.request(
        method,
        path,
        headers=headers(token),
        json={"url": "https://example.com/forged", override: "forged"},
    )
    assert response.status_code == 422, response.text
    assert (
        CRUDBase(models.WebhookEndpoint).get(db_session, id=own["id"]).url == own["url"]
    )
    assert len(client.get(BASE, headers=headers(token)).json()) == 1


@pytest.mark.parametrize("suffix", ["/test", "/deliveries"])
def test_machine_operational_routes_never_inherit_human_permissions(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    suffix: str,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    own = create_endpoint(client, token)
    response = client.request(
        "POST" if suffix == "/test" else "GET",
        f"{BASE}/{own['id']}{suffix}",
        headers=headers(token),
    )
    assert response.status_code == 403, response.text
