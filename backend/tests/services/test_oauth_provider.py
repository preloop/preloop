"""Tests for PreloopOAuthProvider (MCP OAuth 2.1 Authorization Server)."""

import time
from datetime import UTC, datetime

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
from uuid import uuid4

from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from preloop.services.oauth_provider import (
    PreloopOAuthProvider,
    ACCESS_TOKEN_EXPIRY,
    REFRESH_TOKEN_EXPIRY,
    AUTH_CODE_EXPIRY,
)


@pytest.fixture(autouse=True)
def canonical_identity_boundary(monkeypatch):
    """Keep protocol unit tests independent of identity persistence."""
    from preloop.models.crud import crud_person, crud_account

    person = SimpleNamespace(
        id=uuid4(),
        username="protocol-person",
        email="protocol@example.com",
        identity_status="active",
        email_verified_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        crud_person, "resolve_for_membership", lambda *args, **kwargs: person
    )
    monkeypatch.setattr(crud_person, "lock_for_session", lambda *args, **kwargs: person)
    monkeypatch.setattr(
        crud_account, "get", lambda *args, **kwargs: SimpleNamespace(is_active=True)
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    db = MagicMock()
    return db


@pytest.fixture
def provider():
    """Create a PreloopOAuthProvider with test URLs."""
    with patch("preloop.services.oauth_provider._get_db") as _:
        p = PreloopOAuthProvider(
            base_url="http://localhost:8000/mcp",
            issuer_url="http://localhost:8000/mcp",
        )
    return p


@pytest.fixture
def mock_client():
    """A mock registered OAuth client."""
    return OAuthClientInformationFull(
        client_id="preloop_test123",
        client_secret=None,
        redirect_uris=["http://localhost:3000/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
        client_name="Test MCP Client",
        scope="",
    )


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------


class TestGetClient:
    """Tests for get_client method."""

    @pytest.mark.asyncio
    async def test_get_client_found(self, provider, mock_db):
        db_client = MagicMock()
        db_client.client_id = "preloop_abc"
        db_client.client_id_issued_at = 1000
        db_client.client_secret_expires_at = 0
        db_client.redirect_uris = ["http://localhost/cb"]
        db_client.grant_types = ["authorization_code"]
        db_client.response_types = ["code"]
        db_client.token_endpoint_auth_method = "client_secret_post"
        db_client.client_name = "Test"
        db_client.scope = ""

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch("preloop.services.oauth_provider.crud_oauth_mcp_client") as mock_crud,
        ):
            mock_crud.get_by_client_id.return_value = db_client
            result = await provider.get_client("preloop_abc")

        assert result is not None
        assert result.client_id == "preloop_abc"
        assert result.client_secret is None  # Never exposed
        assert result.client_name == "Test"
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_client_not_found(self, provider, mock_db):
        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch("preloop.services.oauth_provider.crud_oauth_mcp_client") as mock_crud,
        ):
            mock_crud.get_by_client_id.return_value = None
            result = await provider.get_client("missing")

        assert result is None
        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# register_client
# ---------------------------------------------------------------------------


class TestRegisterClient:
    """Tests for register_client method."""

    @pytest.mark.asyncio
    async def test_register_client_mutates_info(self, provider, mock_db):
        client_info = OAuthClientInformationFull(
            client_id="placeholder",
            redirect_uris=["http://localhost/cb"],
            client_name="New Client",
        )

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch("preloop.services.oauth_provider.crud_oauth_mcp_client") as mock_crud,
        ):
            mock_crud.generate_client_id.return_value = "preloop_new123"
            mock_crud.generate_client_secret.return_value = "secret_abc"
            mock_crud.hash_secret.return_value = "hashed_secret"
            mock_crud.create.return_value = MagicMock()

            await provider.register_client(client_info)

        # Verify the client_info was mutated with generated credentials
        assert client_info.client_id == "preloop_new123"
        assert client_info.client_secret == "secret_abc"
        assert client_info.client_secret_expires_at == 0
        assert client_info.client_id_issued_at is not None
        mock_crud.create.assert_called_once()
        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# authorize
# ---------------------------------------------------------------------------


