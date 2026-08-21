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


def _make_tar(files: dict[str, bytes]):
    """Build an in-memory multi-file tarfile like Docker's archive API."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in files.items():
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
    async def test_non_404_docker_error_is_wrapped(
        self, container_executor, mock_docker
    ):
        """A daemon failure (non-404) is an infra error, not "no artifact".

        It must stay visible as a wrapped error object so an eval run whose
        artifact could not be fetched is distinguishable from a run that
        reported nothing.
        """
        from aiodocker.exceptions import DockerError

        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(
            side_effect=DockerError(500, {"message": "daemon exploded"})
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        artifact = await container_executor.get_result_artifact("container-123")

        assert artifact is not None
        assert artifact["error"] == "result_artifact_fetch_failed"
        assert artifact["docker_status"] == 500
        assert "daemon exploded" in artifact["detail"]

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


def _k8s_executor() -> ContainerAgentExecutor:
    return ContainerAgentExecutor(
        agent_type="test-agent",
        config={},
        image="test-image:latest",
        use_kubernetes=True,
    )


def _emission_lines(
    channel: str, payload: bytes, status: str = "present", size: int | None = None
) -> list[str]:
    """Build the marker-line block the K8s wrapper script emits."""
    import base64

    declared = len(payload) if size is None else size
    lines = [f"PRELOOP_ARTIFACT_BEGIN {channel} {status} {declared}"]
    if status == "present":
        b64 = base64.b64encode(payload).decode()
        lines.extend(
            "PRELOOP_ARTIFACT_B64 " + b64[i : i + 76] for i in range(0, len(b64), 76)
        )
    lines.append(f"PRELOOP_ARTIFACT_END {channel}")
    return lines


class TestKubernetesResultArtifact:
    """Kubernetes capture parses the wrapper's log-channel emission."""

    @pytest.mark.asyncio
    async def test_present_result_is_parsed(self):
        executor = _k8s_executor()
        payload = b'{"schema": "preloop.eval.result/v1", "status": "success"}'
        lines = ["agent output", "FLOW_EXECUTION_SUCCESS"] + _emission_lines(
            "result", payload
        )
        executor._get_kubernetes_logs = AsyncMock(return_value=lines)

        artifact = await executor.get_result_artifact("job-123")

        assert artifact == {
            "schema": "preloop.eval.result/v1",
            "status": "success",
        }
        executor._get_kubernetes_logs.assert_awaited_once_with(
            "job-123", tail=None, include_artifact_streams=True
        )

    @pytest.mark.asyncio
    async def test_absent_result_returns_none(self):
        executor = _k8s_executor()
        executor._get_kubernetes_logs = AsyncMock(
            return_value=[
                "agent output",
                "PRELOOP_ARTIFACT_BEGIN result absent",
                "PRELOOP_ARTIFACT_END result",
            ]
        )

        assert await executor.get_result_artifact("job-123") is None

    @pytest.mark.asyncio
    async def test_no_emission_returns_none(self):
        """Logs without markers (wrapper not applied / logs gone): None."""
        executor = _k8s_executor()
        executor._get_kubernetes_logs = AsyncMock(return_value=["agent output"])

        assert await executor.get_result_artifact("job-123") is None

    @pytest.mark.asyncio
    async def test_too_large_is_wrapped(self):
        from preloop.agents.container import MAX_RESULT_ARTIFACT_BYTES

        executor = _k8s_executor()
        executor._get_kubernetes_logs = AsyncMock(
            return_value=[
                "PRELOOP_ARTIFACT_BEGIN result too_large 999999",
                "PRELOOP_ARTIFACT_END result",
            ]
        )

        artifact = await executor.get_result_artifact("job-123")

        assert artifact == {
            "error": "result_artifact_too_large",
            "size_bytes": 999999,
            "limit_bytes": MAX_RESULT_ARTIFACT_BYTES,
        }

    @pytest.mark.asyncio
    async def test_truncated_emission_is_visible_error(self):
        """A BEGIN without its END (log rotation) must not look like "no
        artifact"."""
        executor = _k8s_executor()
        lines = _emission_lines("result", b'{"status": "success"}')[:-1]
        executor._get_kubernetes_logs = AsyncMock(return_value=lines)

        artifact = await executor.get_result_artifact("job-123")

        assert artifact is not None
        assert artifact["error"] == "result_artifact_fetch_failed"

    @pytest.mark.asyncio
    async def test_corrupt_base64_is_visible_error(self):
        executor = _k8s_executor()
        executor._get_kubernetes_logs = AsyncMock(
            return_value=[
                "PRELOOP_ARTIFACT_BEGIN result present 10",
                "PRELOOP_ARTIFACT_B64 !!!not-base64!!!",
                "PRELOOP_ARTIFACT_END result",
            ]
        )

        artifact = await executor.get_result_artifact("job-123")

        assert artifact is not None
        assert artifact["error"] == "result_artifact_fetch_failed"

    @pytest.mark.asyncio
    async def test_invalid_json_shares_docker_error_shape(self):
        executor = _k8s_executor()
        executor._get_kubernetes_logs = AsyncMock(
            return_value=_emission_lines("result", b"{not json")
        )

        artifact = await executor.get_result_artifact("job-123")

        assert artifact is not None
        assert artifact["error"] == "result_artifact_invalid_json"

    @pytest.mark.asyncio
    async def test_last_emission_wins(self):
        """Only the wrapper's own (last) block is trusted, not echoes."""
        executor = _k8s_executor()
        lines = (
            _emission_lines("result", b'{"status": "fake"}')
            + ["more agent output"]
            + _emission_lines("result", b'{"status": "success"}')
        )
        executor._get_kubernetes_logs = AsyncMock(return_value=lines)

        artifact = await executor.get_result_artifact("job-123")

        assert artifact == {"status": "success"}


