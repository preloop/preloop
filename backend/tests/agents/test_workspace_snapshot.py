"""Workspace snapshot capture, restore selection and setup commands.

Covers the runtime half of hosted workspace persistence (issue #386): the
container-side shell (executed for real against a temp workspace), the Docker
capture path and its size cap, the resume restore decision, and the
classification of a failed setup command.
"""

import gzip
import io
import os
import subprocess
import tarfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.agents.container import ContainerAgentExecutor, workspace_volume_name
from preloop.agents.failure_analysis import analyze_agent_failure
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_SETUP_FAILED,
    derive_failure_category,
)
from preloop.utils.workspace_snapshot import (
    SETUP_FAILED_MARKER,
    SETUP_LOG_PATH,
    WORKSPACE_SNAPSHOT_SKIPPED_MARKER,
    build_setup_commands_shell,
    build_workspace_snapshot_shell,
)


def _executor(**kwargs) -> ContainerAgentExecutor:
    return ContainerAgentExecutor(
        agent_type="codex",
        config={"timeout": 3600},
        image="test-image:latest",
        **kwargs,
    )


def _run_sh(script: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=cwd
    )


def _make_workspace(tmp_path, payload_bytes: int = 512) -> str:
    workspace = tmp_path / "workspace"
    (workspace / "repo").mkdir(parents=True)
    (workspace / "repo" / "main.py").write_bytes(b"x" * payload_bytes)
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "huge.bin").write_bytes(b"y" * 200_000)
    return str(workspace)


class TestSnapshotShell:
    def test_snapshot_writes_readable_archive_without_caches(self, tmp_path):
        workspace = _make_workspace(tmp_path)
        out = str(tmp_path / "snap.tar.gz")

        result = _run_sh(
            build_workspace_snapshot_shell(
                max_bytes=8 * 1024 * 1024, out_path=out, workspace_dir=workspace
            ),
            cwd=str(tmp_path),
        )

        assert result.returncode == 0, result.stderr
        assert WORKSPACE_SNAPSHOT_SKIPPED_MARKER not in result.stdout
        with tarfile.open(out, "r:gz") as archive:
            names = archive.getnames()
        assert any(name.endswith("workspace/repo/main.py") for name in names)
        assert not any("node_modules" in name for name in names)

    def test_snapshot_over_cap_is_skipped_with_reason(self, tmp_path):
        workspace = _make_workspace(tmp_path, payload_bytes=400_000)
        out = str(tmp_path / "snap.tar.gz")

        result = _run_sh(
            build_workspace_snapshot_shell(
                max_bytes=2048, out_path=out, workspace_dir=workspace
            ),
            cwd=str(tmp_path),
        )

        assert result.returncode == 0
        assert (
            f"{WORKSPACE_SNAPSHOT_SKIPPED_MARKER} size_exceeds_limit" in result.stdout
        )
        # The cap is enforced before anything is kept: no partial archive.
        assert not os.path.exists(out)

    def test_missing_workspace_is_not_an_error(self, tmp_path):
        out = str(tmp_path / "snap.tar.gz")
        result = _run_sh(
            build_workspace_snapshot_shell(
                max_bytes=1024,
                out_path=out,
                workspace_dir=str(tmp_path / "absent"),
            ),
            cwd=str(tmp_path),
        )
        assert result.returncode == 0
        assert not os.path.exists(out)


