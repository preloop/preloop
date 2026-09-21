"""Tests for OAuth server endpoints (/oauth/token, /oauth/revoke)."""

import json
import time
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints.oauth_server import (
    CLI_JWT_REFRESH_TOKEN_EXPIRE_DAYS,
    _issue_jwt_tokens,
    router,
)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /oauth/token — redirect_uri enforcement
# ---------------------------------------------------------------------------


class TestTokenExchangeRedirectUri:
    """Tests for redirect_uri enforcement during authorization code exchange."""

    def _make_db_code(self, redirect_uri="http://localhost/cb", code_challenge=""):
        code = MagicMock()
        code.is_used = False
        code.expires_at = time.time() + 600
        code.redirect_uri = redirect_uri
        code.code_challenge = code_challenge
        code.client_id = "test_client"
        code.user_id = uuid4()
        code.account_id = uuid4()
        code.scopes = []
        code.resource = None
        return code

    def test_rejects_missing_redirect_uri_when_stored(self, client):
        """If auth code has a redirect_uri, token request MUST include it."""
        db_code = self._make_db_code(redirect_uri="http://localhost/cb")

        with (
            patch("preloop.models.db.session.get_db_session") as mock_gen,
            patch(
                "preloop.models.crud.oauth_mcp_token.crud_oauth_mcp_auth_code"
            ) as mock_crud,
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud.get_by_code.return_value = db_code

            response = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "test_code",
                    "client_id": "test_client",
                    "redirect_uri": "",  # empty — should be rejected
                },
            )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert "redirect_uri is required" in response.json()["error_description"]

    def test_rejects_mismatched_redirect_uri(self, client):
        """redirect_uri must exactly match the one stored in the auth code."""
        db_code = self._make_db_code(redirect_uri="http://localhost/cb")

        with (
            patch("preloop.models.db.session.get_db_session") as mock_gen,
            patch(
                "preloop.models.crud.oauth_mcp_token.crud_oauth_mcp_auth_code"
            ) as mock_crud,
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud.get_by_code.return_value = db_code

            response = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "test_code",
                    "client_id": "test_client",
                    "redirect_uri": "http://evil.com/steal",
                },
            )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"
        assert "does not match" in response.json()["error_description"]

    def test_allows_matching_redirect_uri(self, client):
        """Matching redirect_uri should pass validation and proceed to token issuance."""
        db_code = self._make_db_code(
            redirect_uri="http://localhost/cb", code_challenge=""
        )

        with (
            patch("preloop.models.db.session.get_db_session") as mock_gen,
            patch(
                "preloop.models.crud.oauth_mcp_token.crud_oauth_mcp_auth_code"
            ) as mock_crud,
            patch(
                "preloop.api.endpoints.oauth_server._issue_jwt_tokens",
                new_callable=AsyncMock,
                return_value={"access_token": "t", "token_type": "bearer"},
            ),
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud.get_by_code.return_value = db_code

            response = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "test_code",
                    "client_id": "test_client",
                    "redirect_uri": "http://localhost/cb",
                },
            )

        # Should not be a 400 — it passed redirect_uri validation
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# POST /oauth/revoke — refresh token revocation
# ---------------------------------------------------------------------------


class TestTokenRevocation:
    """Tests for token revocation endpoint."""

    def test_revokes_access_token(self, client):
        """Access tokens should be revoked via the provider."""
        mock_provider = MagicMock()
        mock_token = MagicMock()
        mock_provider.load_access_token = AsyncMock(return_value=mock_token)
        mock_provider.revoke_token = AsyncMock()

        with patch(
            "preloop.api.endpoints.oauth_consent.get_oauth_provider",
            return_value=mock_provider,
        ):
            response = client.post("/oauth/revoke", data={"token": "access_tok"})

        assert response.status_code == 200
        assert response.json()["status"] == "revoked"
        mock_provider.revoke_token.assert_called_once_with(mock_token)

    def test_revokes_refresh_token(self, client):
        """Refresh tokens should be revoked when access token lookup fails."""
        mock_provider = MagicMock()
        mock_provider.load_access_token = AsyncMock(return_value=None)

        mock_db_refresh = MagicMock()
        mock_db_refresh.is_revoked = False
        mock_crud = MagicMock()
        mock_crud.get_by_token.return_value = mock_db_refresh

        with (
            patch(
                "preloop.api.endpoints.oauth_consent.get_oauth_provider",
                return_value=mock_provider,
            ),
            patch(
                "preloop.models.crud.oauth_mcp_token.crud_oauth_mcp_refresh_token",
                mock_crud,
            ),
            patch("preloop.models.db.session.get_db_session") as mock_gen,
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])

            response = client.post("/oauth/revoke", data={"token": "refresh_tok"})

        assert response.status_code == 200
        assert response.json()["status"] == "revoked"
        mock_crud.revoke.assert_called_once_with(mock_db, obj=mock_db_refresh)

    def test_unknown_token_returns_success(self, client):
        """Per RFC 7009, revocation of an unknown token should still return success."""
        mock_provider = MagicMock()
        mock_provider.load_access_token = AsyncMock(return_value=None)

        mock_crud = MagicMock()
        mock_crud.get_by_token.return_value = None

        with (
            patch(
                "preloop.api.endpoints.oauth_consent.get_oauth_provider",
                return_value=mock_provider,
            ),
            patch(
                "preloop.models.crud.oauth_mcp_token.crud_oauth_mcp_refresh_token",
                mock_crud,
            ),
            patch("preloop.models.db.session.get_db_session") as mock_gen,
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])

            response = client.post("/oauth/revoke", data={"token": "unknown_tok"})

        assert response.status_code == 200
        assert response.json()["status"] == "revoked"

    def test_cli_jwt_returns_unsupported_token_type(self, client):
        """A CLI JWT is not an opaque token; revoke must not claim success."""
        from preloop.api.auth.jwt import create_access_token

        token = create_access_token(
            {"sub": str(uuid4()), "scopes": [], "refresh": True},
            expires_delta=timedelta(days=1),
        )
        mock_provider = MagicMock()
        mock_provider.load_access_token = AsyncMock(return_value=None)
        mock_crud = MagicMock()
        mock_crud.get_by_token.return_value = None

        with (
            patch(
                "preloop.api.endpoints.oauth_consent.get_oauth_provider",
                return_value=mock_provider,
            ),
            patch(
                "preloop.models.crud.oauth_mcp_token.crud_oauth_mcp_refresh_token",
                mock_crud,
            ),
            patch("preloop.models.db.session.get_db_session") as mock_gen,
        ):
            mock_gen.side_effect = lambda: iter([MagicMock()])
            response = client.post("/oauth/revoke", data={"token": token})

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "unsupported_token_type"
        assert "revoke-all" in body["error_description"]


