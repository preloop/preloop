"""Tests for FlowExecutionLogger service."""

from preloop.services.flow_execution_logger import FlowExecutionLogger
from preloop.utils.redaction import REDACTED_STRING


class TestFlowExecutionLoggerInit:
    """Test FlowExecutionLogger initialization."""

    def test_initialization(self):
        """Test that logger initializes with empty lists."""
        logger = FlowExecutionLogger()

        assert logger.mcp_usage_logs == []
        assert logger.actions_taken == []
        assert logger.milestones == []


class TestLogMCPToolCall:
    """Test log_mcp_tool_call method."""

    def test_log_mcp_tool_call_minimal(self):
        """Test logging MCP tool call with minimal parameters."""
        logger = FlowExecutionLogger()

        logger.log_mcp_tool_call(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"arg1": "value1"},
        )

        assert len(logger.mcp_usage_logs) == 1
        log_entry = logger.mcp_usage_logs[0]
        assert log_entry["server_name"] == "test-server"
        assert log_entry["tool_name"] == "test_tool"
        assert log_entry["arguments"] == {"arg1": "value1"}
        assert log_entry["status"] == "pending"
        assert log_entry["result_summary"] is None
        assert log_entry["error"] is None
        assert "timestamp" in log_entry

    def test_log_mcp_tool_call_with_success(self):
        """Test logging successful MCP tool call."""
        logger = FlowExecutionLogger()

        logger.log_mcp_tool_call(
            server_name="test-server",
            tool_name="test_tool",
            arguments={},
            status="success",
            result_summary="Operation completed successfully",
        )

        log_entry = logger.mcp_usage_logs[0]
        assert log_entry["status"] == "success"
        assert log_entry["result_summary"] == "Operation completed successfully"

    def test_log_mcp_tool_call_with_error(self):
        """Test logging failed MCP tool call."""
        logger = FlowExecutionLogger()

        logger.log_mcp_tool_call(
            server_name="test-server",
            tool_name="test_tool",
            arguments={},
            status="failed",
            error="Connection timeout",
        )

        log_entry = logger.mcp_usage_logs[0]
        assert log_entry["status"] == "failed"
        assert log_entry["error"] == "Connection timeout"

    def test_log_mcp_tool_call_redacts_sensitive_arguments(self):
        """Test that sensitive arguments are redacted before storage."""
        logger = FlowExecutionLogger()

        logger.log_mcp_tool_call(
            server_name="test-server",
            tool_name="create_issue",
            arguments={
                "project": "my-org/repo",
                "title": "Fix bug",
                "api_key": "sk-secret-key-123",
                "password": "super_secret",
            },
        )

        log_entry = logger.mcp_usage_logs[0]
        assert log_entry["arguments"]["project"] == "my-org/repo"
        assert log_entry["arguments"]["title"] == "Fix bug"
        assert log_entry["arguments"]["api_key"] == REDACTED_STRING
        assert log_entry["arguments"]["password"] == REDACTED_STRING

    def test_log_multiple_mcp_tool_calls(self):
        """Test logging multiple MCP tool calls."""
        logger = FlowExecutionLogger()

        logger.log_mcp_tool_call("server1", "tool1", {})
        logger.log_mcp_tool_call("server2", "tool2", {})
        logger.log_mcp_tool_call("server3", "tool3", {})

        assert len(logger.mcp_usage_logs) == 3
        assert logger.mcp_usage_logs[0]["server_name"] == "server1"
        assert logger.mcp_usage_logs[1]["server_name"] == "server2"
        assert logger.mcp_usage_logs[2]["server_name"] == "server3"


