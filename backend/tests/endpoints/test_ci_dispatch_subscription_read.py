"""Listing a restricted CI identity's GitHub dispatch subscriptions."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import ci_identities
from preloop.api.middleware import ci_auth
from preloop.models import crud, models
from preloop.models.db.session import get_db_session
from preloop.plugins.ci_authorization import register_ci_administrator
from tests.api.test_ci_principal import ci_resources as make_resources

BASE = "/api/v1/ci-identities"
TOKEN = "ghs_" + "R" * 36


@pytest.fixture(autouse=True)
def authority() -> Any:
    """Reset the CI administrator hook around each test."""
    register_ci_administrator(None)
    yield
    register_ci_administrator(None)


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    """Provision the owner, project, flow and grant used by CI identities."""
    return make_resources.__wrapped__(db_session)


@pytest.fixture
def client(
    db_session: Session, ci_resources: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Serve the CI identity router as the resources' owner.

    Args:
        db_session: Database session.
        ci_resources: Synthetic CI resources.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A test client.
    """

    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(ci_identities.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    app.dependency_overrides[get_current_active_user] = lambda: ci_resources[0]
    monkeypatch.setattr(ci_auth, "get_auth_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    return TestClient(app)


def _identity(client: TestClient, resources: tuple[Any, ...]) -> dict[str, Any]:
    """Create a CI identity and return its issuance response."""
    response = client.post(
        BASE,
        json={
            "name": "Synthetic review",
            "grant": resources[3].model_dump(mode="json"),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _dispatch(client: TestClient, own: dict[str, Any]) -> dict[str, Any]:
    """Create a GitHub dispatch subscription and return the created body."""
    response = client.post(
        f"{BASE}/{own['identity']['id']}/github-dispatch-subscriptions",
        json={
            "key_id": own["key_id"],
            "event_type": "preloop-review-completed",
            "token": TOKEN,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_list_returns_only_dispatch_subscriptions_without_secrets(
    client: TestClient, db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    """Dispatch rows are listed safely; HTTP callbacks are excluded."""
    own = _identity(client, ci_resources)
    pid = own["identity"]["id"]
    created = _dispatch(client, own)
    callback = client.post(
        f"{BASE}/{pid}/subscriptions",
        json={"key_id": own["key_id"], "url": "https://hooks.example.com/ci"},
    )
    assert callback.status_code == 201, callback.text

    response = client.get(f"{BASE}/{pid}/github-dispatch-subscriptions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["id"] for row in body] == [created["id"]]
    assert response.headers["X-Total-Count"] == "1"
    row = body[0]
    assert row["delivery_kind"] == "github_dispatch"
    assert row["dispatch_event_type"] == "preloop-review-completed"
    assert row["dispatch_credential_configured"] is True
    assert "secret" not in row
    endpoint = db_session.get(models.WebhookEndpoint, created["id"])
    assert endpoint is not None
    for value in (
        TOKEN,
        created["secret"],
        own["token"],
        endpoint.secret_encrypted,
        endpoint.dispatch_token_encrypted,
    ):
        assert value not in response.text


def test_list_is_404_for_unknown_or_foreign_identity(
    client: TestClient, db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    """Another account's identity is indistinguishable from an unknown one."""
    own = _identity(client, ci_resources)
    _dispatch(client, own)
    other = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    stranger = models.User(
        id=uuid4(),
        account_id=other.id,
        email="stranger@example.com",
        username=f"stranger-{uuid4().hex}",
        hashed_password="unused",
        is_active=True,
    )
    db_session.add(stranger)
    db_session.flush()
    path = f"{BASE}/{own['identity']['id']}/github-dispatch-subscriptions"
    client.app.dependency_overrides[get_current_active_user] = lambda: stranger

    response = client.get(path)

    assert response.status_code == 404, response.text
    assert TOKEN not in response.text
    assert (
        client.get(f"{BASE}/{uuid4()}/github-dispatch-subscriptions").status_code == 404
    )


def test_list_requires_view_authorization(
    client: TestClient, ci_resources: tuple[Any, ...]
) -> None:
    """An actor the CI administrator denies viewing gets 403."""
    own = _identity(client, ci_resources)
    created = _dispatch(client, own)
    register_ci_administrator(lambda *_: False)

    response = client.get(
        f"{BASE}/{own['identity']['id']}/github-dispatch-subscriptions"
    )

    assert response.status_code == 403, response.text
    assert created["id"] not in response.text
