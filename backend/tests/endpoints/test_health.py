"""Tests for health check endpoint."""

from unittest.mock import MagicMock, patch

import pytest


class TestHealthCheck:
    """Test health_check endpoint."""

    @patch("preloop.services.mcp_client_pool.get_mcp_client_pool")
    @patch("preloop.services.mcp_http.get_mcp_lifespan_manager")
    def test_health_check_all_healthy(
        self, mock_get_mcp_lifespan, mock_get_client_pool, mock_db_session
    ):
        """Test health check when all systems are healthy."""
        from preloop.api.endpoints.health import health_check

        # Mock database session
        mock_db_session.execute.return_value = None

        # Mock MCP lifespan manager
        mock_lifespan = MagicMock()
        mock_get_mcp_lifespan.return_value = mock_lifespan

        # Mock MCP client pool
        mock_pool = MagicMock()
        mock_pool.get_active_servers.return_value = ["server1", "server2"]
        mock_get_client_pool.return_value = mock_pool

        result = health_check(db=mock_db_session)

        assert result["status"] == "healthy"
        assert result["database"] == "connected"
        assert result["mcp_server"] == "available"
        assert result["upstream_connections"] == 2
        assert result["upstream_servers"] == ["server1", "server2"]
        assert "timestamp" in result

    @patch("preloop.services.mcp_client_pool.get_mcp_client_pool")
    @patch("preloop.services.mcp_http.get_mcp_lifespan_manager")
    def test_health_check_database_error(
        self, mock_get_mcp_lifespan, mock_get_client_pool, mock_db_session
    ):
        """Test health check when database connection fails."""
        from preloop.api.endpoints.health import health_check

        # Mock database error
        mock_db_session.execute.side_effect = Exception("Database connection failed")

        # Mock MCP services (healthy)
        mock_lifespan = MagicMock()
        mock_get_mcp_lifespan.return_value = mock_lifespan
        mock_pool = MagicMock()
        mock_pool.get_active_servers.return_value = []
        mock_get_client_pool.return_value = mock_pool

        result = health_check(db=mock_db_session)

        assert result["status"] == "unhealthy"
        # Health responses expose exception type only (no detail / stack).
        assert result["database"] == "error: Exception"
        assert result["mcp_server"] == "available"
        assert result["upstream_connections"] == 0

    @patch("preloop.services.mcp_client_pool.get_mcp_client_pool")
    @patch("preloop.services.mcp_http.get_mcp_lifespan_manager")
    def test_health_check_mcp_server_not_initialized(
        self, mock_get_mcp_lifespan, mock_get_client_pool, mock_db_session
    ):
        """Test health check when MCP server is not initialized."""
        from preloop.api.endpoints.health import health_check

        # Mock database (healthy)
        mock_db_session.execute.return_value = None

        # Mock MCP lifespan manager returning None
        mock_get_mcp_lifespan.return_value = None

        # Mock MCP client pool
        mock_pool = MagicMock()
        mock_pool.get_active_servers.return_value = []
        mock_get_client_pool.return_value = mock_pool

        result = health_check(db=mock_db_session)

        assert result["status"] == "healthy"
        assert result["database"] == "connected"
        assert result["mcp_server"] == "not_initialized"
        assert result["upstream_connections"] == 0

    @patch("preloop.services.mcp_client_pool.get_mcp_client_pool")
    @patch("preloop.services.mcp_http.get_mcp_lifespan_manager")
    def test_health_check_mcp_server_error(
        self, mock_get_mcp_lifespan, mock_get_client_pool, mock_db_session
    ):
        """Test health check when MCP server check raises an error."""
        from preloop.api.endpoints.health import health_check

        # Mock database (healthy)
        mock_db_session.execute.return_value = None

        # Mock MCP lifespan manager error
        mock_get_mcp_lifespan.side_effect = Exception("MCP initialization error")

        # Mock MCP client pool
        mock_pool = MagicMock()
        mock_pool.get_active_servers.return_value = []
        mock_get_client_pool.return_value = mock_pool

        result = health_check(db=mock_db_session)

        assert result["status"] == "healthy"
        assert result["database"] == "connected"
        assert result["mcp_server"] == "error: Exception"
        assert result["upstream_connections"] == 0

    @patch("preloop.services.mcp_client_pool.get_mcp_client_pool")
    @patch("preloop.services.mcp_http.get_mcp_lifespan_manager")
    def test_health_check_upstream_connections_error(
        self, mock_get_mcp_lifespan, mock_get_client_pool, mock_db_session
    ):
        """Test health check when upstream connections check raises an error."""
        from preloop.api.endpoints.health import health_check

        # Mock database (healthy)
        mock_db_session.execute.return_value = None

        # Mock MCP lifespan manager (healthy)
        mock_lifespan = MagicMock()
        mock_get_mcp_lifespan.return_value = mock_lifespan

        # Mock MCP client pool error
        mock_get_client_pool.side_effect = Exception("Client pool unavailable")

        result = health_check(db=mock_db_session)

        assert result["status"] == "healthy"
        assert result["database"] == "connected"
        assert result["mcp_server"] == "available"
        assert result["upstream_connections"] == "error: Exception"

    @patch("preloop.services.mcp_client_pool.get_mcp_client_pool")
    @patch("preloop.services.mcp_http.get_mcp_lifespan_manager")
    def test_health_check_no_upstream_servers(
        self, mock_get_mcp_lifespan, mock_get_client_pool, mock_db_session
    ):
        """Test health check when no upstream servers are active."""
        from preloop.api.endpoints.health import health_check

        # Mock database (healthy)
        mock_db_session.execute.return_value = None

        # Mock MCP lifespan manager (healthy)
        mock_lifespan = MagicMock()
        mock_get_mcp_lifespan.return_value = mock_lifespan

        # Mock MCP client pool with no active servers
        mock_pool = MagicMock()
        mock_pool.get_active_servers.return_value = []
        mock_get_client_pool.return_value = mock_pool

        result = health_check(db=mock_db_session)

        assert result["status"] == "healthy"
        assert result["database"] == "connected"
        assert result["mcp_server"] == "available"
        assert result["upstream_connections"] == 0
        assert "upstream_servers" not in result


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    return MagicMock()