class TestAuthorize:
    """Tests for authorize method."""

    @pytest.mark.asyncio
    async def test_authorize_returns_consent_url(self, provider, mock_client):
        params = AuthorizationParams(
            client_id=mock_client.client_id,
            redirect_uri="http://localhost:3000/callback",
            state="state123",
            scopes=["read", "write"],
            code_challenge="challenge_abc",
            code_challenge_method="S256",
            redirect_uri_provided_explicitly=True,
        )

        url = await provider.authorize(mock_client, params)

        assert url.startswith("http://localhost:8000/mcp/authorize/consent?")
        assert "client_id=preloop_test123" in url
        assert "code_challenge=challenge_abc" in url
        assert "state=state123" in url
        assert "scopes=read+write" in url

    @pytest.mark.asyncio
    async def test_authorize_strips_double_mcp(self, provider, mock_client):
        """Verify that base_url ending in /mcp doesn't produce /mcp/mcp."""
        params = AuthorizationParams(
            client_id=mock_client.client_id,
            redirect_uri="http://localhost:3000/callback",
            state="s",
            scopes=[],
            code_challenge="ch",
            code_challenge_method="S256",
            redirect_uri_provided_explicitly=True,
        )

        url = await provider.authorize(mock_client, params)
        assert "/mcp/mcp/" not in url
        assert "/mcp/authorize/consent" in url

    @pytest.mark.asyncio
    async def test_authorize_empty_state(self, provider, mock_client):
        params = AuthorizationParams(
            client_id=mock_client.client_id,
            redirect_uri="http://localhost:3000/callback",
            state="",
            scopes=[],
            code_challenge="ch",
            code_challenge_method="S256",
            redirect_uri_provided_explicitly=True,
        )

        url = await provider.authorize(mock_client, params)
        # Should still produce a valid URL
        assert "/mcp/authorize/consent" in url


# ---------------------------------------------------------------------------
# load_authorization_code
# ---------------------------------------------------------------------------


class TestLoadAuthorizationCode:
    """Tests for load_authorization_code method."""

    @pytest.mark.asyncio
    async def test_load_valid_code(self, provider, mock_db, mock_client):
        db_code = MagicMock()
        db_code.expires_at = time.time() + 300
        db_code.is_used = False
        db_code.scopes = ["read"]
        db_code.client_id = mock_client.client_id
        db_code.code_challenge = "ch"
        db_code.redirect_uri = "http://localhost/cb"
        db_code.redirect_uri_provided_explicitly = True
        db_code.resource = None

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_auth_code"
            ) as mock_crud,
        ):
            mock_crud.get_by_code.return_value = db_code
            result = await provider.load_authorization_code(mock_client, "raw-code")

        assert result is not None
        assert isinstance(result, AuthorizationCode)
        assert result.code == "raw-code"
        assert result.scopes == ["read"]
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_expired_code(self, provider, mock_db, mock_client):
        db_code = MagicMock()
        db_code.expires_at = time.time() - 10  # Expired
        db_code.is_used = False

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_auth_code"
            ) as mock_crud,
        ):
            mock_crud.get_by_code.return_value = db_code
            result = await provider.load_authorization_code(mock_client, "expired-code")

        assert result is None

    @pytest.mark.asyncio
    async def test_load_used_code(self, provider, mock_db, mock_client):
        db_code = MagicMock()
        db_code.expires_at = time.time() + 300
        db_code.is_used = True

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_auth_code"
            ) as mock_crud,
        ):
            mock_crud.get_by_code.return_value = db_code
            result = await provider.load_authorization_code(mock_client, "used-code")

        assert result is None

    @pytest.mark.asyncio
    async def test_load_missing_code(self, provider, mock_db, mock_client):
        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_auth_code"
            ) as mock_crud,
        ):
            mock_crud.get_by_code.return_value = None
            result = await provider.load_authorization_code(mock_client, "nope")

        assert result is None


# ---------------------------------------------------------------------------
# exchange_authorization_code
# ---------------------------------------------------------------------------