class TestLogAgentAction:
    """Test log_agent_action method."""

    def test_log_agent_action_minimal(self):
        """Test logging agent action with minimal parameters."""
        logger = FlowExecutionLogger()

        logger.log_agent_action(
            action_type="file_created", description="Created test.py"
        )

        assert len(logger.actions_taken) == 1
        action_entry = logger.actions_taken[0]
        assert action_entry["action_type"] == "file_created"
        assert action_entry["description"] == "Created test.py"
        assert action_entry["details"] == {}
        assert action_entry["status"] == "completed"
        assert "timestamp" in action_entry

    def test_log_agent_action_with_details(self):
        """Test logging agent action with details."""
        logger = FlowExecutionLogger()

        logger.log_agent_action(
            action_type="command_executed",
            description="Ran npm install",
            details={"command": "npm install", "exit_code": 0},
            status="success",
        )

        action_entry = logger.actions_taken[0]
        assert action_entry["details"] == {"command": "npm install", "exit_code": 0}
        assert action_entry["status"] == "success"

    def test_log_multiple_agent_actions(self):
        """Test logging multiple agent actions."""
        logger = FlowExecutionLogger()

        logger.log_agent_action("file_created", "Created file 1")
        logger.log_agent_action("file_modified", "Modified file 2")
        logger.log_agent_action("api_called", "Called API endpoint")

        assert len(logger.actions_taken) == 3
        assert logger.actions_taken[0]["action_type"] == "file_created"
        assert logger.actions_taken[1]["action_type"] == "file_modified"
        assert logger.actions_taken[2]["action_type"] == "api_called"


class TestLogMilestone:
    """Test log_milestone method."""

    def test_log_milestone_minimal(self):
        """Test logging milestone with minimal parameters."""
        logger = FlowExecutionLogger()

        logger.log_milestone("agent_started")

        assert len(logger.milestones) == 1
        milestone_entry = logger.milestones[0]
        assert milestone_entry["milestone"] == "agent_started"
        assert milestone_entry["details"] == {}
        assert "timestamp" in milestone_entry

    def test_log_milestone_with_details(self):
        """Test logging milestone with details."""
        logger = FlowExecutionLogger()

        logger.log_milestone(
            "task_completed", details={"duration": 42.5, "status": "success"}
        )

        milestone_entry = logger.milestones[0]
        assert milestone_entry["details"] == {"duration": 42.5, "status": "success"}

    def test_log_multiple_milestones(self):
        """Test logging multiple milestones."""
        logger = FlowExecutionLogger()

        logger.log_milestone("agent_started")
        logger.log_milestone("processing_data")
        logger.log_milestone("task_completed")

        assert len(logger.milestones) == 3
        assert logger.milestones[0]["milestone"] == "agent_started"
        assert logger.milestones[1]["milestone"] == "processing_data"
        assert logger.milestones[2]["milestone"] == "task_completed"


class TestGetters:
    """Test getter methods."""

    def test_get_mcp_usage_logs(self):
        """Test retrieving MCP usage logs."""
        logger = FlowExecutionLogger()

        logger.log_mcp_tool_call("server1", "tool1", {})
        logger.log_mcp_tool_call("server2", "tool2", {})

        logs = logger.get_mcp_usage_logs()
        assert len(logs) == 2
        assert logs[0]["server_name"] == "server1"
        assert logs[1]["server_name"] == "server2"

    def test_get_actions_taken(self):
        """Test retrieving agent actions."""
        logger = FlowExecutionLogger()

        logger.log_agent_action("action1", "desc1")
        logger.log_agent_action("action2", "desc2")

        actions = logger.get_actions_taken()
        assert len(actions) == 2
        assert actions[0]["action_type"] == "action1"
        assert actions[1]["action_type"] == "action2"

    def test_get_milestones(self):
        """Test retrieving milestones."""
        logger = FlowExecutionLogger()

        logger.log_milestone("milestone1")
        logger.log_milestone("milestone2")

        milestones = logger.get_milestones()
        assert len(milestones) == 2
        assert milestones[0]["milestone"] == "milestone1"
        assert milestones[1]["milestone"] == "milestone2"


