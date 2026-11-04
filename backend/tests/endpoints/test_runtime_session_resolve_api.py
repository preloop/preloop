"""``GET /api/v1/runtime-sessions/resolve`` (#1358)."""

from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints import runtime_session_resolve
from preloop.api.gateway_auth_dependency import get_model_gateway_auth_context
from preloop.models.crud import crud_account, crud_api_key, crud_user
from preloop.models.db.session import get_db_session
from preloop.services.model_gateway_auth import ModelGatewayAuthContext

URL = "/api/v1/runtime-sessions/resolve"


def _key(db_session, user, name="Console key"):
    api_key, _token = crud_api_key.create_runtime_key(
        db_session, name=name, account_id=user.account_id, user_id=user.id
    )
    return api_key


@pytest.fixture
def make_client(db_session) -> Iterator:
    clients = []

    def _make(user, api_key) -> TestClient:
        app = FastAPI()
        app.include_router(runtime_session_resolve.router, prefix="/api/v1")
        app.dependency_overrides[get_db_session] = lambda: db_session
        app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
            ModelGatewayAuthContext(token="t", user=user, api_key=api_key)
        )
        client = TestClient(app)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.close()


def test_not_found_without_create(make_client, db_session, test_user):
    client = make_client(test_user, _key(db_session, test_user))
    response = client.get(URL, params={"client_session_id": "conv-1"})
    assert response.status_code == 404


def test_create_then_resolve(make_client, db_session, test_user):
    client = make_client(test_user, _key(db_session, test_user))
    created = client.get(URL, params={"client_session_id": "conv-1", "create": True})
    assert created.status_code == 200
    body = created.json()
    assert body["created"] is True
    assert body["runtime_session_id"]

    again = client.get(URL, params={"client_session_id": "conv-1"})
    assert again.status_code == 200
    assert again.json() == {
        "runtime_session_id": body["runtime_session_id"],
        "created": False,
    }


def test_invalid_client_session_id_is_rejected(make_client, db_session, test_user):
    client = make_client(test_user, _key(db_session, test_user))
    response = client.get(
        URL, params={"client_session_id": "has spaces!", "create": True}
    )
    assert response.status_code == 422


def test_other_account_key_gets_404(make_client, db_session, test_user):
    owner = make_client(test_user, _key(db_session, test_user))
    assert (
        owner.get(URL, params={"client_session_id": "conv-1", "create": True})
    ).status_code == 200

    account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    other = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": "other@example.com",
            "username": "otheruser",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        },
    )
    intruder = make_client(other, _key(db_session, other, name="Other key"))
    response = intruder.get(URL, params={"client_session_id": "conv-1"})
    assert response.status_code == 404


def test_create_commits_the_new_session(db_session, test_user, monkeypatch):
    """A created row must survive the request (review finding on #2194)."""
    commits: list = []
    real_commit = db_session.commit
    monkeypatch.setattr(
        db_session, "commit", lambda: (commits.append(1), real_commit())[1]
    )
    app = FastAPI()
    app.include_router(runtime_session_resolve.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    api_key = _key(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="t", user=test_user, api_key=api_key)
    )
    with TestClient(app) as client:
        commits.clear()
        response = client.get(URL, params={"client_session_id": "c", "create": True})
    assert response.status_code == 200 and response.json()["created"] is True
    assert commits, "resolve with create=true must commit"
