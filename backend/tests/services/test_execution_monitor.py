"""Tests for execution monitor service."""

import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from preloop.services.execution_monitor import (
    ExecutionMonitor,
    get_execution_monitor,
)
from preloop.agents.base import AgentStatus
from preloop.models.models import FlowExecution, Flow

pytestmark = pytest.mark.asyncio


@pytest.fixture
def execution_monitor():
    """Create an ExecutionMonitor instance."""
    return ExecutionMonitor(check_interval_seconds=1, stale_threshold_minutes=5)


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.all.return_value = []
    mock_db.query.return_value.filter.return_value.first.return_value = None
    return mock_db


@pytest.fixture
def sample_execution():
    """Create a sample flow execution."""
    execution = MagicMock(spec=FlowExecution)
    execution.id = str(uuid4())
    execution.flow_id = str(uuid4())
    execution.status = "RUNNING"
    execution.agent_session_reference = "container-123"
    execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    execution.error_message = None
    execution.end_time = None
    return execution


@pytest.fixture
def sample_flow():
    """Create a sample flow."""
    flow = MagicMock(spec=Flow)
    flow.id = str(uuid4())
    flow.agent_type = "openhands"
    flow.agent_config = {"max_iterations": 10}
    return flow


class TestExecutionMonitorLifecycle:
    """Test ExecutionMonitor start/stop lifecycle."""

    async def test_start_monitor(self, execution_monitor):
        """Test starting the execution monitor."""
        assert not execution_monitor._running
        assert execution_monitor._task is None

        await execution_monitor.start()

        assert execution_monitor._running
        assert execution_monitor._task is not None

        # Clean up
        await execution_monitor.stop()

    async def test_start_monitor_already_running(self, execution_monitor):
        """Test starting monitor when already running."""
        await execution_monitor.start()
        assert execution_monitor._running

        # Try to start again
        await execution_monitor.start()

        # Should still be running
        assert execution_monitor._running

        await execution_monitor.stop()

    async def test_stop_monitor(self, execution_monitor):
        """Test stopping the execution monitor."""
        await execution_monitor.start()
        assert execution_monitor._running

        await execution_monitor.stop()

        assert not execution_monitor._running

    async def test_stop_monitor_not_running(self, execution_monitor):
        """Test stopping monitor when not running."""
        assert not execution_monitor._running

        # Should not raise error
        await execution_monitor.stop()

        assert not execution_monitor._running

    async def test_stop_monitor_handles_cancellation(self, execution_monitor):
        """Test that stop handles task cancellation gracefully."""
        await execution_monitor.start()

        # Stop should cancel the task and wait for it
        await execution_monitor.stop()

        assert not execution_monitor._running