class TestGetSummary:
    """Test get_summary method."""

    def test_get_summary_empty(self):
        """Test summary with no logs."""
        logger = FlowExecutionLogger()

        summary = logger.get_summary()

        assert summary["total_mcp_calls"] == 0
        assert summary["successful_mcp_calls"] == 0
        assert summary["failed_mcp_calls"] == 0
        assert summary["total_actions"] == 0
        assert summary["milestones_reached"] == 0
        assert summary["last_milestone"] is None

    def test_get_summary_with_logs(self):
        """Test summary with various logs."""
        logger = FlowExecutionLogger()

        logger.log_mcp_tool_call("server1", "tool1", {}, status="success")
        logger.log_mcp_tool_call("server2", "tool2", {}, status="success")
        logger.log_mcp_tool_call("server3", "tool3", {}, status="failed")
        logger.log_agent_action("action1", "desc1")
        logger.log_agent_action("action2", "desc2")
        logger.log_milestone("milestone1")

        summary = logger.get_summary()

        assert summary["total_mcp_calls"] == 3
        assert summary["successful_mcp_calls"] == 2
        assert summary["failed_mcp_calls"] == 1
        assert summary["total_actions"] == 2
        assert summary["milestones_reached"] == 1
        assert summary["last_milestone"]["milestone"] == "milestone1"

    def test_get_summary_mixed_statuses(self):
        """Test summary with mixed MCP call statuses."""
        logger = FlowExecutionLogger()

        logger.log_mcp_tool_call("server1", "tool1", {}, status="success")
        logger.log_mcp_tool_call("server2", "tool2", {}, status="pending")
        logger.log_mcp_tool_call("server3", "tool3", {}, status="failed")
        logger.log_mcp_tool_call("server4", "tool4", {}, status="success")

        summary = logger.get_summary()

        assert summary["total_mcp_calls"] == 4
        assert summary["successful_mcp_calls"] == 2
        assert summary["failed_mcp_calls"] == 1


class TestParseAgentLogs:
    """Test parse_agent_logs method."""

    def test_parse_agent_logs_mcp_pattern(self):
        """Test parsing logs with MCP patterns."""
        logger = FlowExecutionLogger()

        log_lines = [
            "Calling MCP tool: preloop-mcp/search_issues with args: {...}",
            "MCP call completed: server-name/tool-name",
        ]

        logger.parse_agent_logs(log_lines)

        # Should detect MCP calls (exact extraction behavior may vary)
        assert len(logger.mcp_usage_logs) > 0

    def test_parse_agent_logs_file_creation_pattern(self):
        """Test parsing logs with file creation patterns."""
        logger = FlowExecutionLogger()

        log_lines = [
            "Created file: /path/to/test.py",
            "Successfully created file: src/components/Button.tsx",
        ]

        logger.parse_agent_logs(log_lines)

        assert len(logger.actions_taken) == 2
        assert logger.actions_taken[0]["action_type"] == "file_created"
        assert "/path/to/test.py" in logger.actions_taken[0]["description"]

    def test_parse_agent_logs_command_execution_pattern(self):
        """Test parsing logs with command execution patterns."""
        logger = FlowExecutionLogger()

        log_lines = [
            "Executed command: npm install",
            "Running: python setup.py install",
        ]

        logger.parse_agent_logs(log_lines)

        assert len(logger.actions_taken) == 2
        assert logger.actions_taken[0]["action_type"] == "command_executed"
        assert "npm install" in logger.actions_taken[0]["description"]

    def test_parse_agent_logs_mixed_patterns(self):
        """Test parsing logs with mixed patterns."""
        logger = FlowExecutionLogger()

        log_lines = [
            "Starting agent execution",
            "Created file: test.py",
            "Calling MCP tool: server/tool",
            "Executed command: ls -la",
            "Completed successfully",
        ]

        logger.parse_agent_logs(log_lines)

        # Should extract file creation, MCP call, and command execution
        assert len(logger.actions_taken) >= 2  # At least file and command
        assert len(logger.mcp_usage_logs) >= 1  # At least one MCP call

    def test_parse_agent_logs_no_matches(self):
        """Test parsing logs with no recognizable patterns."""
        logger = FlowExecutionLogger()

        log_lines = [
            "Some random log line",
            "Another unrelated message",
            "Nothing to extract here",
        ]

        logger.parse_agent_logs(log_lines)

        # Should not extract anything
        assert len(logger.mcp_usage_logs) == 0
        assert len(logger.actions_taken) == 0


