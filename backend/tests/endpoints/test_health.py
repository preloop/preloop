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

        result = health_check()

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

        result = health_check()

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

        result = health_check()

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

        result = health_check()

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

        result = health_check()

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

        result = health_check()

        assert result["status"] == "healthy"
        assert result["database"] == "connected"
        assert result["mcp_server"] == "available"
        assert result["upstream_connections"] == 0
        assert "upstream_servers" not in result


@pytest.fixture
def mock_db_session():
    """Patch the dedicated health engine and expose its connection mock.

    The health endpoint no longer takes a pooled request session; it uses a
    small dedicated engine so probes stay answerable while the request pool is
    saturated. Tests configure ``.execute`` exactly as before.
    """
    conn = MagicMock()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    with patch("preloop.api.endpoints.health.get_health_engine", return_value=engine):
        yield conn


class TestPingLiveness:
    """The liveness probe must survive database saturation."""

    def test_ping_is_async(self):
        """Regression guard for the 2026-08-08 gateway SIGKILL.

        A sync ``def`` endpoint runs in Starlette's bounded anyio threadpool.
        When sync DB endpoints block that pool waiting on connection checkout,
        a sync /ping queues behind them, the liveness probe times out and
        Kubernetes kills the pod. Staying async keeps liveness on the event
        loop and independent of the DB.
        """
        import inspect

        from preloop.api.endpoints.health import ping

        assert inspect.iscoroutinefunction(ping)

    def test_ping_does_not_touch_database(self):
        """/ping must never acquire a database connection."""
        import asyncio

        from preloop.api.endpoints.health import ping

        with patch("preloop.api.endpoints.health.get_health_engine") as engine:
            result = asyncio.run(ping())

        assert not engine.called
        assert result["status"] == "pong"


class TestReadinessPoolReporting:
    """Readiness must show pool saturation without failing on it."""

    @patch("preloop.services.mcp_client_pool.get_mcp_client_pool")
    @patch("preloop.services.mcp_http.get_mcp_lifespan_manager")
    def test_saturated_pool_is_reported_but_still_ready(
        self, mock_get_mcp_lifespan, mock_get_client_pool, mock_db_session
    ):
        """A full pool is visible in the payload and does not fail readiness.

        Failing readiness on saturation would remove pods from the load
        balancer and concentrate the same traffic on fewer pods, which is the
        amplification loop the 2026-09-03 incident already demonstrated.
        """
        from preloop.api.endpoints.health import health_check

        mock_db_session.execute.return_value = None
        mock_get_mcp_lifespan.return_value = MagicMock()
        mock_pool = MagicMock()
        mock_pool.get_active_servers.return_value = []
        mock_get_client_pool.return_value = mock_pool

        saturated = [
            {
                "engine": "sync",
                "size": 8,
                "checked_out": 20,
                "checked_in": 0,
                "overflow_in_use": 12,
                "ceiling": 20,
                "status": "Pool size: 8 Connections in pool: 0 ...",
            }
        ]
        with patch(
            "preloop.services.db_pool_monitor.collect_pool_stats",
            return_value=saturated,
        ):
            result = health_check()

        assert result["status"] == "healthy"
        assert result["db_pool"]["saturated"] is True
        assert result["db_pool"]["engines"][0]["checked_out"] == 20
        assert result["db_pool"]["engines"][0]["ceiling"] == 20

    @patch("preloop.services.mcp_client_pool.get_mcp_client_pool")
    @patch("preloop.services.mcp_http.get_mcp_lifespan_manager")
    def test_usage_queue_counters_are_reported(
        self, mock_get_mcp_lifespan, mock_get_client_pool, mock_db_session
    ):
        """Dropped usage rows are visible to operators on the readiness path."""
        from preloop.api.endpoints.health import health_check

        mock_db_session.execute.return_value = None
        mock_get_mcp_lifespan.return_value = MagicMock()
        mock_pool = MagicMock()
        mock_pool.get_active_servers.return_value = []
        mock_get_client_pool.return_value = mock_pool

        result = health_check()

        assert set(result["api_usage_queue"]) >= {
            "queued",
            "capacity",
            "written",
            "dropped",
            "failed",
        }
