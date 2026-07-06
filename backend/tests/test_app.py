# tests/api/test_app.py
"""
Tests for the Preloop FastAPI application.
"""

import os

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from preloop.api.app import create_app

from tests.route_paths import collect_route_paths


@pytest.fixture
def client():
    """Fixture to create a test client for the FastAPI app."""
    with (
        patch("preloop.api.app.connect_nats", new_callable=AsyncMock),
        patch("preloop.api.app.close_nats", new_callable=AsyncMock),
        patch(
            "preloop.services.websocket_manager.nats_consumer",
            new_callable=AsyncMock,
        ),
    ):
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client


def test_pyinstrument_middleware_disabled_by_default(client):
    """
    Tests that the Pyinstrument middleware is disabled by default.
    """
    with patch("preloop.api.app.Profiler") as mock_profiler:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        mock_profiler.assert_not_called()


@patch.dict(os.environ, {"PROFILING_ENABLED": "true"})
def test_pyinstrument_middleware_enabled(client):
    """
    Tests that the Pyinstrument middleware is enabled when PROFILING_ENABLED is true.
    """
    with (
        patch("preloop.api.app.Profiler") as mock_profiler_cls,
        patch("preloop.api.app.SpeedscopeRenderer") as mock_renderer_cls,
    ):
        mock_profiler_instance = MagicMock()
        mock_profiler_cls.return_value = mock_profiler_instance

        mock_renderer_instance = MagicMock()
        mock_renderer_instance.render.return_value = "{}"  # valid JSON
        mock_renderer_cls.return_value = mock_renderer_instance

        with patch("builtins.open", new_callable=MagicMock) as mock_open:
            response = client.get("/api/v1/health")
            assert response.status_code == 200
            mock_profiler_instance.start.assert_called_once()
            mock_profiler_instance.stop.assert_called_once()
            mock_profiler_instance.output_html.assert_called_once()

            mock_renderer_cls.assert_called_once()
            mock_renderer_instance.render.assert_called_once_with(
                mock_profiler_instance.last_session
            )

            assert mock_open.call_count == 2  # For HTML and speedscope files


@patch.dict(os.environ, {"PROFILING_ENABLED": "true"})
def test_pyinstrument_middleware_non_api_route(client):
    """
    Tests that the Pyinstrument middleware does not profile non-API routes.
    """
    with patch("preloop.api.app.Profiler") as mock_profiler:
        # Test a non-API route - the middleware should not profile it
        response = client.get("/some/non-api/route")
        # The response might be 200 (UI), 403 (RBAC), or 404 depending on test order
        # What matters is that profiling didn't happen
        assert response is not None
        mock_profiler.assert_not_called()


def test_api_usage_middleware_excluded_route(client):
    """
    Tests that API usage is not tracked for excluded routes.
    """
    with patch("preloop.api.app.get_db_session") as mock_get_db_session:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        mock_get_db_session.assert_not_called()


def test_api_usage_middleware_does_not_crash(client):
    """
    Tests that API usage middleware handles requests without crashing.

    The full tracking functionality is tested via integration tests since it
    requires valid authentication and database setup.
    """
    # Just verify the middleware doesn't crash on a tracked endpoint
    response = client.get("/api/v1/trackers")
    # Should return 401 (unauthorized) but not crash
    assert response.status_code == 401


@patch("preloop.api.auth.jwt.decode_token")
def test_api_usage_middleware_db_error(mock_decode_token, client):
    """
    Tests that the middleware handles database errors gracefully.
    """
    mock_decode_token.return_value = MagicMock(sub="testuser")
    mock_session = MagicMock()
    mock_session.commit.side_effect = Exception("DB error")
    mock_db_gen = (i for i in [mock_session])
    with patch("preloop.api.app.get_db_session", return_value=mock_db_gen):
        headers = {"Authorization": "Bearer faketoken"}
        # The request should still complete successfully even if logging fails
        response = client.post("/api/v1/issues", headers=headers)
        # The status code might be 404/422 if the endpoint isn't fully mocked,
        # but it shouldn't be 500.
        assert response.status_code < 500