class TestExchangeAuthorizationCode:
    """Tests for exchange_authorization_code method."""

    @pytest.mark.asyncio
    async def test_exchange_success(self, provider, mock_db, mock_client):
        db_code = MagicMock()
        db_code.is_used = False
        db_code.user_id = uuid4()
        db_code.account_id = uuid4()
        mock_db.query.return_value.filter.return_value.first.return_value.id = (
            db_code.user_id
        )
        mock_db.query.return_value.filter.return_value.first.return_value.account_id = (
            db_code.account_id
        )
        db_code.scopes = ["read"]
        db_code.resource = None

        auth_code = AuthorizationCode(
            code="raw-code",
            scopes=["read"],
            expires_at=time.time() + 300,
            client_id=mock_client.client_id,
            code_challenge="ch",
            redirect_uri="http://localhost/cb",
            redirect_uri_provided_explicitly=True,
        )

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_auth_code"
            ) as mock_code_crud,
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_at_crud,
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_rt_crud,
            patch("preloop.services.oauth_provider.generate_token") as mock_gen,
        ):
            mock_code_crud.get_by_code.return_value = db_code
            mock_gen.side_effect = ["access_tok", "refresh_tok"]

            result = await provider.exchange_authorization_code(mock_client, auth_code)

        assert isinstance(result, OAuthToken)
        assert result.access_token == "access_tok"
        assert result.refresh_token == "refresh_tok"
        assert result.token_type == "Bearer"
        assert result.expires_in == ACCESS_TOKEN_EXPIRY
        mock_code_crud.mark_used.assert_called_once()
        mock_at_crud.create.assert_called_once()
        mock_rt_crud.create.assert_called_once()
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_exchange_code_not_found(self, provider, mock_db, mock_client):
        auth_code = AuthorizationCode(
            code="missing",
            scopes=[],
            expires_at=time.time() + 300,
            client_id=mock_client.client_id,
            code_challenge="ch",
            redirect_uri="http://localhost/cb",
            redirect_uri_provided_explicitly=True,
        )

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_auth_code"
            ) as mock_crud,
        ):
            mock_crud.get_by_code.return_value = None

            with pytest.raises(TokenError) as exc_info:
                await provider.exchange_authorization_code(mock_client, auth_code)

        assert "not found" in str(exc_info.value.error_description).lower()

    @pytest.mark.asyncio
    async def test_exchange_code_already_used(self, provider, mock_db, mock_client):
        db_code = MagicMock()
        db_code.is_used = True

        auth_code = AuthorizationCode(
            code="used",
            scopes=[],
            expires_at=time.time() + 300,
            client_id=mock_client.client_id,
            code_challenge="ch",
            redirect_uri="http://localhost/cb",
            redirect_uri_provided_explicitly=True,
        )

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_auth_code"
            ) as mock_crud,
        ):
            mock_crud.get_by_code.return_value = db_code

            with pytest.raises(TokenError) as exc_info:
                await provider.exchange_authorization_code(mock_client, auth_code)

        assert "already used" in str(exc_info.value.error_description).lower()


# ---------------------------------------------------------------------------
# load_refresh_token
# ---------------------------------------------------------------------------


class TestLoadRefreshToken:
    """Tests for load_refresh_token method."""

    @pytest.mark.asyncio
    async def test_load_valid_refresh(self, provider, mock_db, mock_client):
        db_token = MagicMock()
        db_token.is_revoked = False
        db_token.client_id = mock_client.client_id
        db_token.scopes = ["read"]
        db_token.expires_at = int(time.time()) + 86400

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_crud,
        ):
            mock_crud.get_by_token.return_value = db_token
            result = await provider.load_refresh_token(mock_client, "refresh_tok")

        assert result is not None
        assert isinstance(result, RefreshToken)
        assert result.token == "refresh_tok"

    @pytest.mark.asyncio
    async def test_load_revoked_refresh(self, provider, mock_db, mock_client):
        db_token = MagicMock()
        db_token.is_revoked = True

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_crud,
        ):
            mock_crud.get_by_token.return_value = db_token
            result = await provider.load_refresh_token(mock_client, "revoked")

        assert result is None

    @pytest.mark.asyncio
    async def test_load_wrong_client(self, provider, mock_db, mock_client):
        db_token = MagicMock()
        db_token.is_revoked = False
        db_token.client_id = "different_client"

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_crud,
        ):
            mock_crud.get_by_token.return_value = db_token
            result = await provider.load_refresh_token(mock_client, "tok")

        assert result is None

    @pytest.mark.asyncio
    async def test_load_expired_refresh(self, provider, mock_db, mock_client):
        db_token = MagicMock()
        db_token.is_revoked = False
        db_token.client_id = mock_client.client_id
        db_token.expires_at = int(time.time()) - 10

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_crud,
        ):
            mock_crud.get_by_token.return_value = db_token
            result = await provider.load_refresh_token(mock_client, "expired")

        assert result is None

    @pytest.mark.asyncio
    async def test_load_missing_refresh(self, provider, mock_db, mock_client):
        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_crud,
        ):
            mock_crud.get_by_token.return_value = None
            result = await provider.load_refresh_token(mock_client, "nope")

        assert result is None


# ---------------------------------------------------------------------------
# exchange_refresh_token
# ---------------------------------------------------------------------------


