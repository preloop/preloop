"""Tests for OAuth consent page endpoints (GET + POST /mcp/authorize/consent)."""

import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints.oauth_consent import (
    _CLI_MANUAL_REDIRECT_URI,
    _render_template,
    _validate_client_and_redirect,
    router,
)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_provider_singleton(monkeypatch):
    """Reset the module-level singleton before each test."""
    monkeypatch.setattr(
        "preloop.api.endpoints.oauth_consent._oauth_provider_instance", None
    )


@pytest.fixture
def app():
    """Create a test FastAPI app with the consent router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# _render_template
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    """Tests for the Jinja2 template renderer with autoescaping."""

    def test_replaces_variables(self, tmp_path):
        template = tmp_path / "test.html"
        template.write_text("<p>{{ name }} - {{ value }}</p>")

        with patch("preloop.api.endpoints.oauth_consent._TEMPLATE_DIR", tmp_path):
            result = _render_template("test.html", {"name": "hello", "value": "world"})

        assert result == "<p>hello - world</p>"

    def test_tojson_encodes_script_values(self, tmp_path):
        """``|tojson`` emits a JSON literal safe for use inside <script>."""
        template = tmp_path / "test.html"
        template.write_text("<script>const v = {{ val|tojson }};</script>")

        with patch("preloop.api.endpoints.oauth_consent._TEMPLATE_DIR", tmp_path):
            result = _render_template("test.html", {"val": 'he said "hi"'})

        assert '"he said \\"hi\\""' in result

    def test_tojson_escapes_script_tags(self, tmp_path):
        """``|tojson`` must escape < and > to prevent </script> breakout."""
        template = tmp_path / "test.html"
        template.write_text("<script>const v = {{ xss|tojson }};</script>")

        with patch("preloop.api.endpoints.oauth_consent._TEMPLATE_DIR", tmp_path):
            result = _render_template(
                "test.html", {"xss": "</script><img src=x onerror=alert(1)>"}
            )

        assert "</script>" not in result.split("<script>")[1].split("</script>")[0]
        assert "\\u003c" in result
        assert "\\u003e" in result

    def test_html_escapes_regular_values(self, tmp_path):
        """Autoescape must HTML-escape interpolated values."""
        template = tmp_path / "test.html"
        template.write_text("<p>{{ name }}</p>")

        with patch("preloop.api.endpoints.oauth_consent._TEMPLATE_DIR", tmp_path):
            result = _render_template(
                "test.html", {"name": "<script>alert(1)</script>"}
            )

        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_handles_none_values(self, tmp_path):
        template = tmp_path / "test.html"
        template.write_text("<p>{{ maybe }}</p>")

        with patch("preloop.api.endpoints.oauth_consent._TEMPLATE_DIR", tmp_path):
            result = _render_template("test.html", {"maybe": None})

        assert result == "<p></p>"


# ---------------------------------------------------------------------------
# GET /mcp/authorize/consent
# ---------------------------------------------------------------------------


class TestConsentPageGet:
    """Tests for the GET consent page endpoint."""

    def test_redirects_to_spa(self, client):
        with patch(
            "preloop.api.endpoints.oauth_consent._validate_client_and_redirect",
            return_value={"error": None, "client_name": "Test Client"},
        ):
            response = client.get(
                "/mcp/authorize/consent",
                params={
                    "client_id": "c1",
                    "redirect_uri": "http://localhost/cb",
                    "code_challenge": "ch123",
                },
                follow_redirects=False,
            )

        assert response.status_code == 307
        assert response.headers["location"].startswith("/console/authorize")
        assert "client_id=c1" in response.headers["location"]

    def test_passes_params_in_redirect_url(self, client):
        with patch(
            "preloop.api.endpoints.oauth_consent._validate_client_and_redirect",
            return_value={"error": None, "client_name": "Test Client"},
        ):
            response = client.get(
                "/mcp/authorize/consent",
                params={
                    "client_id": "test_client",
                    "redirect_uri": "http://example.com/cb",
                    "code_challenge": "ch",
                    "state": "state_abc",
                    "scopes": "read write",
                },
                follow_redirects=False,
            )

        assert response.status_code == 307
        location = response.headers["location"]
        assert "client_id=test_client" in location
        assert "redirect_uri=http%3A%2F%2Fexample.com%2Fcb" in location
        assert "state=state_abc" in location
        assert "scopes=read+write" in location

    def test_invalid_client_renders_error_html(self, client):
        with patch(
            "preloop.api.endpoints.oauth_consent._validate_client_and_redirect",
            return_value={"error": "Invalid client", "client_name": ""},
        ):
            response = client.get(
                "/mcp/authorize/consent",
                params={
                    "client_id": "invalid",
                    "redirect_uri": "http://example.com/cb",
                },
                follow_redirects=False,
            )

        assert response.status_code == 400
        assert "OAuth Error" in response.text
        assert "Invalid client" in response.text

    def test_missing_required_params(self, client):
        response = client.get("/mcp/authorize/consent", follow_redirects=False)
        assert response.status_code == 422  # Validation error


# ---------------------------------------------------------------------------
# POST /mcp/authorize/consent
# ---------------------------------------------------------------------------


class TestConsentPagePost:
    """Tests for the POST consent submission endpoint."""

    def test_successful_login_redirects(self, client):
        user = MagicMock()
        user.id = uuid4()
        user.account_id = uuid4()
        user.username = "testuser"
        user.hashed_password = "hashed"
        user.is_active = True

        mock_provider = MagicMock()
        mock_provider.create_authorization_code_for_user.return_value = "auth_code_xyz"

        with (
            patch(
                "preloop.api.endpoints.oauth_consent._validate_client_and_redirect",
                return_value={"error": None, "client_name": "Test Client"},
            ),
            patch("preloop.api.endpoints.oauth_consent.get_db_session") as mock_gen,
            patch("preloop.models.crud.crud_user") as mock_crud_user,
            patch("preloop.api.auth.jwt.verify_password", return_value=True),
            patch(
                "preloop.api.endpoints.oauth_consent._get_oauth_provider",
                return_value=mock_provider,
            ),
            patch(
                "preloop.api.endpoints.oauth_consent.construct_redirect_uri",
                return_value="http://localhost/cb?code=auth_code_xyz",
            ),
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud_user.get_by_username.return_value = user

            response = client.post(
                "/mcp/authorize/consent",
                data={
                    "client_id": "c1",
                    "redirect_uri": "http://localhost/cb",
                    "code_challenge": "ch",
                    "username": "testuser",
                    "password": "pass123",
                },
                follow_redirects=False,
            )

        assert response.status_code == 302
        assert "code=auth_code_xyz" in response.headers["location"]

    def test_successful_manual_cli_login_renders_copy_paste_code(self, client):
        user = MagicMock()
        user.id = uuid4()
        user.account_id = uuid4()
        user.username = "testuser"
        user.hashed_password = "hashed"
        user.is_active = True

        mock_provider = MagicMock()
        mock_provider.create_authorization_code_for_user.return_value = "auth_code_xyz"

        with (
            patch("preloop.api.endpoints.oauth_consent.get_db_session") as mock_gen,
            patch("preloop.models.crud.crud_user") as mock_crud_user,
            patch("preloop.api.auth.jwt.verify_password", return_value=True),
            patch(
                "preloop.api.endpoints.oauth_consent._get_oauth_provider",
                return_value=mock_provider,
            ),
            patch(
                "preloop.api.endpoints.oauth_consent.construct_redirect_uri"
            ) as mock_construct_redirect_uri,
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud_user.get_by_username.return_value = user

            response = client.post(
                "/mcp/authorize/consent",
                data={
                    "client_id": "cli",
                    "redirect_uri": _CLI_MANUAL_REDIRECT_URI,
                    "code_challenge": "",
                    "username": "testuser",
                    "password": "pass123",
                },
                follow_redirects=False,
            )

        assert response.status_code == 200
        assert "Authorization Code" in response.text
        assert "auth_code_xyz" in response.text
        mock_construct_redirect_uri.assert_not_called()

    def test_invalid_credentials_renders_error(self, client):
        with (
            patch(
                "preloop.api.endpoints.oauth_consent._validate_client_and_redirect",
                return_value={"error": None, "client_name": "Test Client"},
            ),
            patch("preloop.api.endpoints.oauth_consent.get_db_session") as mock_gen,
            patch("preloop.models.crud.crud_user") as mock_crud_user,
            patch(
                "preloop.api.endpoints.oauth_consent._render_template",
                return_value="<html>error</html>",
            ),
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud_user.get_by_username.return_value = None
            mock_crud_user.get_by_email.return_value = None

            response = client.post(
                "/mcp/authorize/consent",
                data={
                    "client_id": "c1",
                    "redirect_uri": "http://localhost/cb",
                    "code_challenge": "ch",
                    "username": "nobody",
                    "password": "wrong",
                },
            )

        assert response.status_code == 200
        assert "error" in response.text

    def test_wrong_password_renders_error(self, client):
        user = MagicMock()
        user.hashed_password = "hashed"

        with (
            patch(
                "preloop.api.endpoints.oauth_consent._validate_client_and_redirect",
                return_value={"error": None, "client_name": "Test Client"},
            ),
            patch("preloop.api.endpoints.oauth_consent.get_db_session") as mock_gen,
            patch("preloop.models.crud.crud_user") as mock_crud_user,
            patch("preloop.api.auth.jwt.verify_password", return_value=False),
            patch(
                "preloop.api.endpoints.oauth_consent._render_template",
                return_value="<html>bad pass</html>",
            ),
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud_user.get_by_username.return_value = user

            response = client.post(
                "/mcp/authorize/consent",
                data={
                    "client_id": "c1",
                    "redirect_uri": "http://localhost/cb",
                    "code_challenge": "ch",
                    "username": "user",
                    "password": "wrong",
                },
            )

        assert response.status_code == 200


class TestValidateClientAndRedirect:
    def test_cli_client_allows_manual_redirect(self):
        result = _validate_client_and_redirect("cli", _CLI_MANUAL_REDIRECT_URI)

        assert result == {"error": None, "client_name": "Preloop CLI"}

    def test_inactive_user_renders_error(self, client):
        user = MagicMock()
        user.hashed_password = "hashed"
        user.is_active = False

        with (
            patch(
                "preloop.api.endpoints.oauth_consent._validate_client_and_redirect",
                return_value={"error": None, "client_name": "Test Client"},
            ),
            patch("preloop.api.endpoints.oauth_consent.get_db_session") as mock_gen,
            patch("preloop.models.crud.crud_user") as mock_crud_user,
            patch("preloop.api.auth.jwt.verify_password", return_value=True),
            patch(
                "preloop.api.endpoints.oauth_consent._render_template",
                return_value="<html>deactivated</html>",
            ),
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud_user.get_by_username.return_value = user

            response = client.post(
                "/mcp/authorize/consent",
                data={
                    "client_id": "c1",
                    "redirect_uri": "http://localhost/cb",
                    "code_challenge": "ch",
                    "username": "user",
                    "password": "pass",
                },
            )

        assert response.status_code == 200

    def test_oauth_not_configured_renders_error(self, client):
        user = MagicMock()
        user.hashed_password = "hashed"
        user.is_active = True

        with (
            patch(
                "preloop.api.endpoints.oauth_consent._validate_client_and_redirect",
                return_value={"error": None, "client_name": "Test Client"},
            ),
            patch("preloop.api.endpoints.oauth_consent.get_db_session") as mock_gen,
            patch("preloop.models.crud.crud_user") as mock_crud_user,
            patch("preloop.api.auth.jwt.verify_password", return_value=True),
            patch(
                "preloop.api.endpoints.oauth_consent._get_oauth_provider",
                return_value=None,
            ),
            patch(
                "preloop.api.endpoints.oauth_consent._render_template",
                return_value="<html>not configured</html>",
            ),
        ):
            mock_db = MagicMock()
            mock_gen.return_value = iter([mock_db])
            mock_crud_user.get_by_username.return_value = user

            response = client.post(
                "/mcp/authorize/consent",
                data={
                    "client_id": "c1",
                    "redirect_uri": "http://localhost/cb",
                    "code_challenge": "ch",
                    "username": "user",
                    "password": "pass",
                },
            )

        assert response.status_code == 200
