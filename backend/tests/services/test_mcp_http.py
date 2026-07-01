"""Tests for MCP HTTP streaming integration."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException, Request
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.authentication import AuthCredentials
from starlette.requests import HTTPConnection

from preloop.services.mcp_http import (
    get_mcp_server,
    get_user_context_for_mcp,
    mcp_http_streaming_endpoint,
    setup_mcp_routes,
    get_mcp_lifespan_manager,
    PreloopBearerAuthBackend,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_account():
    """Create a mock account."""
    account = MagicMock()
    account.id = str(uuid4())
    account.username = "testuser"
    return account


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    db = MagicMock()
    return db


@pytest.fixture
def mock_request():
    """Create a mock FastAPI request."""
    request = MagicMock(spec=Request)
    request.scope = {}
    request.json = AsyncMock(return_value={"method": "initialize", "id": 1})
    return request


class TestPreloopBearerAuthBackend:
    """Test PreloopBearerAuthBackend authentication."""

    async def test_authenticate_no_auth_header(self):
        """Test authenticate with no Authorization header."""
        backend = PreloopBearerAuthBackend()
        conn = MagicMock(spec=HTTPConnection)
        conn.headers = {}

        result = await backend.authenticate(conn)

        assert result is None

    async def test_authenticate_non_bearer_auth(self):
        """Test authenticate with non-Bearer authentication."""
        backend = PreloopBearerAuthBackend()
        conn = MagicMock(spec=HTTPConnection)
        conn.headers = {"authorization": "Basic dXNlcjpwYXNzd29yZA=="}

        result = await backend.authenticate(conn)

        assert result is None

    async def test_authenticate_invalid_token(self, mock_db, mock_account):
        """Test authenticate with invalid token."""
        backend = PreloopBearerAuthBackend()
        conn = MagicMock(spec=HTTPConnection)
        conn.headers = {"authorization": "Bearer invalid-token"}

        with patch("preloop.services.mcp_http.get_db") as mock_get_db:
            mock_get_db.return_value = iter([mock_db])
            with patch(
                "preloop.services.mcp_http.get_user_from_token_if_valid",
                return_value=None,
            ):
                result = await backend.authenticate(conn)

        assert result is None
        mock_db.close.assert_called_once()

    async def test_authenticate_valid_token(self, mock_db, mock_account):
        """Test authenticate with valid token."""
        backend = PreloopBearerAuthBackend()
        conn = MagicMock(spec=HTTPConnection)
        conn.headers = {"authorization": "Bearer valid-token"}

        with patch("preloop.services.mcp_http.get_db") as mock_get_db:
            mock_get_db.return_value = iter([mock_db])
            with patch(
                "preloop.services.mcp_http.get_user_from_token_if_valid",
                return_value=mock_account,
            ):
                result = await backend.authenticate(conn)

        assert result is not None
        credentials, auth_user = result
        assert isinstance(credentials, AuthCredentials)
        assert isinstance(auth_user, AuthenticatedUser)
        assert auth_user.access_token.token == "valid-token"
        assert auth_user.access_token.client_id == str(mock_account.id)
        mock_db.close.assert_called_once()

    async def test_authenticate_case_insensitive_header(self, mock_db, mock_account):
        """Test that Authorization header is case-insensitive."""
        backend = PreloopBearerAuthBackend()
        conn = MagicMock(spec=HTTPConnection)
        conn.headers = {"Authorization": "Bearer valid-token"}

        with patch("preloop.services.mcp_http.get_db") as mock_get_db:
            mock_get_db.return_value = iter([mock_db])
            with patch(
                "preloop.services.mcp_http.get_user_from_token_if_valid",
                return_value=mock_account,
            ):
                result = await backend.authenticate(conn)

        assert result is not None
        mock_db.close.assert_called_once()


class TestGetMCPServer:
    """Test get_mcp_server singleton."""

    def test_get_mcp_server_creates_instance(self):
        """Test that get_mcp_server creates instance."""
        # Reset global
        import preloop.services.mcp_http as mcp_http_module

        mcp_http_module._mcp_server_instance = None

        with patch(
            "preloop.services.mcp_http.initialize_dynamic_mcp_server"
        ) as mock_init:
            mock_server = MagicMock()
            mock_init.return_value = mock_server

            server = get_mcp_server()

            assert server == mock_server
            mock_init.assert_called_once()

    def test_get_mcp_server_returns_existing_instance(self):
        """Test that get_mcp_server returns existing instance."""
        import preloop.services.mcp_http as mcp_http_module

        # Set existing instance
        existing_server = MagicMock()
        mcp_http_module._mcp_server_instance = existing_server

        with patch(
            "preloop.services.mcp_http.initialize_dynamic_mcp_server"
        ) as mock_init:
            server = get_mcp_server()

            assert server == existing_server
            # Should not create new instance
            mock_init.assert_not_called()


class TestGetUserContextForMCP:
    """Test get_user_context_for_mcp function."""

    async def test_get_user_context_no_authenticated_user(self, mock_request, mock_db):
        """Test get_user_context when no authenticated user in scope."""
        mock_request.scope = {"user": None}

        with pytest.raises(HTTPException) as exc_info:
            await get_user_context_for_mcp(mock_request, mock_db)

        assert exc_info.value.status_code == 401
        assert "Not authenticated" in exc_info.value.detail

    async def test_get_user_context_wrong_user_type(self, mock_request, mock_db):
        """Test get_user_context when user is wrong type."""
        mock_request.scope = {"user": "not-an-authenticated-user"}

        with pytest.raises(HTTPException) as exc_info:
            await get_user_context_for_mcp(mock_request, mock_db)

        assert exc_info.value.status_code == 401

    async def test_get_user_context_no_cached_account(
        self, mock_request, mock_db, mock_account
    ):
        """Test get_user_context when account not cached (fallback)."""
        access_token = AccessToken(
            token="test-token", client_id=str(mock_account.id), scopes=[]
        )
        auth_user = AuthenticatedUser(access_token)
        mock_request.scope = {"user": auth_user}

        with patch(
            "preloop.services.mcp_http.get_user_from_token_if_valid",
            return_value=mock_account,
        ):
            with patch("preloop.services.mcp_http.has_tracker", return_value=True):
                context = await get_user_context_for_mcp(mock_request, mock_db)

        assert context["user_id"] == str(mock_account.id)
        assert context["username"] == mock_account.username
        assert context["has_tracker"] is True

    async def test_get_user_context_cached_account(
        self, mock_request, mock_db, mock_account
    ):
        """Test get_user_context with cached account."""
        access_token = AccessToken(
            token="test-token", client_id=str(mock_account.id), scopes=[]
        )
        # Cache account in access token
        object.__setattr__(access_token, "account", mock_account)
        auth_user = AuthenticatedUser(access_token)
        mock_request.scope = {"user": auth_user}

        with patch("preloop.services.mcp_http.has_tracker", return_value=False):
            context = await get_user_context_for_mcp(mock_request, mock_db)

        assert context["user_id"] == str(mock_account.id)
        assert context["username"] == mock_account.username
        assert context["has_tracker"] is False
        assert context["enabled_default_tools"] == []
        assert context["enabled_proxied_tools"] == []

    async def test_get_user_context_fallback_fails(
        self, mock_request, mock_db, mock_account
    ):
        """Test get_user_context when fallback auth fails."""
        access_token = AccessToken(
            token="test-token", client_id=str(mock_account.id), scopes=[]
        )
        auth_user = AuthenticatedUser(access_token)
        mock_request.scope = {"user": auth_user}

        with patch(
            "preloop.services.mcp_http.get_user_from_token_if_valid",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_user_context_for_mcp(mock_request, mock_db)

        assert exc_info.value.status_code == 401
        assert "Invalid authentication token" in exc_info.value.detail


class TestMCPHTTPStreamingEndpoint:
    """Test mcp_http_streaming_endpoint."""

    async def test_endpoint_invalid_json(self, mock_request):
        """Test endpoint with invalid JSON body."""
        mock_request.json = AsyncMock(side_effect=ValueError("Invalid JSON"))

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        with pytest.raises(HTTPException) as exc_info:
            await mcp_http_streaming_endpoint(mock_request, user_context)

        assert exc_info.value.status_code == 400
        assert "Invalid JSON" in exc_info.value.detail

    async def test_endpoint_initialize_method(self, mock_request):
        """Test endpoint with initialize method."""
        mock_request.json = AsyncMock(
            return_value={"method": "initialize", "id": 1, "params": {}}
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        with patch("preloop.services.mcp_http.get_mcp_server"):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert response_data["jsonrpc"] == "2.0"
        assert response_data["result"]["protocolVersion"] == "2024-11-05"
        assert response_data["result"]["serverInfo"]["name"] == "preloop-mcp"

    async def test_endpoint_notifications_initialized(self, mock_request):
        """Test endpoint with notifications/initialized method."""
        mock_request.json = AsyncMock(
            return_value={"method": "notifications/initialized"}
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        with patch("preloop.services.mcp_http.get_mcp_server"):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 204

    async def test_endpoint_tools_list(self, mock_request):
        """Test endpoint with tools/list method."""
        mock_request.json = AsyncMock(return_value={"method": "tools/list", "id": 1})

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_server = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        mock_tool.description = "Test Tool"
        mock_tool.inputSchema = {"properties": {}}
        mock_server._get_tools_for_user = MagicMock(return_value=[mock_tool])

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert response_data["jsonrpc"] == "2.0"
        assert len(response_data["result"]["tools"]) == 1
        assert response_data["result"]["tools"][0]["name"] == "test_tool"

    async def test_endpoint_tools_list_error(self, mock_request):
        """Test endpoint with tools/list error."""
        mock_request.json = AsyncMock(return_value={"method": "tools/list", "id": 1})

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_server = MagicMock()
        mock_server._get_tools_for_user = MagicMock(
            side_effect=Exception("Database error")
        )

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            with pytest.raises(HTTPException) as exc_info:
                await mcp_http_streaming_endpoint(mock_request, user_context)

        assert exc_info.value.status_code == 500
        assert "Database error" in exc_info.value.detail

    async def test_endpoint_tools_call_missing_name(self, mock_request):
        """Test endpoint with tools/call missing tool name."""
        mock_request.json = AsyncMock(
            return_value={"method": "tools/call", "id": 1, "params": {}}
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        with patch("preloop.services.mcp_http.get_mcp_server"):
            with pytest.raises(HTTPException) as exc_info:
                await mcp_http_streaming_endpoint(mock_request, user_context)

        assert exc_info.value.status_code == 400
        assert "Missing tool name" in exc_info.value.detail

    async def test_endpoint_tools_call_unauthorized(self, mock_request):
        """Test endpoint with tools/call for unauthorized tool."""
        mock_request.json = AsyncMock(
            return_value={
                "method": "tools/call",
                "id": 1,
                "params": {"name": "unauthorized_tool", "arguments": {}},
            }
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_server = MagicMock()
        mock_server._get_tools_for_user = MagicMock(return_value=[])

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert "error" in response_data
        assert "Access denied" in response_data["error"]["message"]

    async def test_endpoint_tools_call_success(self, mock_request):
        """Test successful tools/call."""
        mock_request.json = AsyncMock(
            return_value={
                "method": "tools/call",
                "id": 1,
                "params": {"name": "test_tool", "arguments": {"arg": "value"}},
            }
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        mock_result = MagicMock()
        mock_result.model_dump_json = MagicMock(return_value='{"result": "success"}')

        mock_handler = AsyncMock(return_value=mock_result)

        mock_server = MagicMock()
        mock_server._get_tools_for_user = MagicMock(return_value=[mock_tool])
        mock_server._check_approval_required = AsyncMock(return_value=False)
        mock_server._tool_handlers = {"test_tool": mock_handler}

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert response_data["jsonrpc"] == "2.0"
        assert "result" in response_data
        assert response_data["result"]["content"][0]["type"] == "text"

    async def test_endpoint_tools_call_no_handler(self, mock_request):
        """Test tools/call when handler not found."""
        mock_request.json = AsyncMock(
            return_value={
                "method": "tools/call",
                "id": 1,
                "params": {"name": "test_tool", "arguments": {}},
            }
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        mock_server = MagicMock()
        mock_server._get_tools_for_user = MagicMock(return_value=[mock_tool])
        mock_server._check_approval_required = AsyncMock(return_value=False)
        mock_server._tool_handlers = {}  # No handler

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert "error" in response_data
        assert "Handler not found" in response_data["error"]["message"]

    async def test_endpoint_tools_call_with_approval_timeout(self, mock_request):
        """Test tools/call with approval timeout."""
        mock_request.json = AsyncMock(
            return_value={
                "method": "tools/call",
                "id": 1,
                "params": {"name": "test_tool", "arguments": {}},
            }
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        mock_server = MagicMock()
        mock_server._get_tools_for_user = MagicMock(return_value=[mock_tool])
        mock_server._check_approval_required = AsyncMock(return_value=True)
        mock_server._request_and_wait_for_approval = AsyncMock(
            side_effect=TimeoutError("Approval timeout")
        )

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert "error" in response_data
        assert "Approval timeout" in response_data["error"]["message"]

    async def test_endpoint_tools_call_with_approval_declined(self, mock_request):
        """Test tools/call with approval declined."""
        mock_request.json = AsyncMock(
            return_value={
                "method": "tools/call",
                "id": 1,
                "params": {"name": "test_tool", "arguments": {}},
            }
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        mock_server = MagicMock()
        mock_server._get_tools_for_user = MagicMock(return_value=[mock_tool])
        mock_server._check_approval_required = AsyncMock(return_value=True)
        mock_server._request_and_wait_for_approval = AsyncMock(
            side_effect=PermissionError("Approval declined")
        )

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert "error" in response_data
        assert "Approval declined" in response_data["error"]["message"]

    async def test_endpoint_tools_call_approval_error(self, mock_request):
        """Test tools/call with approval flow error."""
        mock_request.json = AsyncMock(
            return_value={
                "method": "tools/call",
                "id": 1,
                "params": {"name": "test_tool", "arguments": {}},
            }
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        mock_server = MagicMock()
        mock_server._get_tools_for_user = MagicMock(return_value=[mock_tool])
        mock_server._check_approval_required = AsyncMock(return_value=True)
        mock_server._request_and_wait_for_approval = AsyncMock(
            side_effect=Exception("Unknown error")
        )

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert "error" in response_data
        assert "Approval error" in response_data["error"]["message"]

    async def test_endpoint_tools_call_execution_error(self, mock_request):
        """Test tools/call with execution error."""
        mock_request.json = AsyncMock(
            return_value={
                "method": "tools/call",
                "id": 1,
                "params": {"name": "test_tool", "arguments": {}},
            }
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        mock_handler = AsyncMock(side_effect=Exception("Execution failed"))

        mock_server = MagicMock()
        mock_server._get_tools_for_user = MagicMock(return_value=[mock_tool])
        mock_server._check_approval_required = AsyncMock(return_value=False)
        mock_server._tool_handlers = {"test_tool": mock_handler}

        with patch(
            "preloop.services.mcp_http.get_mcp_server", return_value=mock_server
        ):
            response = await mcp_http_streaming_endpoint(mock_request, user_context)

        assert response.status_code == 200
        response_data = json.loads(response.body)
        assert "error" in response_data
        assert "Error executing tool" in response_data["error"]["message"]

    async def test_endpoint_unsupported_method(self, mock_request):
        """Test endpoint with unsupported method."""
        mock_request.json = AsyncMock(
            return_value={"method": "unsupported_method", "id": 1}
        )

        user_context = {
            "user_id": "1",
            "account_id": "1",
            "username": "test",
            "has_tracker": True,
            "enabled_default_tools": [],
            "enabled_proxied_tools": [],
        }

        with patch("preloop.services.mcp_http.get_mcp_server"):
            with pytest.raises(HTTPException) as exc_info:
                await mcp_http_streaming_endpoint(mock_request, user_context)

        assert exc_info.value.status_code == 400
        assert "Unsupported method" in exc_info.value.detail


class TestSetupMCPRoutes:
    """Test setup_mcp_routes function."""

    def test_setup_mcp_routes(self):
        """Test setup_mcp_routes mounts MCP app."""
        from fastapi import FastAPI

        app = FastAPI()
        mock_mcp = MagicMock()
        mock_mcp_app = MagicMock()

        with patch(
            "preloop.services.initialize_mcp.initialize_mcp_with_tools",
            return_value=mock_mcp,
        ):
            with patch(
                "preloop.services.dynamic_fastmcp_http.setup_dynamic_mcp_http",
                return_value=mock_mcp_app,
            ):
                setup_mcp_routes(app)

        # Verify app was mounted (check routes were added)
        from tests.route_paths import collect_route_paths

        assert any(path.startswith("/mcp") for path in collect_route_paths(app.routes))

    def test_setup_mcp_routes_stores_lifespan(self):
        """Test setup_mcp_routes stores lifespan manager."""
        from fastapi import FastAPI

        app = FastAPI()
        mock_mcp = MagicMock()
        mock_mcp_app = MagicMock()

        # Create mock app with lifespan
        mock_base_app = MagicMock()
        mock_lifespan_manager = MagicMock()
        mock_base_app.lifespan = MagicMock(return_value=mock_lifespan_manager)
        mock_mcp_app.app = mock_base_app

        with patch(
            "preloop.services.initialize_mcp.initialize_mcp_with_tools",
            return_value=mock_mcp,
        ):
            with patch(
                "preloop.services.dynamic_fastmcp_http.setup_dynamic_mcp_http",
                return_value=mock_mcp_app,
            ):
                setup_mcp_routes(app)

        # Verify lifespan manager was stored
        manager = get_mcp_lifespan_manager()
        assert manager is not None


class TestGetMCPLifespanManager:
    """Test get_mcp_lifespan_manager function."""

    def test_get_mcp_lifespan_manager_returns_manager(self):
        """Test get_mcp_lifespan_manager returns stored manager."""
        import preloop.services.mcp_http as mcp_http_module

        mock_manager = MagicMock()
        mcp_http_module._mcp_lifespan_manager = mock_manager

        manager = get_mcp_lifespan_manager()

        assert manager == mock_manager

    def test_get_mcp_lifespan_manager_returns_none(self):
        """Test get_mcp_lifespan_manager returns None when not set."""
        import preloop.services.mcp_http as mcp_http_module

        mcp_http_module._mcp_lifespan_manager = None

        manager = get_mcp_lifespan_manager()

        assert manager is None