class TestExchangeRefreshToken:
    """Tests for exchange_refresh_token (token rotation)."""

    @pytest.mark.asyncio
    async def test_rotation_success(self, provider, mock_db, mock_client):
        user_id = uuid4()
        account_id = uuid4()
        db_old_refresh = MagicMock()
        db_old_refresh.is_revoked = False
        db_old_refresh.client_id = mock_client.client_id
        db_old_refresh.expires_at = int(time.time()) + 3600
        db_old_refresh.user_id = user_id
        db_old_refresh.account_id = account_id
        mock_db.query.return_value.filter.return_value.first.return_value.id = user_id
        mock_db.query.return_value.filter.return_value.first.return_value.account_id = (
            account_id
        )

        refresh = RefreshToken(
            token="old_refresh",
            client_id=mock_client.client_id,
            scopes=["read"],
            expires_at=int(time.time()) + 86400,
        )

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_rt,
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_at,
            patch("preloop.services.oauth_provider.generate_token") as mock_gen,
        ):
            mock_rt.get_by_token.return_value = db_old_refresh
            mock_gen.side_effect = ["new_access", "new_refresh"]

            result = await provider.exchange_refresh_token(mock_client, refresh, [])

        assert isinstance(result, OAuthToken)
        assert result.access_token == "new_access"
        assert result.refresh_token == "new_refresh"
        # Old refresh token should be revoked
        mock_rt.revoke.assert_called_once_with(
            mock_db, obj=db_old_refresh, commit=False
        )
        mock_at.create.assert_called_once()
        mock_rt.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_rotation_not_found(self, provider, mock_db, mock_client):
        refresh = RefreshToken(
            token="missing",
            client_id=mock_client.client_id,
            scopes=[],
        )

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_rt,
        ):
            mock_rt.get_by_token.return_value = None

            with pytest.raises(TokenError):
                await provider.exchange_refresh_token(mock_client, refresh, [])

    @pytest.mark.asyncio
    async def test_rotation_uses_provided_scopes(self, provider, mock_db, mock_client):
        db_old = MagicMock()
        db_old.is_revoked = False
        db_old.client_id = mock_client.client_id
        db_old.expires_at = int(time.time()) + 3600
        db_old.user_id = uuid4()
        db_old.account_id = uuid4()
        mock_db.query.return_value.filter.return_value.first.return_value.id = (
            db_old.user_id
        )
        mock_db.query.return_value.filter.return_value.first.return_value.account_id = (
            db_old.account_id
        )

        refresh = RefreshToken(
            token="tok",
            client_id=mock_client.client_id,
            scopes=["old_scope"],
        )

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_rt,
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_at,
            patch("preloop.services.oauth_provider.generate_token") as mock_gen,
        ):
            mock_rt.get_by_token.return_value = db_old
            mock_gen.side_effect = ["at", "rt"]

            result = await provider.exchange_refresh_token(
                mock_client, refresh, ["new_scope"]
            )

        assert result.scope == "new_scope"
        # Verify new tokens created with new scopes
        at_call_kwargs = mock_at.create.call_args[1]
        assert at_call_kwargs["scopes"] == ["new_scope"]


# ---------------------------------------------------------------------------
# load_access_token
# ---------------------------------------------------------------------------


class TestLoadAccessToken:
    """Tests for load_access_token (token verification + legacy fallback)."""

    @pytest.mark.asyncio
    async def test_valid_oauth_token(self, provider, mock_db):
        user = MagicMock()
        user.id = uuid4()

        db_token = MagicMock()
        db_token.is_revoked = False
        db_token.expires_at = int(time.time()) + 3600
        db_token.client_id = "client_1"
        db_token.scopes = ["read"]
        db_token.resource = None
        db_token.user_id = user.id
        db_token.account_id = user.account_id

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_crud,
            patch("preloop.models.crud.crud_user") as mock_user_crud,
        ):
            mock_crud.get_by_token.return_value = db_token
            mock_user_crud.get.return_value = user

            result = await provider.load_access_token("valid_token")

        assert result is not None
        assert result.token == "valid_token"
        assert result.client_id == "client_1"
        assert result.scopes == ["read"]

    @pytest.mark.asyncio
    async def test_revoked_token_falls_back(self, provider, mock_db):
        db_token = MagicMock()
        db_token.is_revoked = True

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_crud,
            patch(
                "preloop.api.auth.jwt.get_user_from_token_if_valid",
                new_callable=AsyncMock,
            ) as mock_legacy,
        ):
            mock_crud.get_by_token.return_value = db_token
            mock_legacy.return_value = None

            result = await provider.load_access_token("revoked")

        assert result is None

    @pytest.mark.asyncio
    async def test_expired_token_returns_none(self, provider, mock_db):
        db_token = MagicMock()
        db_token.is_revoked = False
        db_token.expires_at = int(time.time()) - 10  # Expired

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_crud,
            patch(
                "preloop.api.auth.jwt.get_user_from_token_if_valid",
                new_callable=AsyncMock,
            ) as mock_legacy,
        ):
            mock_crud.get_by_token.return_value = db_token
            mock_legacy.return_value = None

            result = await provider.load_access_token("expired")

        assert result is None

    @pytest.mark.asyncio
    async def test_legacy_jwt_fallback(self, provider, mock_db):
        user = MagicMock()
        user.id = uuid4()

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_crud,
            patch(
                "preloop.api.auth.jwt.get_user_from_token_if_valid",
                new_callable=AsyncMock,
            ) as mock_legacy,
        ):
            mock_crud.get_by_token.return_value = None
            mock_legacy.return_value = user

            result = await provider.load_access_token("jwt.token.here")

        assert result is not None
        assert result.client_id == str(user.id)

    @pytest.mark.asyncio
    async def test_no_token_no_fallback(self, provider, mock_db):
        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_crud,
            patch(
                "preloop.api.auth.jwt.get_user_from_token_if_valid",
                new_callable=AsyncMock,
            ) as mock_legacy,
        ):
            mock_crud.get_by_token.return_value = None
            mock_legacy.return_value = None

            result = await provider.load_access_token("unknown")

        assert result is None


