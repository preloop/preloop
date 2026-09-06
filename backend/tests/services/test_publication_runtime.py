"""Destruction proof uses actual runtime configs, not generic success states."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiodocker.exceptions import DockerError
from kubernetes_asyncio.client.exceptions import ApiException

from preloop.agents.container import ContainerAgentExecutor
from preloop.services.publication_runtime import remove_owned_publication_runtime
from preloop.services.trusted_publisher import PublicationError


@pytest.mark.asyncio
async def test_docker_removal_uses_labels_created_by_real_start(monkeypatch):
    execution = str(uuid4())
    executor = ContainerAgentExecutor("codex", {}, "example/harness:stable")
    container = SimpleNamespace(id="a" * 64, start=AsyncMock(), delete=AsyncMock())
    docker = SimpleNamespace(images=SimpleNamespace(inspect=AsyncMock()))
    captured = {}

    async def create(*, config):
        captured.update(config)
        return container

    docker.containers = SimpleNamespace(create=AsyncMock(side_effect=create))
    monkeypatch.setattr(executor, "_get_docker_client", AsyncMock(return_value=docker))
    monkeypatch.setattr(executor, "_restore_docker_workspace", AsyncMock())
    reference = await executor._start_docker_container(
        {"execution_id": execution, "flow_id": str(uuid4()), "prompt": "question"}
    )
    container.show = AsyncMock(return_value={"Id": reference, "Config": captured})
    docker.containers.get = AsyncMock(
        side_effect=[container, container, DockerError(404, {"message": "missing"})]
    )
    await remove_owned_publication_runtime(executor, reference, execution)
    container.delete.assert_awaited_once_with(force=True, v=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["initial_missing", "wrong_owner", "delete_failed", "still_present", "auth_error"],
)
async def test_docker_errors_never_confirm_removal(mode, monkeypatch):
    execution = str(uuid4())
    executor = ContainerAgentExecutor("codex", {}, "example/harness:stable")
    container = SimpleNamespace(
        show=AsyncMock(
            return_value={
                "Id": "a" * 64,
                "Config": {"Labels": {"preloop.execution_id": execution}},
            }
        ),
        delete=AsyncMock(),
    )
    get = AsyncMock(return_value=container)
    if mode == "initial_missing":
        get.side_effect = DockerError(404, {"message": "missing"})
    elif mode == "wrong_owner":
        container.show.return_value["Config"]["Labels"]["preloop.execution_id"] = str(
            uuid4()
        )
    elif mode == "delete_failed":
        container.delete.side_effect = DockerError(500, {"message": "failed"})
    elif mode == "auth_error":
        get.side_effect = [
            container,
            container,
            DockerError(403, {"message": "denied"}),
        ]
    monkeypatch.setattr(
        executor,
        "_get_docker_client",
        AsyncMock(return_value=SimpleNamespace(containers=SimpleNamespace(get=get))),
    )
    with pytest.raises(PublicationError):
        await remove_owned_publication_runtime(
            executor, "a" * 64, execution, timeout_seconds=0.02
        )
    if mode in {"initial_missing", "wrong_owner"}:
        container.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_kubernetes_removal_uses_actual_job_uid_labels_and_pods(monkeypatch):
    execution = str(uuid4())
    executor = ContainerAgentExecutor(
        "codex", {}, "example/harness:stable", use_kubernetes=True
    )
    captured = {}

    async def create(job, *, job_name, execution_id):
        job.metadata.uid = "owned-job-uid"
        captured["job"] = job
        return job_name

    monkeypatch.setattr(executor, "_init_kubernetes_clients", AsyncMock())
    monkeypatch.setattr(
        executor, "_create_kubernetes_job", AsyncMock(side_effect=create)
    )
    reference = await executor._start_kubernetes_pod(
        {
            "execution_id": execution,
            "flow_id": str(uuid4()),
            "prompt": "question",
            "git_clone_config": {"publication_mode": "isolated"},
        }
    )
    assert captured["job"].spec.template.spec.automount_service_account_token is False
    batch = SimpleNamespace(
        read_namespaced_job=AsyncMock(
            side_effect=[captured["job"], ApiException(status=404)]
        ),
        delete_namespaced_job=AsyncMock(),
    )
    core = SimpleNamespace(
        list_namespaced_pod=AsyncMock(return_value=SimpleNamespace(items=[]))
    )
    executor._k8s_batch_api, executor._k8s_core_api = batch, core
    await remove_owned_publication_runtime(executor, reference, execution)
    assert (
        batch.delete_namespaced_job.call_args.kwargs["body"].preconditions.uid
        == "owned-job-uid"
    )
    core.list_namespaced_pod.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["initial_missing", "delete_failed", "residual_pod", "auth_error", "replacement"],
)
async def test_kubernetes_incomplete_removal_blocks(mode, monkeypatch):
    execution = str(uuid4())
    executor = ContainerAgentExecutor(
        "codex", {}, "example/harness:stable", use_kubernetes=True
    )
    job = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="original", labels={"preloop.execution_id": execution}
        )
    )
    batch = SimpleNamespace(
        read_namespaced_job=AsyncMock(side_effect=[job, ApiException(status=404)]),
        delete_namespaced_job=AsyncMock(),
    )
    core = SimpleNamespace(
        list_namespaced_pod=AsyncMock(return_value=SimpleNamespace(items=[]))
    )
    if mode == "initial_missing":
        batch.read_namespaced_job.side_effect = ApiException(status=404)
    elif mode == "delete_failed":
        batch.delete_namespaced_job.side_effect = ApiException(status=403)
    elif mode == "residual_pod":
        core.list_namespaced_pod.return_value.items = [object()]
    elif mode == "auth_error":
        core.list_namespaced_pod.side_effect = ApiException(status=403)
    else:
        batch.read_namespaced_job.side_effect = [
            job,
            SimpleNamespace(metadata=SimpleNamespace(uid="replacement")),
        ]
    monkeypatch.setattr(executor, "_init_kubernetes_clients", AsyncMock())
    executor._k8s_batch_api, executor._k8s_core_api = batch, core
    with pytest.raises(PublicationError):
        await remove_owned_publication_runtime(
            executor, "owned-job", execution, timeout_seconds=0.02
        )
