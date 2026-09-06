"""Confirm destruction of an owned sandbox before publication credentials exist."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from uuid import UUID

from aiodocker.exceptions import DockerError
from kubernetes_asyncio.client import V1DeleteOptions, V1Preconditions
from kubernetes_asyncio.client.exceptions import ApiException

from preloop.services.trusted_publisher import PublicationError


async def remove_owned_publication_runtime(
    executor: Any,
    reference: str,
    execution_id: str,
    *,
    timeout_seconds: float = 30,
) -> None:
    """Observe ownership, delete exact runtime, then confirm absence.

    Client cleanup, generic executor status, and a missing object on the first
    read are never removal evidence. API/authentication failures fail closed.
    Call only after capturing the recovery checkpoint and immutable artifacts.
    """
    try:
        UUID(execution_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise PublicationError("Invalid publication execution identity") from exc
    if not isinstance(reference, str) or not reference:
        raise PublicationError("Missing publication runtime identity")
    try:
        async with asyncio.timeout(timeout_seconds):
            if executor.use_kubernetes is True:
                await _remove_kubernetes(executor, reference, execution_id)
            elif executor.use_kubernetes is False:
                await _remove_docker(executor, reference, execution_id)
            else:
                raise PublicationError("Unknown publication runtime boundary")
    except (TimeoutError, DockerError, ApiException) as exc:
        raise PublicationError(
            "Owned agent runtime removal could not be confirmed; publication blocked"
        ) from exc


async def _remove_docker(executor: Any, reference: str, execution_id: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{12,64}", reference):
        raise PublicationError("Invalid Docker publication runtime identity")
    docker = await executor._get_docker_client()
    container = await docker.containers.get(reference)
    info = await container.show()
    observed_id = info.get("Id")
    labels = (info.get("Config") or {}).get("Labels") or {}
    if (
        not isinstance(observed_id, str)
        or not re.fullmatch(r"[a-f0-9]{64}", observed_id)
        or not observed_id.startswith(reference)
        or labels.get("preloop.execution_id") != execution_id
    ):
        raise PublicationError("Docker runtime is not owned by this execution")
    # Use the already-observed full ID, never a reusable container name.
    owned = await docker.containers.get(observed_id)
    await owned.delete(force=True, v=False)
    while True:
        try:
            await docker.containers.get(observed_id)
        except DockerError as exc:
            if exc.status == 404:
                return
            raise
        await asyncio.sleep(0.1)


async def _remove_kubernetes(executor: Any, reference: str, execution_id: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", reference):
        raise PublicationError("Invalid Kubernetes publication runtime identity")
    await executor._init_kubernetes_clients()
    batch = executor._k8s_batch_api
    core = executor._k8s_core_api
    namespace = executor.agent_namespace
    job = await batch.read_namespaced_job(name=reference, namespace=namespace)
    labels = job.metadata.labels or {}
    uid = job.metadata.uid
    if not uid or labels.get("preloop.execution_id") != execution_id:
        raise PublicationError("Kubernetes runtime is not owned by this execution")
    await batch.delete_namespaced_job(
        name=reference,
        namespace=namespace,
        body=V1DeleteOptions(
            propagation_policy="Foreground", preconditions=V1Preconditions(uid=uid)
        ),
    )
    while True:
        gone = False
        try:
            observed = await batch.read_namespaced_job(
                name=reference, namespace=namespace
            )
            if observed.metadata.uid != uid:
                raise PublicationError(
                    "Kubernetes runtime identity changed during removal"
                )
        except ApiException as exc:
            if exc.status != 404:
                raise
            gone = True
        pods = await core.list_namespaced_pod(
            namespace=namespace, label_selector=f"job-name={reference}"
        )
        if gone and pods.items == []:
            return
        await asyncio.sleep(0.1)
