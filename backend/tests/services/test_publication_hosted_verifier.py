"""Credential-free verification adapters and authoritative remote process status."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import aiohttp
import pytest

from preloop.services.publication_hosted_verifier import (
    _check_kubernetes,
    _pod_exec,
    verify_hosted_publication,
)
from preloop.services.trusted_publisher import PublicationError
from preloop.services.verification import resolve_verification_policy


@pytest.mark.parametrize(
    "reason,category",
    [
        (
            "Isolated verification check regression failed with exit 1",
            "verification_failed",
        ),
        (
            "Isolated verification check database failed with exit 127",
            "verification_blocked",
        ),
        (
            "Isolated verification check lint failed with exit 126",
            "verification_blocked",
        ),
        (
            "Isolated verification exceeded its configured budget",
            "verification_blocked",
        ),
        (
            "Isolated verifier runtime unavailable or removal unconfirmed",
            "verification_blocked",
        ),
        ("No required checks selected; publication blocked", "verification_blocked"),
    ],
)
def test_hosted_failure_category_distinguishes_checks_from_environment(
    reason: str, category: str
) -> None:
    from preloop.services.flow_failure_category import derive_failure_category

    assert derive_failure_category(status="FAILED", error_message=reason) == category


@pytest.mark.asyncio
async def test_kubernetes_adapter_streams_input_and_deletes_job_before_return():
    execution = str(uuid4())
    batch = SimpleNamespace(create_namespaced_job=AsyncMock())
    core = SimpleNamespace(
        api_client=SimpleNamespace(configuration=object()),
        list_namespaced_pod=AsyncMock(
            return_value=SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=SimpleNamespace(name="owned-pod"),
                        status=SimpleNamespace(phase="Running"),
                    )
                ]
            )
        ),
    )
    executor = SimpleNamespace(
        _init_kubernetes_clients=AsyncMock(),
        _k8s_batch_api=batch,
        _k8s_core_api=core,
        agent_namespace="isolated",
        use_kubernetes=True,
    )
    policy = SimpleNamespace(
        execution_id=execution,
        verification_image="generic@sha256:" + "a" * 64,
        base_sha="c" * 40,
    )
    check = SimpleNamespace(command="pytest tests/unit", timeout_seconds=10)
    network = SimpleNamespace(
        list_namespaced_network_policy=AsyncMock(
            return_value=SimpleNamespace(items=[])
        ),
        create_namespaced_network_policy=AsyncMock(),
        delete_namespaced_network_policy=AsyncMock(),
    )
    with (
        patch("kubernetes_asyncio.client.NetworkingV1Api", return_value=network),
        patch("kubernetes_asyncio.client.CoreV1Api"),
        patch("kubernetes_asyncio.stream.WsApiClient") as websocket,
        patch(
            "preloop.services.publication_hosted_verifier._pod_exec",
            new=AsyncMock(side_effect=[0, 7]),
        ) as execute,
        patch(
            "preloop.services.publication_hosted_verifier.remove_owned_publication_runtime",
            new=AsyncMock(),
        ) as remove,
    ):
        websocket.return_value.__aenter__ = AsyncMock(return_value=object())
        websocket.return_value.__aexit__ = AsyncMock(return_value=False)
        code = await _check_kubernetes(
            executor, policy, b"frozen-bundle", {"head_sha": "b" * 40}, check
        )
    assert code == 7
    job = batch.create_namespaced_job.call_args.kwargs["body"]
    spec = job.spec.template.spec
    assert spec.automount_service_account_token is False
    assert not spec.volumes and not spec.host_pid and not spec.host_network
    assert spec.containers[0].security_context.allow_privilege_escalation is False
    assert spec.containers[0].security_context.capabilities.drop == ["ALL"]
    assert [env.name for env in spec.containers[0].env] == ["PRELOOP_DISABLE_TELEMETRY"]
    deny = network.create_namespaced_network_policy.call_args.kwargs["body"]
    assert deny.spec.policy_types == ["Ingress", "Egress"]
    assert deny.spec.egress == []
    assert execute.call_args_list[0].args[-1] == b"frozen-bundle"
    assert execute.call_args_list[1].args[-1] is None
    assert execute.call_args_list[1].args[3][-1] == policy.base_sha
    remove.assert_awaited_once_with(executor, job.metadata.name, execution)
    network.delete_namespaced_network_policy.assert_awaited_once()


@pytest.mark.asyncio
async def test_kubernetes_verifier_job_carries_agent_placement(monkeypatch):
    """The verifier runs repository code, so it shares the agent sandbox."""
    execution = str(uuid4())
    batch = SimpleNamespace(create_namespaced_job=AsyncMock())
    core = SimpleNamespace(
        api_client=SimpleNamespace(configuration=object()),
        list_namespaced_pod=AsyncMock(
            return_value=SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=SimpleNamespace(name="owned-pod"),
                        status=SimpleNamespace(phase="Running"),
                    )
                ]
            )
        ),
    )
    executor = SimpleNamespace(
        _init_kubernetes_clients=AsyncMock(),
        _k8s_batch_api=batch,
        _k8s_core_api=core,
        agent_namespace="isolated",
        use_kubernetes=True,
    )
    policy = SimpleNamespace(
        execution_id=execution,
        verification_image="generic@sha256:" + "a" * 64,
        base_sha="c" * 40,
    )
    check = SimpleNamespace(command="pytest tests/unit", timeout_seconds=10)
    network = SimpleNamespace(
        list_namespaced_network_policy=AsyncMock(
            return_value=SimpleNamespace(items=[])
        ),
        create_namespaced_network_policy=AsyncMock(),
        delete_namespaced_network_policy=AsyncMock(),
    )
    monkeypatch.setenv("AGENT_RUNTIME_CLASS_NAME", "kata-containers")
    monkeypatch.setenv("AGENT_NODE_SELECTOR", json.dumps({"runtime": "kata"}))
    monkeypatch.setenv(
        "AGENT_TOLERATIONS",
        json.dumps(
            [
                {
                    "key": "dedicated",
                    "operator": "Equal",
                    "value": "agents",
                    "effect": "NoSchedule",
                }
            ]
        ),
    )
    with (
        patch("kubernetes_asyncio.client.NetworkingV1Api", return_value=network),
        patch("kubernetes_asyncio.client.CoreV1Api"),
        patch("kubernetes_asyncio.stream.WsApiClient") as websocket,
        patch(
            "preloop.services.publication_hosted_verifier._pod_exec",
            new=AsyncMock(side_effect=[0, 0]),
        ),
        patch(
            "preloop.services.publication_hosted_verifier.remove_owned_publication_runtime",
            new=AsyncMock(),
        ),
    ):
        websocket.return_value.__aenter__ = AsyncMock(return_value=object())
        websocket.return_value.__aexit__ = AsyncMock(return_value=False)
        await _check_kubernetes(
            executor, policy, b"frozen-bundle", {"head_sha": "b" * 40}, check
        )
    spec = batch.create_namespaced_job.call_args.kwargs["body"].spec.template.spec
    assert spec.runtime_class_name == "kata-containers"
    assert spec.node_selector == {"runtime": "kata"}
    assert len(spec.tolerations) == 1
    assert spec.tolerations[0].key == "dedicated"
    assert spec.tolerations[0].effect == "NoSchedule"


class Socket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.send_bytes = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (b'{"status":"Success"}', 0),
        (b'{"status":"Failure","details":{"causes":[{"message":"9"}]}}', 9),
    ],
)
async def test_pod_exec_uses_remote_exit_channel_not_stdout(status, expected):
    socket = Socket(
        [
            SimpleNamespace(
                type=aiohttp.WSMsgType.BINARY, data=b'\x01{"status":"success"}'
            ),
            SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"\x03" + status),
        ]
    )
    api = SimpleNamespace(
        connect_get_namespaced_pod_exec=AsyncMock(return_value=socket)
    )
    assert await _pod_exec(api, "namespace", "pod", ["command"], None) == expected


@pytest.mark.asyncio
async def test_pod_exec_missing_exit_is_not_success():
    api = SimpleNamespace(
        connect_get_namespaced_pod_exec=AsyncMock(return_value=Socket([]))
    )
    with pytest.raises(PublicationError, match="authoritative"):
        await _pod_exec(api, "namespace", "pod", ["command"], None)


@pytest.mark.asyncio
async def test_empty_selection_cannot_create_verifier_or_attestation():
    policy = SimpleNamespace(
        base_sha="a" * 40,
        verification_policy=resolve_verification_policy(
            {
                "verification": {
                    "mode": "gate",
                    "profile": {"version": "v1", "profile_id": "empty"},
                }
            }
        ),
    )
    with (
        patch(
            "preloop.services.publication_hosted_verifier.inspect_bundle",
            return_value={"changed_files": ["unknown"]},
        ),
        patch(
            "preloop.services.publication_hosted_verifier._check_docker",
            new=AsyncMock(),
        ) as check,
    ):
        with pytest.raises(PublicationError, match="No required checks"):
            await verify_hosted_publication(MagicMock(), policy, b"bundle")
    check.assert_not_awaited()


def test_diagnostic_tail_is_bounded_and_scrubs_tokens_and_controls():
    from preloop.services.publication_hosted_verifier import _append_tail

    logs = []
    _append_tail(logs, "x" * 100000)
    _append_tail(logs, "\x1b\x00Authorization: Bearer test-secret\n")
    assert len(logs[0]) <= 65536
    assert "\x1b" not in logs[0] and "\x00" not in logs[0]
    assert "test-secret" not in logs[0]
    _append_tail(logs, "界" * 100000)
    assert len(logs[0].encode("utf-8")) <= 65536


@pytest.mark.asyncio
async def test_ambiguous_job_create_keeps_network_denial_until_removal_proven():
    executor = SimpleNamespace(
        _init_kubernetes_clients=AsyncMock(),
        _k8s_batch_api=SimpleNamespace(
            create_namespaced_job=AsyncMock(side_effect=TimeoutError())
        ),
        _k8s_core_api=SimpleNamespace(
            api_client=SimpleNamespace(configuration=object())
        ),
        agent_namespace="isolated",
        use_kubernetes=True,
    )
    policy = SimpleNamespace(
        execution_id=str(uuid4()), verification_image="generic@sha256:" + "a" * 64
    )
    network = SimpleNamespace(
        list_namespaced_network_policy=AsyncMock(
            return_value=SimpleNamespace(items=[])
        ),
        create_namespaced_network_policy=AsyncMock(),
        delete_namespaced_network_policy=AsyncMock(),
    )
    with (
        patch("kubernetes_asyncio.client.NetworkingV1Api", return_value=network),
        patch(
            "preloop.services.publication_hosted_verifier.remove_owned_publication_runtime",
            new=AsyncMock(side_effect=PublicationError("removal unconfirmed")),
        ) as remove,
    ):
        with pytest.raises(PublicationError, match="removal unconfirmed"):
            await _check_kubernetes(
                executor,
                policy,
                b"bundle",
                {"head_sha": "a" * 40},
                SimpleNamespace(command="true", timeout_seconds=10),
            )
    remove.assert_awaited_once()
    network.delete_namespaced_network_policy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector,blocked",
    [({}, True), ({"app": "another"}, False), ({"job-name": "owned"}, True)],
)
async def test_additive_network_policies_cannot_override_verifier_denial(
    selector, blocked
):
    from kubernetes_asyncio import client
    from preloop.services.publication_hosted_verifier import (
        _require_isolated_network_policy,
    )

    policy = client.V1NetworkPolicy(
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(match_labels=selector),
            egress=[client.V1NetworkPolicyEgressRule()],
        )
    )
    api = SimpleNamespace(
        list_namespaced_network_policy=AsyncMock(
            return_value=SimpleNamespace(items=[policy])
        )
    )
    if blocked:
        with pytest.raises(PublicationError, match="overlapping"):
            await _require_isolated_network_policy(
                api, "namespace", {"job-name": "owned"}
            )
    else:
        await _require_isolated_network_policy(api, "namespace", {"job-name": "owned"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", ["bundle", "base", "profile", "image", "execution", "runtime"]
)
async def test_controller_cache_reuse_requires_every_verification_input(
    changed: str,
) -> None:
    policy = SimpleNamespace(
        execution_id=str(uuid4()),
        base_sha="a" * 40,
        verification_image="toolchain@sha256:" + "b" * 64,
        verification_policy=resolve_verification_policy(
            {
                "verification": {
                    "mode": "gate",
                    "profile": {
                        "profile_id": "cache",
                        "always": [
                            {"id": "check", "command": "true", "reason": "required"},
                        ],
                    },
                }
            }
        ),
    )
    executor = SimpleNamespace(use_kubernetes=False)
    import hashlib

    def inspect(bundle: bytes, base: str) -> dict:
        return {
            "head_sha": "c" * 40,
            "changed_files": ["file"],
            "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
        }

    with (
        patch(
            "preloop.services.publication_hosted_verifier.inspect_bundle",
            side_effect=inspect,
        ),
        patch(
            "preloop.services.publication_hosted_verifier._check_docker",
            new=AsyncMock(return_value=0),
        ) as docker,
        patch(
            "preloop.services.publication_hosted_verifier._check_kubernetes",
            new=AsyncMock(return_value=0),
        ) as kubernetes,
    ):
        first = await verify_hosted_publication(executor, policy, b"bundle")
        assert first.checks[0]["reused"] is False
        reused = await verify_hosted_publication(executor, policy, b"bundle")
        assert reused.checks[0]["reused"] is True
        assert docker.await_count == 1
        bundle = b"bundle"
        if changed == "bundle":
            bundle = b"another"
        elif changed == "base":
            policy.base_sha = "d" * 40
        elif changed == "profile":
            policy.verification_policy.profile.always[0].command = "echo changed"
        elif changed == "image":
            policy.verification_image = "toolchain@sha256:" + "e" * 64
        elif changed == "execution":
            policy.execution_id = str(uuid4())
        else:
            executor.use_kubernetes = True
        fresh = await verify_hosted_publication(executor, policy, bundle)
        assert fresh.checks[0]["reused"] is False
        assert docker.await_count + kubernetes.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [7, PublicationError("removal unconfirmed")])
async def test_failed_check_or_unconfirmed_teardown_never_populates_cache(
    failure: int | PublicationError,
) -> None:
    policy = SimpleNamespace(
        execution_id=str(uuid4()),
        base_sha="a" * 40,
        verification_image="toolchain@sha256:" + "b" * 64,
        verification_policy=resolve_verification_policy(
            {
                "verification": {
                    "mode": "gate",
                    "profile": {
                        "profile_id": "cache",
                        "always": [
                            {"id": "check", "command": "true", "reason": "required"},
                        ],
                    },
                }
            }
        ),
    )
    executor = SimpleNamespace(use_kubernetes=False)
    with (
        patch(
            "preloop.services.publication_hosted_verifier.inspect_bundle",
            return_value={
                "head_sha": "c" * 40,
                "changed_files": ["file"],
                "bundle_sha256": "d" * 64,
            },
        ),
        patch(
            "preloop.services.publication_hosted_verifier._check_docker",
            new=AsyncMock(side_effect=[failure, 0]),
        ) as check,
    ):
        with pytest.raises(PublicationError):
            await verify_hosted_publication(executor, policy, b"bundle")
        assert not getattr(executor, "_publication_check_cache", {})
        good = await verify_hosted_publication(executor, policy, b"bundle")
        assert check.await_count == 2
        good.manifest["head_sha"] = "tampered after return"
        good.checks[0]["exit_code"] = 7
        reused = await verify_hosted_publication(executor, policy, b"bundle")
        assert reused.manifest["head_sha"] == "c" * 40
        assert reused.checks[0]["exit_code"] == 0
        reused.checks[0]["exit_code"] = 9
        again = await verify_hosted_publication(executor, policy, b"bundle")
        assert again.checks[0]["exit_code"] == 0
        assert check.await_count == 2