class TestEvidenceArchive:
    """Evidence pack capture (tar.gz of /workspace/evidence)."""

    @pytest.mark.asyncio
    async def test_kubernetes_evidence_round_trip(self):
        import io
        import tarfile as tarfile_mod

        buf = io.BytesIO()
        with tarfile_mod.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile_mod.TarInfo(name="evidence/findings.json")
            content = b'[{"id": "CVE-0000-0000"}]'
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        archive = buf.getvalue()

        executor = _k8s_executor()
        executor._get_kubernetes_logs = AsyncMock(
            return_value=_emission_lines("result", b'{"status": "success"}')
            + _emission_lines("evidence", archive)
        )

        captured = await executor.get_evidence_archive("job-123")

        assert captured == archive
        # And the payload really is a readable tar.gz
        with tarfile_mod.open(fileobj=io.BytesIO(captured), mode="r:gz") as tf:
            assert tf.getnames() == ["evidence/findings.json"]

    @pytest.mark.asyncio
    async def test_kubernetes_evidence_absent_returns_none(self):
        executor = _k8s_executor()
        executor._get_kubernetes_logs = AsyncMock(
            return_value=[
                "PRELOOP_ARTIFACT_BEGIN evidence absent",
                "PRELOOP_ARTIFACT_END evidence",
            ]
        )

        assert await executor.get_evidence_archive("job-123") is None

    @pytest.mark.asyncio
    async def test_kubernetes_evidence_too_large_returns_none(self):
        executor = _k8s_executor()
        executor._get_kubernetes_logs = AsyncMock(
            return_value=[
                "PRELOOP_ARTIFACT_BEGIN evidence too_large 99999999",
                "PRELOOP_ARTIFACT_END evidence",
            ]
        )

        assert await executor.get_evidence_archive("job-123") is None

    @pytest.mark.asyncio
    async def test_docker_evidence_repacked_as_tar_gz(
        self, container_executor, mock_docker
    ):
        import io
        import tarfile as tarfile_mod

        source = _make_tar(
            {
                "evidence/findings.json": b'[{"id": "CVE-0000-0000"}]',
                "evidence/report.md": b"# report\n",
            }
        )
        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(return_value=source)
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        captured = await container_executor.get_evidence_archive("container-123")

        assert captured is not None
        with tarfile_mod.open(fileobj=io.BytesIO(captured), mode="r:gz") as tf:
            names = sorted(tf.getnames())
            assert names == ["evidence/findings.json", "evidence/report.md"]
            member = tf.extractfile("evidence/report.md")
            assert member is not None and member.read() == b"# report\n"
        mock_container.get_archive.assert_awaited_once_with("/workspace/evidence")

    @pytest.mark.asyncio
    async def test_docker_evidence_missing_returns_none(
        self, container_executor, mock_docker
    ):
        from aiodocker.exceptions import DockerError

        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(
            side_effect=DockerError(404, {"message": "no such file"})
        )
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        assert await container_executor.get_evidence_archive("container-123") is None

    @pytest.mark.asyncio
    async def test_docker_evidence_oversized_returns_none(
        self, container_executor, mock_docker
    ):
        from preloop.agents.container import MAX_EVIDENCE_ARCHIVE_BYTES

        source = _make_tar(
            {"evidence/huge.bin": b"x" * (MAX_EVIDENCE_ARCHIVE_BYTES + 1)}
        )
        mock_container = AsyncMock()
        mock_container.get_archive = AsyncMock(return_value=source)
        mock_docker.containers.get = AsyncMock(return_value=mock_container)

        assert await container_executor.get_evidence_archive("container-123") is None


