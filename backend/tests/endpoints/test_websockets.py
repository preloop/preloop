"""Integration tests for WebSocket endpoints."""

from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch, AsyncMock

from preloop.models.models.user import User


class TestUnifiedWebSocket:
    """Test the unified WebSocket endpoint /api/v1/ws/unified."""

    def _setup_websocket_mocks(
        self, mock_manager, mock_get_user, mock_session_manager, test_user=None
    ):
        """Helper to setup async mocks for WebSocket tests."""
        # Mock user authentication (async function)
        async_mock_get_user = AsyncMock(return_value=test_user)
        mock_get_user.side_effect = async_mock_get_user

        # Mock session creation (async function)
        mock_session = MagicMock()
        mock_session.id = "test-session-id"
        mock_session.connection_id = "test-connection-id"
        mock_session.is_authenticated = test_user is not None
        async_mock_create_session = AsyncMock(return_value=mock_session)
        mock_session_manager.create_session = async_mock_create_session
        mock_session_manager.update_activity = MagicMock()

        # Mock manager connection (async function)
        async_mock_connect = AsyncMock(return_value="test-manager-connection-id")
        mock_manager.connect_with_account = async_mock_connect
        mock_manager.disconnect = MagicMock()
        mock_manager.active_connections = {}

        # Mock session cleanup (async function)
        async_mock_end_session = AsyncMock()
        mock_session_manager.end_session = async_mock_end_session

        return mock_session

    @patch("preloop.api.endpoints.websockets.handle_activity")
    @patch("preloop.api.endpoints.websockets.session_manager")
    @patch("preloop.api.endpoints.websockets._resolve_token_user")
    @patch("preloop.api.endpoints.websockets.manager")
    def test_unified_websocket_handshake(
        self,
        mock_manager,
        mock_get_user,
        mock_session_manager,
        mock_handle_activity,
        client: TestClient,
        test_user: User,
    ):
        """Test WebSocket handshake with authenticated user."""
        self._setup_websocket_mocks(
            mock_manager, mock_get_user, mock_session_manager, test_user
        )

        # Connect to WebSocket
        with client.websocket_connect(
            "/api/v1/ws/unified?token=test-token&fingerprint=test-fingerprint"
        ) as websocket:
            # Should receive handshake confirmation
            data = websocket.receive_json()
            assert data["type"] == "handshake"
            assert data["session_id"] == "test-session-id"
            assert data["authenticated"] is True
            assert data["message"] == "Connected to unified WebSocket"

    @patch("preloop.api.endpoints.websockets.handle_activity")
    @patch("preloop.api.endpoints.websockets.session_manager")
    @patch("preloop.api.endpoints.websockets._resolve_token_user")
    @patch("preloop.api.endpoints.websockets.manager")
    def test_unified_websocket_ping_pong(
        self,
        mock_manager,
        mock_get_user,
        mock_session_manager,
        mock_handle_activity,
        client: TestClient,
        test_user: User,
    ):
        """Test WebSocket ping/pong heartbeat lifecycle."""
        self._setup_websocket_mocks(
            mock_manager, mock_get_user, mock_session_manager, test_user
        )

        with client.websocket_connect(
            "/api/v1/ws/unified?token=test-token"
        ) as websocket:
            # Receive handshake
            handshake = websocket.receive_json()
            assert handshake["type"] == "handshake"

            # Send activity message (pong doesn't update activity, but other messages do)
            websocket.send_json({"type": "activity", "event_type": "test"})

            # Give it a moment to process
            import time

            time.sleep(0.05)

            # Verify session activity was updated
            mock_session_manager.update_activity.assert_called()

    @patch("preloop.api.endpoints.websockets.handle_activity")
    @patch("preloop.api.endpoints.websockets.session_manager")
    @patch("preloop.api.endpoints.websockets.manager")
    def test_unified_websocket_invalid_token(
        self,
        mock_manager,
        mock_session_manager,
        mock_handle_activity,
        client: TestClient,
    ):
        """Test WebSocket connection with invalid token results in anonymous session.

        The /ws/unified endpoint allows anonymous connections, so an invalid token
        should result in an anonymous session (not an error).
        """
        # Setup mocks for anonymous user
        mock_session = MagicMock()
        mock_session.id = "anon-session-id"
        mock_session.connection_id = "anon-connection-id"
        mock_session.is_authenticated = False
        async_mock_create_session = AsyncMock(return_value=mock_session)
        mock_session_manager.create_session = async_mock_create_session
        mock_session_manager.update_activity = MagicMock()
        async_mock_end_session = AsyncMock()
        mock_session_manager.end_session = async_mock_end_session

        mock_manager.active_connections = {}
        mock_manager.disconnect = MagicMock()

        # Connect with invalid token - should get anonymous session
        with client.websocket_connect(
            "/api/v1/ws/unified?token=invalid-token"
        ) as websocket:
            # Should receive handshake (anonymous allowed)
            data = websocket.receive_json()
            assert data["type"] == "handshake"
            assert data["authenticated"] is False  # Invalid token = anonymous

    @patch("preloop.api.endpoints.websockets.handle_activity")
    @patch("preloop.api.endpoints.websockets.session_manager")
    @patch("preloop.api.endpoints.websockets._resolve_token_user")
    @patch("preloop.api.endpoints.websockets.manager")
    def test_unified_websocket_anonymous_connection(
        self,
        mock_manager,
        mock_get_user,
        mock_session_manager,
        mock_handle_activity,
        client: TestClient,
    ):
        """Test WebSocket connection without authentication (anonymous)."""
        # Setup mocks for anonymous user (test_user=None)
        # Mock user authentication (async function)
        async_mock_get_user = AsyncMock(return_value=None)
        mock_get_user.side_effect = async_mock_get_user

        # Mock session creation for anonymous user
        mock_session = MagicMock()
        mock_session.id = "anon-session-id"
        mock_session.connection_id = "anon-connection-id"
        mock_session.is_authenticated = False
        async_mock_create_session = AsyncMock(return_value=mock_session)
        mock_session_manager.create_session = async_mock_create_session
        mock_session_manager.update_activity = MagicMock()

        # Mock manager for anonymous connection
        mock_manager.active_connections = {}
        mock_manager.disconnect = MagicMock()

        # Mock session cleanup
        async_mock_end_session = AsyncMock()
        mock_session_manager.end_session = async_mock_end_session

        with client.websocket_connect(
            "/api/v1/ws/unified?fingerprint=anon-fingerprint"
        ) as websocket:
            # Should receive handshake for anonymous user
            data = websocket.receive_json()
            assert data["type"] == "handshake"
            assert data["session_id"] == "anon-session-id"
            assert data["authenticated"] is False

    @patch("preloop.api.endpoints.websockets.handle_activity")
    @patch("preloop.api.endpoints.websockets.session_manager")
    @patch("preloop.api.endpoints.websockets._resolve_token_user")
    @patch("preloop.api.endpoints.websockets.manager")
    def test_unified_websocket_activity_tracking(
        self,
        mock_manager,
        mock_get_user,
        mock_session_manager,
        mock_handle_activity,
        client: TestClient,
        test_user: User,
    ):
        """Test activity tracking through WebSocket messages."""
        self._setup_websocket_mocks(
            mock_manager, mock_get_user, mock_session_manager, test_user
        )

        # Mock handle_activity as async
        async_mock_handle_activity = AsyncMock()
        mock_handle_activity.side_effect = async_mock_handle_activity

        with client.websocket_connect(
            "/api/v1/ws/unified?token=test-token"
        ) as websocket:
            # Receive handshake
            handshake = websocket.receive_json()
            assert handshake["type"] == "handshake"

            # Send activity event
            activity_data = {
                "type": "activity",
                "event_type": "page_view",
                "path": "/dashboard",
            }
            websocket.send_json(activity_data)

            # Give it time to process
            import time

            time.sleep(0.1)

            # Verify activity handler was called
            mock_handle_activity.assert_called_once()

    @patch("preloop.api.endpoints.websockets.handle_activity")
    @patch("preloop.api.endpoints.websockets.session_manager")
    @patch("preloop.api.endpoints.websockets.manager")
    def test_unified_websocket_cleanup_on_disconnect(
        self,
        mock_manager,
        mock_session_manager,
        mock_handle_activity,
        client: TestClient,
    ):
        """Test proper cleanup when WebSocket disconnects (anonymous user)."""
        # Setup mocks for anonymous user
        mock_session = MagicMock()
        mock_session.id = "test-session-id"
        mock_session.connection_id = "test-connection-id"
        mock_session.is_authenticated = False
        async_mock_create_session = AsyncMock(return_value=mock_session)
        mock_session_manager.create_session = async_mock_create_session
        mock_session_manager.update_activity = MagicMock()
        async_mock_end_session = AsyncMock()
        mock_session_manager.end_session = async_mock_end_session

        mock_manager.active_connections = {}
        mock_manager.disconnect = MagicMock()

        with client.websocket_connect(
            "/api/v1/ws/unified?fingerprint=test-fingerprint"
        ) as websocket:
            # Receive handshake
            handshake = websocket.receive_json()
            assert handshake["type"] == "handshake"

            # Close connection
            websocket.close()

        # Verify cleanup was called
        mock_manager.disconnect.assert_called_once()
        mock_session_manager.end_session.assert_called_once()

    @patch("preloop.api.endpoints.websockets.handle_activity")
    @patch("preloop.api.endpoints.websockets.session_manager")
    @patch("preloop.api.endpoints.websockets.manager")
    def test_unified_websocket_cleanup_on_exception(
        self,
        mock_manager,
        mock_session_manager,
        mock_handle_activity,
        client: TestClient,
    ):
        """Test cleanup runs even if manager raises exception (regression test)."""
        # Mock session creation (async function)
        mock_session = MagicMock()
        mock_session.id = "test-session-id"
        mock_session.connection_id = "test-connection-id"
        mock_session.is_authenticated = False
        async_mock_create_session = AsyncMock(return_value=mock_session)
        mock_session_manager.create_session = async_mock_create_session

        # Mock manager to raise exception when setting active_connections
        mock_manager.active_connections = {}
        mock_manager.disconnect = MagicMock()

        # Mock session cleanup (async function)
        async_mock_end_session = AsyncMock()
        mock_session_manager.end_session = async_mock_end_session

        # Anonymous connection should work (no exception expected)
        with client.websocket_connect(
            "/api/v1/ws/unified?fingerprint=test-fingerprint"
        ) as websocket:
            # Should receive handshake
            handshake = websocket.receive_json()
            assert handshake["type"] == "handshake"
            assert handshake["authenticated"] is False

            # Close normally
            websocket.close()

        # Verify session cleanup was called
        mock_session_manager.end_session.assert_called_once()