@patch("preloop.api.app.init_sentry")
@patch("preloop.api.app.setup_database")
def test_lifespan_startup_and_shutdown(
    mock_setup_database,
    mock_init_sentry,
):
    """
    Tests the lifespan manager for correct startup and shutdown procedures.

    Note: NATS and other services are skipped in TESTING mode, so we only
    test the core startup/shutdown logic (Sentry and database).
    """
    with patch.dict(os.environ, {"INIT_DB": "true", "TESTING": "true"}):
        app = create_app()
        with TestClient(app) as client:
            # Startup assertions - only test what actually runs in TESTING mode
            mock_init_sentry.assert_called_once()
            mock_setup_database.assert_called_once()
            # NATS is skipped in TESTING mode, so we don't check it


@patch("preloop.api.app.setup_database", side_effect=Exception("DB setup failed"))
def test_lifespan_startup_db_error(mock_setup_database):
    """
    Tests that a database setup failure during startup raises a RuntimeError.
    """
    with patch.dict(os.environ, {"INIT_DB": "true"}):
        app = create_app()
        with pytest.raises(RuntimeError, match="Database setup failed"):
            with TestClient(app):
                pass  # The error should be raised on context entry


@patch(
    "preloop.api.app.connect_nats",
    new_callable=AsyncMock,
    side_effect=Exception("NATS connection failed"),
)
def test_lifespan_startup_nats_error(mock_connect_nats):
    """
    Tests that a NATS connection failure during startup raises a RuntimeError.

    Note: This test needs TESTING mode disabled so NATS actually gets called.
    """
    # Temporarily disable TESTING mode so NATS connection is attempted
    old_testing = os.environ.get("TESTING")
    if "TESTING" in os.environ:
        del os.environ["TESTING"]

    try:
        app = create_app()
        with pytest.raises(RuntimeError, match="NATS connection failed"):
            with TestClient(app):
                pass  # The error should be raised on context entry
    finally:
        # Restore TESTING mode
        if old_testing is not None:
            os.environ["TESTING"] = old_testing


def test_create_app_configuration():
    """
    Tests that the create_app function configures the FastAPI app correctly.
    """
    # Ensure DEV_MODE and ALLOW_ALL_ORIGINS are not set to get specific origins
    with patch.dict(
        os.environ, {"DEV_MODE": "false", "ALLOW_ALL_ORIGINS": "false"}, clear=False
    ):
        app = create_app()
        assert app.title == "Preloop API"
        assert app.openapi_url == "/api/v1/openapi.json"
        assert len(app.user_middleware) > 0  # Check that middleware is configured

        # Check for CORS middleware
        cors_middleware = [
            m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware"
        ]
        assert len(cors_middleware) == 1
        # In non-dev mode, specific origins should be configured
        assert "http://localhost:5173" in cors_middleware[0].kwargs["allow_origins"]


@patch.dict(os.environ, {"PRELOOP_SERVICE_ROLE": "gateway"}, clear=False)
def test_gateway_role_mounts_only_gateway_surface():
    """Gateway role should not expose the core control-plane API routes."""
    app = create_app()
    route_paths = collect_route_paths(app.routes)

    assert "/api/v1/health" in route_paths
    assert "/api/v1/version" in route_paths
    assert "/openai/v1/models" in route_paths
    assert "/api/v1/trackers" not in route_paths
    assert "/api/v1/auth/login" not in route_paths


@patch.dict(os.environ, {"PRELOOP_SERVICE_ROLE": "api"}, clear=False)
def test_api_role_excludes_model_gateway_surface():
    """Core API role should not serve long-lived model gateway endpoints."""
    app = create_app()
    route_paths = collect_route_paths(app.routes)

    assert "/api/v1/health" in route_paths
    assert "/api/v1/trackers" in route_paths
    assert "/openai/v1/models" not in route_paths


def test_custom_openapi_schema(client):
    """
    Tests that the custom OpenAPI schema is generated correctly.
    """
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "bearerAuth" in schema["components"]["securitySchemes"]
    # Check that a protected route has the security requirement
    assert "security" in schema["paths"]["/api/v1/trackers"]["get"]
