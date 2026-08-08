"""Tests for websocket manager."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from preloop.services.websocket_manager import (
    LOG_PERSIST_MAX_ATTEMPTS,
    LOG_PERSIST_MAX_CONCURRENCY,
    WebSocketManager,
    _log_persist_semaphore,
    nats_consumer,
    persist_execution_log,
    _sync_batch_insert_logs,
)

pytestmark = pytest.mark.asyncio


class TestPersistExecutionLog:
    """Test persist_execution_log function."""

    @patch("preloop.services.websocket_manager.get_log_queue")
    async def test_persist_execution_log_success(self, mock_get_queue):
        """Test persisting execution log successfully just enqueues it."""
        execution_id = "exec_123"
        log_data = {"message": "Step completed", "level": "INFO"}

        mock_queue = MagicMock()
        mock_get_queue.return_value = mock_queue

        await persist_execution_log(execution_id, log_data)

        # Verify queue was called
        mock_queue.put_nowait.assert_called_once_with((execution_id, log_data))

    @patch("preloop.services.websocket_manager.get_db")
    @patch("preloop.models.crud.crud_flow_execution.append_log")
    def test_sync_batch_insert_logs_success(self, mock_append, mock_get_db):
        """Test the synchronous batch insertion function."""
        execution_id = "exec_123"
        log_data = {"message": "Step completed", "level": "INFO"}
        batch = [(execution_id, log_data)]

        # Mock database session
        mock_db = MagicMock()
        mock_get_db.return_value = iter([mock_db])

        _sync_batch_insert_logs(batch)

        # Verify CRUD append_log was called with commit=False
        mock_append.assert_called_once_with(
            mock_db, execution_id=execution_id, log_data=log_data, commit=False
        )
        assert mock_db.commit.called
        assert mock_db.close.called

    @patch("preloop.services.websocket_manager.get_db")
    @patch("preloop.models.crud.crud_flow_execution.append_log")
    def test_sync_batch_insert_logs_with_complex_data(self, mock_append, mock_get_db):
        """Test batch persisting execution log with complex data."""
        execution_id = "exec_456"
        log_data = {
            "message": "Complex step",
            "level": "DEBUG",
            "metadata": {"key1": "value1", "key2": [1, 2, 3]},
        }
        batch = [(execution_id, log_data)]

        mock_db = MagicMock()
        mock_get_db.return_value = iter([mock_db])

        _sync_batch_insert_logs(batch)

        # Verify CRUD was called with the complex data dict
        mock_append.assert_called_once_with(
            mock_db, execution_id=execution_id, log_data=log_data, commit=False
        )
        assert mock_db.commit.called
        assert mock_db.close.called

    @patch("preloop.services.websocket_manager.notify_admins")
    @patch("preloop.services.websocket_manager.get_db")
    @patch("preloop.services.websocket_manager.logger")
    @patch("preloop.models.crud.crud_flow_execution.append_log")
    def test_sync_batch_insert_logs_database_error(
        self, mock_append, mock_logger, mock_get_db, mock_notify
    ):
        """Test handling database error when persisting log batch.

        ``notify_admins`` is patched because the drop path really sends admin
        email/Slack/Mattermost alerts; tests must not depend on TESTING=true
        being exported to stay quiet.
        """
        execution_id = "exec_789"
        log_data = {"message": "Test"}
        batch = [(execution_id, log_data)]

        # Mock CRUD to raise exception
        mock_db = MagicMock()
        mock_get_db.return_value = iter([mock_db])
        mock_append.side_effect = Exception("Database error")

        _sync_batch_insert_logs(batch)

        # Verify error was logged
        assert mock_logger.error.called
        # Database should still close, but commit shouldn't happen after error
        assert mock_db.close.called
        # Operators are alerted about the dropped batch (and no live alert
        # escaped the test suite).
        assert mock_notify.called

    @patch("preloop.services.websocket_manager.get_db")
    def test_sync_batch_insert_logs_closes_db_on_success(self, mock_get_db):
        """Test that database is closed even on success."""
        mock_db = MagicMock()
        mock_get_db.return_value = iter([mock_db])
        batch = [("exec_id", {"message": "test"})]

        with patch("preloop.models.crud.crud_flow_execution.append_log"):
            _sync_batch_insert_logs(batch)

        assert mock_db.close.called


class TestSyncBatchInsertLogsRetry:
    """Retry/backoff behaviour for transient DB failures (2026-08-08 incident).

    A pool checkout timeout used to drop the whole batch on the first error,
    silently losing execution logs. These tests pin the retry contract.
    """

    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Keep tests fast while still asserting the backoff schedule."""
        with patch("preloop.services.websocket_manager.time.sleep") as sleep:
            yield sleep

    @patch("preloop.services.websocket_manager.get_db")
    @patch("preloop.models.crud.crud_flow_execution.append_log")
    def test_retries_pool_timeout_then_succeeds(self, mock_append, mock_get_db):
        """A transient QueuePool timeout is retried and the batch is saved."""
        mock_db = MagicMock()
        mock_get_db.side_effect = lambda: iter([mock_db])
        # Fail once with the exact error seen in production, then succeed.
        mock_append.side_effect = [
            SQLAlchemyTimeoutError("QueuePool limit of size 3 overflow 7 reached"),
            None,
        ]

        assert _sync_batch_insert_logs([("exec_1", {"message": "hi"})]) is True
        assert mock_append.call_count == 2
        assert mock_db.commit.called

    @patch("preloop.services.websocket_manager.notify_admins")
    @patch("preloop.services.websocket_manager.get_db")
    @patch("preloop.models.crud.crud_flow_execution.append_log")
    def test_drops_only_after_max_attempts(
        self, mock_append, mock_get_db, mock_notify, _no_sleep
    ):
        """Persistent failure drops the batch, but only after all attempts."""
        mock_db = MagicMock()
        mock_get_db.side_effect = lambda: iter([mock_db])
        mock_append.side_effect = SQLAlchemyTimeoutError("QueuePool limit reached")

        assert _sync_batch_insert_logs([("exec_1", {"message": "hi"})]) is False
        assert mock_append.call_count == LOG_PERSIST_MAX_ATTEMPTS
        # Operator is still told about real data loss.
        assert mock_notify.called
        # Exponential backoff between attempts: 0.5s then 1.0s.
        assert [c.args[0] for c in _no_sleep.call_args_list] == [0.5, 1.0]

    @patch("preloop.services.websocket_manager.get_db")
    @patch("preloop.models.crud.crud_flow_execution.append_log")
    def test_retries_operational_error(self, mock_append, mock_get_db):
        """Dropped connections (OperationalError) are also transient."""
        mock_db = MagicMock()
        mock_get_db.side_effect = lambda: iter([mock_db])
        mock_append.side_effect = [
            OperationalError("SELECT 1", {}, Exception("server closed connection")),
            None,
        ]

        assert _sync_batch_insert_logs([("exec_1", {"message": "hi"})]) is True
        assert mock_append.call_count == 2

    @patch("preloop.services.websocket_manager.notify_admins")
    @patch("preloop.services.websocket_manager.get_db")
    @patch("preloop.models.crud.crud_flow_execution.append_log")
    def test_non_retryable_error_fails_fast(
        self, mock_append, mock_get_db, mock_notify, _no_sleep
    ):
        """Programming errors are not retried - no point hammering the DB."""
        mock_db = MagicMock()
        mock_get_db.side_effect = lambda: iter([mock_db])
        mock_append.side_effect = ValueError("bad log payload")

        assert _sync_batch_insert_logs([("exec_1", {"message": "hi"})]) is False
        assert mock_append.call_count == 1
        assert not _no_sleep.called
        assert mock_notify.called

    @patch("preloop.services.websocket_manager.get_db")
    @patch("preloop.models.crud.crud_flow_execution.append_log")
    def test_failed_attempt_rolls_back(self, mock_append, mock_get_db):
        """A failed batch must not leave a dirty transaction on the pool."""
        mock_db = MagicMock()
        mock_get_db.side_effect = lambda: iter([mock_db])
        mock_append.side_effect = [SQLAlchemyTimeoutError("pool"), None]

        _sync_batch_insert_logs([("exec_1", {"message": "hi"})])

        assert mock_db.rollback.called
        assert mock_db.close.call_count == 2

    @patch("preloop.services.websocket_manager.get_db")
    def test_empty_batch_touches_no_connection(self, mock_get_db):
        """An empty batch must not check out a connection at all."""
        assert _sync_batch_insert_logs([]) is True
        assert not mock_get_db.called

    def test_semaphore_bounds_log_persistence_concurrency(self):
        """Background log writes are capped so they cannot drain the pool."""
        assert LOG_PERSIST_MAX_CONCURRENCY >= 1
        # Acquire every permit; the next attempt must not succeed immediately.
        acquired = [
            _log_persist_semaphore.acquire(blocking=False)
            for _ in range(LOG_PERSIST_MAX_CONCURRENCY)
        ]
        try:
            assert all(acquired)
            assert _log_persist_semaphore.acquire(blocking=False) is False
        finally:
            for ok in acquired:
                if ok:
                    _log_persist_semaphore.release()