class TestPrivateParsers:
    """Test private parser methods."""

    def test_try_extract_mcp_call_valid(self):
        """Test extracting MCP call from valid line."""
        logger = FlowExecutionLogger()

        logger._try_extract_mcp_call("Calling server-name/tool-name with args")

        assert len(logger.mcp_usage_logs) == 1
        # Exact parsing behavior depends on implementation
        assert logger.mcp_usage_logs[0]["status"] == "detected"
        assert logger.mcp_usage_logs[0]["server_name"] == "server-name"
        assert logger.mcp_usage_logs[0]["tool_name"] == "tool-name"

    def test_try_extract_mcp_call_ignores_url_version_suffix(self):
        """URL fragments like /v1 should not be treated as tool calls."""
        logger = FlowExecutionLogger()

        logger._try_extract_mcp_call("POST https://example.com/mcp/v1")

        assert logger.mcp_usage_logs == []

    def test_try_extract_mcp_call_invalid(self):
        """Test extracting MCP call from invalid line."""
        logger = FlowExecutionLogger()

        # Should not crash on malformed lines
        logger._try_extract_mcp_call("No slashes here")
        logger._try_extract_mcp_call("/single/slash/")
        logger._try_extract_mcp_call("")

        # May or may not extract depending on implementation
        # Just ensure it doesn't crash

    def test_try_extract_file_creation_valid(self):
        """Test extracting file creation from valid line."""
        logger = FlowExecutionLogger()

        logger._try_extract_file_creation("Created file: /path/to/file.py")

        assert len(logger.actions_taken) == 1
        assert logger.actions_taken[0]["action_type"] == "file_created"
        assert "/path/to/file.py" in logger.actions_taken[0]["description"]

    def test_try_extract_file_creation_invalid(self):
        """Test extracting file creation from invalid line."""
        logger = FlowExecutionLogger()

        # Should not crash on malformed lines
        logger._try_extract_file_creation("No colon here")
        logger._try_extract_file_creation("")

        # May or may not extract depending on implementation

    def test_try_extract_command_execution_valid(self):
        """Test extracting command execution from valid line."""
        logger = FlowExecutionLogger()

        logger._try_extract_command_execution("Executed command: npm install")

        assert len(logger.actions_taken) == 1
        assert logger.actions_taken[0]["action_type"] == "command_executed"
        assert "npm install" in logger.actions_taken[0]["description"]

    def test_try_extract_command_execution_invalid(self):
        """Test extracting command execution from invalid line."""
        logger = FlowExecutionLogger()

        # Should not crash on malformed lines
        logger._try_extract_command_execution("No colon here")
        logger._try_extract_command_execution("")

        # May or may not extract depending on implementation


class TestLogAgentOutputScrubbing:
    """Agent output becomes ``model_output_summary``, which the issue #173
    report names as one of the places the PAT was visible.
    """

    PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"

    def test_leaked_remote_line_is_scrubbed(self):
        logger = FlowExecutionLogger()
        logger.log_agent_output(
            f"origin\thttps://{self.PAT}@github.com/acme/private.git (fetch)"
        )

        assert self.PAT not in logger.get_agent_output_lines()[0]
        assert "[REDACTED]" in logger.get_agent_output_lines()[0]

    def test_summary_carries_no_token(self):
        logger = FlowExecutionLogger()
        logger.log_agent_output("$ git remote -v")
        logger.log_agent_output(
            f"origin\thttps://{self.PAT}@github.com/acme/private.git (push)"
        )

        assert self.PAT not in logger.get_agent_output_summary()

    def test_clean_output_is_unchanged(self):
        logger = FlowExecutionLogger()
        logger.log_agent_output("Running tests...")

        assert logger.get_agent_output_lines() == ["Running tests..."]

    def test_empty_line_is_preserved(self):
        """The summary is joined by newline, so blank lines must survive."""
        logger = FlowExecutionLogger()
        logger.log_agent_output("")

        assert logger.get_agent_output_lines() == [""]

    def test_token_only_line_keeps_redaction_marker(self):
        """A line that is nothing but a token must scrub to the prefixed
        ``github_pat_[REDACTED]`` form, not collapse to the empty string.

        This locks in that the ``or ""`` fallback in ``log_agent_output`` only
        guards against ``None``; it must never swallow the redaction marker.
        """
        logger = FlowExecutionLogger()
        logger.log_agent_output(self.PAT)

        assert logger.get_agent_output_lines() == ["github_pat_[REDACTED]"]
