"""Tests for container agent executor."""

import json
import pathlib
import subprocess
import sys
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.agents.container import (
    COMMIT_PR_BODY_FILE,
    COMMIT_PR_LIST_FILE,
    COMMIT_PR_TITLE_FILE,
    FLOW_PR_BODY_FILE,
    FLOW_PR_TITLE_FILE,
    WORKSPACE_PROBE_MARKER,
    WORKSPACE_PROBE_REPO_LIST,
    WORKSPACE_PROGRESS_PROBE_SCRIPT,
    WRITE_PR_PAYLOAD_PY,
    ContainerAgentExecutor,
    _validated_git_ref,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def container_executor():
    """Create a ContainerAgentExecutor instance for testing."""
    return ContainerAgentExecutor(
        agent_type="codex",
        config={"test": True},
        image="test-image:latest",
        use_kubernetes=False,
    )


@pytest.fixture
def kubernetes_executor():
    """Create a ContainerAgentExecutor instance for Kubernetes testing."""
    return ContainerAgentExecutor(
        agent_type="codex",
        config={"test": True},
        image="test-image:latest",
        use_kubernetes=True,
    )


@pytest.fixture
def sample_execution_context():
    """Sample execution context for testing."""
    return {
        "flow_id": str(uuid.uuid4()),
        "execution_id": str(uuid.uuid4()),
        "prompt": "Test prompt",
        "agent_config": {},
        "model_api_key": "test-key",
        "model_identifier": "gpt-5.4",
        "model_provider": "openai",
    }


class TestDetectErrorInLogs:
    """Tests for _detect_error_in_logs method."""

    def test_empty_logs_no_error(self, container_executor):
        """Test that empty logs don't indicate error."""
        result = container_executor._detect_error_in_logs("")
        assert result is False

    def test_normal_logs_no_error(self, container_executor):
        """Test that normal logs don't indicate error."""
        logs = """
        Starting agent execution...
        Processing request...
        Task completed successfully.
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    # Critical error patterns
    def test_litellm_bad_request_error(self, container_executor):
        """Test detection of LiteLLM BadRequestError."""
        logs = "litellm.BadRequestError: Invalid model"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_litellm_authentication_error(self, container_executor):
        """Test detection of LiteLLM AuthenticationError."""
        logs = "litellm.AuthenticationError: Invalid API key"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_litellm_rate_limit_error(self, container_executor):
        """Test detection of LiteLLM RateLimitError."""
        logs = "litellm.RateLimitError: Rate limit exceeded"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_openai_exception(self, container_executor):
        """Test detection of OpenAI exception."""
        logs = "OpenAIException: Connection failed"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_anthropic_exception(self, container_executor):
        """Test detection of Anthropic exception."""
        logs = "AnthropicException: Service unavailable"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_python_traceback(self, container_executor):
        """Test detection of Python traceback."""
        logs = """
        Processing...
        Traceback (most recent call last):
          File "main.py", line 10, in <module>
            raise ValueError("Error")
        ValueError: Error
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_fatal_error(self, container_executor):
        """Test detection of fatal error."""
        logs = "fatal error: system failure"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_critical_error(self, container_executor):
        """Test detection of critical error level."""
        logs = "CRITICAL: Database connection failed"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_agent_execution_failed(self, container_executor):
        """Test detection of agent execution failed message."""
        logs = "Agent execution failed: timeout"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_unhandled_exception(self, container_executor):
        """Test detection of unhandled exception."""
        logs = "Unhandled exception in main thread"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    # Benign patterns that should NOT trigger errors
    def test_no_commits_is_benign(self, container_executor):
        """Test that 'no commits' message is benign."""
        logs = """
        Checking git status...
        No commits to push
        ERROR: no commits on branch
        Task completed.
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    def test_skipping_push_is_benign(self, container_executor):
        """Test that 'skipping push' message is benign."""
        logs = """
        Git status: clean
        Skipping push - no changes
        ERROR: nothing to push
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    def test_nothing_to_commit_is_benign(self, container_executor):
        """Test that 'nothing to commit' message is benign."""
        logs = """
        Analyzing repository...
        Nothing to commit, working tree clean
        ERROR: nothing to commit
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    def test_no_changes_is_benign(self, container_executor):
        """Test that 'no changes' message is benign."""
        logs = """
        Checking for changes...
        No changes detected
        ERROR: no changes found
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    def test_up_to_date_is_benign(self, container_executor):
        """Test that 'up to date' message is benign."""
        logs = """
        Pulling latest...
        Already up to date
        ERROR: already up to date
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    def test_up_to_date_hyphenated_is_benign(self, container_executor):
        """Test that 'up-to-date' message is benign."""
        logs = """
        Repository is up-to-date
        Everything up-to-date
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    def test_pr_already_exists_is_benign(self, container_executor):
        """Test that PR already exists error is benign."""
        logs = """
        Creating pull request...
        Failed to create PR (may already exist)
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    def test_mr_already_exists_is_benign(self, container_executor):
        """Test that MR already exists error is benign."""
        logs = """
        Creating merge request...
        Failed to create MR (may already exist)
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    # Edge cases for error counts
    def test_multiple_errors_without_benign_pattern(self, container_executor):
        """Test that multiple ERROR: lines without benign patterns indicate failure."""
        logs = """
        ERROR: first error
        ERROR: second error
        ERROR: third error
        Some other stuff
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_single_error_with_benign_pattern(self, container_executor):
        """Test that a single ERROR with benign context is benign."""
        logs = """
        Nothing to commit
        ERROR: no changes to commit
        """
        result = container_executor._detect_error_in_logs(logs)
        assert result is False

    def test_case_insensitive_detection(self, container_executor):
        """Test that error detection is case-insensitive."""
        logs = "LITELLM.BADREQUESTERROR: invalid request"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True

    def test_mixed_case_fatal_error(self, container_executor):
        """Test mixed case fatal error detection."""
        logs = "Fatal Error: system crash"
        result = container_executor._detect_error_in_logs(logs)
        assert result is True


class TestExtractErrorFromLogs:
    """Tests for _extract_error_from_logs method."""

    def test_empty_logs_returns_empty(self, container_executor):
        """Test that empty logs return empty string."""
        result = container_executor._extract_error_from_logs("")
        assert result == ""

    def test_no_error_pattern_returns_last_lines(self, container_executor):
        """Test that logs without explicit errors return last lines as context.

        This is useful because when an execution fails, the last few lines
        often contain relevant context even if they don't match error patterns.
        """
        logs = """
        Starting process...
        Processing complete.
        Unexpected termination!
        """
        result = container_executor._extract_error_from_logs(logs)
        # Should return last content lines as fallback context
        assert "Unexpected termination" in result

    def test_extracts_error_context(self, container_executor):
        """Test that error context is extracted."""
        logs = """line 1
line 2
line 3
error occurred here
line 5
line 6
line 7
line 8
"""
        result = container_executor._extract_error_from_logs(logs)
        assert "error occurred here" in result
        # Should include context lines
        assert "line 2" in result or "line 3" in result

    def test_extracts_exception_context(self, container_executor):
        """Test extraction of exception context."""
        logs = """
        Starting...
        Processing data...
        Exception: Something went wrong
        Cleanup started...
        """
        result = container_executor._extract_error_from_logs(logs)
        assert "Exception: Something went wrong" in result

    def test_extracts_failed_message(self, container_executor):
        """Test extraction of failed message."""
        logs = """
        Step 1 complete
        Step 2 complete
        Step 3 failed with error
        Attempting recovery
        """
        result = container_executor._extract_error_from_logs(logs)
        assert "failed" in result

    def test_extracts_fatal_context(self, container_executor):
        """Test extraction of fatal error context."""
        logs = """
        Initializing...
        Fatal: Out of memory
        Shutting down
        """
        result = container_executor._extract_error_from_logs(logs)
        assert "Fatal" in result

    def test_filters_status_lines_and_finds_real_error(self, container_executor):
        """Test that status lines are filtered and error is found from end.

        This tests the real-world scenario where agent output ends with
        status updates but the actual error is right before them.
        """
        logs = """Processing PR description...
Some unrelated content about the PR
ERROR: Quota exceeded. Check your plan and billing details.
[Agent Status]
{"status":"RUNNING","elapsed":50}
[Agent Status]
{"status":"FAILED","elapsed":55}
[Status Update]
Status: FAILED"""
        result = container_executor._extract_error_from_logs(logs)
        # Should find the actual error, not the status lines
        assert "Quota exceeded" in result
        assert "Check your plan and billing details" in result
        # Status lines should be filtered out
        assert "[Agent Status]" not in result
        assert '{"status":"' not in result

    def test_prioritizes_explicit_error_prefix(self, container_executor):
        """Test that ERROR: prefixed lines are prioritized."""
        logs = """This line has the word error in it but isn't an error
Processing failed to complete quickly (just informational)
ERROR: This is the actual error message
More context here"""
        result = container_executor._extract_error_from_logs(logs)
        # Should prioritize the explicit ERROR: line
        assert "This is the actual error message" in result

    def test_finds_error_from_end_not_beginning(self, container_executor):
        """Test that errors at end of logs are found first."""
        logs = """error: some early warning
Lots of normal processing output
More normal output
Final processing step
ERROR: This is the final real error"""
        result = container_executor._extract_error_from_logs(logs)
        # Should find the error from the end
        assert "This is the final real error" in result


class TestGetStatus:
    """Tests for get_status method with Docker."""

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_status_running(self, mock_get_client, container_executor):
        """Test getting running status from container."""
        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {
            "State": {"Running": True, "Status": "running"}
        }
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_status("container-123")

        assert result == AgentStatus.RUNNING

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_status_starting(self, mock_get_client, container_executor):
        """Test getting starting status from container."""
        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {
            "State": {"Running": False, "Status": "created"}
        }
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_status("container-123")

        assert result == AgentStatus.STARTING

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_status_succeeded(self, mock_get_client, container_executor):
        """Test getting succeeded status from container."""
        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {
            "State": {"Running": False, "Status": "exited", "ExitCode": 0}
        }
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_status("container-123")

        assert result == AgentStatus.SUCCEEDED

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_status_failed_nonzero_exit(
        self, mock_get_client, container_executor
    ):
        """Test getting failed status from container with non-zero exit."""
        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {
            "State": {"Running": False, "Status": "exited", "ExitCode": 1}
        }
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_status("container-123")

        assert result == AgentStatus.FAILED

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_status_stopped(self, mock_get_client, container_executor):
        """Test getting stopped status from container."""
        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {
            "State": {"Running": False, "Status": "stopped"}
        }
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_status("container-123")

        assert result == AgentStatus.STOPPED

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_status_docker_error(self, mock_get_client, container_executor):
        """Test handling Docker error when getting status."""
        from aiodocker.exceptions import DockerError

        mock_docker = AsyncMock()
        mock_docker.containers.get.side_effect = DockerError(
            404, {"message": "Not found"}
        )
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_status("container-123")

        assert result == AgentStatus.FAILED


class TestGetResult:
    """Tests for get_result method."""

    @patch.object(ContainerAgentExecutor, "get_logs")
    @patch.object(ContainerAgentExecutor, "get_status")
    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_result_succeeded(
        self, mock_get_client, mock_get_status, mock_get_logs, container_executor
    ):
        """Test getting result from successful container."""
        mock_get_status.return_value = AgentStatus.SUCCEEDED
        mock_get_logs.return_value = ["Log line 1", "Log line 2", "Success"]

        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {"State": {"ExitCode": 0, "Error": ""}}
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_result("container-123")

        assert isinstance(result, AgentExecutionResult)
        assert result.status == AgentStatus.SUCCEEDED
        assert result.exit_code == 0
        assert result.error_message is None

    @patch.object(ContainerAgentExecutor, "get_logs")
    @patch.object(ContainerAgentExecutor, "get_status")
    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_result_failed_with_error(
        self, mock_get_client, mock_get_status, mock_get_logs, container_executor
    ):
        """Test getting result from failed container."""
        mock_get_status.return_value = AgentStatus.FAILED
        mock_get_logs.return_value = ["Error: Something went wrong"]

        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {
            "State": {"ExitCode": 1, "Error": "Container crashed"}
        }
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_result("container-123")

        assert result.status == AgentStatus.FAILED
        assert result.exit_code == 1
        assert result.error_message is not None

    @patch.object(ContainerAgentExecutor, "get_logs")
    @patch.object(ContainerAgentExecutor, "get_status")
    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_result_succeeded_but_logs_show_error(
        self, mock_get_client, mock_get_status, mock_get_logs, container_executor
    ):
        """Test that exit code 0 with critical errors in logs is marked FAILED."""
        mock_get_status.return_value = AgentStatus.SUCCEEDED
        mock_get_logs.return_value = [
            "Starting agent...",
            "litellm.AuthenticationError: Invalid API key",
            "Exiting",
        ]

        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {"State": {"ExitCode": 0, "Error": ""}}
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_result("container-123")

        # Should be marked as failed due to critical error in logs
        assert result.status == AgentStatus.FAILED

    @patch.object(ContainerAgentExecutor, "get_logs")
    @patch.object(ContainerAgentExecutor, "get_status")
    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_result_succeeded_with_benign_error_messages(
        self, mock_get_client, mock_get_status, mock_get_logs, container_executor
    ):
        """Test that exit code 0 with benign 'error' messages remains SUCCEEDED."""
        mock_get_status.return_value = AgentStatus.SUCCEEDED
        mock_get_logs.return_value = [
            "Checking repository...",
            "No commits to push",
            "ERROR: nothing to commit",
            "Task completed successfully",
        ]

        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.show.return_value = {"State": {"ExitCode": 0, "Error": ""}}
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_result("container-123")

        # Should remain succeeded because the error is benign
        assert result.status == AgentStatus.SUCCEEDED

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_result_docker_error(self, mock_get_client, container_executor):
        """Test handling Docker error when getting result."""
        from aiodocker.exceptions import DockerError

        mock_docker = AsyncMock()
        mock_docker.containers.get.side_effect = DockerError(
            404, {"message": "Not found"}
        )
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_result("container-123")

        assert result.status == AgentStatus.FAILED
        assert result.error_message is not None


class TestGetLogs:
    """Tests for get_logs method."""

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_get_logs_success(self, mock_get_client, container_executor):
        """Test successful log retrieval."""
        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.log.return_value = [
            "Line 1",
            "Line 2",
            "Line 3",
        ]
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_logs("container-123", tail=100)

        assert len(result) == 3
        assert "Line 1" in result

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_get_logs_handles_bytes(self, mock_get_client, container_executor):
        """Test that bytes logs are decoded properly."""
        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.log.return_value = [
            b"Line 1",
            b"Line 2",
        ]
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_logs("container-123")

        assert len(result) == 2
        assert result[0] == "Line 1"
        assert result[1] == "Line 2"

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_get_logs_docker_error(self, mock_get_client, container_executor):
        """Test handling Docker error when getting logs."""
        from aiodocker.exceptions import DockerError

        mock_docker = AsyncMock()
        mock_docker.containers.get.side_effect = DockerError(
            404, {"message": "Not found"}
        )
        mock_get_client.return_value = mock_docker

        result = await container_executor.get_logs("container-123")

        assert result == []


class TestKubernetesStatus:
    """Tests for Kubernetes status detection."""

    @patch.object(ContainerAgentExecutor, "_init_kubernetes_clients")
    async def test_k8s_status_running(self, mock_init, kubernetes_executor):
        """Test Kubernetes job running status."""
        mock_init.return_value = None
        kubernetes_executor._k8s_batch_api = AsyncMock()

        mock_job = MagicMock()
        mock_job.status.active = 1
        mock_job.status.succeeded = None
        mock_job.status.failed = None
        kubernetes_executor._k8s_batch_api.read_namespaced_job_status.return_value = (
            mock_job
        )

        result = await kubernetes_executor._get_kubernetes_status("job-123")

        assert result == AgentStatus.RUNNING

    @patch.object(ContainerAgentExecutor, "_init_kubernetes_clients")
    async def test_k8s_status_succeeded(self, mock_init, kubernetes_executor):
        """Test Kubernetes job succeeded status."""
        mock_init.return_value = None
        kubernetes_executor._k8s_batch_api = AsyncMock()

        mock_job = MagicMock()
        mock_job.status.active = None
        mock_job.status.succeeded = 1
        mock_job.status.failed = None
        kubernetes_executor._k8s_batch_api.read_namespaced_job_status.return_value = (
            mock_job
        )

        result = await kubernetes_executor._get_kubernetes_status("job-123")

        assert result == AgentStatus.SUCCEEDED

    @patch.object(ContainerAgentExecutor, "_init_kubernetes_clients")
    async def test_k8s_status_failed(self, mock_init, kubernetes_executor):
        """Test Kubernetes job failed status."""
        mock_init.return_value = None
        kubernetes_executor._k8s_batch_api = AsyncMock()

        mock_job = MagicMock()
        mock_job.status.active = None
        mock_job.status.succeeded = None
        mock_job.status.failed = 1
        kubernetes_executor._k8s_batch_api.read_namespaced_job_status.return_value = (
            mock_job
        )

        result = await kubernetes_executor._get_kubernetes_status("job-123")

        assert result == AgentStatus.FAILED

    @patch.object(ContainerAgentExecutor, "_init_kubernetes_clients")
    async def test_k8s_status_starting(self, mock_init, kubernetes_executor):
        """Test Kubernetes job starting status."""
        mock_init.return_value = None
        kubernetes_executor._k8s_batch_api = AsyncMock()

        mock_job = MagicMock()
        mock_job.status.active = None
        mock_job.status.succeeded = None
        mock_job.status.failed = None
        kubernetes_executor._k8s_batch_api.read_namespaced_job_status.return_value = (
            mock_job
        )

        result = await kubernetes_executor._get_kubernetes_status("job-123")

        assert result == AgentStatus.STARTING


class TestPrepareInitCommands:
    """Tests for _prepare_init_commands method."""

    def test_no_git_config_returns_empty(self, container_executor):
        """Test that no git config returns empty string."""
        context = {"flow_id": "123", "execution_id": "456"}
        result = container_executor._prepare_init_commands(context)
        assert result == ""

    def test_empty_repositories_returns_empty(self, container_executor):
        """Test that empty repositories returns empty string."""
        context = {
            "flow_id": "123",
            "execution_id": "456",
            "git_clone_config": {"repositories": []},
        }
        result = container_executor._prepare_init_commands(context)
        assert result == ""

    def test_custom_commands_added(self, container_executor):
        """Test that custom commands are added."""
        context = {
            "flow_id": "123",
            "execution_id": "456",
            "custom_commands": {
                "enabled": True,
                "commands": ["npm install", "npm run build"],
            },
        }
        result = container_executor._prepare_init_commands(context)
        assert "npm install" in result
        assert "npm run build" in result


class TestWorkspaceSeedCommands:
    """Tests for trigger-payload workspace_files materialization."""

    @staticmethod
    def _context(workspace_files):
        return {
            "flow_id": "123",
            "execution_id": "456",
            "trigger_event_data": {
                "source": "webhook",
                "payload": {"workspace_files": workspace_files},
            },
        }

    def test_no_workspace_files_returns_empty(self, container_executor):
        """No workspace_files in the payload adds no commands."""
        context = {
            "flow_id": "123",
            "execution_id": "456",
            "trigger_event_data": {"payload": {"x": 1}},
        }
        assert container_executor._prepare_init_commands(context) == ""

    def test_seed_commands_write_files(self, container_executor):
        """workspace_files become base64-decode writes under /workspace."""
        import base64

        content = base64.b64encode(b'{"fixture": true}').decode("ascii")
        context = self._context(
            [{"path": "fixtures/input.json", "content_base64": content}]
        )
        result = container_executor._prepare_init_commands(context)
        assert "w0=/workspace" in result
        assert "__pl_seed fixtures/input.json" in result
        assert "base64 -d" in result
        # Content is referenced by environment variable, never inlined: the
        # launch command is one execve string capped at MAX_ARG_STRLEN, and
        # inlining made the seed budget share 128 KiB with the rendered
        # prompt (preloop/preloop#505).
        assert content not in result
        assert '"$PRELOOP_WORKSPACE_SEED_0"' in result
        # Runtime symlink-containment guard travels with the block.
        assert "cd -P" in result

    def test_seed_content_travels_in_the_environment(self, container_executor):
        """The env carries what the command no longer does."""
        import base64

        content = base64.b64encode(b'{"fixture": true}').decode("ascii")
        context = self._context(
            [{"path": "fixtures/input.json", "content_base64": content}]
        )
        env = container_executor._apply_git_credential_env({}, context)
        assert env["PRELOOP_WORKSPACE_SEED_0"] == content

    def test_no_seed_env_when_nothing_is_declared(self, container_executor):
        context = {
            "flow_id": "123",
            "execution_id": "456",
            "trigger_event_data": {"payload": {"x": 1}},
        }
        env = container_executor._apply_git_credential_env({}, context)
        assert not [key for key in env if key.startswith("PRELOOP_WORKSPACE_SEED_")]

    def test_command_size_does_not_grow_with_seed_size(self, container_executor):
        """The regression that made a 128 KiB budget shared with the prompt."""
        import base64

        small = base64.b64encode(b"x").decode("ascii")
        large = base64.b64encode(b"x" * (64 * 1024)).decode("ascii")
        small_cmd = container_executor._prepare_init_commands(
            self._context([{"path": "a.bin", "content_base64": small}])
        )
        large_cmd = container_executor._prepare_init_commands(
            self._context([{"path": "a.bin", "content_base64": large}])
        )
        assert small_cmd == large_cmd

    def test_seed_commands_run_after_custom_setup_ordering(self, container_executor):
        """Seeds are written before custom commands so they can be consumed."""
        import base64

        content = base64.b64encode(b"data").decode("ascii")
        context = self._context([{"path": "seed.txt", "content_base64": content}])
        context["custom_commands"] = {
            "enabled": True,
            "commands": ["cat seed.txt"],
        }
        result = container_executor._prepare_init_commands(context)
        assert result.index("__pl_seed seed.txt") < result.index("cat seed.txt")

    def test_traversal_path_raises_before_any_command(self, container_executor):
        """Defense-in-depth: unvalidated traversal paths must raise, not run."""
        from preloop.utils.workspace_seed import WorkspaceSeedError

        context = self._context(
            [{"path": "../../etc/cron.d/evil", "content_base64": "eA=="}]
        )
        with pytest.raises(WorkspaceSeedError):
            container_executor._prepare_init_commands(context)


class TestExtractBranchFromTrigger:
    """Tests for branch extraction helpers used during git clone."""

    def test_extract_source_branch_from_gitlab_mr(self, container_executor):
        """GitLab MR payloads should expose the source branch."""
        trigger_data = {
            "payload": {
                "object_attributes": {
                    "source_branch": "feature/foo",
                    "target_branch": "main",
                }
            }
        }
        assert (
            container_executor._extract_source_branch_from_trigger(trigger_data)
            == "feature/foo"
        )

    def test_extract_target_branch_from_gitlab_mr(self, container_executor):
        """GitLab MR payloads should expose the target branch."""
        trigger_data = {
            "payload": {
                "object_attributes": {
                    "source_branch": "control-plane",
                    "target_branch": "main",
                }
            }
        }
        assert (
            container_executor._extract_target_branch_from_trigger(trigger_data)
            == "main"
        )

    def test_extract_target_branch_from_github_pr(self, container_executor):
        """GitHub PR payloads should expose the base branch."""
        trigger_data = {
            "payload": {
                "pull_request": {
                    "head": {"ref": "feature/foo"},
                    "base": {"ref": "develop"},
                }
            }
        }
        assert (
            container_executor._extract_target_branch_from_trigger(trigger_data)
            == "develop"
        )

    def test_git_clone_uses_target_branch_when_commit_sha_present(
        self, container_executor
    ):
        """MR review clones should avoid unavailable source branch refs."""
        context = {
            "flow_id": "flow-1",
            "execution_id": "exec-12345678",
            "flow_name": "Merge Request Reviewer",
            "trigger_project_id": "project-1",
            "trigger_event_data": {
                "payload": {
                    "object_attributes": {
                        "source_branch": "control-plane",
                        "target_branch": "main",
                        "last_commit": {
                            "id": "91f578b058c4d0426067f7f28632d77d2c2c374b"
                        },
                    },
                    "project": {
                        "http_url": "https://gitlab.example.com/group/repo.git"
                    },
                }
            },
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": "https://gitlab.example.com/group/repo.git",
                        "clone_path": "/workspace",
                    }
                ],
            },
        }

        with patch.object(
            container_executor,
            "_get_token_from_project",
            return_value=(None, None),
        ):
            command = container_executor._prepare_git_clone_command(context)

        assert "git clone -b main" in command
        assert "git clone -b control-plane" not in command
        assert "91f578b058c4d0426067f7f28632d77d2c2c374b" in command
        assert "refs/merge-requests/2/head" not in command  # no iid in payload

    def test_git_clone_fetches_gitlab_mr_ref_when_iid_present(self, container_executor):
        """GitLab MR review should fetch merge request refs for commit checkout."""
        context = {
            "flow_id": "flow-1",
            "execution_id": "exec-12345678",
            "flow_name": "Merge Request Reviewer",
            "trigger_event_data": {
                "payload": {
                    "object_attributes": {
                        "iid": 2,
                        "source_branch": "control-plane",
                        "target_branch": "main",
                        "last_commit": {
                            "id": "91f578b058c4d0426067f7f28632d77d2c2c374b"
                        },
                    },
                    "project": {
                        "http_url": "https://gitlab.example.com/group/repo.git"
                    },
                }
            },
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": "https://gitlab.example.com/group/repo.git",
                        "clone_path": "/workspace",
                    }
                ],
            },
        }

        command = container_executor._prepare_git_clone_command(context)

        assert "refs/merge-requests/2/head:preloop-mr-head" in command
        assert "FATAL ERROR: Could not checkout commit" in command
        # On failure we must re-run git WITH stderr so the log shows why.
        assert "--- diagnostics ---" in command
        assert "git fetch origin" in command
        assert "git for-each-ref" in command


class TestGitShellQuoting:
    """Tests for safe shell quoting in git setup commands."""

    def test_git_global_setup_quotes_user_identity(self, container_executor):
        malicious_name = 'evil"; rm -rf / #'
        commands = container_executor._build_git_global_setup_commands(
            malicious_name, 'also"; evil #@example.com'
        )
        assert (
            "git -c safe.directory='*' config --global user.name 'evil\"; rm -rf / #'"
        ) in commands
        assert (
            "git -c safe.directory='*' config --global user.email "
            "'also\"; evil #@example.com'"
        ) in commands
        assert "git -c safe.directory='*' config --global --add safe.directory '*'" in (
            commands
        )

    def test_clone_trusts_root_owned_workspace_before_checkout(
        self, container_executor
    ):
        """A root-owned fsGroup mount must be trusted before commit checkout.

        ``git clone`` into that directory succeeds. The next git command
        fails with dubious ownership unless ``safe.directory`` was written
        first. Non-root harnesses (DeepSeek, Pi) hit this on Kubernetes.
        """
        context = {
            "flow_id": "flow-1",
            "execution_id": "exec-12345678",
            "flow_name": "Pull Request Reviewer",
            "trigger_event_data": {
                "payload": {
                    "pull_request": {
                        "head": {
                            "ref": "feature/foo",
                            "sha": "3a22977b0a7dea6f672720ddb56a6331e10a53f8",
                        },
                        "base": {"ref": "main"},
                    },
                    "repository": {
                        "clone_url": "https://github.com/example/repo.git",
                    },
                }
            },
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": "https://github.com/example/repo.git",
                        "clone_path": "/workspace",
                    }
                ],
            },
        }

        command = container_executor._prepare_git_clone_command(context)
        trust = "git -c safe.directory='*' config --global --add safe.directory '*'"
        clone_at = command.index("git clone")
        checkout_at = command.index("Checking out specific commit")
        assert command.index(trust) < clone_at < checkout_at

    def test_git_clone_shell_quotes_repo_url_and_branch(self, container_executor):
        import shlex

        repo_url = "https://example.com/repo.git; echo pwned"
        full_path = "/workspace/repo"
        clone_branch = "main; echo pwned"
        shell = container_executor._build_git_clone_shell(
            repo_url, full_path, clone_branch
        )
        assert (
            f"git clone -b {shlex.quote(clone_branch)} "
            f"{shlex.quote(repo_url)} {shlex.quote(full_path)}"
        ) in shell

    def test_git_pre_clone_replaces_unwritable_target(
        self, container_executor, tmp_path
    ):
        """CRI workingDir is created as root; UID 10000 cannot mkdir .git inside it."""
        import os
        import shlex
        import shutil
        import tempfile

        target = tmp_path / "workspace"
        target.mkdir()
        shell = container_executor._build_git_pre_clone_shell(str(target))
        quoted = shlex.quote(str(target))
        assert f"[ ! -w {quoted} ]" in shell
        assert f"rm -rf {quoted}" in shell
        # Empty, like CRI workingDir. 0555 so a non-root user cannot mkdir .git.
        # Root always passes bash -w (CI job containers), so drop privileges.
        if os.geteuid() == 0:
            setpriv = shutil.which("setpriv")
            probe = (
                subprocess.run(
                    [
                        setpriv,
                        "--reuid=65534",
                        "--regid=65534",
                        "--clear-groups",
                        "true",
                    ]
                )
                if setpriv
                else None
            )
            if probe is None or probe.returncode != 0:
                pytest.skip("root always passes [ -w ]; cannot drop privileges")
            # /tmp is 1777: world-traversable, unlike pytest's 0700 basetemp.
            workdir = pathlib.Path(
                tempfile.mkdtemp(prefix="preloop-preclone-", dir="/tmp")
            )
            workdir.chmod(0o777)
            try:
                target = workdir / "workspace"
                target.mkdir()
                target.chmod(0o555)
                subprocess.run(
                    [
                        setpriv,
                        "--reuid=65534",
                        "--regid=65534",
                        "--clear-groups",
                        "bash",
                        "-c",
                        container_executor._build_git_pre_clone_shell(str(target)),
                    ],
                    check=True,
                )
                assert not target.exists()
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            return

        target.chmod(0o555)
        subprocess.run(["bash", "-c", shell], check=True)
        assert not target.exists()

    def test_git_branch_setup_shell_quotes_trigger_derived_values(
        self, container_executor
    ):
        import shlex

        malicious_branch = "main; echo pwned #"
        malicious_sha = "deadbeef; echo pwned #"
        malicious_path = "/workspace/repo; echo pwned"
        shell = container_executor._build_git_branch_setup_shell(
            full_path=malicious_path,
            commit_sha=malicious_sha,
            source_branch=malicious_branch,
            target_branch=malicious_branch,
            trigger_data={
                "payload": {
                    "object_attributes": {"iid": 2},
                }
            },
        )
        q_branch = shlex.quote(malicious_branch)
        q_sha = shlex.quote(malicious_sha)
        q_path = shlex.quote(malicious_path)
        assert f"cd {q_path}" in shell
        assert f"git checkout {q_sha}" in shell
        assert f"git fetch origin {q_branch}:preloop-source-head" in shell
        assert "git fetch origin refs/merge-requests/2/head:preloop-mr-head" in shell
        assert f"git checkout -b {q_branch}" in shell

    def test_git_clone_validation_shell_quotes_trigger_derived_values(
        self, container_executor
    ):
        import shlex

        malicious_branch = "main; echo pwned #"
        malicious_path = "/workspace/repo; echo pwned"
        shell = container_executor._build_git_clone_validation_shell(
            full_path=malicious_path,
            source_branch=malicious_branch,
            target_branch=malicious_branch,
            commit_sha="abc123",
        )
        assert f"[ ! -d {shlex.quote(malicious_path)} ]" in shell
        assert f"[ ! -d {shlex.quote(malicious_path + '/.git')} ]" in shell
        assert f"Branch: {shlex.quote(malicious_branch)}" in shell


class TestExtractRepoUrlFromTrigger:
    """Tests for _extract_repo_url_from_trigger method."""

    def test_github_repository_structure(self, container_executor):
        """Test extraction from GitHub repository structure."""
        trigger_data = {
            "repository": {
                "clone_url": "https://github.com/owner/repo.git",
                "html_url": "https://github.com/owner/repo",
            }
        }
        result = container_executor._extract_repo_url_from_trigger(trigger_data)
        assert result == "https://github.com/owner/repo.git"

    def test_github_repository_fallback_to_html_url(self, container_executor):
        """Test fallback to html_url when clone_url is missing."""
        trigger_data = {
            "repository": {
                "html_url": "https://github.com/owner/repo",
            }
        }
        result = container_executor._extract_repo_url_from_trigger(trigger_data)
        assert result == "https://github.com/owner/repo"

    def test_gitlab_project_structure(self, container_executor):
        """Test extraction from GitLab project structure."""
        trigger_data = {
            "project": {
                "http_url_to_repo": "https://gitlab.com/group/project.git",
                "web_url": "https://gitlab.com/group/project",
            }
        }
        result = container_executor._extract_repo_url_from_trigger(trigger_data)
        assert result == "https://gitlab.com/group/project.git"

    def test_gitlab_project_fallback_to_web_url(self, container_executor):
        """Test fallback to web_url when http_url_to_repo is missing."""
        trigger_data = {
            "project": {
                "web_url": "https://gitlab.com/group/project",
            }
        }
        result = container_executor._extract_repo_url_from_trigger(trigger_data)
        assert result == "https://gitlab.com/group/project"

    def test_empty_trigger_data(self, container_executor):
        """Test handling empty trigger data."""
        result = container_executor._extract_repo_url_from_trigger({})
        assert result == ""

    def test_invalid_structure(self, container_executor):
        """Test handling invalid structure."""
        trigger_data = {"repository": "not a dict"}
        result = container_executor._extract_repo_url_from_trigger(trigger_data)
        assert result == ""


class TestCleanup:
    """Tests for cleanup method."""

    async def test_cleanup_closes_docker_client(self, container_executor):
        """Test that cleanup closes Docker client."""
        mock_client = AsyncMock()
        container_executor._docker_client = mock_client

        await container_executor.cleanup()

        mock_client.close.assert_called_once()
        assert container_executor._docker_client is None

    async def test_cleanup_closes_kubernetes_client(self, kubernetes_executor):
        """Test that cleanup closes Kubernetes client."""
        mock_client = AsyncMock()
        kubernetes_executor._k8s_api_client = mock_client
        kubernetes_executor._k8s_initialized = True

        await kubernetes_executor.cleanup()

        mock_client.close.assert_called_once()
        assert kubernetes_executor._k8s_api_client is None
        assert kubernetes_executor._k8s_initialized is False

    async def test_cleanup_handles_no_clients(self, container_executor):
        """Test that cleanup handles case with no active clients."""
        container_executor._docker_client = None
        container_executor._k8s_api_client = None

        # Should not raise
        await container_executor.cleanup()


class TestKubernetesPodWaitMessage:
    """Tests for user-facing Kubernetes pending pod diagnostics."""

    def test_unschedulable_pod_message_includes_scheduler_reason(
        self, kubernetes_executor
    ):
        """Pending pods should report scheduler messages instead of blank errors."""
        pod = MagicMock()
        pod.metadata.name = "agent-test-abc"
        pod.status.phase = "Pending"
        pod.status.container_statuses = []
        condition = MagicMock()
        condition.type = "PodScheduled"
        condition.status = "False"
        condition.reason = "Unschedulable"
        condition.message = "0/1 nodes are available: 1 Insufficient cpu."
        pod.status.conditions = [condition]

        message = kubernetes_executor._format_kubernetes_pod_wait_message(pod)

        assert "agent-test-abc" in message
        assert "Unschedulable" in message
        assert "Insufficient cpu" in message

    def test_container_waiting_message_includes_waiting_reason(
        self, kubernetes_executor
    ):
        """Container wait states should be surfaced when scheduling has succeeded."""
        pod = MagicMock()
        pod.metadata.name = "agent-test-abc"
        pod.status.phase = "Pending"
        waiting = MagicMock()
        waiting.reason = "ImagePullBackOff"
        waiting.message = "Back-off pulling image"
        container_status = MagicMock()
        container_status.state.waiting = waiting
        pod.status.container_statuses = [container_status]
        pod.status.conditions = []

        message = kubernetes_executor._format_kubernetes_pod_wait_message(pod)

        assert "ImagePullBackOff" in message
        assert "Back-off pulling image" in message


class TestGitCloneCredentialsNotInUrl:
    """Regression tests for issue #173: the tracker PAT leaked into flow logs
    because it was embedded in the clone URL, which made it part of the cloned
    repository's ``origin`` remote and therefore visible in ``git remote -v``.
    """

    PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"

    def _context(self, repo_url="https://github.com/acme/private.git", **overrides):
        context = {
            "flow_id": "flow-1",
            "execution_id": "exec-1",
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": repo_url,
                        "clone_path": "/workspace",
                        "tracker_id": "tracker-1",
                    }
                ],
            },
            "git_credentials_map": {
                "tracker-1": {"token": self.PAT, "tracker_type": "github"}
            },
        }
        context.update(overrides)
        return context

    def test_clone_command_contains_no_token(self, container_executor):
        command = container_executor._prepare_git_clone_command(self._context())
        assert self.PAT not in command

    def test_clone_url_is_credential_free(self, container_executor):
        """The URL handed to ``git clone`` becomes the origin remote verbatim."""
        command = container_executor._prepare_git_clone_command(self._context())
        assert "https://github.com/acme/private.git" in command
        assert "@github.com" not in command

    def test_clone_url_is_stripped_when_config_already_has_a_token(
        self, container_executor
    ):
        """A token pasted into the configured URL must not survive either."""
        context = self._context(
            repo_url=f"https://{self.PAT}@github.com/acme/private.git"
        )
        command = container_executor._prepare_git_clone_command(context)
        assert self.PAT not in command
        assert "@github.com" not in command

    def test_credential_helper_is_configured(self, container_executor):
        command = container_executor._prepare_git_clone_command(self._context())
        assert "credential.helper" in command
        assert "PRELOOP_GIT_CREDENTIALS" in command

    def test_token_is_passed_through_the_environment(self, container_executor):
        """The secret travels as an env var, never inside the shell script."""
        context = self._context()
        container_executor._prepare_git_clone_command(context)

        env = container_executor._git_credential_env(context)
        assert self.PAT in env["PRELOOP_GIT_CREDENTIALS"]
        assert "x-access-token" in env["PRELOOP_GIT_CREDENTIALS"]

    def test_gitlab_uses_its_credential_username(self, container_executor):
        context = self._context(repo_url="https://gitlab.com/acme/repo.git")
        context["git_credentials_map"]["tracker-1"]["tracker_type"] = "gitlab"
        container_executor._prepare_git_clone_command(context)

        env = container_executor._git_credential_env(context)
        assert "gitlab-ci-token" in env["PRELOOP_GIT_CREDENTIALS"]

    def test_no_credential_env_without_a_token(self, container_executor):
        context = self._context()
        context["git_credentials_map"] = {}
        with patch.object(
            container_executor, "_get_token_from_project", return_value=(None, None)
        ):
            command = container_executor._prepare_git_clone_command(context)

        assert "git clone" in command
        assert container_executor._git_credential_env(context) == {}

    def test_multiple_repos_each_get_a_credential(self, container_executor):
        context = self._context()
        context["git_clone_config"]["repositories"].append(
            {
                "repository_url": "https://gitlab.com/acme/other.git",
                "clone_path": "/workspace-2",
                "tracker_id": "tracker-2",
            }
        )
        context["git_credentials_map"]["tracker-2"] = {
            "token": "glpat-aBcDeFgHiJkLmNoPqRs",
            "tracker_type": "gitlab",
        }

        command = container_executor._prepare_git_clone_command(context)
        assert self.PAT not in command
        assert "glpat-aBcDeFgHiJkLmNoPqRs" not in command

        credentials = context[container_executor.GIT_CREDENTIALS_CONTEXT_KEY]
        assert len(credentials) == 2

    def test_credentials_are_keyed_by_repository_index(self, container_executor):
        """A skipped repository must not shift the remaining tokens' indices,
        which would give repo #2 the API token belonging to repo #1.
        """
        context = self._context()
        context["git_clone_config"]["repositories"].insert(
            0, {"clone_path": "/workspace-0"}
        )  # no repository_url: this one is skipped

        container_executor._prepare_git_clone_command(context)

        credentials = context[container_executor.GIT_CREDENTIALS_CONTEXT_KEY]
        assert list(credentials) == [1]

    def test_setup_runs_before_any_clone(self, container_executor):
        """Otherwise the first clone would prompt for credentials and hang."""
        command = container_executor._prepare_git_clone_command(self._context())
        assert command.index("credential.helper") < command.index("git clone")


class TestValidatedGitRef:
    """Branch names interpolated into origin/<ref>..HEAD must match git rules."""

    @pytest.mark.parametrize(
        "name",
        ["main", "preloop/fix", "preloop/issue-353", "feat/foo-bar_1.2"],
    )
    def test_accepts_normal_names(self, name):
        assert _validated_git_ref(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "foo..bar",
            "-d",
            "preloop/fix.",
            "foo~1",
            "foo^2",
            "foo:bar",
            ".hidden",
            "feat/.dot",
            "heads/foo.lock",
            "/abs",
            "trailing/",
            "a//b",
        ],
    )
    def test_rejects_git_forbidden_names(self, name):
        assert _validated_git_ref(name) is None


class TestGitApiTokensNotInScript:
    """The PR/MR creation curls used to interpolate the raw token into the
    generated shell script (issue #173).
    """

    PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"

    def _context(self):
        return {
            "flow_id": "flow-1",
            "execution_id": "exec-1",
            "flow_name": "PR Reviewer",
            "_git_target_branch": "preloop/fix",
            "_git_source_branch": "main",
            "git_clone_config": {
                "enabled": True,
                "create_pull_request": True,
                "repositories": [
                    {
                        "repository_url": "https://github.com/acme/private.git",
                        "clone_path": "/workspace",
                        "tracker_id": "tracker-1",
                    }
                ],
            },
            "git_credentials_map": {
                "tracker-1": {"token": self.PAT, "tracker_type": "github"}
            },
            "trigger_event_data": {
                "repository": {"clone_url": "https://github.com/acme/private.git"},
            },
        }

    def test_plain_push_exits_when_provenance_update_fails(self, container_executor):
        from preloop.agents.container import (
            build_github_pr_capture_shell,
            provenance_failure_exit_shell,
        )

        capture = build_github_pr_capture_shell(
            token_ref="${PRELOOP_GIT_TOKEN_1}",
            owner="acme",
            repo="private",
            branch="preloop/fix",
            execution_link="https://app.example.com/console/flows/executions/x",
        )
        warning = capture.split("PRELOOP_PROVENANCE_FAILED", 1)[1]
        assert "exit 1" not in warning.split("elif", 1)[0]
        commands = container_executor._prepare_git_post_execution_commands(
            self._context()
        )
        assert provenance_failure_exit_shell().strip() in commands

    def test_post_execution_commands_contain_no_token(self, container_executor):
        context = self._context()
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert self.PAT not in commands

    def test_post_execution_uses_an_env_var_reference(self, container_executor):
        context = self._context()
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert "${PRELOOP_GIT_TOKEN_1}" in commands

    def test_token_reaches_the_container_environment(self, container_executor):
        context = self._context()
        container_executor._prepare_git_post_execution_commands(context)
        env = container_executor._apply_git_credential_env({}, context)
        assert env["PRELOOP_GIT_TOKEN_1"] == self.PAT

    def test_post_execution_reinstalls_credential_helper(self, container_executor):
        context = self._context()
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert "credential.helper" in commands
        assert "git credential approve" in commands
        helper_at = commands.index("credential.helper")
        push_at = commands.index("git push origin")
        assert helper_at < push_at

    def test_post_execution_writes_recovery_artifacts_before_push(
        self, container_executor
    ):
        context = self._context()
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert "/workspace/evidence/branch.bundle" in commands
        assert "/workspace/evidence/branch.patch" in commands
        assert commands.index("branch.bundle") < commands.index("git push origin")

    def test_post_execution_username_follows_host_kind(self, container_executor):
        """Clone uses host_kind when tracker_type is missing; push must match."""
        context = self._context()
        context["git_credentials_map"]["tracker-1"]["tracker_type"] = None
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert "username=x-access-token" in commands
        assert "username=oauth2" not in commands

    def test_resume_commit_count_uses_origin_target_head(self, container_executor):
        context = self._context()
        context["_git_source_branch"] = "preloop/issue-353"
        context["_git_target_branch"] = "preloop/issue-353"
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert "origin/preloop/issue-353..HEAD" in commands
        assert "origin/'preloop" not in commands

    def test_normal_commit_count_falls_back_to_source_target(self, container_executor):
        context = self._context()
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert "origin/preloop/fix..HEAD" in commands
        assert "main..preloop/fix" in commands
        assert "origin/'preloop" not in commands

    def test_unsafe_target_branch_skips_post_execution(self, container_executor):
        context = self._context()
        context["_git_target_branch"] = "feat/x; rm -rf /"
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert commands == ""

    @pytest.mark.parametrize(
        "unsafe",
        ["foo..bar", "-d", "preloop/fix.", "foo~1", "foo^2", "foo:bar"],
    )
    def test_git_forbidden_target_ref_skips_post_execution(
        self, container_executor, unsafe
    ):
        context = self._context()
        context["_git_target_branch"] = unsafe
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert commands == ""
        assert f"origin/{unsafe}" not in commands

    def test_unsafe_source_branch_skips_post_execution(self, container_executor):
        context = self._context()
        context["_git_source_branch"] = "foo..bar"
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert commands == ""
        assert "origin/preloop/fix" not in commands

    def test_empty_branch_prints_the_no_commits_marker(self, container_executor):
        """The evidence the no-progress classification rests on (#851).

        The block already says "No commits ..." in prose; the marker is its
        machine-readable twin, so the orchestrator never has to match a
        sentence that somebody will reword.
        """
        from preloop.services.no_progress_guard import NO_COMMITS_MARKER

        context = self._context()
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert f"{NO_COMMITS_MARKER} preloop/fix" in commands
        # Printed on the branch-was-empty side only: a run that pushed work
        # must never be read as a run that produced none.
        no_commits_at = commands.index(NO_COMMITS_MARKER)
        assert commands.index("git push origin") < no_commits_at


class TestPushCredentialsWithoutRepositoryTracker:
    """Reproduces the post-execution push that had no credentials.

    Production execution 85b67a24: the repository entry declared no
    ``tracker_id`` and the triggering project's tracker was a GitHub App
    installation, which stores no API key. Both the clone credential and the
    push token resolved empty, the public repository still cloned, and the push
    printed "WARNING: no git credentials available for push" followed by
    "could not read Username for 'https://github.com'".

    The orchestrator now resolves that tracker (minting an installation token
    when needed) and passes it as ``trigger_tracker_id``.
    """

    APP_TOKEN = "ghs_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"

    def _context(self, *, with_trigger_tracker=True):
        context = {
            "flow_id": "flow-1",
            "execution_id": "exec-1",
            "flow_name": "Automated Issue Implementation (GitHub)",
            "account_id": "account-1",
            "_git_target_branch": "preloop/automated-issue-implementation-85b67a24",
            "_git_source_branch": "main",
            "trigger_project_id": "daa4a88a-0dfa-4fd6-95bc-664f363ad033",
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": "https://github.com/preloop/preloop.git",
                        "clone_path": "workspace",
                    }
                ],
            },
            "trigger_event_data": {},
        }
        if with_trigger_tracker:
            context["trigger_tracker_id"] = "tracker-app"
            context["git_credentials_map"] = {
                "tracker-app": {
                    "token": self.APP_TOKEN,
                    "tracker_type": "github",
                }
            }
        return context

    def test_push_token_reaches_the_container_environment(self, container_executor):
        context = self._context()
        with patch.object(
            container_executor, "_get_token_from_project", return_value=(None, None)
        ):
            commands = container_executor._prepare_git_post_execution_commands(context)

        assert "${PRELOOP_GIT_TOKEN_1}" in commands
        assert self.APP_TOKEN not in commands
        env = container_executor._apply_git_credential_env({}, context)
        assert env["PRELOOP_GIT_TOKEN_1"] == self.APP_TOKEN

    def test_clone_installs_the_credential_helper(self, container_executor):
        context = self._context()
        with patch.object(
            container_executor, "_get_token_from_project", return_value=(None, None)
        ):
            command = container_executor._prepare_git_clone_command(context)

        assert self.APP_TOKEN not in command
        env = container_executor._git_credential_env(context)
        assert self.APP_TOKEN in env["PRELOOP_GIT_CREDENTIALS"]
        assert "x-access-token" in env["PRELOOP_GIT_CREDENTIALS"]

    def test_without_the_trigger_tracker_the_push_has_no_token(
        self, container_executor
    ):
        """The pre-fix behaviour, kept as the contrast case."""
        context = self._context(with_trigger_tracker=False)
        with patch.object(
            container_executor, "_get_token_from_project", return_value=(None, None)
        ):
            commands = container_executor._prepare_git_post_execution_commands(context)

        assert "${PRELOOP_GIT_TOKEN_1}" not in commands
        assert "no git credentials available for push" in commands
        assert container_executor._apply_git_credential_env({}, context) == {}

    def test_repository_tracker_still_wins(self, container_executor):
        """A repository with its own tracker keeps using that tracker's token."""
        context = self._context()
        context["git_clone_config"]["repositories"][0]["tracker_id"] = "tracker-repo"
        context["git_credentials_map"]["tracker-repo"] = {
            "token": "github_pat_repo_specific_token",
            "tracker_type": "github",
        }
        container_executor._prepare_git_post_execution_commands(context)

        env = container_executor._apply_git_credential_env({}, context)
        assert env["PRELOOP_GIT_TOKEN_1"] == "github_pat_repo_specific_token"

    def test_empty_map_entry_falls_through_to_the_project_lookup(
        self, container_executor
    ):
        """A tracker recorded without a token must not shadow other sources."""
        context = self._context()
        context["git_credentials_map"]["tracker-app"]["token"] = ""
        with patch.object(
            container_executor,
            "_get_token_from_project",
            return_value=("github_pat_from_project", "github"),
        ):
            container_executor._prepare_git_post_execution_commands(context)

        env = container_executor._apply_git_credential_env({}, context)
        assert env["PRELOOP_GIT_TOKEN_1"] == "github_pat_from_project"


class TestProjectRepoUrlForAppTrackers:
    """An app-installed tracker stores no API key.

    The project lookup used to refuse to build a clone URL in that case, so a
    flow relying on the trigger project (rather than an explicit
    repository_url) could not clone at all.
    """

    def _db(self, tracker):
        project = MagicMock()
        project.id = "project-1"
        project.slug = "preloop/preloop"
        project.organization = MagicMock(tracker_id="tracker-app")
        db = MagicMock()
        return project, tracker, db

    def _lookup(self, container_executor, tracker):
        project, tracker, db = self._db(tracker)
        with (
            patch("preloop.models.db.session.get_db_session", return_value=iter([db])),
            patch("preloop.models.crud.crud_project.get", return_value=project),
            patch("preloop.models.crud.crud_tracker.get", return_value=tracker),
        ):
            return container_executor._get_repo_url_from_project(
                "project-1", "account-1"
            )

    def test_app_tracker_without_a_key_still_yields_a_url(self, container_executor):
        tracker = MagicMock(
            id="tracker-app",
            resolved_api_key="",
            auth_type="github_app",
            tracker_type="github",
        )
        assert (
            self._lookup(container_executor, tracker)
            == "https://github.com/preloop/preloop.git"
        )

    def test_pat_tracker_without_a_key_is_still_refused(self, container_executor):
        tracker = MagicMock(
            id="tracker-pat",
            resolved_api_key="",
            auth_type="api_token",
            tracker_type="github",
        )
        assert self._lookup(container_executor, tracker) is None


class TestLogScrubbing:
    """Logs are scrubbed on read as well, so a token already present in a
    running container's output never reaches the API or the console (#173).
    """

    PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"

    @patch.object(ContainerAgentExecutor, "_get_docker_client")
    async def test_get_logs_scrubs_leaked_remote(
        self, mock_get_client, container_executor
    ):
        leak = f"origin\thttps://{self.PAT}@github.com/acme/private.git (fetch)"
        mock_docker = AsyncMock()
        mock_container = AsyncMock()
        mock_container.log.return_value = ["Cloning...", leak]
        mock_docker.containers.get.return_value = mock_container
        mock_get_client.return_value = mock_docker

        logs = await container_executor.get_logs("container-123")

        assert not any(self.PAT in line for line in logs)
        assert any("[REDACTED]" in line for line in logs)
        assert logs[0] == "Cloning...", "clean lines pass through unchanged"

    @patch.object(ContainerAgentExecutor, "_stream_docker_logs")
    async def test_stream_logs_scrubs_leaked_remote(
        self, mock_stream, container_executor
    ):
        leak = f"origin\thttps://{self.PAT}@github.com/acme/private.git (push)"

        async def _lines(_session_reference):
            yield leak

        mock_stream.side_effect = _lines

        streamed = [
            line async for line in container_executor.stream_logs("container-123")
        ]

        assert streamed == [
            "origin\thttps://[REDACTED]@github.com/acme/private.git (push)"
        ]


class TestResumeRebaseShell:
    """Resume rebase is present only on resume; force-with-lease only after rebase."""

    def _clone_context(self, **overrides):
        context = {
            "flow_id": "flow-1",
            "execution_id": "exec-1",
            "flow_name": "Automated Issue Implementation",
            "git_clone_config": {
                "enabled": True,
                "source_branch": "main",
                "repositories": [
                    {
                        "repository_url": "https://github.com/acme/private.git",
                        "clone_path": "/workspace",
                    }
                ],
            },
            "trigger_event_data": {},
        }
        context.update(overrides)
        return context

    def _post_context(self, **overrides):
        context = {
            "flow_id": "flow-1",
            "execution_id": "exec-1",
            "flow_name": "PR Reviewer",
            "_git_target_branch": "preloop/issue-353",
            "_git_source_branch": "preloop/issue-353",
            "git_clone_config": {
                "enabled": True,
                "source_branch": "main",
                "repositories": [
                    {
                        "repository_url": "https://github.com/acme/private.git",
                        "clone_path": "/workspace",
                    }
                ],
            },
            "trigger_event_data": {},
        }
        context.update(overrides)
        return context

    def test_clone_omits_rebase_when_not_resuming(self, container_executor):
        command = container_executor._prepare_git_clone_command(self._clone_context())
        assert "git rebase" not in command
        assert "PRELOOP_RESUME_REBASE_CONFLICT" not in command

    def test_clone_rebases_when_resume_from_set(self, container_executor):
        context = self._clone_context(resume_from="prior-exec")
        command = container_executor._prepare_git_clone_command(context)
        assert "git fetch origin main" in command
        assert "git rebase origin/main" in command
        assert "/workspace/evidence/rebase-conflict.txt" in command
        assert "git rebase --abort" in command
        assert "export PRELOOP_RESUME_REBASE_CONFLICT=1" in command
        assert context.get("_git_resume_rebase") is True

    def test_clone_rebases_when_resume_metadata_present(self, container_executor):
        context = self._clone_context(
            trigger_event_data={
                "_resume": {
                    "execution_id": "prior-exec",
                    "source_branch": "preloop/issue-353",
                }
            }
        )
        command = container_executor._prepare_git_clone_command(context)
        assert "git rebase origin/main" in command
        assert context.get("resume_from") == "prior-exec"

    def test_clone_rebases_onto_repo_branch(self, container_executor):
        context = self._clone_context()
        context["git_clone_config"]["repositories"][0]["branch"] = "develop"
        context["resume_from"] = "prior-exec"
        command = container_executor._prepare_git_clone_command(context)
        assert "git fetch origin develop" in command
        assert "git rebase origin/develop" in command
        assert "git rebase origin/main" not in command

    def test_clone_rebases_onto_config_branch(self, container_executor):
        context = self._clone_context(resume_from="prior-exec")
        context["git_clone_config"]["branch"] = "release"
        command = container_executor._prepare_git_clone_command(context)
        assert "git rebase origin/release" in command

    def test_unsafe_base_branch_skips_rebase(self, container_executor):
        context = self._clone_context(resume_from="prior-exec")
        context["git_clone_config"]["source_branch"] = "main; echo pwned"
        command = container_executor._prepare_git_clone_command(context)
        assert "git rebase" not in command
        assert "origin/main; echo pwned" not in command

    def test_post_exec_omits_force_with_lease_when_not_resuming(
        self, container_executor
    ):
        commands = container_executor._prepare_git_post_execution_commands(
            self._post_context(
                _git_target_branch="preloop/fix",
                _git_source_branch="main",
            )
        )
        assert "--force-with-lease" not in commands
        assert "git push origin preloop/fix" in commands

    def test_post_exec_force_with_lease_only_after_rebase(self, container_executor):
        commands = container_executor._prepare_git_post_execution_commands(
            self._post_context(resume_from="prior-exec")
        )
        assert "git push --force-with-lease origin preloop/issue-353" in commands
        assert "/workspace/evidence/resume-rebased" in commands
        assert "PRELOOP_RESUME_REBASED" in commands
        force_at = commands.index("git push --force-with-lease origin")
        plain_at = commands.index("git push origin preloop/issue-353")
        assert force_at < plain_at

    def test_resume_rebase_shell_records_conflict_and_aborts(self, container_executor):
        shell = container_executor._build_git_resume_rebase_shell(
            full_path="/workspace", base_branch="main"
        )
        assert "git fetch origin main" in shell
        assert "git rebase origin/main" in shell
        assert "git rebase --abort" in shell
        assert "/workspace/evidence/rebase-conflict.txt" in shell
        assert "Resolve these paths before continuing:" in shell
        assert "export PRELOOP_RESUME_REBASE_CONFLICT=1" in shell
        assert "export PRELOOP_RESUME_REBASED=1" in shell


class TestResolveGitBranchPlan:
    def test_resume_overrides_config_source_branch(self, container_executor):
        source, target, _, _, _ = container_executor._resolve_git_branch_plan(
            {
                "flow_name": "Automated Issue Implementation",
                "execution_id": "83021dcc-4658-45a3-814c-0e67d07642f6",
                "trigger_event_data": {
                    "_resume": {
                        "execution_id": "prior",
                        "source_branch": "preloop/issue-353",
                    }
                },
            },
            {"source_branch": "main", "target_branch": None},
        )
        assert source == "preloop/issue-353"
        assert target == "preloop/issue-353"

    def test_default_target_when_not_resuming(self, container_executor):
        source, target, _, _, _ = container_executor._resolve_git_branch_plan(
            {
                "flow_name": "Automated Issue Implementation",
                "execution_id": "83021dcc-4658-45a3-814c-0e67d07642f6",
                "trigger_event_data": {},
            },
            {"source_branch": None, "target_branch": None},
        )
        assert source == "main"
        assert target == "preloop/automated-issue-implementation-83021dcc"

    def test_issue_trigger_names_branch_from_issue_number(self, container_executor):
        source, target, _, _, _ = container_executor._resolve_git_branch_plan(
            {
                "flow_name": "Automated Issue Implementation",
                "execution_id": "08095fd6-f861-4939-997d-2600d1ec5a80",
                "trigger_event_data": {
                    "payload": {
                        "issue": {
                            "number": 356,
                            "title": "Persist OpenCode/Codex session IDs",
                        }
                    }
                },
            },
            {"source_branch": None, "target_branch": None},
        )
        assert source == "main"
        assert target == "preloop/issue-356-08095fd6"


class TestInterpolateGitConfigText:
    def test_github_issue_aliases_object_attributes_paths(self):
        from preloop.agents.container import interpolate_git_config_text

        trigger = {
            "payload": {
                "issue": {
                    "number": 356,
                    "title": "Persist session ids",
                    "body": "Wanted",
                }
            }
        }
        assert (
            interpolate_git_config_text(
                "Implements: {{trigger_event.payload.object_attributes.title}}",
                trigger,
            )
            == "Implements: Persist session ids"
        )
        assert (
            interpolate_git_config_text(
                "Closes #{{trigger_event.payload.object_attributes.number}}",
                trigger,
            )
            == "Closes #356"
        )

    def test_unresolved_placeholder_is_empty(self):
        from preloop.agents.container import interpolate_git_config_text

        assert interpolate_git_config_text("Implements: {{missing.title}}", {}) == ""


def _run_write_pr_payload_py(
    tmp_path: pathlib.Path,
    *,
    commit_count: int,
    commit_title: str = "",
    commit_body: str = "",
    commit_list: str = "",
    flow_name: str = "",
    execution_link: str = "",
    issue_number: str = "",
    flow_title: str = "",
    flow_body: str = "",
) -> dict[str, Any]:
    """Run the in-container payload script against temp files."""

    files = {
        FLOW_PR_TITLE_FILE: flow_title,
        FLOW_PR_BODY_FILE: flow_body,
        COMMIT_PR_TITLE_FILE: commit_title,
        COMMIT_PR_BODY_FILE: commit_body,
        COMMIT_PR_LIST_FILE: commit_list,
    }
    out_path = tmp_path / "pr-payload.json"
    for path, content in files.items():
        pathlib.Path(path).write_text(content, encoding="utf-8")
    try:
        subprocess.run(
            [
                sys.executable,
                "-",
                str(out_path),
                "preloop/issue-356-08095fd6",
                "main",
                "github",
                issue_number,
                flow_name,
                execution_link,
                str(commit_count),
            ],
            input=WRITE_PR_PAYLOAD_PY,
            text=True,
            check=True,
            capture_output=True,
        )
        return json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        for path in files:
            pathlib.Path(path).unlink(missing_ok=True)


class TestWritePrPayloadPy:
    def test_json_roundtrip_survives_quotes_and_newlines(self):
        body = (
            "A restart used to start a cold\n"
            'agent: files die with "PRELOOP_AGENT_SESSION".'
        )
        encoded = json.dumps(
            {
                "title": "Persist ids so resumes can use native --resume",
                "body": body,
                "head": "preloop/issue-356-08095fd6",
                "base": "main",
            },
            ensure_ascii=False,
        )
        parsed = json.loads(encoded)
        assert parsed["body"] == body
        assert "\n" not in encoded.replace("\\n", "")

    def test_commit_fallback_single_commit_includes_execution_link(self, tmp_path):
        payload = _run_write_pr_payload_py(
            tmp_path,
            commit_count=1,
            commit_title="Persist session ids",
            commit_body="Keeps native --resume working.",
            flow_name="Automated Issue Implementation",
            execution_link=(
                "https://app.preloop.ai/console/flows/executions/"
                "08095fd6-f861-4939-997d-2600d1ec5a80"
            ),
        )
        assert payload["title"] == "Persist session ids"
        assert "Automated changes from Preloop flow:" in payload["body"]
        assert (
            "[Automated Issue Implementation](https://app.preloop.ai"
            "/console/flows/executions/08095fd6-f861-4939-997d-2600d1ec5a80)"
            in payload["body"]
        )
        assert "Keeps native --resume working." in payload["body"]
        assert "**Commits:**" not in payload["body"]

    def test_commit_fallback_multi_commit_lists_subjects(self, tmp_path):
        payload = _run_write_pr_payload_py(
            tmp_path,
            commit_count=2,
            commit_title="Persist session ids",
            commit_body="unused for multi-commit",
            commit_list="- Persist session ids\n- Add tests",
            flow_name="Automated Issue Implementation",
            execution_link=(
                "https://app.preloop.ai/console/flows/executions/"
                "08095fd6-f861-4939-997d-2600d1ec5a80"
            ),
        )
        assert payload["title"] == "[Preloop] Automated Issue Implementation"
        assert "**Commits:**" in payload["body"]
        assert "- Persist session ids" in payload["body"]
        assert "- Add tests" in payload["body"]
        assert "/console/flows/executions/08095fd6" in payload["body"]


class TestPostExecutionPullRequest:
    """The wrapper used to interpolate title/body into JSON, so a multi-line
    pull_request_description (preset 011) or a commit body with quotes made
    GitHub reject the create call after a successful push.
    """

    def _context(self, **overrides):
        context = {
            "flow_id": "flow-1",
            "execution_id": "08095fd6-f861-4939-997d-2600d1ec5a80",
            "flow_name": "Automated Issue Implementation",
            "_git_target_branch": "preloop/issue-356-08095fd6",
            "_git_source_branch": "main",
            "git_clone_config": {
                "enabled": True,
                "create_pull_request": True,
                "pull_request_title": (
                    "Implements: {{trigger_event.payload.object_attributes.title}}"
                ),
                "pull_request_description": (
                    "Automated implementation for "
                    "#{{trigger_event.payload.object_attributes.number}}\n\n"
                    "Closes #{{trigger_event.payload.object_attributes.number}}"
                ),
                "repositories": [
                    {
                        "repository_url": "https://github.com/acme/private.git",
                        "clone_path": "/workspace",
                        "tracker_id": "tracker-1",
                    }
                ],
            },
            "git_credentials_map": {
                "tracker-1": {
                    "token": "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
                    "tracker_type": "github",
                }
            },
            "trigger_event_data": {
                "payload": {
                    "issue": {"number": 356, "title": "Persist session ids"},
                    "repository": {"clone_url": "https://github.com/acme/private.git"},
                }
            },
        }
        context.update(overrides)
        return context

    def test_multiline_description_is_not_raw_json(self, container_executor):
        commands = container_executor._prepare_git_post_execution_commands(
            self._context()
        )
        assert "json.dumps" in commands
        assert "/workspace/result.json" in commands
        assert "https://api.github.com/repos/acme/private/pulls" in commands
        assert '  "body": "Automated implementation' not in commands
        assert "Closes #356" in commands or "base64 -d" in commands
        assert "${PRELOOP_GIT_TOKEN_1}" in commands
        assert "github_pat_11ABCDEFG" not in commands

    def test_host_kind_creates_pr_when_tracker_type_missing(self, container_executor):
        context = self._context()
        context["git_credentials_map"]["tracker-1"]["tracker_type"] = None
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert "https://api.github.com/repos/acme/private/pulls" in commands

    def test_result_json_fields_are_consulted(self, container_executor):
        commands = container_executor._prepare_git_post_execution_commands(
            self._context()
        )
        assert "pr_title" in commands
        assert "pr_body" in commands

    def test_commit_fallback_keeps_execution_link_and_commit_list(
        self, container_executor
    ):
        context = self._context()
        context["git_clone_config"]["pull_request_title"] = ""
        context["git_clone_config"]["pull_request_description"] = ""
        commands = container_executor._prepare_git_post_execution_commands(context)
        assert (
            "/console/flows/executions/08095fd6-f861-4939-997d-2600d1ec5a80" in commands
        )
        assert "**Commits:**" in commands
        assert "[Preloop]" in commands
        assert '"$COMMIT_COUNT"' in commands
        assert "/tmp/preloop-commit-pr-list.txt" in commands
        assert 'git log --format="- %s"' in commands


class TestExtractMergeRequestRef:
    def test_github_pr_comment_issue_stub(self, container_executor):
        ref = container_executor._extract_merge_request_ref_from_trigger(
            {
                "payload": {
                    "issue": {
                        "number": 353,
                        "pull_request": {
                            "html_url": "https://github.com/preloop/preloop/pull/353"
                        },
                    }
                }
            }
        )
        assert ref == "pull/353/head"

    def test_gitlab_mr_note(self, container_executor):
        ref = container_executor._extract_merge_request_ref_from_trigger(
            {"payload": {"merge_request": {"iid": 10}}}
        )
        assert ref == "refs/merge-requests/10/head"


class TestExtractSourceBranch:
    def test_gitlab_mr_note_source_branch(self, container_executor):
        branch = container_executor._extract_source_branch_from_trigger(
            {"payload": {"merge_request": {"source_branch": "feat/x"}}}
        )
        assert branch == "feat/x"


@pytest.mark.parametrize(
    "tool_output",
    [
        "Traceback (most recent call last):\nValueError: expected regression",
        '    echo "FATAL ERROR: Git clone failed!"',
        "ERROR: first\nERROR: second\nERROR: third",
    ],
)
@pytest.mark.parametrize("duration", ["100ms", "59868ms", "4.56s", "1m 02s"])
def test_codex_tool_output_is_not_a_harness_failure(
    container_executor, tool_output, duration
):
    logs = (
        "PRELOOP_AGENT_EXEC_START\nexec\n"
        '/bin/bash -lc "pytest" in /workspace/repo\n'
        f" exited 1 in {duration}:\n"
        + tool_output
        + "\ncodex\nI reproduced the bug and will fix it.\n"
        "tokens used\n1000\n"
        "PRELOOP_WORKSPACE_SNAPSHOT_SKIPPED size_exceeds_limit limit=2097152"
    )
    assert container_executor._detect_error_in_logs(logs) is False


def test_codex_harness_failure_after_tool_output_is_detected(container_executor):
    logs = (
        'exec\n/bin/bash -lc "true" in /workspace/repo\n succeeded in 10ms:\n'
        "codex\nAgent execution failed: connection lost"
    )
    assert container_executor._detect_error_in_logs(logs) is True


def test_unterminated_codex_command_keeps_real_harness_failure(container_executor):
    logs = 'exec\n/bin/bash -lc "true" in /workspace/repo\n succeeded in 1ms:\nAgent execution failed: CLI crashed'
    assert container_executor._detect_error_in_logs(logs) is True


@pytest.mark.parametrize("source_line", ["user", "tool"])
def test_source_words_do_not_end_codex_command_transcript(
    container_executor, source_line
):
    logs = (
        'exec\n/bin/bash -lc "cat source.py" in /workspace/repo\n succeeded in 1ms:\n'
        + source_line
        + "\nTraceback (most recent call last):\ncodex\nI inspected the fixture."
    )
    assert container_executor._detect_error_in_logs(logs) is False


def test_unconfirmed_command_header_does_not_suppress_failure(container_executor):
    logs = 'exec\n/bin/bash -lc "true"\nAgent execution failed: cannot start CLI\ncodex\nStopped'
    assert container_executor._detect_error_in_logs(logs) is True


def _git(repo: pathlib.Path, *args: str) -> None:
    """Run one git command in ``repo`` with an identity of its own."""
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=agent@example.com",
            "-c",
            "user.name=Probe Agent",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _make_repo(path: pathlib.Path, *, with_remote: bool = True) -> pathlib.Path:
    """A checkout with one commit already shared with its remote."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", str(path)],
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("start\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "start")
    if with_remote:
        bare = path.parent / f"{path.name}-remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)], check=True, capture_output=True
        )
        _git(path, "remote", "add", "origin", str(bare))
        _git(path, "push", "origin", "HEAD")
        _git(path, "fetch", "origin")
    return path


def _probe(*roots: str) -> str:
    """Run the real probe script over ``roots`` and return its verdict."""
    result = subprocess.run(
        ["sh", "-c", WORKSPACE_PROGRESS_PROBE_SCRIPT, "sh", *roots],
        capture_output=True,
        text=True,
    )
    verdict = ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(WORKSPACE_PROBE_MARKER):
            verdict = stripped[len(WORKSPACE_PROBE_MARKER) :].strip()
    return verdict


class TestWorkspaceProgressProbeScript:
    """The probe runs for real here, against real git repositories (#851).

    A stop rests on this script's answer, so "clean" has to mean the agent
    genuinely produced nothing, and everything the script cannot see has to
    come back as "unknown".
    """

    def test_an_untouched_checkout_is_clean(self, tmp_path):
        _make_repo(tmp_path / "workspace" / "repo")
        assert _probe(str(tmp_path / "workspace")) == "clean"

    def test_an_edited_file_is_progress(self, tmp_path):
        repo = _make_repo(tmp_path / "workspace" / "repo")
        (repo / "README.md").write_text("edited\n")
        assert _probe(str(tmp_path / "workspace")) == "dirty"

    def test_a_new_untracked_file_is_progress(self, tmp_path):
        repo = _make_repo(tmp_path / "workspace" / "repo")
        (repo / "new_module.py").write_text("x = 1\n")
        assert _probe(str(tmp_path / "workspace")) == "dirty"

    def test_a_commit_that_is_not_pushed_is_progress(self, tmp_path):
        """A committed run has a clean tree and has still done the work."""
        repo = _make_repo(tmp_path / "workspace" / "repo")
        (repo / "feature.py").write_text("def f():\n    return 1\n")
        _git(repo, "add", "feature.py")
        _git(repo, "commit", "-m", "add the feature")
        assert _probe(str(tmp_path / "workspace")) == "dirty"

    def test_a_checkout_with_no_remote_is_unknown(self, tmp_path):
        """ "Not pushed yet" has no meaning without a remote to push to.

        ``rev-list --not --remotes`` filters nothing in such a repository and
        would count every commit that was ever made, which used to read as
        progress forever and quietly retired the guard.
        """
        _make_repo(tmp_path / "workspace" / "repo", with_remote=False)
        assert _probe(str(tmp_path / "workspace")) == "unknown"

    def test_a_checkout_with_no_remote_still_shows_its_edits(self, tmp_path):
        repo = _make_repo(tmp_path / "workspace" / "repo", with_remote=False)
        (repo / "README.md").write_text("edited\n")
        assert _probe(str(tmp_path / "workspace")) == "dirty"

    def test_a_path_with_a_space_is_probed_not_split(self, tmp_path):
        repo = _make_repo(tmp_path / "workspace" / "my app")
        (repo / "README.md").write_text("edited\n")
        assert _probe(str(tmp_path / "workspace")) == "dirty"

    def test_a_root_that_does_not_exist_is_unknown(self, tmp_path):
        _make_repo(tmp_path / "workspace" / "repo")
        assert _probe(str(tmp_path / "workspace"), str(tmp_path / "gone")) == "unknown"

    def test_a_root_with_no_repository_is_unknown(self, tmp_path):
        (tmp_path / "workspace").mkdir()
        assert _probe(str(tmp_path / "workspace")) == "unknown"

    def test_work_outside_the_workspace_root_is_found(self, tmp_path):
        """An absolute clone_path puts the checkout outside /workspace."""
        (tmp_path / "workspace").mkdir()
        elsewhere = _make_repo(tmp_path / "srv" / "checkout")
        (elsewhere / "README.md").write_text("edited\n")
        assert _probe(str(tmp_path / "workspace")) == "unknown"
        assert _probe(str(tmp_path / "workspace"), str(elsewhere)) == "dirty"

    def test_the_same_repository_twice_is_probed_once(self, tmp_path):
        repo = _make_repo(tmp_path / "workspace" / "repo")
        assert _probe(str(tmp_path / "workspace"), str(repo)) == "clean"

    def test_evidence_of_work_outranks_an_unreadable_sibling(self, tmp_path):
        """One repository nobody can answer for must not mask a busy one."""
        repo = _make_repo(tmp_path / "workspace" / "repo")
        (repo / "README.md").write_text("edited\n")
        _make_repo(tmp_path / "workspace" / "other", with_remote=False)
        assert _probe(str(tmp_path / "workspace")) == "dirty"

    def test_the_probe_cleans_up_after_itself(self, tmp_path):
        _make_repo(tmp_path / "workspace" / "repo")
        _probe(str(tmp_path / "workspace"))
        leftovers = list(
            pathlib.Path(WORKSPACE_PROBE_REPO_LIST).parent.glob(
                f"{pathlib.Path(WORKSPACE_PROBE_REPO_LIST).name}.*"
            )
        )
        assert leftovers == []


class TestWorkspaceProbeRoots:
    """Which directories the probe is pointed at (#851)."""

    def _container(self, working_dir, *, fails=False):
        container = MagicMock()
        if fails:
            container.show = AsyncMock(side_effect=RuntimeError("no such container"))
        else:
            container.show = AsyncMock(
                return_value={"Config": {"WorkingDir": working_dir}}
            )
        return container

    async def test_an_absolute_clone_path_is_added(self, container_executor):
        roots = await container_executor._workspace_probe_roots(
            self._container("/srv/checkout")
        )
        assert roots == ["/workspace", "/srv/checkout"]

    @pytest.mark.parametrize(
        "working_dir",
        ["/workspace", "/workspace/repo", "", "   ", "relative/path", "/"],
    )
    async def test_the_workspace_is_enough_on_its_own(
        self, container_executor, working_dir
    ):
        roots = await container_executor._workspace_probe_roots(
            self._container(working_dir)
        )
        assert roots == ["/workspace"]

    async def test_an_unreadable_container_still_probes_the_workspace(
        self, container_executor
    ):
        roots = await container_executor._workspace_probe_roots(
            self._container("/srv/checkout", fails=True)
        )
        assert roots == ["/workspace"]

    async def test_the_roots_travel_as_arguments_not_as_script_text(
        self, container_executor
    ):
        """The paths are argv, so a space in one can never split a word."""
        container = self._container("/srv/my checkout")
        exec_handle = MagicMock()
        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=stream)
        stream.__aexit__ = AsyncMock(return_value=False)
        message = MagicMock()
        message.data = f"{WORKSPACE_PROBE_MARKER} clean\n".encode()
        stream.read_out = AsyncMock(side_effect=[message, None])
        exec_handle.start = MagicMock(return_value=stream)
        container.exec = AsyncMock(return_value=exec_handle)
        docker = MagicMock()
        docker.containers.get = AsyncMock(return_value=container)
        container_executor._get_docker_client = AsyncMock(return_value=docker)

        assert await container_executor.probe_workspace_changed("abc123") is False
        cmd = container.exec.await_args.kwargs["cmd"]
        assert cmd[:2] == ["sh", "-c"]
        assert cmd[3:] == ["sh", "/workspace", "/srv/my checkout"]
        assert "/srv/my checkout" not in cmd[2]


_FAKE_PROVIDER = r'''#!{python}
"""In-process stand-in for the GitHub or GitLab pull-request API."""
import json
import os
import pathlib
import sys

store = pathlib.Path(os.environ["FAKE_STORE"])
calls = pathlib.Path(os.environ["FAKE_CALLS"])
kind = os.environ["FAKE_KIND"]
update_status = os.environ.get("FAKE_UPDATE_STATUS", "200")
args = sys.argv[1:]
method = args[args.index("-X") + 1] if "-X" in args else "GET"
url = next(arg for arg in args if arg.startswith("http"))
output = args[args.index("-o") + 1] if "-o" in args else ""
wants_code = "-w" in args
blob = next((arg[1:] for arg in args if arg.startswith("@")), "")
payload = json.loads(pathlib.Path(blob).read_text()) if blob else {{}}
with calls.open("a") as stream:
    stream.write(method + " " + url + "\n")
pulls = json.loads(store.read_text()) if store.exists() else []


def branch_of(item):
    if kind == "gitlab":
        return item.get("source_branch")
    return (item.get("head") or {{}}).get("ref")


def write(body, code):
    if output and output != "/dev/null":
        pathlib.Path(output).write_text(json.dumps(body))
    if wants_code:
        sys.stdout.write(code)


if method == "POST":
    branch = payload.get("head") or payload.get("source_branch") or ""
    if any(branch_of(item) == branch for item in pulls):
        write({{"message": "already exists"}}, "422")
    else:
        number = len(pulls) + 1
        if kind == "gitlab":
            row = {{
                "iid": number,
                "title": payload.get("title", ""),
                "description": payload.get("description", ""),
                "source_branch": branch,
                "web_url": (
                    "https://gitlab.example.com/example/widgets/-/merge_requests/"
                    + str(number)
                ),
            }}
        else:
            row = {{
                "number": number,
                "title": payload.get("title", ""),
                "body": payload.get("body", ""),
                "head": {{"ref": branch}},
                "html_url": f"https://github.com/example/widgets/pull/{{number}}",
            }}
        pulls.append(row)
        store.write_text(json.dumps(pulls))
        write(row, "201")
elif method == "GET":
    write(pulls, "200")
else:
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    field = "description" if kind == "gitlab" else "body"
    if update_status.startswith("2"):
        for item in pulls:
            ident = item.get("iid") if kind == "gitlab" else item.get("number")
            if ident == number:
                item[field] = payload.get(field, item.get(field))
                if "title" in payload:
                    item["title"] = payload["title"]
        store.write_text(json.dumps(pulls))
    write({{"ok": True}}, update_status)
'''


class TestLegacyContinuationProvenance:
    """Legacy continuation upserts the owned block and does not claim a failed update."""

    INITIAL = "11111111-1111-4111-8111-111111111111"
    CURRENT = "33333333-3333-4333-8333-333333333333"
    PRIOR_SHA = "c" * 40

    def _run(
        self,
        tmp_path: pathlib.Path,
        *,
        provider: str,
        body: str,
        execution_id: str | None = None,
        update_status: str = "200",
        result_json: bytes
        | None = b'{"pr_title":"Agent title","pr_body":"Agent body"}',
        reset_store: bool = True,
        same_branch_twin: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        import os
        import shutil

        from preloop.utils.pr_metadata import PublicationRecord, upsert_provenance

        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        workspace = tmp_path / "workspace"
        (workspace / "evidence").mkdir(parents=True, exist_ok=True)
        if result_json is not None:
            (workspace / "result.json").write_bytes(result_json)
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            "GIT_CONFIG_GLOBAL": str(tmp_path / "home" / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Jane Doe",
            "GIT_AUTHOR_EMAIL": "jane@example.com",
            "GIT_COMMITTER_NAME": "Jane Doe",
            "GIT_COMMITTER_EMAIL": "jane@example.com",
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "PRELOOP_URL": "https://app.example.com",
            "FAKE_STORE": str(tmp_path / "store.json"),
            "FAKE_CALLS": str(tmp_path / "calls.txt"),
            "FAKE_KIND": provider,
            "FAKE_UPDATE_STATUS": update_status,
        }
        (tmp_path / "home").mkdir(exist_ok=True)
        if not (repo / ".git").exists():
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
            )
            (repo / "README.md").write_text("seed\n")
            subprocess.run(
                ["git", "add", "README.md"],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "seed"],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
            )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.head = head
        field = "description" if provider == "gitlab" else "body"
        seeded_body = body
        if "preloop:executions:start" not in body:
            seeded_body = upsert_provenance(
                body,
                [PublicationRecord(self.INITIAL, self.PRIOR_SHA)],
                "https://app.example.com",
            )
        row: dict[str, Any] = {
            "title": "Original title",
            field: seeded_body,
        }
        if provider == "gitlab":
            row.update(
                {
                    "iid": 7,
                    "source_branch": "preloop/issue-1",
                    "web_url": "https://gitlab.example.com/example/widgets/-/merge_requests/7",
                }
            )
        else:
            row.update(
                {
                    "number": 7,
                    "head": {"ref": "preloop/issue-1"},
                    "html_url": "https://github.com/example/widgets/pull/7",
                }
            )
        if reset_store or not (tmp_path / "store.json").exists():
            rows = [row]
            if same_branch_twin:
                twin = dict(row)
                if provider == "gitlab":
                    twin["iid"] = 8
                    twin["web_url"] = (
                        "https://gitlab.example.com/example/widgets/-/merge_requests/8"
                    )
                else:
                    twin["number"] = 8
                    twin["html_url"] = "https://github.com/example/widgets/pull/8"
                rows.append(twin)
            (tmp_path / "store.json").write_text(json.dumps(rows))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "curl").write_text(_FAKE_PROVIDER.format(python=sys.executable))
        (bin_dir / "curl").chmod(0o755)
        real_git = shutil.which("git")
        assert real_git is not None
        (bin_dir / "git").write_text("#!/bin/sh\n" + f'exec {real_git} "$@"\n')
        (bin_dir / "git").chmod(0o755)
        env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
        executor = ContainerAgentExecutor("codex", {}, "test-image")
        context = {
            "execution_id": execution_id or self.CURRENT,
            "flow_name": "Example flow",
            "trigger_event_data": {"issue": {"number": 1}},
            "git_clone_config": {
                "create_pull_request": True,
                "pull_request_title": "Configured title",
                "pull_request_description": "Configured body",
                "repositories": [
                    {
                        "repository_url": (
                            "https://gitlab.example.com/example/widgets.git"
                            if provider == "gitlab"
                            else "https://github.com/example/widgets.git"
                        ),
                        "clone_path": str(repo),
                        "tracker_id": "tracker-1",
                    }
                ],
            },
            "git_credentials_map": {
                "tracker-1": {"token": "fake-token", "tracker_type": provider}
            },
            "_git_target_branch": "preloop/issue-1",
            "_git_source_branch": "main",
        }
        script = executor._build_pr_or_mr_create_shell(
            execution_context=context,
            git_config=context["git_clone_config"],
            token_ref="${PRELOOP_GIT_TOKEN}",
            tracker_type=provider,
            host_kind=provider,
            repo_url=context["git_clone_config"]["repositories"][0]["repository_url"],
            safe_target="preloop/issue-1",
            safe_source="main",
        )
        assert script
        from preloop.agents.container import provenance_failure_exit_shell

        script = script.replace("/workspace", str(workspace)).replace(
            "/tmp/preloop-", str(tmp_path / "preloop-")
        )
        script += provenance_failure_exit_shell()
        return subprocess.run(
            ["bash", "-c", script],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )

    def _stored_body(self, tmp_path: pathlib.Path, provider: str) -> str:
        row = json.loads((tmp_path / "store.json").read_text())[0]
        return row["description" if provider == "gitlab" else "body"]

    def test_github_continuation_appends_record_and_keeps_prose(self, tmp_path):
        from preloop.utils.pr_metadata import PublicationRecord, upsert_provenance

        fixtures = pathlib.Path(__file__).parents[1] / "fixtures/pr_templates"
        prefix = (fixtures / "github/pull_request_template.md").read_text()
        suffix = (fixtures / "gitlab/Default.md").read_text()
        seeded = (
            upsert_provenance(
                prefix,
                [PublicationRecord(self.INITIAL, self.PRIOR_SHA)],
                "https://app.example.com",
            )
            + suffix
        )
        result = self._run(tmp_path, provider="github", body=seeded)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PRELOOP_PR_OPENED" in result.stdout
        body = self._stored_body(tmp_path, "github")
        assert body.startswith(prefix)
        assert body.endswith(suffix)
        assert self.INITIAL in body and self.PRIOR_SHA in body
        assert self.CURRENT in body and self.head in body
        assert json.loads((tmp_path / "store.json").read_text())[0]["title"] == (
            "Original title"
        )
        assert len(json.loads((tmp_path / "store.json").read_text())) == 1

    def test_gitlab_continuation_appends_record(self, tmp_path):
        from preloop.utils.pr_metadata import PublicationRecord, upsert_provenance

        fixtures = pathlib.Path(__file__).parents[1] / "fixtures/pr_templates"
        prefix = (fixtures / "gitlab/Default.md").read_text()
        seeded = (
            upsert_provenance(
                prefix,
                [PublicationRecord(self.INITIAL, self.PRIOR_SHA)],
                "https://app.example.com",
            )
            + "Human suffix\n"
        )
        result = self._run(tmp_path, provider="gitlab", body=seeded)
        assert result.returncode == 0, result.stdout + result.stderr
        body = self._stored_body(tmp_path, "gitlab")
        assert body.startswith(prefix)
        assert body.endswith("Human suffix\n")
        assert self.CURRENT in body and self.head in body
        assert json.loads((tmp_path / "store.json").read_text())[0]["title"] == (
            "Original title"
        )

    def test_repeated_continuation_is_idempotent_and_reuses_the_pr(self, tmp_path):
        first = self._run(tmp_path, provider="github", body="Human prose\n")
        assert first.returncode == 0, first.stdout + first.stderr
        calls = (tmp_path / "calls.txt").read_text()
        assert calls.count("POST ") == 1
        assert "GET " in calls
        (tmp_path / "calls.txt").write_text("")
        second = self._run(
            tmp_path,
            provider="github",
            body="ignored because seeded",
            reset_store=False,
        )
        assert second.returncode == 0, second.stdout + second.stderr
        assert "PRELOOP_PR_OPENED" in second.stdout
        body = self._stored_body(tmp_path, "github")
        assert body.count(self.CURRENT) == 1
        assert body.count(self.head) == 1
        second_calls = (tmp_path / "calls.txt").read_text()
        assert "POST " in second_calls
        assert "GET " in second_calls
        assert "PATCH " not in second_calls
        assert len(json.loads((tmp_path / "store.json").read_text())) == 1

    @pytest.mark.parametrize("update_status", ["422", "503"])
    def test_provider_update_failure_is_not_success(self, tmp_path, update_status):
        result = self._run(
            tmp_path,
            provider="github",
            body="Human prose\n",
            update_status=update_status,
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "PRELOOP_PR_METADATA_WARNING" in combined
        assert "PRELOOP_PR_OPENED" not in result.stdout
        assert self.CURRENT not in self._stored_body(tmp_path, "github")

    def test_missing_metadata_warns_and_keeps_existing_prose(self, tmp_path):
        fixtures = pathlib.Path(__file__).parents[1] / "fixtures/pr_templates"
        prefix = (fixtures / "github/pull_request_template.md").read_text()
        result = self._run(tmp_path, provider="github", body=prefix, result_json=None)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PRELOOP_PR_METADATA_WARNING" in result.stderr
        assert self._stored_body(tmp_path, "github").startswith(prefix)
        assert self.CURRENT in self._stored_body(tmp_path, "github")

    def test_malformed_region_leaves_the_body_unchanged(self, tmp_path):
        original = "Human prose\n<!-- preloop:executions:start -->\n"
        result = self._run(tmp_path, provider="gitlab", body=original)
        assert result.returncode != 0
        assert "PRELOOP_PR_METADATA_WARNING" in result.stderr
        assert "PRELOOP_PR_OPENED" not in result.stdout
        assert self._stored_body(tmp_path, "gitlab") == original

    def test_multiple_open_prs_are_not_reported_as_opened(self, tmp_path):
        original = "Human prose\n"
        result = self._run(
            tmp_path,
            provider="github",
            body=original,
            same_branch_twin=True,
        )
        assert result.returncode != 0
        assert "PRELOOP_PR_METADATA_WARNING" in result.stderr
        assert "PRELOOP_PR_OPENED" not in result.stdout
        stored = json.loads((tmp_path / "store.json").read_text())
        assert len(stored) == 2
        assert all(self.CURRENT not in row["body"] for row in stored)

    def test_near_limit_failure_notice_is_not_reported_as_opened(self, tmp_path):
        from preloop.utils.pr_metadata import PublicationRecord, upsert_provenance

        seeded = upsert_provenance(
            "Human prose\n",
            [PublicationRecord(self.INITIAL, self.PRIOR_SHA)],
            "https://app.example.com",
        )
        seeded += "h" * (65500 - len(seeded.encode("utf-8")))
        result = self._run(
            tmp_path,
            provider="github",
            body=seeded,
            result_json=(
                b'{"status":"failure","reason":"Tests are failing",'
                b'"pr_title":"Agent title","pr_body":"Agent body"}'
            ),
        )
        assert result.returncode != 0
        assert "PRELOOP_PR_METADATA_WARNING" in result.stderr
        assert "PRELOOP_PR_OPENED" not in result.stdout
        assert self._stored_body(tmp_path, "github") == seeded

    def test_oversize_region_leaves_the_body_unchanged(self, tmp_path):
        from preloop.utils.pr_metadata import PublicationRecord, upsert_provenance

        seeded = upsert_provenance(
            "Human prose\n",
            [PublicationRecord(self.INITIAL, self.PRIOR_SHA)],
            "https://app.example.com",
        )
        seeded += "h" * (65536 - len(seeded.encode("utf-8")))
        result = self._run(tmp_path, provider="github", body=seeded)
        assert result.returncode != 0
        assert "provider limit" in result.stderr
        assert "PRELOOP_PR_OPENED" not in result.stdout
        assert self._stored_body(tmp_path, "github") == seeded
