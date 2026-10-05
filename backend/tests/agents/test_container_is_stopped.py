"""``is_stopped`` is termination evidence, not a stop request echo.

A stop is only confirmed (``stop_confirmed_at``) on what this returns, so it
has to say True exactly when the runtime is gone.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiodocker.exceptions import DockerError
from kubernetes_asyncio.client.rest import ApiException

from preloop.agents.container import ContainerAgentExecutor

pytestmark = pytest.mark.asyncio


def _executor(use_kubernetes: bool) -> ContainerAgentExecutor:
    return ContainerAgentExecutor(
        agent_type="codex",
        config={},
        image="dummy-image",
        use_kubernetes=use_kubernetes,
    )


def _docker_with(container_get) -> MagicMock:
    docker = MagicMock()
    docker.containers.get = container_get
    return docker


async def test_docker_container_that_no_longer_exists_is_stopped(monkeypatch):
    executor = _executor(False)
    missing = AsyncMock(side_effect=DockerError(404, {"message": "No such container"}))
    monkeypatch.setattr(
        executor, "_get_docker_client", AsyncMock(return_value=_docker_with(missing))
    )
    assert await executor.is_stopped("agent-gone") is True


async def test_docker_lookup_failure_is_not_confirmation(monkeypatch):
    executor = _executor(False)
    broken = AsyncMock(side_effect=DockerError(500, {"message": "daemon error"}))
    monkeypatch.setattr(
        executor, "_get_docker_client", AsyncMock(return_value=_docker_with(broken))
    )
    with pytest.raises(DockerError):
        await executor.is_stopped("agent-unknown")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"Running": True, "Status": "running"}, False),
        ({"Running": False, "Status": "exited"}, True),
    ],
)
async def test_docker_state_decides(monkeypatch, state, expected):
    executor = _executor(False)
    container = MagicMock()
    container.show = AsyncMock(return_value={"State": state})
    monkeypatch.setattr(
        executor,
        "_get_docker_client",
        AsyncMock(return_value=_docker_with(AsyncMock(return_value=container))),
    )
    assert await executor.is_stopped("agent-1") is expected


async def test_kubernetes_job_deleting_is_not_stopped_until_pods_are_gone(
    monkeypatch,
):
    """Foreground deletion accepted: the Job is gone before its pods are."""
    executor = _executor(True)
    monkeypatch.setattr(executor, "_init_kubernetes_clients", AsyncMock())
    executor._k8s_batch_api = MagicMock()
    executor._k8s_batch_api.read_namespaced_job_status = AsyncMock(
        side_effect=ApiException(status=404)
    )
    executor._k8s_core_api = MagicMock()
    executor._k8s_core_api.list_namespaced_pod = AsyncMock(
        side_effect=[
            SimpleNamespace(items=[object()]),
            SimpleNamespace(items=[]),
        ]
    )
    assert await executor.is_stopped("agent-job") is False
    assert await executor.is_stopped("agent-job") is True
