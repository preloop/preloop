"""Tests for agent log streaming functionality."""

from unittest.mock import AsyncMock, patch, MagicMock
import pytest

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


@pytest.fixture
def k8s_executor():
    """Create a Kubernetes ContainerAgentExecutor instance."""
    config = {
        "max_iterations": 10,
        "timeout": 3600,
    }
    return ContainerAgentExecutor(
        agent_type="test-agent",
        config=config,
        image="test-image:latest",
        use_kubernetes=True,
    )


class TestDockerLogStreaming:
    """Test Docker log streaming functionality."""

    @pytest.mark.asyncio
    async def test_stream_docker_logs_success(self, container_executor, mock_docker):
        """Test streaming logs from a Docker container."""
        mock_container = AsyncMock()

        # Mock streaming logs
        async def mock_log_stream(*args, **kwargs):
            # Simulate streaming log lines
            for line in [b"Line 1\n", b"Line 2\n", b"Line 3\n"]:
                yield line

        mock_container.log = mock_log_stream
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        # Collect streamed lines
        lines = []
        async for line in container_executor.stream_logs("container-123"):
            lines.append(line)

        assert len(lines) == 3
        assert lines[0] == "Line 1"
        assert lines[1] == "Line 2"
        assert lines[2] == "Line 3"
        mock_docker.containers.get.assert_called_once_with("container-123")

    @pytest.mark.asyncio
    async def test_stream_docker_logs_empty_lines_filtered(
        self, container_executor, mock_docker
    ):
        """Test that empty lines are filtered out."""
        mock_container = AsyncMock()

        async def mock_log_stream(*args, **kwargs):
            for line in [b"Line 1\n", b"\n", b"Line 2\n", b"", b"Line 3\n"]:
                yield line

        mock_container.log = mock_log_stream
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        lines = []
        async for line in container_executor.stream_logs("container-123"):
            lines.append(line)

        # Empty lines should be filtered
        assert len(lines) == 3
        assert lines == ["Line 1", "Line 2", "Line 3"]

    @pytest.mark.asyncio
    async def test_stream_docker_logs_string_format(
        self, container_executor, mock_docker
    ):
        """Test that string logs (not bytes) are handled correctly."""
        mock_container = AsyncMock()

        async def mock_log_stream(*args, **kwargs):
            # Some aiodocker versions return strings instead of bytes
            for line in ["String line 1\n", "String line 2\n", "String line 3\n"]:
                yield line

        mock_container.log = mock_log_stream
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        lines = []
        async for line in container_executor.stream_logs("container-123"):
            lines.append(line)

        assert len(lines) == 3
        assert lines[0] == "String line 1"
        assert lines[1] == "String line 2"
        assert lines[2] == "String line 3"

    @pytest.mark.asyncio
    async def test_stream_docker_logs_unicode(self, container_executor, mock_docker):
        """Test streaming logs with unicode characters."""
        mock_container = AsyncMock()

        async def mock_log_stream(*args, **kwargs):
            for line in [b"Hello \xf0\x9f\x91\x8b\n", b"Testing \xe2\x9c\x93\n"]:
                yield line

        mock_container.log = mock_log_stream
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        lines = []
        async for line in container_executor.stream_logs("container-123"):
            lines.append(line)

        assert len(lines) == 2
        assert "👋" in lines[0]
        assert "✓" in lines[1]

    @pytest.mark.asyncio
    async def test_stream_docker_logs_handles_docker_error(
        self, container_executor, mock_docker
    ):
        """Test that streaming handles Docker errors gracefully."""
        from aiodocker.exceptions import DockerError

        mock_docker.containers.get = AsyncMock(
            side_effect=DockerError(500, {"message": "Container not found"})
        )

        # Should yield error message instead of crashing
        lines = []
        async for line in container_executor.stream_logs("container-123"):
            lines.append(line)

        assert len(lines) > 0
        assert "[ERROR]" in lines[0]
        assert "Failed to stream logs" in lines[0]

    @pytest.mark.asyncio
    async def test_stream_docker_logs_handles_unexpected_error(
        self, container_executor, mock_docker
    ):
        """Test that streaming handles unexpected errors gracefully."""
        mock_container = AsyncMock()

        async def mock_log_stream(*args, **kwargs):
            yield b"Line 1\n"
            raise ValueError("Unexpected error")

        mock_container.log = mock_log_stream
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        lines = []
        async for line in container_executor.stream_logs("container-123"):
            lines.append(line)

        # Should get first line and then error
        assert len(lines) >= 1
        assert lines[0] == "Line 1"
        # The error should be caught and logged