class TestBroadcastLoggingVolume:
    """Zero-listener broadcasts were ~69% of production gateway log lines."""

    @pytest.fixture
    def manager(self):
        return WebSocketManager()

    async def test_no_info_log_when_no_listeners(self, manager):
        """Status broadcasts with zero matching connections log at DEBUG."""
        with patch("preloop.services.websocket_manager.logger") as mock_logger:
            await manager.broadcast_json(
                {"type": "flow_status_update"}, account_id="acct-1"
            )

        assert not mock_logger.info.called
        assert mock_logger.debug.called

    async def test_info_log_retained_when_listeners_present(self, manager):
        """Real listeners still produce the operator-visible INFO line."""
        ws = AsyncMock()
        conn_id = "conn-1"
        manager.active_connections[conn_id] = ws
        manager.connection_accounts[conn_id] = "acct-1"

        with patch("preloop.services.websocket_manager.logger") as mock_logger:
            await manager.broadcast_json(
                {"type": "flow_status_update"}, account_id="acct-1"
            )

        assert mock_logger.info.called
        assert "matching_connections=1" in mock_logger.info.call_args.args[0]

    async def test_high_frequency_broadcast_skips_scan_when_debug_disabled(
        self, manager
    ):
        """The hot path must not scan every connection just to pick a log level.

        ``agent_log_line`` is the highest-volume message type. When DEBUG is
        off (production), the count is never rendered, so paying an
        O(connections) scan per message is pure waste.
        """

        class CountingAccounts(dict):
            """Dict that records how often the full set of values is scanned."""

            scans = 0

            def values(self):  # type: ignore[override]
                type(self).scans += 1
                return super().values()

        manager.connection_accounts = CountingAccounts({"conn-1": "acct-1"})

        with patch("preloop.services.websocket_manager.logger") as mock_logger:
            mock_logger.isEnabledFor.return_value = False
            await manager.broadcast_json(
                {"type": "agent_log_line"}, account_id="acct-1"
            )

        assert CountingAccounts.scans == 0
        assert not mock_logger.debug.called
        assert not mock_logger.info.called

    async def test_high_frequency_broadcast_still_logs_count_when_debug_on(
        self, manager
    ):
        """With DEBUG enabled the count is still reported accurately."""
        ws = AsyncMock()
        manager.active_connections["conn-1"] = ws
        manager.connection_accounts["conn-1"] = "acct-1"

        with patch("preloop.services.websocket_manager.logger") as mock_logger:
            mock_logger.isEnabledFor.return_value = True
            await manager.broadcast_json(
                {"type": "agent_log_line"}, account_id="acct-1"
            )

        assert mock_logger.debug.called
        assert any(
            "1 connection(s)" in call.args[0]
            for call in mock_logger.debug.call_args_list
        )