# ---------------------------------------------------------------------------
# revoke_token
# ---------------------------------------------------------------------------


class TestRevokeToken:
    """Tests for revoke_token method."""

    @pytest.mark.asyncio
    async def test_revoke_access_token(self, provider, mock_db):
        from fastmcp.server.auth.auth import AccessToken

        access = AccessToken(
            token="at_123",
            client_id="c1",
            scopes=[],
        )

        db_access = MagicMock()
        db_access.user_id = uuid4()
        db_access.client_id = "c1"

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_at,
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_rt,
        ):
            mock_at.get_by_token.return_value = db_access

            await provider.revoke_token(access)

        mock_at.revoke.assert_called_once_with(mock_db, obj=db_access)
        mock_rt.revoke_by_user_and_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_refresh_token(self, provider, mock_db):
        refresh = RefreshToken(
            token="rt_123",
            client_id="c1",
            scopes=[],
        )

        db_refresh = MagicMock()
        db_refresh.user_id = uuid4()
        db_refresh.client_id = "c1"

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_access_token"
            ) as mock_at,
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_refresh_token"
            ) as mock_rt,
        ):
            mock_rt.get_by_token.return_value = db_refresh

            await provider.revoke_token(refresh)

        mock_rt.revoke.assert_called_once_with(mock_db, obj=db_refresh)
        mock_at.revoke_by_user_and_client.assert_called_once()


# ---------------------------------------------------------------------------
# create_authorization_code_for_user
# ---------------------------------------------------------------------------


class TestCreateAuthorizationCodeForUser:
    """Tests for the helper used by the consent page handler."""

    def test_creates_and_returns_code(self, provider, mock_db):
        user_id = uuid4()
        account_id = uuid4()
        mock_db.query.return_value.filter.return_value.first.return_value.id = user_id
        mock_db.query.return_value.filter.return_value.first.return_value.account_id = (
            account_id
        )

        with (
            patch("preloop.services.oauth_provider._get_db", return_value=mock_db),
            patch(
                "preloop.services.oauth_provider.crud_oauth_mcp_auth_code"
            ) as mock_crud,
            patch(
                "preloop.services.oauth_provider.generate_authorization_code",
                return_value="test_code",
            ),
        ):
            code = provider.create_authorization_code_for_user(
                client_id="c1",
                user_id=user_id,
                account_id=account_id,
                redirect_uri="http://localhost/cb",
                redirect_uri_provided_explicitly=True,
                code_challenge="challenge",
                scopes=["read"],
            )

        assert code == "test_code"
        mock_crud.create.assert_called_once()
        create_kwargs = mock_crud.create.call_args[1]
        assert create_kwargs["code"] == "test_code"
        assert create_kwargs["client_id"] == "c1"
        assert create_kwargs["user_id"] == user_id
        mock_db.close.assert_called_once()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestDefaults:
    """Verify default expiry constants."""

    def test_access_token_expiry(self):
        assert ACCESS_TOKEN_EXPIRY == 3600

    def test_refresh_token_expiry(self):
        assert REFRESH_TOKEN_EXPIRY == 2592000

    def test_auth_code_expiry(self):
        assert AUTH_CODE_EXPIRY == 300
