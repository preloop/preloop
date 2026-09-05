"""Tests for container agent executor."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.agents.container import ContainerAgentExecutor, _validated_git_ref

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
        assert content in result
        # Runtime symlink-containment guard travels with the block.
        assert "cd -P" in result

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
        assert "git config --global user.name 'evil\"; rm -rf / #'" in commands
        assert "git config --global user.email 'also\"; evil #@example.com'" in commands

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
