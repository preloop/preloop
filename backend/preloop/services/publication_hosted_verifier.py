"""Run selected checks in fresh credential-free runtimes on immutable input."""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from aiodocker.exceptions import DockerError
from kubernetes_asyncio.client.exceptions import ApiException

from preloop.services.publication_runtime import remove_owned_publication_runtime
from preloop.services.publication_verification import VerifiedPublication
from preloop.services.publication_worker import inspect_bundle
from preloop.services.trusted_publisher import PublicationError
from preloop.services.verification import select_required_checks
from preloop.utils.secret_scrubbing import scrub_secrets

# Repository code executes only inside these credential-free disposable checks.
# Each check starts from a new clean checkout and may install bounded dependencies
# from its generic toolchain image. No agent workspace, cache, or config is reused.
CHECKOUT_SCRIPT = r"""
import os, subprocess, sys
os.makedirs('/tmp/preloop-check', exist_ok=False)
os.chdir('/tmp/preloop-check')
env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='0')
git = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=', '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never']
for args in [['init', '--template='], ['bundle', 'unbundle', '/tmp/branch.bundle'], ['checkout', '--detach', '--force', sys.argv[1]]]:
    subprocess.run(git + args, env=env, check=True)
observed = subprocess.check_output(git + ['rev-parse', 'HEAD'], env=env, text=True).strip()
if observed != sys.argv[1]:
    raise SystemExit(125)
raise SystemExit(subprocess.run(['/bin/sh', '-c', sys.argv[2]], env=env).returncode)
"""


@dataclass(frozen=True)
class HostedVerification:
    """Controller result retained separately from untrusted agent artifacts."""

    verification: VerifiedPublication
    manifest: dict[str, Any]
    checks: tuple[dict[str, Any], ...]
    image: str


class HostedVerificationError(PublicationError):
    """Trusted check diagnostics; never an agent-provided attestation."""

    def __init__(
        self, reason: str, manifest: dict[str, Any], checks: list[dict[str, Any]]
    ) -> None:
        super().__init__(reason)
        self.evidence = {"manifest": manifest, "checks": checks}


def _append_tail(logs: list[str] | None, value: str) -> None:
    if logs is not None:
        # No credentials are supplied to verifier runtimes. Strip control bytes
        # except newlines/tabs so repository output cannot spoof terminal UI.
        clean = "".join(
            char
            for char in value
            if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
        )
        tail = scrub_secrets("".join(logs) + clean) or ""
        logs[:] = [tail.encode("utf-8")[-65536:].decode("utf-8", errors="ignore")]


