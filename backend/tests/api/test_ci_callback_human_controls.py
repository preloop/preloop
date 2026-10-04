"""Human operators retain receiver controls without broadening CI event scope."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.ci import get_current_actor
from preloop.api.endpoints import event_webhooks
from preloop.models import crud
from preloop.models.db.session import get_db_session
from preloop.schemas.ci_subscription import CiSubscriptionCreate
from tests.api.test_ci_principal import ci_resources as create_resources
from tests.api.test_ci_principal import provision


@pytest.mark.parametrize("event_types", [[], ["approval.created"]])
def test_human_filter_change_rejected_before_receiver_mutation(
    db_session: Session,
    event_types: list[str],
) -> None:
    resources = create_resources.__wrapped__(db_session)
    _, _, token = provision(db_session, resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    endpoint, _ = crud.crud_ci_subscription.create(
        db_session,
        context=context,
        payload=CiSubscriptionCreate(url="https://example.com/original"),
    )
    endpoint_id = endpoint.id

    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(event_webhooks.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    app.dependency_overrides[get_current_actor] = lambda: resources[0]
    client = TestClient(app)
    response = client.patch(
        f"/api/v1/event-webhooks/endpoints/{endpoint_id}",
        json={"event_types": event_types, "url": "https://example.com/forged"},
    )
    assert response.status_code == 422, response.text
    db_session.refresh(endpoint)
    assert endpoint.event_types == ["flow.execution.finished"]
    assert endpoint.url == "https://example.com/original"

    changed = client.patch(
        f"/api/v1/event-webhooks/endpoints/{endpoint_id}",
        json={
            "event_types": ["flow.execution.finished"],
            "description": "updated",
            "active": False,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["description"] == "updated"
    assert changed.json()["active"] is False


@pytest.mark.parametrize("field", ["event_types", "url", "active"])
def test_machine_null_update_denies_without_changing_receiver(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from preloop.api.middleware import ci_auth

    resources = create_resources.__wrapped__(db_session)
    _, _, token = provision(db_session, resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    endpoint, _ = crud.crud_ci_subscription.create(
        db_session,
        context=context,
        payload=CiSubscriptionCreate(url="https://example.com/original"),
    )

    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(event_webhooks.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    monkeypatch.setattr(ci_auth, "get_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    response = TestClient(app).patch(
        f"/api/v1/event-webhooks/endpoints/{endpoint.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={field: None, "description": "forged"},
    )
    assert response.status_code == 422, response.text
    db_session.refresh(endpoint)
    assert endpoint.url == "https://example.com/original"
    assert endpoint.event_types == ["flow.execution.finished"]
    assert endpoint.active is True
    assert endpoint.description is None


def test_human_send_test_denies_restricted_callback_without_queueing(
    db_session: Session,
) -> None:
    from preloop.api.auth import get_current_active_user
    from preloop.api.common import get_account_for_user
    from preloop.models import models

    resources = create_resources.__wrapped__(db_session)
    _, _, token = provision(db_session, resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    endpoint, _ = crud.crud_ci_subscription.create(
        db_session,
        context=context,
        payload=CiSubscriptionCreate(url="https://example.com/original"),
    )

    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(event_webhooks.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    app.dependency_overrides[get_current_active_user] = lambda: resources[0]
    app.dependency_overrides[get_account_for_user] = lambda: crud.crud_account.get(
        db_session, id=resources[0].account_id
    )
    response = TestClient(app).post(
        f"/api/v1/event-webhooks/endpoints/{endpoint.id}/test"
    )
    assert response.status_code == 422, response.text
    assert "completion" in response.json()["detail"].lower()
    assert (
        db_session.query(models.WebhookDelivery)
        .filter_by(endpoint_id=endpoint.id)
        .count()
        == 0
    )


def test_human_read_exposes_ci_marker_without_binding_or_secret(
    db_session: Session,
) -> None:
    resources = create_resources.__wrapped__(db_session)
    _, _, token = provision(db_session, resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    endpoint, secret = crud.crud_ci_subscription.create(
        db_session,
        context=context,
        payload=CiSubscriptionCreate(url="https://example.com/original"),
    )
    body = event_webhooks._to_read(endpoint).model_dump(mode="json")
    assert body.get("restricted_ci") is True
    assert "ci_subscription_binding" not in body
    assert "initiating_ci_key_id" not in body
    assert secret not in str(body)