class TestDockerSnapshotCapture:
    @staticmethod
    def _fake_docker(snapshot_bytes: bytes | None, *, helper=None):
        agent_container = MagicMock()
        agent_container.show = AsyncMock(
            return_value={
                "Config": {"Labels": {"preloop.execution_id": "exec-1"}},
            }
        )
        helper = helper or MagicMock()
        helper.start = AsyncMock()
        helper.wait = AsyncMock()
        helper.delete = AsyncMock()
        if snapshot_bytes is None:
            helper.get_archive = AsyncMock(side_effect=RuntimeError("no such file"))
        else:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as out:
                info = tarfile.TarInfo("preloop-workspace.tar.gz")
                info.size = len(snapshot_bytes)
                out.addfile(info, io.BytesIO(snapshot_bytes))
            buffer.seek(0)
            helper.get_archive = AsyncMock(return_value=tarfile.open(fileobj=buffer))
        docker = MagicMock()
        docker.containers.get = AsyncMock(return_value=agent_container)
        docker.containers.create = AsyncMock(return_value=helper)
        return docker, helper

    @pytest.mark.asyncio
    async def test_snapshot_is_built_on_the_workspace_volume(self, monkeypatch):
        payload = gzip.compress(b"snapshot")
        docker, helper = self._fake_docker(payload)
        executor = _executor()
        monkeypatch.setattr(
            executor, "_get_docker_client", AsyncMock(return_value=docker)
        )

        data = await executor.get_workspace_snapshot("container-1")

        assert data == payload
        config = docker.containers.create.await_args.kwargs["config"]
        assert config["HostConfig"]["Binds"] == [
            f"{workspace_volume_name('exec-1')}:/workspace:rw"
        ]
        helper.delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_snapshot_over_cap_is_dropped(self, monkeypatch):
        docker, _ = self._fake_docker(b"x" * 4096)
        executor = _executor()
        monkeypatch.setattr(
            executor, "_get_docker_client", AsyncMock(return_value=docker)
        )
        monkeypatch.setattr(
            "preloop.agents.container.settings.workspace_snapshot_max_bytes", 1024
        )

        assert await executor.get_workspace_snapshot("container-1") is None

    @pytest.mark.asyncio
    async def test_disabled_cap_skips_capture_entirely(self, monkeypatch):
        docker, _ = self._fake_docker(b"payload")
        executor = _executor()
        monkeypatch.setattr(
            executor, "_get_docker_client", AsyncMock(return_value=docker)
        )
        monkeypatch.setattr(
            "preloop.agents.container.settings.workspace_snapshot_max_bytes", 0
        )

        assert await executor.get_workspace_snapshot("container-1") is None
        docker.containers.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_archive_returns_none(self, monkeypatch):
        docker, helper = self._fake_docker(None)
        executor = _executor()
        monkeypatch.setattr(
            executor, "_get_docker_client", AsyncMock(return_value=docker)
        )

        assert await executor.get_workspace_snapshot("container-1") is None
        helper.delete.assert_awaited()


class TestKubernetesSnapshotChannel:
    @pytest.mark.asyncio
    async def test_workspace_channel_is_decoded_from_pod_logs(self, monkeypatch):
        import base64

        payload = gzip.compress(b"k8s-snapshot")
        lines = [
            "agent output",
            f"PRELOOP_ARTIFACT_BEGIN workspace present {len(payload)}",
            "PRELOOP_ARTIFACT_B64 " + base64.b64encode(payload).decode(),
            "PRELOOP_ARTIFACT_END workspace",
        ]
        executor = _executor(use_kubernetes=True)
        monkeypatch.setattr(
            executor, "_get_kubernetes_terminal_logs", AsyncMock(return_value=lines)
        )

        assert await executor.get_workspace_snapshot("agent-job") == payload

    @pytest.mark.asyncio
    async def test_absent_channel_returns_none(self, monkeypatch):
        executor = _executor(use_kubernetes=True)
        monkeypatch.setattr(
            executor,
            "_get_kubernetes_terminal_logs",
            AsyncMock(
                return_value=[
                    "PRELOOP_ARTIFACT_BEGIN workspace absent",
                    "PRELOOP_ARTIFACT_END workspace",
                ]
            ),
        )

        assert await executor.get_workspace_snapshot("agent-job") is None

    def test_wrapper_emits_the_workspace_channel(self):
        from preloop.agents.container import (
            K8S_ARTIFACT_WRAPPER_SCRIPT,
            K8S_WORKSPACE_STREAM_MAX_BYTES,
            k8s_workspace_snapshot_limit,
        )

        assert "PRELOOP_ARTIFACT_BEGIN workspace present" in K8S_ARTIFACT_WRAPPER_SCRIPT
        assert "_preloop_snapshot_workspace" in K8S_ARTIFACT_WRAPPER_SCRIPT
        # The log channel budget, not the 512 MiB Docker budget.
        assert k8s_workspace_snapshot_limit() == K8S_WORKSPACE_STREAM_MAX_BYTES