class TestCheckStaleExecutions:
    """Test _check_stale_executions method."""

    async def test_check_stale_executions_no_active(self, execution_monitor):
        """Test checking when no active executions exist."""
        with patch("preloop.services.execution_monitor.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = []
            mock_get_db.return_value = iter([mock_db])

            # Should complete without error
            await execution_monitor._check_stale_executions()

            mock_db.close.assert_called_once()

    async def test_check_stale_executions_with_active(
        self, execution_monitor, sample_execution
    ):
        """Test checking with active executions."""
        with patch("preloop.services.execution_monitor.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.all.return_value = [
                sample_execution
            ]
            mock_get_db.return_value = iter([mock_db])

            with patch.object(
                execution_monitor, "_check_execution", new_callable=AsyncMock
            ) as mock_check:
                await execution_monitor._check_stale_executions()

                mock_check.assert_called_once_with(mock_db, sample_execution)
                mock_db.commit.assert_called_once()
                mock_db.close.assert_called_once()

    async def test_check_stale_executions_error_handling(self, execution_monitor):
        """Test error handling in _check_stale_executions."""
        with patch("preloop.services.execution_monitor.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.query.side_effect = Exception("Database error")
            mock_get_db.return_value = iter([mock_db])

            # Should handle error and not raise
            await execution_monitor._check_stale_executions()

            mock_db.rollback.assert_called_once()
            mock_db.close.assert_called_once()


class TestCheckExecution:
    """Test _check_execution method."""

    async def test_check_execution_not_stale(
        self, execution_monitor, mock_db_session, sample_execution
    ):
        """Test checking execution that is not yet stale."""
        # Set start time to be recent (not stale)
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=2)

        await execution_monitor._check_execution(mock_db_session, sample_execution)

        # Status should not be changed
        assert sample_execution.status == "RUNNING"

    async def test_check_execution_no_session_reference(
        self, execution_monitor, mock_db_session, sample_execution
    ):
        """Test checking execution with no session reference."""
        sample_execution.agent_session_reference = None
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)

        await execution_monitor._check_execution(mock_db_session, sample_execution)

        # Should be marked as failed
        assert sample_execution.status == "FAILED"
        assert "failed to start" in sample_execution.error_message.lower()
        assert sample_execution.end_time is not None

    async def test_check_execution_flow_not_found(
        self, execution_monitor, mock_db_session, sample_execution
    ):
        """Test checking execution when flow is not found."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = None

        await execution_monitor._check_execution(mock_db_session, sample_execution)

        # Should log error but not crash
        # Status should remain unchanged
        assert sample_execution.status == "RUNNING"

    async def test_check_execution_agent_executor_creation_fails(
        self, execution_monitor, mock_db_session, sample_execution, sample_flow
    ):
        """Test checking execution when agent executor creation fails."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = (
            sample_flow
        )

        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            side_effect=Exception("Failed to create executor"),
        ):
            await execution_monitor._check_execution(mock_db_session, sample_execution)

            # Should log error but not crash
            assert sample_execution.status == "RUNNING"

    async def test_check_execution_container_failed(
        self, execution_monitor, mock_db_session, sample_execution, sample_flow
    ):
        """Test checking execution when container has failed."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = (
            sample_flow
        )

        mock_executor = AsyncMock()
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.FAILED)

        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            return_value=mock_executor,
        ):
            await execution_monitor._check_execution(mock_db_session, sample_execution)

            assert sample_execution.status == "FAILED"
            assert "container failed" in sample_execution.error_message.lower()
            assert sample_execution.end_time is not None

    async def test_check_execution_container_succeeded(
        self, execution_monitor, mock_db_session, sample_execution, sample_flow
    ):
        """Test checking execution when container has succeeded."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = (
            sample_flow
        )

        mock_executor = AsyncMock()
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.SUCCEEDED)

        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            return_value=mock_executor,
        ):
            await execution_monitor._check_execution(mock_db_session, sample_execution)

            assert sample_execution.status == "SUCCEEDED"
            assert sample_execution.end_time is not None

    async def test_check_execution_container_stopped(
        self, execution_monitor, mock_db_session, sample_execution, sample_flow
    ):
        """Test checking execution when container was stopped."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = (
            sample_flow
        )

        mock_executor = AsyncMock()
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.STOPPED)

        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            return_value=mock_executor,
        ):
            await execution_monitor._check_execution(mock_db_session, sample_execution)

            # Container stopped should result in STOPPED status (not FAILED)
            # This preserves user-initiated stops
            assert sample_execution.status == "STOPPED"
            assert "stopped" in sample_execution.error_message.lower()
            assert sample_execution.end_time is not None

    async def test_check_execution_container_still_running(
        self, execution_monitor, mock_db_session, sample_execution, sample_flow
    ):
        """Test checking execution when container is still running."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = (
            sample_flow
        )

        mock_executor = AsyncMock()
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)

        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            return_value=mock_executor,
        ):
            await execution_monitor._check_execution(mock_db_session, sample_execution)

            # Status should remain unchanged
            assert sample_execution.status == "RUNNING"
            assert sample_execution.end_time is None

    async def test_check_execution_container_starting(
        self, execution_monitor, mock_db_session, sample_execution, sample_flow
    ):
        """Test checking execution when container is starting."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = (
            sample_flow
        )

        mock_executor = AsyncMock()
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.STARTING)

        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            return_value=mock_executor,
        ):
            await execution_monitor._check_execution(mock_db_session, sample_execution)

            # Status should remain unchanged
            assert sample_execution.status == "RUNNING"
            assert sample_execution.end_time is None

    async def test_check_execution_status_check_fails(
        self, execution_monitor, mock_db_session, sample_execution, sample_flow
    ):
        """Test checking execution when status check raises an error."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = (
            sample_flow
        )

        mock_executor = AsyncMock()
        mock_executor.get_status = AsyncMock(
            side_effect=Exception("Container not found")
        )

        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            return_value=mock_executor,
        ):
            await execution_monitor._check_execution(mock_db_session, sample_execution)

            # Should be marked as failed
            assert sample_execution.status == "FAILED"
            assert "lost connection" in sample_execution.error_message.lower()
            assert sample_execution.end_time is not None

    async def test_check_execution_with_cleanup(
        self, execution_monitor, mock_db_session, sample_execution, sample_flow
    ):
        """Test that cleanup is called on executor."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.return_value.filter.return_value.first.return_value = (
            sample_flow
        )

        mock_executor = AsyncMock()
        mock_executor.get_status = AsyncMock(return_value=AgentStatus.SUCCEEDED)
        mock_executor.cleanup = AsyncMock()

        with patch(
            "preloop.services.execution_monitor.create_agent_executor",
            return_value=mock_executor,
        ):
            await execution_monitor._check_execution(mock_db_session, sample_execution)

            mock_executor.cleanup.assert_called_once()

    async def test_check_execution_error_in_check(
        self, execution_monitor, mock_db_session, sample_execution
    ):
        """Test error handling in _check_execution."""
        sample_execution.start_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_db_session.query.side_effect = Exception("Database error")

        # Should not raise, just log error
        await execution_monitor._check_execution(mock_db_session, sample_execution)


class TestMonitorLoop:
    """Test _monitor_loop method."""

    async def test_monitor_loop_runs(self, execution_monitor):
        """Test that monitor loop runs and checks executions."""
        with patch.object(
            execution_monitor, "_check_stale_executions", new_callable=AsyncMock
        ) as mock_check:
            # Start monitor
            await execution_monitor.start()

            # Wait for at least one check
            await asyncio.sleep(0.5)

            # Stop monitor
            await execution_monitor.stop()

            # Should have been called at least once
            assert mock_check.call_count >= 1

    async def test_monitor_loop_handles_errors(self, execution_monitor):
        """Test that monitor loop continues after errors."""
        call_count = 0

        async def mock_check_with_error():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("First check failed")
            # Second check succeeds

        with patch.object(
            execution_monitor,
            "_check_stale_executions",
            side_effect=mock_check_with_error,
        ):
            await execution_monitor.start()

            # Wait for multiple checks
            await asyncio.sleep(1.5)

            await execution_monitor.stop()

            # Should have attempted multiple checks despite error
            assert call_count >= 2

    async def test_monitor_loop_stops_on_cancellation(self, execution_monitor):
        """Test that monitor loop stops when cancelled."""
        await execution_monitor.start()

        # Stop should cancel the loop
        await execution_monitor.stop()

        assert not execution_monitor._running


class TestGetExecutionMonitor:
    """Test get_execution_monitor function."""

    def test_get_execution_monitor_creates_instance(self, monkeypatch):
        """Test that get_execution_monitor creates a singleton instance."""
        # Reset global instance
        monkeypatch.setattr(
            "preloop.services.execution_monitor._monitor_instance", None
        )

        monitor1 = get_execution_monitor()
        assert monitor1 is not None
        assert isinstance(monitor1, ExecutionMonitor)

        # Second call should return same instance
        monitor2 = get_execution_monitor()
        assert monitor2 is monitor1

    def test_get_execution_monitor_with_env_vars(self, monkeypatch):
        """Test that environment variables configure the monitor."""
        monkeypatch.setattr(
            "preloop.services.execution_monitor._monitor_instance", None
        )

        with patch("preloop.services.execution_monitor.os.getenv") as mock_getenv:
            mock_getenv.side_effect = lambda key, default: {
                "EXECUTION_MONITOR_INTERVAL": "120",
                "EXECUTION_STALE_THRESHOLD_MINUTES": "90",
            }.get(key, default)

            monitor = get_execution_monitor()

            assert monitor.check_interval == 120
            assert monitor.stale_threshold == timedelta(minutes=90)