def bundle_tar(bundle: bytes) -> bytes:
    """Create one controller-authored tar entry for Docker's archive endpoint."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("branch.bundle")
        member.mode = 0o444
        member.size = len(bundle)
        archive.addfile(member, io.BytesIO(bundle))
    return buffer.getvalue()


async def verify_hosted_publication(
    executor: Any, policy: Any, bundle: bytes
) -> HostedVerification:
    """Select checks from the trusted policy and observe process exits and removal."""
    manifest = await asyncio.to_thread(inspect_bundle, bundle, policy.base_sha)
    profile = policy.verification_policy.profile
    if policy.verification_policy.mode != "gate" or profile is None:
        raise PublicationError(
            "Isolated publication requires a trusted verification profile"
        )
    checks = select_required_checks(profile, manifest["changed_files"]).commands
    if not checks:
        raise PublicationError("No required checks selected; publication blocked")
    outcomes = []
    try:
        async with asyncio.timeout(policy.verification_policy.gate_budget_seconds):
            for check in checks:
                started = time.monotonic()
                logs: list[str] = []
                outcome = {
                    "id": check.id,
                    "command": check.command,
                    "timeout_seconds": check.timeout_seconds,
                    "exit_code": None,
                    "log_tail": "",
                }
                outcomes.append(outcome)
                if executor.use_kubernetes:
                    exit_code = await _check_kubernetes(
                        executor, policy, bundle, manifest, check, logs
                    )
                else:
                    exit_code = await _check_docker(
                        executor, policy, bundle, manifest, check, logs
                    )
                outcome.update(
                    exit_code=exit_code,
                    log_tail="".join(logs),
                    duration_seconds=round(time.monotonic() - started, 3),
                )
                if exit_code != 0:
                    raise PublicationError(
                        f"Isolated verification check {check.id} failed with exit {exit_code}; ensure the configured generic toolchain image includes its dependencies"
                    )
    except (PublicationError, TimeoutError, DockerError, ApiException, OSError) as exc:
        if outcomes:
            outcomes[-1].update(
                log_tail="".join(logs),
                duration_seconds=round(time.monotonic() - started, 3),
            )
        if isinstance(exc, PublicationError):
            reason = str(exc)
        elif isinstance(exc, TimeoutError):
            reason = "Isolated verification exceeded its configured budget"
        else:
            reason = "Isolated verifier runtime unavailable or removal unconfirmed; ensure the pinned toolchain supplies Python 3, Git and the required check dependencies"
        raise HostedVerificationError(reason, manifest, outcomes) from exc
    return HostedVerification(
        VerifiedPublication(
            policy.execution_id, manifest["head_sha"], manifest["bundle_sha256"]
        ),
        manifest,
        tuple(outcomes),
        policy.verification_image,
    )


async def _check_docker(
    executor: Any,
    policy: Any,
    bundle: bytes,
    manifest: dict[str, Any],
    check: Any,
    logs: list[str] | None = None,
) -> int:
    docker = await executor._get_docker_client()
    config = {
        "Image": policy.verification_image,
        "Entrypoint": ["python3", "-I", "-c"],
        "Cmd": [CHECKOUT_SCRIPT, manifest["head_sha"], check.command],
        "Env": [
            "HOME=/tmp",
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "PRELOOP_DISABLE_TELEMETRY=true",
        ],
        "Labels": {
            "preloop.execution_id": policy.execution_id,
            "preloop.purpose": "publication-verifier",
        },
        "NetworkDisabled": True,
        "HostConfig": {
            "NetworkMode": "none",
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": 256,
            "Memory": 4 * 1024**3,
            "NanoCpus": 2 * 10**9,
            "LogConfig": {
                "Type": "json-file",
                "Config": {"max-size": "1m", "max-file": "1"},
            },
            "Privileged": False,
            "ReadonlyRootfs": False,
        },
    }
    container = await docker.containers.create(config=config)
    try:
        async with asyncio.timeout(check.timeout_seconds):
            await container.put_archive("/tmp", bundle_tar(bundle))
            await container.start()
            result = await container.wait()
            for line in await container.log(stdout=True, stderr=True, tail=200):
                _append_tail(logs, line)
            code = result.get("StatusCode")
            if type(code) is not int or result.get("Error"):
                raise PublicationError(
                    "Verifier did not report an authoritative exit status"
                )
            return code
    finally:
        # Removal uses full observed ID and ownership labels, never just wait().
        await remove_owned_publication_runtime(
            executor, container.id, policy.execution_id
        )


async def _check_kubernetes(
    executor: Any,
    policy: Any,
    bundle: bytes,
    manifest: dict[str, Any],
    check: Any,
    logs: list[str] | None = None,
) -> int:
    from kubernetes_asyncio import client
    from kubernetes_asyncio.stream import WsApiClient

    await executor._init_kubernetes_clients()
    name = "preloop-verify-" + uuid4().hex
    labels = {"preloop.execution_id": policy.execution_id, "preloop.verifier": name}
    network = client.NetworkingV1Api(executor._k8s_core_api.api_client)
    namespace = executor.agent_namespace
    job = client.V1Job(
        metadata=client.V1ObjectMeta(name=name, labels=labels),
        spec=client.V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=check.timeout_seconds + 30,
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    automount_service_account_token=False,
                    containers=[
                        client.V1Container(
                            name="verifier",
                            image=policy.verification_image,
                            command=[
                                "python3",
                                "-I",
                                "-c",
                                "import time; time.sleep(14460)",
                            ],
                            env=[
                                client.V1EnvVar(
                                    name="PRELOOP_DISABLE_TELEMETRY", value="true"
                                )
                            ],
                            security_context=client.V1SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=client.V1Capabilities(drop=["ALL"]),
                            ),
                            resources=client.V1ResourceRequirements(
                                limits={"cpu": "2", "memory": "4Gi"},
                                requests={"cpu": "100m", "memory": "256Mi"},
                            ),
                        )
                    ],
                ),
            ),
        ),
    )
    await _require_isolated_network_policy(
        network,
        namespace,
        {**labels, "job-name": name, "batch.kubernetes.io/job-name": name},
    )
    network_created = False
    job_attempted = False
    try:
        await network.create_namespaced_network_policy(
            namespace=namespace,
            body=client.V1NetworkPolicy(
                metadata=client.V1ObjectMeta(name=name, labels=labels),
                spec=client.V1NetworkPolicySpec(
                    pod_selector=client.V1LabelSelector(
                        match_labels={"preloop.verifier": name}
                    ),
                    policy_types=["Ingress", "Egress"],
                    ingress=[],
                    egress=[],
                ),
            ),
        )
        network_created = True
        job_attempted = True
        await executor._k8s_batch_api.create_namespaced_job(
            namespace=namespace, body=job
        )
        async with asyncio.timeout(check.timeout_seconds):
            while True:
                pods = await executor._k8s_core_api.list_namespaced_pod(
                    namespace=namespace, label_selector=f"job-name={name}"
                )
                running = [pod for pod in pods.items if pod.status.phase == "Running"]
                if len(running) == 1:
                    pod_name = running[0].metadata.name
                    # Admission may add labels. Recheck the actual pod before
                    # transferring source or running any repository command.
                    await _require_isolated_network_policy(
                        network,
                        namespace,
                        getattr(running[0].metadata, "labels", None) or labels,
                    )
                    break
                await asyncio.sleep(0.2)
            transfer = "import sys; data=sys.stdin.buffer.read(int(sys.argv[1])); len(data)==int(sys.argv[1]) or sys.exit(125); open('/tmp/branch.bundle','xb').write(data)"
            async with WsApiClient(
                configuration=executor._k8s_core_api.api_client.configuration
            ) as ws_client:
                api = client.CoreV1Api(ws_client)
                await _pod_exec(
                    api,
                    namespace,
                    pod_name,
                    ["python3", "-I", "-c", transfer, str(len(bundle))],
                    bundle,
                )
                return await _pod_exec(
                    api,
                    namespace,
                    pod_name,
                    [
                        "python3",
                        "-I",
                        "-c",
                        CHECKOUT_SCRIPT,
                        manifest["head_sha"],
                        check.command,
                    ],
                    None,
                    logs,
                )
    finally:
        # Foreground job deletion also verifies no residual pods remain.
        if job_attempted:
            await remove_owned_publication_runtime(executor, name, policy.execution_id)
        if network_created:
            await network.delete_namespaced_network_policy(
                name=name, namespace=namespace
            )


async def _require_isolated_network_policy(
    network: Any, namespace: str, labels: dict[str, str]
) -> None:
    """NetworkPolicy is additive: reject existing overlapping allow rules."""
    policies = await network.list_namespaced_network_policy(namespace=namespace)
    automatic_uid_labels = {"controller-uid", "batch.kubernetes.io/controller-uid"}
    for policy in policies.items:
        selector = policy.spec.pod_selector
        if any(
            labels.get(key) != value
            for key, value in (selector.match_labels or {}).items()
            if key not in automatic_uid_labels
        ):
            continue
        matches = True
        for expression in selector.match_expressions or []:
            if expression.key in automatic_uid_labels:
                # Controller UID is assigned during create. Treat possible
                # matches conservatively before scheduling the workload.
                continue
            value = labels.get(expression.key)
            choices = expression.values or []
            if expression.operator == "In":
                matches = matches and value in choices
            elif expression.operator == "NotIn":
                matches = matches and value not in choices
            elif expression.operator == "Exists":
                matches = matches and expression.key in labels
            elif expression.operator == "DoesNotExist":
                matches = matches and expression.key not in labels
            else:
                raise PublicationError(
                    "Unknown namespace NetworkPolicy selector; isolation unconfirmed"
                )
        if matches and (policy.spec.ingress or policy.spec.egress):
            raise PublicationError(
                "Verifier namespace has an overlapping permissive NetworkPolicy; use an isolated namespace with enforced deny rules"
            )


async def _pod_exec(
    api: Any,
    namespace: str,
    pod: str,
    command: list[str],
    stdin: bytes | None,
    logs: list[str] | None = None,
) -> int:
    """Stream bundle input and read the API's remote-process exit channel."""
    import aiohttp
    from kubernetes_asyncio.stream import WsApiClient

    stream = await api.connect_get_namespaced_pod_exec(
        name=pod,
        namespace=namespace,
        container="verifier",
        command=command,
        stdin=stdin is not None,
        stdout=True,
        stderr=True,
        tty=False,
        _preload_content=False,
    )
    async with stream as websocket:
        if stdin is not None:
            for offset in range(0, len(stdin), 65536):
                await websocket.send_bytes(b"\x00" + stdin[offset : offset + 65536])
        async for message in websocket:
            if (
                message.type == aiohttp.WSMsgType.BINARY
                and message.data
                and message.data[0] in {1, 2}
            ):
                _append_tail(logs, message.data[1:].decode("utf-8", errors="replace"))
            if (
                message.type == aiohttp.WSMsgType.BINARY
                and message.data
                and message.data[0] == 3
            ):
                try:
                    code = WsApiClient.parse_error_data(message.data[1:].decode())
                except (KeyError, TypeError, ValueError) as exc:
                    raise PublicationError(
                        "Verifier returned an invalid process status"
                    ) from exc
                if stdin is not None and code != 0:
                    raise PublicationError("Frozen bundle transfer to verifier failed")
                return code
    raise PublicationError("Verifier exec ended without authoritative process status")
