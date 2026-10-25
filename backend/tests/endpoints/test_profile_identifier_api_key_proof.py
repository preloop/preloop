"""Canonical identifier writes reject API keys on the real auth dependency."""

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.router import router
from preloop.models import models
from preloop.models.db.session import get_db_session


@pytest.mark.parametrize(
    "field,value", [("username", "changed-person"), ("email", "changed@example.com")]
)
def test_profile_read_then_identifier_write_with_real_api_key(
    db_session: Session, test_user: models.User, field: str, value: str
) -> None:
    """Reading a profile with a key never grants session-only write authority."""
    token = f"synthetic-personal-key-{uuid4().hex}"
    key = models.ApiKey(
        name=f"Example personal key {uuid4()}",
        key=token,
        user_id=test_user.id,
        account_id=test_user.account_id,
        is_active=True,
        scopes=[],
        context_data={},
        credential_type="legacy",
    )
    db_session.add(key)
    db_session.flush()
    original = getattr(test_user.person, field)
    original_generation = test_user.person.auth_generation
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/auth")
    # Keep the actual bearer authentication dependency; only isolate DB I/O.
    app.dependency_overrides[get_db_session] = lambda: db_session
    assert key.key is None
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        profile = client.get("/api/v1/auth/users/me", headers=headers)
        assert profile.status_code == 200
        assert profile.json()["id"] == str(test_user.id)
        assert profile.json()["person_id"] == str(test_user.person_id)
        assert profile.json()["account_id"] == str(test_user.account_id)
        assert profile.json()[field] == original
        response = client.put(
            "/api/v1/auth/users/me", headers=headers, json={field: value}
        )

    assert test_user._auth_api_key is key
    assert response.status_code == 403
    assert response.json() == {
        "detail": "Sign in to change your username or recovery email"
    }
    db_session.refresh(test_user.person)
    assert getattr(test_user.person, field) == original
    assert test_user.person.auth_generation == original_generation