class TestWebSocketManager:
    """Test WebSocketManager class."""

    @pytest.fixture
    def manager(self):
        """Create a WebSocketManager instance."""
        return WebSocketManager()

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket."""
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_text = AsyncMock()
        return ws

    async def test_connect_websocket(self, manager, mock_websocket):
        """Test connecting a WebSocket."""
        connection_id = await manager.connect(mock_websocket)

        # Verify connection was accepted
        assert mock_websocket.accept.called
        # Verify connection ID was returned
        assert isinstance(connection_id, str)
        # Verify connection was stored
        assert connection_id in manager.active_connections
        assert manager.active_connections[connection_id] == mock_websocket

    async def test_connect_multiple_websockets(self, manager, mock_websocket):
        """Test connecting multiple WebSockets."""
        mock_ws1 = AsyncMock()
        mock_ws1.accept = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws2.accept = AsyncMock()

        connection_id1 = await manager.connect(mock_ws1)
        connection_id2 = await manager.connect(mock_ws2)

        # Verify both connections are stored
        assert len(manager.active_connections) == 2
        assert connection_id1 != connection_id2
        assert manager.active_connections[connection_id1] == mock_ws1
        assert manager.active_connections[connection_id2] == mock_ws2

    async def test_disconnect_websocket(self, manager, mock_websocket):
        """Test disconnecting a WebSocket."""
        connection_id = await manager.connect(mock_websocket)
        assert connection_id in manager.active_connections

        manager.disconnect(connection_id)

        # Verify connection was removed
        assert connection_id not in manager.active_connections

    async def test_disconnect_nonexistent_connection(self, manager):
        """Test disconnecting a connection that doesn't exist."""
        fake_id = str(uuid.uuid4())

        # Should not raise an error
        manager.disconnect(fake_id)

        assert fake_id not in manager.active_connections

    async def test_broadcast_to_single_client(self, manager, mock_websocket):
        """Test broadcasting message to single client."""
        await manager.connect(mock_websocket)
        message = "Test message"

        await manager.broadcast(message)

        # Verify message was sent
        mock_websocket.send_text.assert_called_once_with(message)

    async def test_broadcast_to_multiple_clients(self, manager):
        """Test broadcasting message to multiple clients."""
        # Connect multiple websockets
        mock_ws1 = AsyncMock()
        mock_ws1.accept = AsyncMock()
        mock_ws1.send_text = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws2.accept = AsyncMock()
        mock_ws2.send_text = AsyncMock()

        await manager.connect(mock_ws1)
        await manager.connect(mock_ws2)

        message = "Broadcast to all"
        await manager.broadcast(message)

        # Verify message was sent to both
        mock_ws1.send_text.assert_called_once_with(message)
        mock_ws2.send_text.assert_called_once_with(message)

    @patch("preloop.services.websocket_manager.logger")
    async def test_broadcast_with_failed_connection(self, mock_logger, manager):
        """Test broadcasting when one connection fails."""
        # Connect two websockets, one that will fail
        mock_ws1 = AsyncMock()
        mock_ws1.accept = AsyncMock()
        mock_ws1.send_text = AsyncMock(side_effect=Exception("Connection closed"))
        mock_ws2 = AsyncMock()
        mock_ws2.accept = AsyncMock()
        mock_ws2.send_text = AsyncMock()

        await manager.connect(mock_ws1)
        await manager.connect(mock_ws2)

        await manager.broadcast("Test message")

        # Verify warning was logged for failed connection
        assert mock_logger.warning.called
        # Verify second connection still received message
        assert mock_ws2.send_text.called

    async def test_broadcast_json(self, manager, mock_websocket):
        """Test broadcasting JSON data."""
        await manager.connect(mock_websocket)
        data = {"type": "update", "value": 42}

        await manager.broadcast_json(data)

        # Verify JSON was sent as string
        mock_websocket.send_text.assert_called_once()
        sent_message = mock_websocket.send_text.call_args[0][0]
        assert isinstance(sent_message, str)
        # Verify it's valid JSON
        parsed = json.loads(sent_message)
        assert parsed == data

    async def test_broadcast_json_with_complex_data(self, manager, mock_websocket):
        """Test broadcasting complex JSON data."""
        await manager.connect(mock_websocket)
        data = {
            "type": "execution_update",
            "execution_id": "exec_123",
            "status": "running",
            "logs": [{"message": "Step 1 complete"}, {"message": "Step 2 started"}],
        }

        await manager.broadcast_json(data)

        mock_websocket.send_text.assert_called_once()
        sent_message = mock_websocket.send_text.call_args[0][0]
        parsed = json.loads(sent_message)
        assert parsed == data

    async def test_manager_initially_empty(self):
        """Test that manager starts with no connections."""
        manager = WebSocketManager()
        assert len(manager.active_connections) == 0

    async def test_connection_count_after_operations(self, manager):
        """Test connection count after various operations."""
        mock_ws1 = AsyncMock()
        mock_ws1.accept = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws2.accept = AsyncMock()

        # Initially empty
        assert len(manager.active_connections) == 0

        # After first connection
        conn1_id = await manager.connect(mock_ws1)
        assert len(manager.active_connections) == 1

        # After second connection
        conn2_id = await manager.connect(mock_ws2)
        assert len(manager.active_connections) == 2

        # After disconnect
        manager.disconnect(conn1_id)
        assert len(manager.active_connections) == 1

        # After disconnect all
        manager.disconnect(conn2_id)
        assert len(manager.active_connections) == 0