@pytest.mark.asyncio
async def test_issue_jwt_tokens_uses_long_lived_cli_refresh_tokens():
    db_code = MagicMock()
    db_code.user_id = uuid4()

    user = MagicMock()
    user.id = db_code.user_id
    user.auth_generation = 3
    captured = []

    def _capture_token(*args, **kwargs):
        captured.append(kwargs)
        return f"token-{len(captured)}"

    with (
        patch("preloop.models.crud.crud_user.get", return_value=user),
        patch("preloop.api.auth.jwt.create_access_token", side_effect=_capture_token),
        patch("preloop.api.auth.jwt.ACCESS_TOKEN_EXPIRE_MINUTES", 60),
    ):
        response = await _issue_jwt_tokens(MagicMock(), db_code)

    assert response.status_code == 200
    assert captured[0]["expires_delta"] == timedelta(minutes=60)
    assert captured[0]["auth_generation"] == 3
    assert captured[1]["expires_delta"] == timedelta(
        days=CLI_JWT_REFRESH_TOKEN_EXPIRE_DAYS
    )
    assert captured[1]["auth_generation"] == 3
    assert captured[1]["data"]["refresh"] is True


def _jwt_refresh_patches(user):
    mock_crud = MagicMock()
    mock_crud.get_by_token.return_value = None
    stack = ExitStack()
    stack.enter_context(
        patch(
            "preloop.models.crud.oauth_mcp_token.crud_oauth_mcp_refresh_token",
            mock_crud,
        )
    )
    stack.enter_context(
        patch(
            "preloop.models.db.session.get_db_session",
            side_effect=lambda: iter([MagicMock()]),
        )
    )
    stack.enter_context(patch("preloop.models.crud.crud_user.get", return_value=user))
    return stack


@pytest.mark.asyncio
async def test_oauth_jwt_refresh_rejects_stale_generation():
    from preloop.api.auth.jwt import create_access_token
    from preloop.api.endpoints.oauth_server import _handle_refresh_token

    user = MagicMock()
    user.is_active = True
    user.auth_generation = 2
    user.id = uuid4()
    token = create_access_token(
        {"sub": str(user.id), "scopes": [], "refresh": True},
        expires_delta=timedelta(days=1),
        auth_generation=1,
    )

    with _jwt_refresh_patches(user):
        response = await _handle_refresh_token(token, "cli")

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"] == "invalid_grant"
    assert "Session revoked" in body["error_description"]


@pytest.mark.asyncio
async def test_oauth_jwt_refresh_rejects_inactive_user():
    from preloop.api.auth.jwt import create_access_token
    from preloop.api.endpoints.oauth_server import _handle_refresh_token

    user = MagicMock()
    user.is_active = False
    user.auth_generation = 0
    user.id = uuid4()
    token = create_access_token(
        {"sub": str(user.id), "scopes": [], "refresh": True},
        expires_delta=timedelta(days=1),
        auth_generation=0,
    )

    with _jwt_refresh_patches(user):
        response = await _handle_refresh_token(token, "cli")

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"] == "invalid_grant"
    assert "inactive" in body["error_description"].lower()


@pytest.mark.asyncio
async def test_oauth_jwt_refresh_rejects_missing_gen_after_bump():
    import jwt as pyjwt
    from preloop.api.auth.jwt import SECRET_KEY, ALGORITHM
    from preloop.api.endpoints.oauth_server import _handle_refresh_token

    user = MagicMock()
    user.is_active = True
    user.auth_generation = 1
    user.id = uuid4()
    token = pyjwt.encode(
        {
            "sub": str(user.id),
            "scopes": [],
            "refresh": True,
            "exp": int((time.time()) + 86400),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )

    with _jwt_refresh_patches(user):
        response = await _handle_refresh_token(token, "cli")

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"] == "invalid_grant"
    assert "Session revoked" in body["error_description"]


@pytest.mark.asyncio
async def test_oauth_jwt_refresh_remints_with_current_generation():
    from preloop.api.auth.jwt import create_access_token, decode_token
    from preloop.api.endpoints.oauth_server import _handle_refresh_token

    user = MagicMock()
    user.is_active = True
    user.auth_generation = 4
    user.id = uuid4()
    token = create_access_token(
        {"sub": str(user.id), "scopes": [], "refresh": True},
        expires_delta=timedelta(days=365),
        auth_generation=4,
    )

    with _jwt_refresh_patches(user):
        response = await _handle_refresh_token(token, "cli")

    assert response.status_code == 200
    body = json.loads(response.body)
    rotated = decode_token(body["refresh_token"])
    assert rotated.refresh is True
    assert rotated.gen == 4
    assert rotated.session_started_at is None
