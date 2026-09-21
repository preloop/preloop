"""Tests for WebSocket authentication middleware.

Regression tests for WebSocket path matching under /api/v1 prefix.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from preloop.api.middleware.websocket_auth import (
    WebSocketAuthMiddleware,
    AUTHENTICATED_WS_PATHS,
    AUTHENTICATED_WS_PREFIXES,
    ANONYMOUS_WS_PATHS,
    _is_authenticated_ws_path,
    _is_anonymous_ws_path,
)


class TestWebSocketPathMatching:
    """Test that WebSocket paths are correctly matched under /api/v1 prefix."""

    def test_authenticated_paths(self):
        """Verify /api/v1/ws is in AUTHENTICATED_WS_PATHS."""
        assert "/api/v1/ws" in AUTHENTICATED_WS_PATHS

    def test_authenticated_prefixes_include_flow_executions(self):
        """Verify flow-executions paths use prefix matching."""
        assert "/api/v1/ws/flow-executions" in AUTHENTICATED_WS_PREFIXES

    def test_anonymous_paths(self):
        """Verify /api/v1/ws/unified and /api/v1/ws/execution are in ANONYMOUS_WS_PATHS."""
        assert "/api/v1/ws/unified" in ANONYMOUS_WS_PATHS
        assert "/api/v1/ws/execution" in ANONYMOUS_WS_PATHS

    def test_is_authenticated_ws_path_exact_match(self):
        """Test exact path matching for authenticated paths."""
        assert _is_authenticated_ws_path("/api/v1/ws") is True
        assert _is_authenticated_ws_path("/ws/other") is False

    def test_is_authenticated_ws_path_prefix_match(self):
        """Test prefix matching for flow-executions paths."""
        assert _is_authenticated_ws_path("/api/v1/ws/flow-executions/abc-def") is True
        assert _is_authenticated_ws_path("/api/v1/ws/flow-executions") is True

    def test_is_anonymous_ws_path(self):
        """Test anonymous path matching."""
        assert _is_anonymous_ws_path("/api/v1/ws/unified") is True
        assert _is_anonymous_ws_path("/api/v1/ws/execution") is True
        assert _is_anonymous_ws_path("/ws/other") is False


class TestWebSocketAuthMiddleware:
    """Test WebSocket authentication middleware behavior."""

    @pytest.fixture
    def middleware(self):
        """Create middleware with a mock app."""
        mock_app = AsyncMock()
        return WebSocketAuthMiddleware(mock_app)

    @pytest.mark.asyncio
    async def test_non_websocket_passes_through(self, middleware):
        """Non-WebSocket requests should pass through unchanged."""
        scope = {"type": "http", "path": "/api/v1/some-endpoint"}
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)

        middleware.app.assert_called_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_non_managed_websocket_passes_through(self, middleware):
        """WebSocket paths we don't manage should pass through."""
        scope = {"type": "websocket", "path": "/some/other/ws"}
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)

        middleware.app.assert_called_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_authenticated_path_without_token_rejected(self, middleware):
        """Authenticated WebSocket path without token should be rejected."""
        scope = {
            "type": "websocket",
            "path": "/api/v1/ws",
            "headers": [],
            "query_string": b"",
        }
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)

        # Should send close frame, not call app
        send.assert_called()
        call_args = send.call_args[0][0]
        assert call_args["type"] == "websocket.close"
        assert call_args["code"] == 1008  # Policy violation

    @pytest.mark.asyncio
    async def test_anonymous_path_without_token_allowed(self, middleware):
        """Anonymous WebSocket path without token should be allowed."""
        scope = {
            "type": "websocket",
            "path": "/api/v1/ws/unified",
            "headers": [],
            "query_string": b"",
            "state": {},
        }
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)

        # Should pass through to app
        middleware.app.assert_called_once()
        # User should be None for anonymous
        assert scope["state"]["user"] is None
        assert scope["state"]["is_authenticated"] is False

    @pytest.mark.asyncio
    @patch(
        "preloop.api.middleware.websocket_auth.WebSocketAuthMiddleware._validate_token"
    )
    async def test_authenticated_path_with_valid_token(self, mock_validate, middleware):
        """Authenticated WebSocket path with valid token should be allowed."""
        mock_user = MagicMock()
        mock_user.id = "user-123"
        mock_user.account_id = "account-456"
        mock_user.username = "testuser"
        mock_validate.return_value = mock_user

        scope = {
            "type": "websocket",
            "path": "/api/v1/ws",
            "headers": [(b"authorization", b"Bearer valid-token")],
            "query_string": b"",
            "state": {},
        }
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)

        # Should pass through to app with user in state
        middleware.app.assert_called_once()
        assert scope["state"]["user"] == mock_user
        assert scope["state"]["is_authenticated"] is True

    @pytest.mark.asyncio
    async def test_authenticated_path_rejects_stale_generation(self, middleware):
        """A bumped auth_generation must reject the /ws upgrade."""
        from uuid import uuid4

        from preloop.api.auth.jwt import create_access_token

        user_id = uuid4()
        token = create_access_token(
            {"sub": str(user_id)},
            auth_generation=0,
        )
        mock_user = MagicMock()
        mock_user.id = user_id
        mock_user.account_id = "account-456"
        mock_user.username = "testuser"
        mock_user.is_active = True
        mock_user.auth_generation = 1

        async def _run_db(fn):
            return fn(MagicMock())

        with (
            patch("preloop.models.crud.crud_user.get", return_value=mock_user),
            patch(
                "preloop.services.db_executor.run_db_async",
                side_effect=_run_db,
            ),
            patch(
                "preloop.services.db_executor.detach_user",
                side_effect=lambda _db, user: user,
            ),
        ):
            scope = {
                "type": "websocket",
                "path": "/api/v1/ws",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
                "query_string": b"",
                "state": {},
            }
            receive = AsyncMock()
            send = AsyncMock()
            await middleware(scope, receive, send)

        send.assert_called()
        close = send.call_args[0][0]
        assert close["type"] == "websocket.close"
        assert close["code"] == 1008
        middleware.app.assert_not_called()
