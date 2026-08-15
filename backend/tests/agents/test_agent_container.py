"""Tests for ContainerAgentExecutor."""

from unittest.mock import AsyncMock, patch, PropertyMock

import pytest

from preloop.agents.base import AgentStatus
from preloop.agents.container import ContainerAgentExecutor


@pytest.fixture
def mock_docker():
    """Mock aiodocker Docker client."""
    with patch("preloop.agents.container.aiodocker.Docker") as mock:
        docker_instance = AsyncMock()
        mock.return_value = docker_instance

        # Mock containers API
        mock_containers = AsyncMock()
        docker_instance.containers = mock_containers

        # Mock images API
        mock_images = AsyncMock()
        docker_instance.images = mock_images

        yield docker_instance


@pytest.fixture
def container_executor():
    """Create a ContainerAgentExecutor instance."""
    config = {
        "max_iterations": 10,
        "timeout": 3600,
    }
    return ContainerAgentExecutor(
        agent_type="test-agent",
        config=config,
        image="test-image:latest",
        use_kubernetes=False,
    )


class TestContainerAgentExecutor:
    """Test ContainerAgentExecutor class."""

    def test_init(self, container_executor):
        """Test ContainerAgentExecutor initialization."""
        assert container_executor.agent_type == "test-agent"
        assert container_executor.config["max_iterations"] == 10
        assert container_executor.image == "test-image:latest"
        assert container_executor.use_kubernetes is False

    def test_init_with_kubernetes(self):
        """Test ContainerAgentExecutor with Kubernetes enabled."""
        executor = ContainerAgentExecutor(
            agent_type="test-agent",
            config={},
            image="test-image:latest",
            use_kubernetes=True,
        )
        assert executor.use_kubernetes is True

    @pytest.mark.asyncio
    async def test_start_docker_success(self, container_executor, mock_docker):
        """Test starting a Docker container successfully."""
        # Mock image inspection (image exists)
        mock_docker.images.inspect = AsyncMock()

        # Mock container
        mock_container = AsyncMock()
        mock_container.id = "container-123"
        type(mock_container).id = PropertyMock(return_value="container-123")
        mock_container.start = AsyncMock()
        mock_docker.containers.create = AsyncMock(return_value=mock_container)

        execution_context = {
            "flow_id": "flow-456",
            "execution_id": "exec-789",
            "prompt": "Test prompt",
            "agent_config": {},
            "model_identifier": "gpt-5.4",
            "model_api_key": "test-key",
            "allowed_mcp_servers": ["preloop-mcp"],
            "allowed_mcp_tools": [
                {"server_name": "preloop-mcp", "tool_name": "tool1"},
                {"server_name": "preloop-mcp", "tool_name": "tool2"},
            ],
            "account_api_token": "test-token",
        }

        session_ref = await container_executor.start(execution_context)

        assert session_ref == "container-123"
        mock_docker.containers.create.assert_called_once()
        mock_container.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_kubernetes_fallback(self, mock_docker):
        """Test that Kubernetes execution falls back to Docker when not available."""
        with patch("preloop.agents.container.KUBERNETES_AVAILABLE", False):
            executor = ContainerAgentExecutor(
                agent_type="test-agent",
                config={},
                image="test-image:latest",
                use_kubernetes=True,
            )

            # Mock image inspection and container creation
            mock_docker.images.inspect = AsyncMock()
            mock_container = AsyncMock()
            mock_container.id = "container-k8s-123"
            type(mock_container).id = PropertyMock(return_value="container-k8s-123")
            mock_container.start = AsyncMock()
            mock_docker.containers.create = AsyncMock(return_value=mock_container)

            execution_context = {
                "flow_id": "flow-123",
                "execution_id": "exec-456",
                "prompt": "Test",
                "agent_config": {},
            }

            # Should fall back to Docker when Kubernetes is not available
            session_ref = await executor.start(execution_context)
            assert session_ref == "container-k8s-123"

    @pytest.mark.asyncio
    async def test_get_status_running(self, container_executor, mock_docker):
        """Test getting status of a running container."""
        mock_container = AsyncMock()
        mock_container.show = AsyncMock(
            return_value={"State": {"Running": True, "Status": "running"}}
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        status = await container_executor.get_status("container-123")

        assert status == AgentStatus.RUNNING
        mock_docker.containers.get.assert_called_once_with("container-123")

    @pytest.mark.asyncio
    async def test_get_status_exited_success(self, container_executor, mock_docker):
        """Test getting status of a successfully exited container."""
        mock_container = AsyncMock()
        mock_container.show = AsyncMock(
            return_value={
                "State": {"Running": False, "Status": "exited", "ExitCode": 0}
            }
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        status = await container_executor.get_status("container-123")

        assert status == AgentStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_get_status_exited_failure(self, container_executor, mock_docker):
        """Test getting status of a failed container."""
        mock_container = AsyncMock()
        mock_container.show = AsyncMock(
            return_value={
                "State": {"Running": False, "Status": "exited", "ExitCode": 1}
            }
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        status = await container_executor.get_status("container-123")

        assert status == AgentStatus.FAILED

    @pytest.mark.asyncio
    async def test_get_status_created(self, container_executor, mock_docker):
        """Test getting status of a newly created container."""
        mock_container = AsyncMock()
        mock_container.show = AsyncMock(
            return_value={"State": {"Running": False, "Status": "created"}}
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        status = await container_executor.get_status("container-123")

        assert status == AgentStatus.STARTING

    @pytest.mark.asyncio
    async def test_get_result_success(self, container_executor, mock_docker):
        """Test getting result from a successful execution."""
        mock_container = AsyncMock()
        mock_container.show = AsyncMock(
            return_value={
                "State": {"Running": False, "Status": "exited", "ExitCode": 0}
            }
        )
        mock_container.log = AsyncMock(
            return_value=[b"Agent started", b"Task completed successfully"]
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        result = await container_executor.get_result("container-123")

        assert result.status == AgentStatus.SUCCEEDED
        assert result.session_reference == "container-123"
        assert result.exit_code == 0
        assert "Agent started" in result.output_summary
        assert "Task completed successfully" in result.output_summary

    @pytest.mark.asyncio
    async def test_get_result_failure(self, container_executor, mock_docker):
        """Test getting result from a failed execution."""
        mock_container = AsyncMock()
        mock_container.show = AsyncMock(
            return_value={
                "State": {"Running": False, "Status": "exited", "ExitCode": 1}
            }
        )
        mock_container.log = AsyncMock(
            return_value=[b"Agent started", b"Error: Task failed"]
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        result = await container_executor.get_result("container-123")

        assert result.status == AgentStatus.FAILED
        assert result.exit_code == 1
        # Error message can be extracted from logs or from container exit code
        assert result.error_message is not None
        assert (
            "Error: Task failed" in result.error_message
            or "Container exited with code 1" in result.error_message
        )

    @pytest.mark.asyncio
    async def test_stop_container(self, container_executor, mock_docker):
        """Test stopping a running container."""
        mock_container = AsyncMock()
        mock_container.stop = AsyncMock()
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        await container_executor.stop("container-123")

        mock_docker.containers.get.assert_called_once_with("container-123")
        mock_container.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_logs(self, container_executor, mock_docker):
        """Test retrieving container logs."""
        mock_container = AsyncMock()
        mock_container.log = AsyncMock(
            return_value=[b"Log line 1", b"Log line 2", b"Log line 3"]
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        logs = await container_executor.get_logs("container-123", tail=3)

        assert len(logs) == 3
        assert logs[0] == "Log line 1"
        assert logs[1] == "Log line 2"
        assert logs[2] == "Log line 3"
        mock_container.log.assert_called_once_with(stdout=True, stderr=True, tail=3)

    @pytest.mark.asyncio
    async def test_get_logs_string_format(self, container_executor, mock_docker):
        """Test retrieving container logs when API returns strings instead of bytes."""
        mock_container = AsyncMock()
        # Some aiodocker versions return strings
        mock_container.log = AsyncMock(
            return_value=["String log 1", "String log 2", "String log 3"]
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        logs = await container_executor.get_logs("container-123", tail=3)

        assert len(logs) == 3
        assert logs[0] == "String log 1"
        assert logs[1] == "String log 2"
        assert logs[2] == "String log 3"

    @pytest.mark.asyncio
    async def test_container_labels(self, container_executor, mock_docker):
        """Test that containers are created with proper labels."""
        # Mock image inspection
        mock_docker.images.inspect = AsyncMock()

        mock_container = AsyncMock()
        type(mock_container).id = PropertyMock(return_value="container-xyz")
        mock_container.start = AsyncMock()

        # Capture the config passed to create
        async def capture_create(config):
            # Verify labels are present
            assert "Labels" in config
            labels = config["Labels"]
            assert labels["preloop.agent_type"] == "test-agent"
            assert labels["preloop.flow_id"] == "flow-123"
            assert labels["preloop.execution_id"] == "exec-456"
            return mock_container

        mock_docker.containers.create = AsyncMock(side_effect=capture_create)

        execution_context = {
            "flow_id": "flow-123",
            "execution_id": "exec-456",
            "prompt": "Test",
            "agent_config": {},
        }

        session_ref = await container_executor.start(execution_context)
        assert session_ref == "container-xyz"

    @pytest.mark.asyncio
    async def test_container_resource_limits(self, container_executor, mock_docker):
        """Test that containers have proper resource limits."""
        # Mock image inspection
        mock_docker.images.inspect = AsyncMock()

        mock_container = AsyncMock()
        type(mock_container).id = PropertyMock(return_value="container-xyz")
        mock_container.start = AsyncMock()

        # Capture the config passed to create
        async def capture_create(config):
            # Verify resource limits are present
            assert "HostConfig" in config
            host_config = config["HostConfig"]
            assert "Memory" in host_config
            assert "CpuQuota" in host_config
            return mock_container

        mock_docker.containers.create = AsyncMock(side_effect=capture_create)

        execution_context = {
            "flow_id": "flow-123",
            "execution_id": "exec-456",
            "prompt": "Test",
            "agent_config": {},
        }

        session_ref = await container_executor.start(execution_context)
        assert session_ref == "container-xyz"


def _result_artifact_tar(content: bytes, name: str = "result.json"):
    """Build an in-memory tarfile like Docker's archive API returns."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


class TestGetResultArtifact:
    """Tests for first-class /workspace/result.json capture."""

    @pytest.mark.asyncio
    async def test_valid_result_artifact(self, container_executor, mock_docker):
        """A valid JSON object in /workspace/result.json is returned parsed."""
        payload = (
            b'{"schema": "preloop.eval.result/v1", "status": "pass",'
            b' "summary": "ok", "metrics": {"latency_ms": 12}}'
        )
        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(
            return_value=_result_artifact_tar(payload)
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        artifact = await container_executor.get_result_artifact("container-123")

        assert artifact == {
            "schema": "preloop.eval.result/v1",
            "status": "pass",
            "summary": "ok",
            "metrics": {"latency_ms": 12},
        }
        mock_container.get_archive.assert_awaited_once_with("/workspace/result.json")

    @pytest.mark.asyncio
    async def test_missing_result_artifact_returns_none(
        self, container_executor, mock_docker
    ):
        """No result.json (Docker 404) is the normal case: returns None."""
        from aiodocker.exceptions import DockerError

        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(
            side_effect=DockerError(
                404, {"message": "Could not find the file in container"}
            )
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        artifact = await container_executor.get_result_artifact("container-123")

        assert artifact is None

    @pytest.mark.asyncio
    async def test_invalid_json_is_wrapped(self, container_executor, mock_docker):
        """A present-but-broken result.json yields a wrapped error object."""
        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(
            return_value=_result_artifact_tar(b"{not json")
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        artifact = await container_executor.get_result_artifact("container-123")

        assert artifact is not None
        assert artifact["error"] == "result_artifact_invalid_json"

    @pytest.mark.asyncio
    async def test_non_object_json_is_wrapped(self, container_executor, mock_docker):
        """A JSON array/scalar (not an object) yields a wrapped error object."""
        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(
            return_value=_result_artifact_tar(b'["not", "an", "object"]')
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        artifact = await container_executor.get_result_artifact("container-123")

        assert artifact is not None
        assert artifact["error"] == "result_artifact_not_object"

    @pytest.mark.asyncio
    async def test_oversized_artifact_is_not_persisted(
        self, container_executor, mock_docker
    ):
        """Artifacts above the size cap are reported, not stored."""
        from preloop.agents.container import MAX_RESULT_ARTIFACT_BYTES

        big = b'{"pad": "' + b"x" * (MAX_RESULT_ARTIFACT_BYTES + 10) + b'"}'
        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(return_value=_result_artifact_tar(big))
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        artifact = await container_executor.get_result_artifact("container-123")

        assert artifact is not None
        assert artifact["error"] == "result_artifact_too_large"
        assert artifact["limit_bytes"] == MAX_RESULT_ARTIFACT_BYTES

    @pytest.mark.asyncio
    async def test_kubernetes_backend_returns_none(self):
        """Kubernetes capture is stubbed (TODO): returns None."""
        executor = ContainerAgentExecutor(
            agent_type="test-agent",
            config={},
            image="test-image:latest",
            use_kubernetes=True,
        )

        artifact = await executor.get_result_artifact("job-123")

        assert artifact is None