class TestKubernetesLogStreaming:
    """Test Kubernetes log streaming functionality."""

    @pytest.mark.asyncio
    async def test_stream_kubernetes_logs_success(self, k8s_executor):
        """Test streaming logs from a Kubernetes pod."""
        with patch("preloop.agents.container.KUBERNETES_AVAILABLE", True):
            with patch("preloop.agents.container.config") as mock_config:
                with patch("preloop.agents.container.client") as mock_client:
                    # Mock K8s API initialization
                    mock_config.load_incluster_config = MagicMock()
                    mock_batch_api = AsyncMock()
                    mock_core_api = AsyncMock()
                    mock_client.BatchV1Api.return_value = mock_batch_api
                    mock_client.CoreV1Api.return_value = mock_core_api

                    # Mock pod listing
                    mock_pod = MagicMock()
                    mock_pod.metadata.name = "test-pod-123"
                    mock_pods_list = MagicMock()
                    mock_pods_list.items = [mock_pod]
                    mock_core_api.list_namespaced_pod = AsyncMock(
                        return_value=mock_pods_list
                    )

                    # Mock log streaming response
                    mock_response = AsyncMock()

                    async def mock_content():
                        for line in [b"K8s Line 1\n", b"K8s Line 2\n", b"K8s Line 3\n"]:
                            yield line

                    mock_response.content = mock_content()
                    mock_core_api.read_namespaced_pod_log = AsyncMock(
                        return_value=mock_response
                    )

                    # Collect streamed lines
                    lines = []
                    async for line in k8s_executor.stream_logs("job-name"):
                        lines.append(line)

                    assert len(lines) == 3
                    assert lines[0] == "K8s Line 1"
                    assert lines[1] == "K8s Line 2"
                    assert lines[2] == "K8s Line 3"

    @pytest.mark.asyncio
    async def test_stream_kubernetes_logs_no_pods(self, k8s_executor, monkeypatch):
        """Test streaming when no pods are found."""
        # Exercise every retry without spending a minute on real wall-clock waits.
        sleep = AsyncMock()
        monkeypatch.setattr("preloop.agents.container.asyncio.sleep", sleep)
        with patch("preloop.agents.container.KUBERNETES_AVAILABLE", True):
            with patch("preloop.agents.container.config") as mock_config:
                with patch("preloop.agents.container.client") as mock_client:
                    mock_config.load_incluster_config = MagicMock()
                    mock_batch_api = AsyncMock()
                    mock_core_api = AsyncMock()
                    mock_client.BatchV1Api.return_value = mock_batch_api
                    mock_client.CoreV1Api.return_value = mock_core_api

                    # Mock empty pod list
                    mock_pods_list = MagicMock()
                    mock_pods_list.items = []
                    mock_core_api.list_namespaced_pod = AsyncMock(
                        return_value=mock_pods_list
                    )

                    lines = []
                    async for line in k8s_executor.stream_logs("job-name"):
                        lines.append(line)

                    assert len(lines) == 1
                    assert "[WARN]" in lines[0]
                    assert "No pods found" in lines[0]
                    assert mock_core_api.list_namespaced_pod.await_count == 60
                    assert sleep.await_count == 59
                    assert all(call.args == (1,) for call in sleep.await_args_list)

    @pytest.mark.asyncio
    async def test_stream_kubernetes_logs_handles_api_exception(self, k8s_executor):
        """Test that streaming handles Kubernetes API exceptions."""
        from kubernetes_asyncio.client.rest import ApiException

        with patch("preloop.agents.container.KUBERNETES_AVAILABLE", True):
            with patch("preloop.agents.container.config") as mock_config:
                with patch("preloop.agents.container.client") as mock_client:
                    mock_config.load_incluster_config = MagicMock()
                    mock_batch_api = AsyncMock()
                    mock_core_api = AsyncMock()
                    mock_client.BatchV1Api.return_value = mock_batch_api
                    mock_client.CoreV1Api.return_value = mock_core_api

                    # Mock API exception with proper ApiException class
                    api_error = ApiException(status=404, reason="Not Found")
                    mock_core_api.list_namespaced_pod = AsyncMock(side_effect=api_error)

                    lines = []
                    async for line in k8s_executor.stream_logs("job-name"):
                        lines.append(line)

                    assert len(lines) > 0
                    assert "[WARN]" in lines[0] or "[ERROR]" in lines[0]


class TestStreamLogsUnified:
    """Test the unified stream_logs interface."""

    @pytest.mark.asyncio
    async def test_stream_logs_routes_to_docker(self, container_executor, mock_docker):
        """Test that stream_logs routes to Docker implementation."""
        mock_container = AsyncMock()

        async def mock_log_stream(*args, **kwargs):
            yield b"Docker log\n"

        mock_container.log = mock_log_stream
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        lines = []
        async for line in container_executor.stream_logs("container-123"):
            lines.append(line)

        assert len(lines) == 1
        assert lines[0] == "Docker log"

    @pytest.mark.asyncio
    async def test_stream_logs_routes_to_kubernetes(self, k8s_executor):
        """Test that stream_logs routes to Kubernetes implementation."""
        with patch("preloop.agents.container.KUBERNETES_AVAILABLE", True):
            with patch("preloop.agents.container.config") as mock_config:
                with patch("preloop.agents.container.client") as mock_client:
                    mock_config.load_incluster_config = MagicMock()
                    mock_batch_api = AsyncMock()
                    mock_core_api = AsyncMock()
                    mock_client.BatchV1Api.return_value = mock_batch_api
                    mock_client.CoreV1Api.return_value = mock_core_api

                    mock_pod = MagicMock()
                    mock_pod.metadata.name = "test-pod"
                    mock_pods_list = MagicMock()
                    mock_pods_list.items = [mock_pod]
                    mock_core_api.list_namespaced_pod = AsyncMock(
                        return_value=mock_pods_list
                    )

                    mock_response = AsyncMock()

                    async def mock_content():
                        yield b"K8s log\n"

                    mock_response.content = mock_content()
                    mock_core_api.read_namespaced_pod_log = AsyncMock(
                        return_value=mock_response
                    )

                    lines = []
                    async for line in k8s_executor.stream_logs("job-name"):
                        lines.append(line)

                    assert len(lines) == 1
                    assert lines[0] == "K8s log"