class TestNatsConsumer:
    """Test nats_consumer function."""

    @patch("preloop.services.websocket_manager.get_task_publisher")
    @patch("preloop.services.websocket_manager.persist_execution_log")
    async def test_nats_consumer_processes_message(
        self, mock_persist, mock_get_publisher
    ):
        """Test that NATS consumer processes messages correctly."""
        manager = WebSocketManager()

        # Mock NATS client and publisher
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_publisher = MagicMock()
        mock_publisher.nc = mock_nc
        mock_get_publisher.return_value = mock_publisher

        # Mock subscribe to capture the message handlers
        mock_sub = AsyncMock()
        message_handler = None
        persistence_handler = None

        async def mock_subscribe(subject, queue=None, cb=None, **kwargs):
            nonlocal message_handler, persistence_handler
            handler = cb or kwargs.get("cb")
            if subject == "flow-updates.*" and queue == "log-persisters":
                persistence_handler = handler
            elif subject == "flow-updates.*" and not queue:
                message_handler = handler
            return mock_sub

        mock_nc.subscribe = mock_subscribe

        # Start consumer in background (will run briefly)
        consumer_task = asyncio.create_task(nats_consumer(manager))

        # Give it time to subscribe
        await asyncio.sleep(0.1)

        # Verify handlers were captured
        assert message_handler is not None
        assert persistence_handler is not None

        # Test the message handlers
        test_message = {
            "execution_id": "exec_123",
            "message": "Test update",
            "level": "INFO",
        }
        mock_msg = MagicMock()
        mock_msg.data.decode.return_value = json.dumps(test_message)

        # Call the persistence handler to verify persistence
        await persistence_handler(mock_msg)

        # Verify persist_execution_log was called
        assert mock_persist.called
        call_args = mock_persist.call_args
        assert call_args[0][0] == "exec_123"
        assert call_args[0][1] == test_message

        # Clean up
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            # Expected when cancelling the NATS consumer background task.
            pass

    @patch("preloop.services.websocket_manager.get_task_publisher")
    @patch("preloop.services.websocket_manager.logger")
    async def test_nats_consumer_no_connection(self, mock_logger, mock_get_publisher):
        """Test NATS consumer when NATS is not connected."""
        manager = WebSocketManager()

        # Mock NATS client as not connected
        mock_nc = MagicMock()
        mock_nc.is_connected = False
        mock_publisher = MagicMock()
        mock_publisher.nc = mock_nc
        mock_get_publisher.return_value = mock_publisher

        await nats_consumer(manager)

        # Verify error was logged
        assert mock_logger.error.called

    @patch("preloop.services.websocket_manager.notify_admins")
    @patch("preloop.services.websocket_manager.get_task_publisher")
    async def test_nats_consumer_handles_invalid_json(
        self, mock_get_publisher, mock_notify
    ):
        """Test NATS consumer handles invalid JSON messages.

        The malformed-message path also alerts admins (on a worker thread), so
        ``notify_admins`` is patched here for the same reason as the log-drop
        path: tests must never emit real notifications.
        """
        manager = WebSocketManager()

        # Mock NATS client
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_publisher = MagicMock()
        mock_publisher.nc = mock_nc
        mock_get_publisher.return_value = mock_publisher

        captured_handler = None

        async def mock_subscribe(subject, cb=None, **kwargs):
            nonlocal captured_handler
            # Capture the broadcasting handler for testing
            if subject == "flow-updates.*" and not kwargs.get("queue"):
                captured_handler = cb or kwargs.get("cb")
            return AsyncMock()

        mock_nc.subscribe = mock_subscribe

        # Start consumer briefly
        consumer_task = asyncio.create_task(nats_consumer(manager))
        await asyncio.sleep(0.1)

        # Test with invalid JSON
        mock_msg = MagicMock()
        mock_msg.data.decode.return_value = "invalid json {{"

        # Should handle the error gracefully
        with patch("preloop.services.websocket_manager.logger") as mock_logger:
            await captured_handler(mock_msg)
            assert mock_logger.warning.called

        # The alert is dispatched to a worker thread; give it a moment to land.
        await asyncio.sleep(0.1)
        assert mock_notify.called

        # Clean up
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            # Expected when cancelling the NATS consumer background task.
            pass

    @patch("preloop.services.websocket_manager.get_task_publisher")
    async def test_nats_consumer_message_without_execution_id(self, mock_get_publisher):
        """Test NATS consumer handles messages without execution_id."""
        manager = WebSocketManager()

        # Add a mock websocket to receive broadcasts
        mock_ws = AsyncMock()
        mock_ws.accept = AsyncMock()
        mock_ws.send_text = AsyncMock()
        await manager.connect(mock_ws)

        # Mock NATS client
        mock_nc = MagicMock()
        mock_nc.is_connected = True
        mock_publisher = MagicMock()
        mock_publisher.nc = mock_nc
        mock_get_publisher.return_value = mock_publisher

        captured_handler = None

        async def mock_subscribe(subject, cb=None, **kwargs):
            nonlocal captured_handler
            if subject == "flow-updates.*" and not kwargs.get("queue"):
                captured_handler = cb or kwargs.get("cb")
            return AsyncMock()

        mock_nc.subscribe = mock_subscribe

        # Start consumer
        consumer_task = asyncio.create_task(nats_consumer(manager))
        await asyncio.sleep(0.1)

        # Message without execution_id
        test_message = {"message": "No execution ID", "level": "INFO"}
        mock_msg = MagicMock()
        mock_msg.data.decode.return_value = json.dumps(test_message)

        with patch(
            "preloop.services.websocket_manager.persist_execution_log"
        ) as mock_persist:
            await captured_handler(mock_msg)

            # persist should not be called (no execution_id)
            assert not mock_persist.called
            # But broadcast should still happen
            assert mock_ws.send_text.called

        # Clean up
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            # Expected when cancelling the NATS consumer background task.
            pass
