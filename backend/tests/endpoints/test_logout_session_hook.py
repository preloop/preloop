"""Server-side sign out (``POST /auth/logout``) and the H9 session hook.

With no hook registered, sign out answers with no redirect and changes
nothing else. A registered hook sees the token's claims, may name a
same-origin path for the client, and may revoke individual tokens.
"""

from typing import Any, Iterator, Mapping, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import auth_router
from preloop.api.auth.jwt import create_access_token, user_auth_generation
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import LogoutOutcome, SessionHook


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.dependency_overrides[get_db_session] = lambda: db_session
    yield TestClient(app)


@pytest.fixture(autouse=True)
def _clean_hooks() -> Iterator[None]:
    account_hooks.reset_account_hooks()
    yield
    account_hooks.reset_account_hooks()


def _token(user: User, **claims: Any) -> str:
    return create_access_token(
        {"sub": str(user.id), **claims},
        auth_generation=user_auth_generation(user),
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _RecordingHook(SessionHook):
    def __init__(self, redirect: Optional[str] = None) -> None:
        self.redirect = redirect
        self.seen: list[Mapping[str, Any]] = []
        self.revoked_marker: Optional[str] = None

    def on_logout(self, db, *, user, claims):
        self.seen.append(dict(claims))
        return LogoutOutcome(redirect_url=self.redirect)

    def is_token_revoked(self, db, *, user, claims):
        return (
            self.revoked_marker is not None
            and claims.get("marker") == self.revoked_marker
        )


def test_logout_without_hook_returns_no_redirect(client, test_user):
    token = _token(test_user)
    response = client.post("/api/v1/auth/logout", headers=_auth(token))
    assert response.status_code == 200
    assert response.json() == {"redirect_url": None}
    # The default hook revokes nothing: the token still works.
    assert client.get("/api/v1/auth/users/me", headers=_auth(token)).status_code == 200


def test_logout_requires_authentication(client):
    assert client.post("/api/v1/auth/logout").status_code == 401


def test_default_session_hook_is_a_no_op(db_session, test_user):
    hook = SessionHook()
    assert hook.on_logout(db_session, user=test_user, claims={"a": 1}) is None
    assert hook.is_token_revoked(db_session, user=test_user, claims={}) is False
    assert account_hooks.run_logout_hook(db_session, test_user, {}) == (LogoutOutcome())
    assert account_hooks.is_token_revoked(db_session, test_user, {}) is False


def test_logout_passes_claims_and_returns_same_origin_redirect(client, test_user):
    hook = _RecordingHook(redirect="/somewhere?x=1")
    account_hooks.register_session_hook(hook)
    token = _token(test_user, marker="m1")

    response = client.post("/api/v1/auth/logout", headers=_auth(token))

    assert response.status_code == 200
    assert response.json() == {"redirect_url": "/somewhere?x=1"}
    assert hook.seen[0]["marker"] == "m1"
    assert hook.seen[0]["sub"] == str(test_user.id)


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example",
        "javascript:alert(1)",
        "relative/path",
        "/ok\r\nSet-Cookie: x",
        "",
    ],
)
def test_logout_drops_redirects_that_leave_the_origin(client, test_user, redirect):
    account_hooks.register_session_hook(_RecordingHook(redirect=redirect))
    response = client.post("/api/v1/auth/logout", headers=_auth(_token(test_user)))
    assert response.status_code == 200
    assert response.json() == {"redirect_url": None}


def test_hook_can_revoke_an_individual_token(client, test_user):
    hook = _RecordingHook()
    account_hooks.register_session_hook(hook)
    revoked = _token(test_user, marker="dead")
    other = _token(test_user, marker="alive")

    hook.revoked_marker = "dead"

    me = client.get("/api/v1/auth/users/me", headers=_auth(revoked))
    assert me.status_code == 401
    assert me.json()["detail"] == "Session revoked, please sign in again"
    assert client.get("/api/v1/auth/users/me", headers=_auth(other)).status_code == 200
