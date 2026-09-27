"""Per-login CLI sessions: revocable CLI JWTs (issue #839, slice 2).

These run the real OAuth and auth routers against the test database. A CLI
login creates a ``cli_session`` row; revoking it through ``/oauth/revoke``,
``DELETE /auth/sessions/cli/{id}`` or refresh-token reuse rules must stop
both the access and the refresh token of that login, and no other login.
"""

import time
from datetime import timedelta
from typing import Any, Iterator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import auth_router
from preloop.api.auth.jwt import (
    create_access_token,
    decode_token,
    get_user_from_token_if_valid_sync,
)
from preloop.api.endpoints.oauth_server import router as oauth_router
from preloop.models.crud import crud_cli_session
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User


class _NoCloseSession:
    """Hand the shared test session to code that closes what it opens."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def close(self) -> None:
        """Keep the per-test transaction open."""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(oauth_router)
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with patch(
        "preloop.models.db.session.get_db_session",
        side_effect=lambda: iter([_NoCloseSession(db_session)]),
    ):
        yield TestClient(app)


def _cli_login(
    client: TestClient,
    user: User,
    *,
    hostname: str = "laptop.example.com",
    user_agent: str = "preloop-cli/0.17.0",
) -> dict:
    """Run the no-PKCE code exchange the CLI uses and return the tokens."""
    db_code = MagicMock()
    db_code.is_used = False
    db_code.expires_at = time.time() + 600
    db_code.redirect_uri = "http://127.0.0.1:9999/callback"
    db_code.code_challenge = ""
    db_code.client_id = "cli"
    db_code.user_id = user.id
    with patch(
        "preloop.models.crud.oauth_mcp_token.crud_oauth_mcp_auth_code"
    ) as auth_codes:
        auth_codes.get_by_code.return_value = db_code
        response = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "code",
                "client_id": "cli",
                "redirect_uri": db_code.redirect_uri,
                "device_name": hostname,
            },
            headers={"User-Agent": user_agent},
        )
    assert response.status_code == 200, response.text
    return response.json()


def _refresh(client: TestClient, refresh_token: str):
    return client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
    )


def _me(client: TestClient, access_token: str):
    return client.get(
        "/api/v1/auth/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )


def test_cli_login_records_session_and_binds_both_tokens(client, db_session, test_user):
    tokens = _cli_login(client, test_user)

    access = decode_token(tokens["access_token"])
    refresh = decode_token(tokens["refresh_token"])
    assert access.sid is not None
    assert access.sid == refresh.sid
    assert access.jti is None
    assert refresh.jti

    rows = crud_cli_session.list_active(db_session, user_id=test_user.id)
    assert [str(row.id) for row in rows] == [access.sid]
    assert rows[0].refresh_jti == refresh.jti
    assert rows[0].hostname == "laptop.example.com"
    assert rows[0].user_agent == "preloop-cli/0.17.0"
    assert _me(client, tokens["access_token"]).status_code == 200


def test_oauth_revoke_with_refresh_token_kills_refresh_and_access(client, test_user):
    tokens = _cli_login(client, test_user)

    revoked = client.post("/oauth/revoke", data={"token": tokens["refresh_token"]})

    assert revoked.status_code == 200
    assert revoked.json() == {"status": "revoked"}
    assert _refresh(client, tokens["refresh_token"]).json()["error"] == (
        "invalid_grant"
    )
    assert _me(client, tokens["access_token"]).status_code == 401


def test_oauth_revoke_with_access_token_kills_the_whole_login(client, test_user):
    tokens = _cli_login(client, test_user)

    revoked = client.post("/oauth/revoke", data={"token": tokens["access_token"]})

    assert revoked.status_code == 200
    assert _me(client, tokens["access_token"]).status_code == 401
    assert _refresh(client, tokens["refresh_token"]).status_code == 400


def test_revoking_one_login_leaves_the_other_signed_in(client, test_user):
    laptop = _cli_login(client, test_user, hostname="laptop")
    server = _cli_login(client, test_user, hostname="build-host")

    client.post("/oauth/revoke", data={"token": laptop["refresh_token"]})

    assert _me(client, laptop["access_token"]).status_code == 401
    assert _me(client, server["access_token"]).status_code == 200
    assert _refresh(client, server["refresh_token"]).status_code == 200


def test_revoked_session_access_token_fails_websocket_style_auth(
    client, db_session, test_user
):
    tokens = _cli_login(client, test_user)
    assert get_user_from_token_if_valid_sync(tokens["access_token"], db_session)

    client.post("/oauth/revoke", data={"token": tokens["refresh_token"]})

    assert get_user_from_token_if_valid_sync(tokens["access_token"], db_session) is None


def test_refresh_rotates_and_rejects_reuse_of_old_refresh_token(
    client, db_session, test_user
):
    tokens = _cli_login(client, test_user)
    first = _refresh(client, tokens["refresh_token"])
    assert first.status_code == 200
    rotated = first.json()

    old = decode_token(tokens["refresh_token"])
    new = decode_token(rotated["refresh_token"])
    assert new.sid == old.sid
    assert new.jti != old.jti

    reuse = _refresh(client, tokens["refresh_token"])
    assert reuse.status_code == 400
    assert reuse.json()["error"] == "invalid_grant"
    assert _refresh(client, rotated["refresh_token"]).status_code == 200


def test_refresh_with_sid_but_no_jti_is_rejected(client, test_user):
    tokens = _cli_login(client, test_user)
    sid = decode_token(tokens["refresh_token"]).sid
    forged = create_access_token(
        {"sub": str(test_user.id), "scopes": [], "refresh": True, "sid": sid},
        expires_delta=timedelta(days=1),
    )

    assert _refresh(client, forged).status_code == 400


def test_legacy_cli_refresh_token_moves_onto_a_session(client, db_session, test_user):
    legacy = create_access_token(
        {"sub": str(test_user.id), "scopes": [], "refresh": True},
        expires_delta=timedelta(days=365),
    )

    response = _refresh(client, legacy)

    assert response.status_code == 200
    body = response.json()
    sid = decode_token(body["refresh_token"]).sid
    assert sid is not None
    assert [
        str(r.id)
        for r in crud_cli_session.list_active(db_session, user_id=test_user.id)
    ] == [sid]
    client.post("/oauth/revoke", data={"token": body["refresh_token"]})
    assert _me(client, body["access_token"]).status_code == 401


def test_oauth_revoke_for_jwt_without_session_still_does_not_claim_success(
    client, test_user
):
    console_refresh = create_access_token(
        {"sub": str(test_user.id), "scopes": [], "refresh": True},
        expires_delta=timedelta(days=1),
    )

    response = client.post("/oauth/revoke", data={"token": console_refresh})

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_token_type"


def test_console_refresh_endpoint_rejects_cli_session_refresh_token(client, test_user):
    tokens = _cli_login(client, test_user)

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 401


def test_list_cli_sessions_marks_current_and_hides_revoked(client, test_user):
    laptop = _cli_login(client, test_user, hostname="laptop")
    server = _cli_login(client, test_user, hostname="build-host")
    gone = _cli_login(client, test_user, hostname="old-host")
    client.post("/oauth/revoke", data={"token": gone["refresh_token"]})

    response = client.get(
        "/api/v1/auth/sessions/cli",
        headers={"Authorization": f"Bearer {laptop['access_token']}"},
    )

    assert response.status_code == 200
    rows = {row["hostname"]: row for row in response.json()}
    assert set(rows) == {"laptop", "build-host"}
    assert rows["laptop"]["current"] is True
    assert rows["build-host"]["current"] is False
    assert rows["build-host"]["id"] == decode_token(server["access_token"]).sid


def test_delete_cli_session_revokes_only_that_session(client, test_user):
    laptop = _cli_login(client, test_user, hostname="laptop")
    server = _cli_login(client, test_user, hostname="build-host")
    server_sid = decode_token(server["access_token"]).sid
    auth = {"Authorization": f"Bearer {laptop['access_token']}"}

    response = client.delete(f"/api/v1/auth/sessions/cli/{server_sid}", headers=auth)

    assert response.status_code == 204
    assert _me(client, server["access_token"]).status_code == 401
    assert _refresh(client, server["refresh_token"]).status_code == 400
    assert _me(client, laptop["access_token"]).status_code == 200
    again = client.delete(f"/api/v1/auth/sessions/cli/{server_sid}", headers=auth)
    assert again.status_code == 404


def test_delete_cli_session_of_another_user_is_not_found(
    client, test_user, test_viewer_user
):
    mine = _cli_login(client, test_user)
    theirs = _cli_login(client, test_viewer_user)
    their_sid = decode_token(theirs["access_token"]).sid

    response = client.delete(
        f"/api/v1/auth/sessions/cli/{their_sid}",
        headers={"Authorization": f"Bearer {mine['access_token']}"},
    )

    assert response.status_code == 404
    assert _me(client, theirs["access_token"]).status_code == 200


def test_oauth_revoke_with_unknown_session_returns_success(client, test_user):
    token = create_access_token(
        {"sub": str(test_user.id), "scopes": [], "sid": str(uuid4())},
        expires_delta=timedelta(minutes=5),
    )

    response = client.post("/oauth/revoke", data={"token": token})

    assert response.status_code == 200
    assert _me(client, token).status_code == 401


def test_revoke_all_marks_every_cli_session_revoked(client, db_session, test_user):
    laptop = _cli_login(client, test_user, hostname="laptop")
    _cli_login(client, test_user, hostname="build-host")

    response = client.post(
        "/api/v1/auth/sessions/revoke-all",
        headers={"Authorization": f"Bearer {laptop['access_token']}"},
    )

    assert response.status_code == 200
    db_session.expire_all()
    assert crud_cli_session.list_active(db_session, user_id=test_user.id) == []
    fresh = _cli_login(client, test_user, hostname="new-host")
    listed = client.get(
        "/api/v1/auth/sessions/cli",
        headers={"Authorization": f"Bearer {fresh['access_token']}"},
    )
    assert [row["hostname"] for row in listed.json()] == ["new-host"]


def test_oauth_revoke_with_expired_cli_token_revokes_its_session(
    client, db_session, test_user
):
    tokens = _cli_login(client, test_user)
    refresh = decode_token(tokens["refresh_token"])
    expired = create_access_token(
        {
            "sub": str(test_user.id),
            "scopes": [],
            "refresh": True,
            "sid": refresh.sid,
            "jti": refresh.jti,
        },
        expires_delta=timedelta(minutes=-5),
    )

    response = client.post("/oauth/revoke", data={"token": expired})

    assert response.status_code == 200
    db_session.expire_all()
    assert crud_cli_session.list_active(db_session, user_id=test_user.id) == []
    assert _me(client, tokens["access_token"]).status_code == 401