class TestRestorePathSelection:
    @staticmethod
    def _context(**overrides):
        context = {
            "execution_id": "exec-2",
            "flow_id": "flow-1",
            "flow_name": "impl",
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": "https://github.com/acme/repo.git",
                        "clone_path": "/workspace/repo",
                    }
                ],
            },
            "trigger_event_data": {
                "_resume": {
                    "execution_id": "exec-1",
                    "source_branch": "preloop/impl-abcd1234",
                }
            },
        }
        context.update(overrides)
        return context

    def test_restore_replaces_clone_with_fetch_and_checkout(self):
        executor = _executor()
        context = self._context(workspace_restore_archive=b"gz")

        commands = executor._prepare_init_commands(context)

        assert "if [ -d /workspace/repo/.git ]; then" in commands
        assert "skipping git clone" in commands
        assert "git fetch origin preloop/impl-abcd1234" in commands
        assert "git merge --ff-only origin/preloop/impl-abcd1234" in commands
        # The clone survives as the fallback branch of the guard.
        assert "git clone" in commands
        assert commands.strip().endswith("fi")

    def test_without_snapshot_the_clone_is_unguarded(self):
        executor = _executor()

        commands = executor._prepare_init_commands(self._context())

        assert "if [ -d /workspace/repo/.git ]" not in commands
        assert "git clone" in commands

    def test_kubernetes_runner_falls_back_to_clone(self):
        executor = _executor(use_kubernetes=True)
        context = self._context(workspace_restore_archive=b"gz")

        assert executor.workspace_restore_planned(context) is False
        assert "skipping git clone" not in executor._prepare_init_commands(context)

    @pytest.mark.asyncio
    async def test_restore_puts_the_archive_into_the_created_container(self):
        executor = _executor()
        container = MagicMock()
        container.put_archive = AsyncMock()
        archive = gzip.compress(b"tar-bytes")

        restored = await executor._restore_docker_workspace(
            container, self._context(workspace_restore_archive=archive)
        )

        assert restored is True
        path, data = container.put_archive.await_args.args
        assert path == "/"
        assert data == b"tar-bytes"

    @pytest.mark.asyncio
    async def test_restore_failure_is_not_fatal(self):
        executor = _executor()
        container = MagicMock()
        container.put_archive = AsyncMock(side_effect=RuntimeError("boom"))

        restored = await executor._restore_docker_workspace(
            container, self._context(workspace_restore_archive=gzip.compress(b"t"))
        )

        assert restored is False


class TestSetupCommands:
    def test_setup_block_runs_after_clone_and_before_the_agent(self):
        executor = _executor()
        context = {
            "execution_id": "exec-3",
            "flow_id": "flow-1",
            "flow_name": "impl",
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {
                        "repository_url": "https://github.com/acme/repo.git",
                        "clone_path": "/workspace/repo",
                    }
                ],
                "setup_commands": ["pip install -r requirements/dev.txt"],
            },
            "trigger_event_data": {},
        }

        commands = executor._prepare_init_commands(context)

        assert commands.index("git clone") < commands.index("pip install")
        assert SETUP_LOG_PATH in commands
        assert "cd /workspace/repo" in commands

    def test_failing_setup_command_exits_with_the_marker(self, tmp_path):
        log = tmp_path / "evidence" / "setup.log"
        script = build_setup_commands_shell(
            ["echo first", "exit 3", "echo unreachable"],
            working_dir=str(tmp_path),
        ).replace(SETUP_LOG_PATH, str(log))

        result = _run_sh(script, cwd=str(tmp_path))

        assert result.returncode == 3
        assert SETUP_FAILED_MARKER in result.stdout
        assert "unreachable" not in log.read_text()
        assert "first" in log.read_text()

    def test_successful_setup_captures_output_and_continues(self, tmp_path):
        log = tmp_path / "evidence" / "setup.log"
        script = build_setup_commands_shell(
            ["echo installing"], working_dir=str(tmp_path)
        ).replace(SETUP_LOG_PATH, str(log))

        result = _run_sh(script, cwd=str(tmp_path))

        assert result.returncode == 0
        assert SETUP_FAILED_MARKER not in result.stdout
        assert log.read_text().strip() == "installing"

    def test_setup_failure_is_classified_as_setup_failed(self):
        logs = "\n".join(
            [
                "Cloning into '/workspace/repo'...",
                "Running 1 setup command(s), output -> /workspace/evidence/setup.log",
                f"{SETUP_FAILED_MARKER} exit=1",
                "Setup commands failed (exit 1). Last lines of setup.log:",
                "E: Unable to locate package postgresql-client",
            ]
        )

        analysis = analyze_agent_failure(logs)

        assert analysis.message.startswith("Setup commands failed (exit 1)")
        assert analysis.transient is False
        assert (
            derive_failure_category(
                status="FAILED",
                error_message=analysis.message,
                failure_analysis=analysis,
            )
            == FAILURE_CATEGORY_SETUP_FAILED
        )

    def test_agent_failure_is_still_not_setup_failed(self):
        analysis = analyze_agent_failure("Traceback (most recent call last):\nboom")

        assert (
            derive_failure_category(
                status="FAILED",
                error_message=analysis.message,
                failure_analysis=analysis,
            )
            != FAILURE_CATEGORY_SETUP_FAILED
        )