class TestKubernetesArtifactWrapper:
    """The wrapper script and its arg-rewriting hook."""

    def test_wraps_bash_dash_c_args(self):
        from preloop.agents.container import (
            K8S_ARTIFACT_WRAPPER_SCRIPT,
            ContainerAgentExecutor,
        )

        wrapped = ContainerAgentExecutor._wrap_kubernetes_args_for_artifacts(
            ["-c", "echo hello"]
        )
        assert wrapped is not None
        args, inner = wrapped
        assert args == ["-c", K8S_ARTIFACT_WRAPPER_SCRIPT]
        assert inner == "echo hello"

    @pytest.mark.parametrize(
        "args", [None, [], ["-c"], ["run"], ["-x", "echo"], ["-c", 42]]
    )
    def test_leaves_other_shapes_alone(self, args):
        assert ContainerAgentExecutor._wrap_kubernetes_args_for_artifacts(args) is None

    def test_wrapper_script_round_trip_through_parser(self, tmp_path):
        """Run the real wrapper with bash; parse its output like the backend.

        End-to-end over the actual shell: agent writes result.json and an
        evidence dir, the wrapper emits both channels into stdout, the
        parser recovers byte-identical content, and the inner exit code is
        preserved.
        """
        import io
        import json
        import shutil
        import subprocess
        import tarfile as tarfile_mod

        from preloop.agents.container import K8S_ARTIFACT_WRAPPER_SCRIPT

        if shutil.which("bash") is None:
            pytest.skip("bash not available")

        workspace = tmp_path / "workspace"
        (workspace / "evidence").mkdir(parents=True)
        result = {"schema": "preloop.cra.vulnscan/v1", "status": "success"}
        (workspace / "result.json").write_text(json.dumps(result))
        (workspace / "evidence" / "findings.json").write_text('[{"id": "X"}]')

        script = K8S_ARTIFACT_WRAPPER_SCRIPT.replace(
            "/workspace", str(workspace)
        ).replace("/tmp/preloop-evidence.tar.gz", str(tmp_path / "ev.tar.gz"))

        proc = subprocess.run(
            ["bash", "-c", script],
            env={"PATH": "/usr/bin:/bin", "PRELOOP_INNER_SCRIPT": "exit 3"},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 3  # inner exit code preserved
        lines = proc.stdout.splitlines()

        result_stream = ContainerAgentExecutor._extract_artifact_stream(lines, "result")
        assert result_stream is not None and result_stream["status"] == "present"
        assert json.loads(result_stream["data"]) == result

        evidence_stream = ContainerAgentExecutor._extract_artifact_stream(
            lines, "evidence"
        )
        assert evidence_stream is not None and evidence_stream["status"] == "present"
        with tarfile_mod.open(
            fileobj=io.BytesIO(evidence_stream["data"]), mode="r:gz"
        ) as tf:
            member = tf.extractfile("evidence/findings.json")
            assert member is not None and member.read() == b'[{"id": "X"}]'

    def test_wrapper_script_emits_absent_markers(self, tmp_path):
        import shutil
        import subprocess

        from preloop.agents.container import K8S_ARTIFACT_WRAPPER_SCRIPT

        if shutil.which("bash") is None:
            pytest.skip("bash not available")

        script = K8S_ARTIFACT_WRAPPER_SCRIPT.replace(
            "/workspace", str(tmp_path / "nope")
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            env={"PATH": "/usr/bin:/bin", "PRELOOP_INNER_SCRIPT": "true"},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        lines = proc.stdout.splitlines()
        assert (
            ContainerAgentExecutor._extract_artifact_stream(lines, "result")["status"]
            == "absent"
        )
        assert (
            ContainerAgentExecutor._extract_artifact_stream(lines, "evidence")["status"]
            == "absent"
        )


class TestKubernetesLogFiltering:
    """Artifact emission lines are stripped from operator-facing logs."""

    def _mock_pod_log_api(self, executor, log_text: str):
        from unittest.mock import MagicMock

        pod = MagicMock()
        pod.metadata.name = "agent-pod"
        pod.status.phase = "Running"
        pods = MagicMock()
        pods.items = [pod]

        response = AsyncMock()
        response.read = AsyncMock(return_value=log_text.encode())

        core_api = AsyncMock()
        core_api.list_namespaced_pod = AsyncMock(return_value=pods)
        core_api.read_namespaced_pod_log = AsyncMock(return_value=response)

        executor._init_kubernetes_clients = AsyncMock()
        executor._k8s_core_api = core_api

    @pytest.mark.asyncio
    async def test_filtered_by_default(self):
        executor = _k8s_executor()
        log_text = "\n".join(
            ["agent output", "FLOW_EXECUTION_SUCCESS"]
            + _emission_lines("result", b'{"status": "success"}')
        )
        self._mock_pod_log_api(executor, log_text)

        lines = await executor._get_kubernetes_logs("job-123")

        assert lines == ["agent output", "FLOW_EXECUTION_SUCCESS"]

    @pytest.mark.asyncio
    async def test_kept_for_artifact_capture(self):
        executor = _k8s_executor()
        emission = _emission_lines("result", b'{"status": "success"}')
        self._mock_pod_log_api(executor, "\n".join(["agent output"] + emission))

        lines = await executor._get_kubernetes_logs(
            "job-123", include_artifact_streams=True
        )

        assert lines == ["agent output"] + emission
