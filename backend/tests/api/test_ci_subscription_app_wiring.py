"""Restricted CI subscription lifecycle through the production application.

Isolated router tests include ``event_webhooks.router`` without the router
level human dependency that ``create_app`` adds, and the shared ``app``
fixture overrides ``get_current_active_user`` for every test. Both masked a
wiring defect: a valid ``ci_`` token authorized by the ASGI guard for the
five subscription operations was still rejected with 401 by the human-only
router dependency before the machine-aware handler ran. These tests use the
real application wiring with no authentication override.
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.api.middleware import ci_auth
from preloop.models import crud
from preloop.models.db.session import get_db_session
from tests.api.test_ci_principal import ci_resources, provision  # noqa: F401

BASE = "/api/v1/event-webhooks/endpoints"


@pytest.fixture
def production_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    def sessions() -> Any:
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = sessions
    monkeypatch.setattr(ci_auth, "get_db_session", sessions)
    return TestClient(app)


def test_ci_token_completes_own_subscription_lifecycle_in_production_app(
    db_session: Session,
    ci_resources: tuple[Any, ...],  # noqa: F811
    production_client: TestClient,
) -> None:
    principal, _, token = provision(db_session, ci_resources)
    headers = {"Authorization": f"Bearer {token}"}

    created = production_client.post(
        BASE, json={"url": "https://example.com/preloop-completion"}, headers=headers
    )
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]
    assert created.json()["restricted_ci"] is True
    owned = crud.crud_ci_subscription.get_owned(
        db_session,
        context=crud.crud_ci_principal.authenticate(db_session, token=token),
        endpoint_id=UUID(endpoint_id),
    )
    assert owned.ci_principal_id == principal.id

    listed = production_client.get(BASE, headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [endpoint_id]

    updated = production_client.patch(
        f"{BASE}/{endpoint_id}",
        json={"url": "https://example.com/preloop-completion-v2"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["url"] == "https://example.com/preloop-completion-v2"

    rotated = production_client.post(
        f"{BASE}/{endpoint_id}/secret/rotate", json={}, headers=headers
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["secret"] != created.json()["secret"]

    deleted = production_client.delete(f"{BASE}/{endpoint_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    assert production_client.get(BASE, headers=headers).json() == []


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", "/catalogue"),
        ("GET", "/deliveries"),
        ("GET", "/deliveries/dead-letter"),
        ("POST", "/deliveries/00000000-0000-4000-8000-000000000001/replay"),
    ],
)
def test_human_only_webhook_routes_still_require_authentication(
    production_client: TestClient, method: str, suffix: str
) -> None:
    """Dropping the router dependency must not expose any handler anonymously."""
    anonymous = production_client.request(method, "/api/v1/event-webhooks" + suffix)
    assert anonymous.status_code == 401, anonymous.text


def test_ci_token_still_denied_on_human_only_webhook_routes(
    db_session: Session,
    ci_resources: tuple[Any, ...],  # noqa: F811
    production_client: TestClient,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    headers = {"Authorization": f"Bearer {token}"}
    created = production_client.post(
        BASE, json={"url": "https://example.com/preloop-completion"}, headers=headers
    )
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]
    for method, suffix in (
        ("GET", "/catalogue"),
        ("GET", "/deliveries"),
        ("POST", f"/endpoints/{endpoint_id}/test"),
    ):
        response = production_client.request(
            method, "/api/v1/event-webhooks" + suffix, headers=headers
        )
        assert response.status_code == 403, (method, suffix, response.text)
