"""Tests for flow orchestrator streaming and command handling."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.fixture
def mock_db():
    """Mock database session."""
    return MagicMock()


@pytest.fixture
def mock_nats_client():
    """Mock NATS client."""
    client = AsyncMock()
    client.is_connected = True
    client.publish = AsyncMock()
    client.subscribe = AsyncMock()
    return client


@pytest.fixture
def orchestrator(mock_db, mock_nats_client):
    """Create orchestrator instance."""
    flow_id = "test-flow-id"
    trigger_event_data = {"event": "test"}

    orch = FlowExecutionOrchestrator(
        db=mock_db,
        flow_id=flow_id,
        trigger_event_data=trigger_event_data,
        nats_client=mock_nats_client,
    )

    # Mock execution log
    orch.execution_log = MagicMock()
    orch.execution_log.id = "test-execution-id"

    return orch


class TestLogStreaming:
    """Test real-time log streaming to NATS."""

    def test_detect_repeated_tool_cycle_identifies_alternating_loop(self):
        """Alternating identical MCP actions should be flagged as a loop."""
        signatures = [
            json.dumps(
                {
                    "server_name": "preloop-mcp",
                    "tool_name": "update_pull_request",
                    "arguments": {"pull_request": "mr-22", "add_reaction": "eyes"},
                },
                sort_keys=True,
            ),
            json.dumps(
                {
                    "server_name": "preloop-mcp",
                    "tool_name": "get_pull_request",
                    "arguments": {"pull_request": "mr-22", "include_diff": True},
                },
                sort_keys=True,
            ),
        ] * 3

        detection = FlowExecutionOrchestrator._detect_repeated_tool_cycle(signatures)

        assert detection is not None
        assert detection["pattern_length"] == 2
        assert detection["repetitions"] == 3
        assert detection["pattern"][0]["tool_name"] == "update_pull_request"
        assert detection["pattern"][1]["tool_name"] == "get_pull_request"

    def test_detect_repeated_tool_cycle_allows_distinct_update_pull_request_calls(
        self,
    ):
        """PR review flows may call update_pull_request multiple times with new args."""
        signatures = [
            json.dumps(
                {
                    "server_name": "preloop-mcp",
                    "tool_name": "update_pull_request",
                    "arguments": {
                        "pull_request": "https://github.com/org/repo/pull/49",
                        "add_reaction": "eyes",
                    },
                },
                sort_keys=True,
            ),
            json.dumps(
                {
                    "server_name": "preloop-mcp",
                    "tool_name": "update_pull_request",
                    "arguments": {
                        "pull_request": "https://github.com/org/repo/pull/49",
                        "review_action": "comment",
                    },
                },
                sort_keys=True,
            ),
            json.dumps(
                {
                    "server_name": "preloop-mcp",
                    "tool_name": "update_pull_request",
                    "arguments": {
                        "pull_request": "https://github.com/org/repo/pull/49",
                        "description": "updated body",
                    },
                },
                sort_keys=True,
            ),
            json.dumps(
                {
                    "server_name": "preloop-mcp",
                    "tool_name": "update_pull_request",
                    "arguments": {
                        "pull_request": "https://github.com/org/repo/pull/49",
                        "remove_reaction": "eyes",
                    },
                },
                sort_keys=True,
            ),
        ]

        detection = FlowExecutionOrchestrator._detect_repeated_tool_cycle(signatures)

        assert detection is None

    def test_dedupe_rapid_duplicate_signatures_ignores_paired_mcp_calls(self):
        """Paired duplicate MCP calls inside a short window count once."""
        from datetime import UTC, datetime, timedelta

        base = datetime(2026, 6, 9, 21, 14, 0, tzinfo=UTC)
        remove_reaction = json.dumps(
            {
                "server_name": "preloop-mcp",
                "tool_name": "update_pull_request",
                "arguments": {
                    "pull_request": "https://github.com/org/repo/pull/49",
                    "remove_reaction": "eyes",
                },
            },
            sort_keys=True,
        )
        resolve_comment = json.dumps(
            {
                "server_name": "preloop-mcp",
                "tool_name": "update_comment",
                "arguments": {
                    "target": "https://github.com/org/repo/pull/49",
                    "comment_id": "123",
                    "resolved": True,
                    "comment_type": "review_comment",
                },
            },
            sort_keys=True,
        )
        signatures = [
            resolve_comment,
            resolve_comment,
            remove_reaction,
            remove_reaction,
            remove_reaction,
            remove_reaction,
        ]
        timestamps = [
            base,
            base + timedelta(milliseconds=7),
            base + timedelta(seconds=16),
            base + timedelta(seconds=16, milliseconds=7),
            base + timedelta(seconds=23),
            base + timedelta(seconds=23, milliseconds=7),
        ]

        deduped = FlowExecutionOrchestrator._dedupe_rapid_duplicate_signatures(
            signatures,
            timestamps,
        )

        assert deduped == [resolve_comment, remove_reaction, remove_reaction]
        assert FlowExecutionOrchestrator._detect_repeated_tool_cycle(deduped) is None

    def test_detect_repeated_tool_cycle_flags_identical_payload_retries(self):
        """True loops repeat the same tool call with identical arguments."""
        signature = json.dumps(
            {
                "server_name": "preloop-mcp",
                "tool_name": "get_pull_request",
                "arguments": {
                    "pull_request": "https://github.com/org/repo/pull/49",
                    "include_diff": True,
                },
            },
            sort_keys=True,
        )

        detection = FlowExecutionOrchestrator._detect_repeated_tool_cycle(
            [signature] * 4
        )

        assert detection is not None
        assert detection["pattern_length"] == 1
        assert detection["repetitions"] == 4
        assert detection["pattern"][0]["tool_name"] == "get_pull_request"

    @pytest.mark.asyncio
    async def test_sync_runtime_tool_activity_metrics_updates_count_and_detects_loop(
        self, orchestrator
    ):
        """Persisted runtime activity should drive live counts and loop detection."""
        orchestrator._get_runtime_tool_activity_count = MagicMock(return_value=6)
        orchestrator._get_recent_runtime_tool_activity_signatures = MagicMock(
            return_value=[
                json.dumps(
                    {
                        "server_name": "preloop-mcp",
                        "tool_name": "update_pull_request",
                        "arguments": {
                            "pull_request": "mr-22",
                            "add_reaction": "eyes",
                        },
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "server_name": "preloop-mcp",
                        "tool_name": "get_pull_request",
                        "arguments": {
                            "pull_request": "mr-22",
                            "include_diff": True,
                        },
                    },
                    sort_keys=True,
                ),
            ]
            * 3
        )
        orchestrator._publish_update = AsyncMock()
        orchestrator._persist_live_metrics = AsyncMock()

        detection = await orchestrator._sync_runtime_tool_activity_metrics()

        assert orchestrator.tool_calls_count == 6
        assert detection is not None
        assert detection["pattern_length"] == 2
        orchestrator._publish_update.assert_awaited()
        orchestrator._persist_live_metrics.assert_awaited()

    @pytest.mark.asyncio
    async def test_stream_logs_to_nats_success(self, orchestrator, mock_nats_client):
        """Test that logs are streamed to NATS."""
        mock_agent_executor = AsyncMock()

        # Mock log streaming
        async def mock_stream(session_reference):
            for line in ["Log line 1", "Log line 2", "Log line 3"]:
                yield line

        mock_agent_executor.stream_logs = mock_stream

        # Run streaming task
        task = asyncio.create_task(
            orchestrator._stream_logs_to_nats(mock_agent_executor, "session-123")
        )

        # Give it time to process
        await asyncio.sleep(0.1)

        # Cancel task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Expected when cancelling the streaming task after assertions.
            pass

        # Verify NATS publish was called
        assert mock_nats_client.publish.call_count >= 3

        # Verify message format
        calls = mock_nats_client.publish.call_args_list
        for call in calls:
            subject, message = call[0]
            assert subject.startswith("flow-updates.")

            # Decode and verify message
            data = json.loads(message.decode())
            assert data["type"] == "agent_log_line"
            assert "timestamp" in data["payload"]
            assert "line" in data["payload"]

    @pytest.mark.asyncio
    async def test_stream_logs_handles_cancellation(self, orchestrator):
        """Test that log streaming handles cancellation gracefully."""
        mock_agent_executor = AsyncMock()

        async def mock_stream(session_reference):
            for i in range(100):
                yield f"Line {i}"
                await asyncio.sleep(0.01)

        mock_agent_executor.stream_logs = mock_stream

        # Start task
        task = asyncio.create_task(
            orchestrator._stream_logs_to_nats(mock_agent_executor, "session-123")
        )

        # Let it run briefly
        await asyncio.sleep(0.05)

        # Cancel
        task.cancel()

        # Should not raise
        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected

    @pytest.mark.asyncio
    async def test_stream_logs_handles_errors(self, orchestrator, mock_nats_client):
        """Test that streaming handles errors gracefully."""
        mock_agent_executor = AsyncMock()

        async def mock_stream(session_reference):
            yield "Line 1"
            raise RuntimeError("Test error")

        mock_agent_executor.stream_logs = mock_stream

        # Run streaming task
        task = asyncio.create_task(
            orchestrator._stream_logs_to_nats(mock_agent_executor, "session-123")
        )

        await asyncio.sleep(0.1)

        # Should complete without raising
        await asyncio.wait_for(task, timeout=1.0)

        # Should have published error
        calls = [call for call in mock_nats_client.publish.call_args_list]
        error_messages = [
            call
            for call in calls
            if b"agent_log_error" in call[0][1] or b"error" in call[0][1].lower()
        ]
        assert len(error_messages) > 0

    @pytest.mark.asyncio
    async def test_stream_logs_persists_live_tool_call_metrics(self, orchestrator):
        """Tool call updates should persist live counters for page reloads."""
        mock_agent_executor = AsyncMock()

        async def mock_stream(session_reference):
            yield "Tool call line"

        mock_agent_executor.stream_logs = mock_stream
        orchestrator._persist_live_metrics = AsyncMock()
        orchestrator.execution_logger.parse_agent_logs = MagicMock(
            side_effect=lambda lines: (
                orchestrator.execution_logger.mcp_usage_logs.append(
                    {"tool_name": "search"}
                )
            )
        )

        await orchestrator._stream_logs_to_nats(mock_agent_executor, "session-123")

        assert orchestrator.tool_calls_count == 1
        orchestrator._persist_live_metrics.assert_awaited()

    @pytest.mark.asyncio
    async def test_stream_logs_publishes_structured_mcp_call_event(self, orchestrator):
        """Detected MCP calls should be published as structured live events."""
        mock_agent_executor = AsyncMock()

        async def mock_stream(session_reference):
            yield "Calling github/create_issue with args"

        mock_agent_executor.stream_logs = mock_stream
        orchestrator._persist_live_metrics = AsyncMock()
        orchestrator._publish_update = AsyncMock()

        await orchestrator._stream_logs_to_nats(mock_agent_executor, "session-123")

        published_types = [
            call.args[0] for call in orchestrator._publish_update.await_args_list
        ]
        assert "mcp_call" in published_types
        mcp_call_payloads = [
            call.args[1]
            for call in orchestrator._publish_update.await_args_list
            if call.args[0] == "mcp_call"
        ]
        assert len(mcp_call_payloads) == 1
        assert mcp_call_payloads[0]["server_name"] == "github"
        assert mcp_call_payloads[0]["tool_name"] == "create_issue"
        assert mcp_call_payloads[0]["status"] == "detected"

    @pytest.mark.asyncio
    async def test_persist_live_metrics_stores_mcp_usage_logs(
        self, orchestrator, mock_db
    ):
        """Persisted live metrics should include tool activity details."""
        orchestrator.tool_calls_count = 2
        orchestrator.total_tokens = 99
        orchestrator.estimated_cost = 0.12
        orchestrator.execution_logger.mcp_usage_logs = [
            {"timestamp": "2026-03-10T10:00:00Z", "tool_name": "search_issues"},
            {"timestamp": "2026-03-10T10:00:01Z", "tool_name": "get_issue"},
        ]

        await orchestrator._persist_live_metrics()

        assert orchestrator.execution_log.tool_calls_count == 2
        assert orchestrator.execution_log.total_tokens == 99
        assert orchestrator.execution_log.estimated_cost == 0.12
        assert orchestrator.execution_log.mcp_usage_logs == [
            {"timestamp": "2026-03-10T10:00:00Z", "tool_name": "search_issues"},
            {"timestamp": "2026-03-10T10:00:01Z", "tool_name": "get_issue"},
        ]
        mock_db.add.assert_called_with(orchestrator.execution_log)
        mock_db.commit.assert_called()
        mock_db.refresh.assert_called_with(orchestrator.execution_log)


class TestCommandListener:
    """Test command listener for user intervention."""

    @pytest.mark.asyncio
    async def test_listen_for_commands_stop(self, orchestrator, mock_nats_client):
        """Test that stop command is handled."""

        # Capture the subscription callback
        callback = None

        async def mock_subscribe(subject, cb):
            nonlocal callback
            callback = cb
            return AsyncMock()

        mock_nats_client.subscribe = mock_subscribe

        # Start listening
        await orchestrator._listen_for_commands()

        # Verify subscription
        assert callback is not None

        # Simulate stop command
        mock_msg = MagicMock()
        mock_msg.data = json.dumps({"command": "stop"}).encode()

        await callback(mock_msg)

        # Verify stop was requested
        assert orchestrator._stop_requested.is_set()

    @pytest.mark.asyncio
    async def test_listen_for_commands_send_message(
        self, orchestrator, mock_nats_client
    ):
        """Test that send_message command is handled."""
        callback = None

        async def mock_subscribe(subject, cb):
            nonlocal callback
            callback = cb
            return AsyncMock()

        mock_nats_client.subscribe = mock_subscribe

        await orchestrator._listen_for_commands()

        # Simulate send_message command
        mock_msg = MagicMock()
        mock_msg.data = json.dumps(
            {"command": "send_message", "message": "Hello agent!"}
        ).encode()

        await callback(mock_msg)

        # Verify message was queued
        assert orchestrator._user_messages.qsize() == 1
        message = await orchestrator._user_messages.get()
        assert message == "Hello agent!"

    @pytest.mark.asyncio
    async def test_listen_for_commands_invalid_json(
        self, orchestrator, mock_nats_client
    ):
        """Test that invalid JSON is handled."""
        callback = None

        async def mock_subscribe(subject, cb):
            nonlocal callback
            callback = cb
            return AsyncMock()

        mock_nats_client.subscribe = mock_subscribe

        await orchestrator._listen_for_commands()

        # Simulate invalid JSON
        mock_msg = MagicMock()
        mock_msg.data = b"invalid json"

        # Should not raise
        await callback(mock_msg)

    @pytest.mark.asyncio
    async def test_listen_for_commands_unknown_command(
        self, orchestrator, mock_nats_client
    ):
        """Test that unknown commands are logged."""
        callback = None

        async def mock_subscribe(subject, cb):
            nonlocal callback
            callback = cb
            return AsyncMock()

        mock_nats_client.subscribe = mock_subscribe

        await orchestrator._listen_for_commands()

        # Simulate unknown command
        mock_msg = MagicMock()
        mock_msg.data = json.dumps({"command": "unknown"}).encode()

        # Should not raise
        await callback(mock_msg)


class TestCleanupMonitoring:
    """Test cleanup of monitoring resources."""

    @pytest.mark.asyncio
    async def test_cleanup_waits_for_log_streaming(self, orchestrator):
        """Test that cleanup waits for log streaming task to complete."""

        completed = False

        # Create a real async task that completes quickly
        async def dummy_stream():
            nonlocal completed
            await asyncio.sleep(0.1)  # Short sleep to complete quickly
            completed = True

        # Create actual task
        mock_task = asyncio.create_task(dummy_stream())

        orchestrator._log_streaming_task = mock_task

        # Run cleanup - should wait for task to complete
        await orchestrator._cleanup_monitoring()

        # Verify task completed (not cancelled)
        assert mock_task.done()
        assert completed
        assert not mock_task.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_cancels_log_streaming_on_timeout(self, orchestrator):
        """Test that cleanup cancels log streaming task if it times out."""

        # Create a real async task that takes too long
        async def slow_stream():
            try:
                await asyncio.sleep(100)  # Very long sleep
            except asyncio.CancelledError:
                raise

        # Create actual task
        mock_task = asyncio.create_task(slow_stream())

        orchestrator._log_streaming_task = mock_task

        # Patch the timeout to be very short for testing

        async def short_timeout_cleanup():
            # Wait for just 0.1 seconds instead of 30
            if (
                orchestrator._log_streaming_task
                and not orchestrator._log_streaming_task.done()
            ):
                try:
                    await asyncio.wait_for(
                        orchestrator._log_streaming_task, timeout=0.1
                    )
                except asyncio.TimeoutError:
                    orchestrator._log_streaming_task.cancel()
                    try:
                        await orchestrator._log_streaming_task
                    except asyncio.CancelledError:
                        # Expected after timeout-driven cancellation in cleanup test.
                        pass

        # Run cleanup with short timeout
        await short_timeout_cleanup()

        # Give a moment for cancellation to propagate
        await asyncio.sleep(0.05)

        # Verify task was cancelled due to timeout
        assert mock_task.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_unsubscribes_from_commands(self, orchestrator):
        """Test that cleanup unsubscribes from commands."""
        mock_subscription = AsyncMock()
        orchestrator._command_subscription = mock_subscription

        await orchestrator._cleanup_monitoring()

        # Verify unsubscribe was called
        mock_subscription.unsubscribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_handles_errors(self, orchestrator):
        """Test that cleanup handles errors gracefully."""
        # Create subscription that raises on unsubscribe
        mock_subscription = AsyncMock()
        mock_subscription.unsubscribe = AsyncMock(
            side_effect=RuntimeError("Test error")
        )
        orchestrator._command_subscription = mock_subscription

        # Should not raise
        await orchestrator._cleanup_monitoring()


class TestMonitoringIntegration:
    """Integration tests for monitoring with streaming."""

    @pytest.mark.asyncio
    async def test_monitor_starts_log_streaming(self, orchestrator, mock_nats_client):
        """Test that monitoring starts log streaming task."""
        from preloop.agents.base import AgentStatus, AgentExecutionResult

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution"
        ) as mock_create:
            mock_agent_executor = AsyncMock()
            mock_agent_executor.get_status = AsyncMock(
                return_value=AgentStatus.SUCCEEDED
            )
            mock_agent_executor.get_result = AsyncMock(
                return_value=AgentExecutionResult(
                    status=AgentStatus.SUCCEEDED,
                    session_reference="session-123",
                    exit_code=0,
                    output_summary="Done",
                    error_message=None,
                )
            )

            async def mock_stream(session_reference):
                yield "Test log"

            mock_agent_executor.stream_logs = mock_stream
            mock_create.return_value = mock_agent_executor

            # Mock flow
            orchestrator.flow = MagicMock()
            orchestrator.flow.agent_type = "test"
            orchestrator.flow.agent_config = {}

            # Mock execution logger
            orchestrator.execution_logger.get_actions_taken = MagicMock(return_value=[])
            orchestrator.execution_logger.get_mcp_usage_logs = MagicMock(
                return_value=[]
            )
            orchestrator.execution_logger.log_milestone = MagicMock()

            # Run monitoring (will complete quickly due to SUCCEEDED status)
            await orchestrator._monitor_agent_execution(
                "session-123", mock_agent_executor
            )

            # Verify log streaming task was created
            assert orchestrator._log_streaming_task is not None

    @pytest.mark.asyncio
    async def test_monitor_handles_stop_command(self, orchestrator, mock_nats_client):
        """Test that monitoring handles stop command."""
        from preloop.agents.base import AgentStatus

        with patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution"
        ) as mock_create:
            mock_agent_executor = AsyncMock()
            mock_agent_executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)
            mock_agent_executor.stop = AsyncMock()

            async def mock_stream(session_reference):
                while True:
                    yield "Test log"
                    await asyncio.sleep(0.1)

            mock_agent_executor.stream_logs = mock_stream
            mock_create.return_value = mock_agent_executor

            orchestrator.flow = MagicMock()
            orchestrator.flow.agent_type = "test"
            orchestrator.flow.agent_config = {}

            # Set stop requested immediately
            orchestrator._stop_requested.set()

            # Start monitoring
            task = asyncio.create_task(
                orchestrator._monitor_agent_execution(
                    "session-123", mock_agent_executor
                )
            )

            # Give it time to process
            await asyncio.sleep(0.2)

            # Should have called stop at least once
            assert mock_agent_executor.stop.call_count >= 1
            mock_agent_executor.stop.assert_any_call("session-123")

            # Cancel task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Expected when cancelling the orchestrator run task in the test.
                pass


class TestUserStopResult:
    """A user-requested stop must not be reported as a timeout."""

    @pytest.mark.asyncio
    async def test_stop_returns_stopped_status_not_timeout(self, orchestrator):
        """Regression: stop used to fall through to the timeout branch.

        Executions cancelled after a few seconds were persisted with
        "Execution timed out after 3600 seconds", which was both a wrong
        status (FAILED instead of STOPPED) and a nonsensical message.
        """
        from preloop.agents.base import AgentStatus

        mock_agent_executor = AsyncMock()
        mock_agent_executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)
        mock_agent_executor.stop = AsyncMock()

        async def mock_stream(session_reference):
            yield "Test log"

        mock_agent_executor.stream_logs = mock_stream

        orchestrator.flow = MagicMock()
        orchestrator.flow.agent_type = "test"
        orchestrator.flow.agent_config = {}
        orchestrator.execution_logger.get_actions_taken = MagicMock(return_value=[])
        orchestrator.execution_logger.get_mcp_usage_logs = MagicMock(return_value=[])
        orchestrator.execution_logger.log_milestone = MagicMock()

        orchestrator._stop_requested.set()

        result = await orchestrator._monitor_agent_execution(
            "session-123", mock_agent_executor
        )

        assert result["status"] == "STOPPED"
        assert "timed out" not in (result["error_message"] or "")
        assert "stopped by user request" in result["error_message"]
        mock_agent_executor.stop.assert_any_call("session-123")


@pytest.mark.asyncio
async def test_remote_runner_logs_are_not_streamed_twice(
    orchestrator: FlowExecutionOrchestrator, caplog: pytest.LogCaptureFixture
) -> None:
    """The runner WebSocket already persists and publishes its log lines."""
    from uuid import uuid4
    from preloop.agents.remote_runner import RemoteRunnerExecutor

    executor = RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="auto", account_id=uuid4()
    )
    executor.get_logs = AsyncMock()
    await orchestrator._stream_logs_to_nats(executor, f"runner:queued:auto:{uuid4()}")
    assert not [record for record in caplog.records if record.levelname == "ERROR"]
    executor.get_logs.assert_not_called()


def test_replay_persisted_runner_logs_binds_opened_pr(
    orchestrator: FlowExecutionOrchestrator,
) -> None:
    """Private-runner log rows still feed PR-opened binding after the skip."""
    from unittest.mock import patch

    orchestrator.execution_log.agent_session_reference = "runner:abc:exec"
    orchestrator.execution_logger.agent_output_lines = []
    row = MagicMock()
    row.log_type = "agent_log_line"
    row.message = (
        'PRELOOP_PR_OPENED {"url": "https://example.com/mr/1", "branch": "fix"}'
    )
    row.metadata_ = None
    with patch(
        "preloop.services.flow_orchestrator.crud_flow_execution_log.get_by_execution_id",
        return_value=[row],
    ):
        orchestrator._replay_persisted_runner_logs()
    assert orchestrator.execution_logger.get_agent_output_lines() == [
        'PRELOOP_PR_OPENED {"url": "https://example.com/mr/1", "branch": "fix"}'
    ]
    assert orchestrator._opened_pr is not None
    assert orchestrator._opened_pr["url"] == "https://example.com/mr/1"
