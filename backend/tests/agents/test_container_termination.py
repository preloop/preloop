"""Authoritative runtime termination evidence beats historical agent logs."""

from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.agents.container import ContainerAgentExecutor
from preloop.services.flow_failure_category import derive_failure_category
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


FINISHED = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
LOGS = [
    "Attempt 1 failed with status 504. Retrying...",
    "I have completed the first change and will now run tests.",
]


def pod(name: str, reason: str, exit_code: int, created: int = 1) -> SimpleNamespace:
    """Build a pod with a sidecar first and an explicitly named agent."""

    def container(name: str, reason: str, exit_code: int) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            container_id=f"containerd://{name}",
            state=SimpleNamespace(
                terminated=SimpleNamespace(
                    reason=reason,
                    message="",
                    exit_code=exit_code,
                    signal=9,
                    started_at=FINISHED,
                    finished_at=FINISHED,
                )
            ),
        )

    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, creation_timestamp=FINISHED.replace(day=created)
        ),
        status=SimpleNamespace(
            container_statuses=[
                container("sidecar", "OOMKilled", 137),
                container("agent", reason, exit_code),
            ]
        ),
    )


def kubernetes_executor(pods: list[SimpleNamespace]) -> ContainerAgentExecutor:
    """Never initialize live Kubernetes clients or access the default context."""
    executor = ContainerAgentExecutor("test", {}, "test-image", use_kubernetes=True)
    executor._init_kubernetes_clients = AsyncMock()
    executor._k8s_core_api = AsyncMock()
    executor._k8s_core_api.list_namespaced_pod.return_value = SimpleNamespace(
        items=pods
    )
    executor.get_status = AsyncMock(return_value=AgentStatus.FAILED)
    executor._get_kubernetes_terminal_logs = AsyncMock(return_value=LOGS)
    return executor


def assert_oom(result: AgentExecutionResult) -> None:
    assert result.status == AgentStatus.FAILED
    assert result.exit_code == 137
    assert "OOMKilled" in result.error_message
    assert "memory" in result.error_message.lower()
    assert result.failure_analysis.transient is False
    assert (
        derive_failure_category(
            status="FAILED",
            error_message=result.error_message,
            failure_analysis=asdict(result.failure_analysis),
        )
        == "runner_error"
    )
    orchestrator = FlowExecutionOrchestrator(MagicMock(), "flow", {}, AsyncMock())
    orchestrator.execution_logger = MagicMock()
    orchestrator.execution_logger.get_actions_taken.return_value = []
    orchestrator.execution_logger.get_agent_output_lines.return_value = LOGS
    assert (
        orchestrator._retry_decision(
            {
                "status": "FAILED",
                "exit_code": 137,
                "error_message": result.error_message,
                "failure_analysis": asdict(result.failure_analysis),
            }
        )
        is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AgentStatus.FAILED, AgentStatus.SUCCEEDED])
@pytest.mark.parametrize("logs", [LOGS, []])
async def test_kubernetes_oom_overrides_old_provider_error_and_progress(
    status: AgentStatus, logs: list[str]
) -> None:
    executor = kubernetes_executor([pod("test-pod", "OOMKilled", 137)])
    executor.get_status.return_value = status
    executor._get_kubernetes_terminal_logs.return_value = logs
    result = await executor.get_result("test-job")
    assert_oom(result)
    assert result.termination.container_name == "agent"
    assert result.termination.container_id == "containerd://agent"
    assert result.termination.pod_name == "test-pod"
    assert result.termination.reason == "OOMKilled"
    assert result.termination.signal == 9
    assert result.termination.finished_at == FINISHED.isoformat()


@pytest.mark.asyncio
async def test_bare_exit_137_does_not_prove_oom() -> None:
    executor = kubernetes_executor([pod("test-pod", "Error", 137)])
    result = await executor.get_result("test-job")
    assert result.termination.oom_killed is False
    assert "OOMKilled" not in result.error_message


@pytest.mark.asyncio
async def test_successful_agent_ignores_sidecar_oom_and_previous_pod() -> None:
    executor = kubernetes_executor(
        [
            pod("old-pod", "OOMKilled", 137),
            pod("new-pod", "Completed", 0, created=2),
        ]
    )
    executor.get_status.return_value = AgentStatus.SUCCEEDED
    executor._get_kubernetes_terminal_logs.return_value = ["Work completed"]
    result = await executor.get_result("test-job")
    assert result.status == AgentStatus.SUCCEEDED
    assert result.exit_code == 0
    assert result.termination.pod_name == "new-pod"
    assert result.termination.oom_killed is False
    assert result.error_message is None


@pytest.mark.asyncio
async def test_missing_agent_status_does_not_use_sidecar() -> None:
    sidecar_only = pod("test-pod", "Completed", 0)
    sidecar_only.status.container_statuses.pop()
    result = await kubernetes_executor([sidecar_only]).get_result("test-job")
    assert result.exit_code is None
    assert result.termination is None
    assert "OOMKilled" not in result.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize("oom_killed", [True, False])
async def test_docker_uses_oom_flag_not_exit_code(oom_killed: bool) -> None:
    executor = ContainerAgentExecutor("test", {}, "test-image", use_kubernetes=False)
    docker = AsyncMock()
    executor._get_docker_client = AsyncMock(return_value=docker)
    container = docker.containers.get.return_value
    container.show.return_value = {
        "Id": "container-id",
        "Name": "/agent-test",
        "State": {
            "ExitCode": 137,
            "OOMKilled": oom_killed,
            "FinishedAt": FINISHED.isoformat(),
            "Error": "misleading error",
        },
    }
    executor.get_status = AsyncMock(return_value=AgentStatus.FAILED)
    executor.get_logs = AsyncMock(return_value=LOGS)
    result = await executor.get_result("container-id")
    assert result.termination.oom_killed is oom_killed
    assert result.termination.container_name == "agent-test"
    assert result.termination.finished_at == FINISHED.isoformat()
    if oom_killed:
        assert_oom(result)
    else:
        assert "OOMKilled" not in result.error_message
