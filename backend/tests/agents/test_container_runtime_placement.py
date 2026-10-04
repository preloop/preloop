"""The agent Job pod spec carries configured runtime and node placement.

Issue #1076: a cluster that offers Kata Containers, gVisor, or Firecracker
as a RuntimeClass must be able to run agent pods inside it, and pin them to
the node pool that provides it. These tests drive ``_start_kubernetes_pod``
and inspect the Job it would create.
"""

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from preloop.agents.container import ContainerAgentExecutor

pytestmark = pytest.mark.asyncio


@pytest.fixture
def executor():
    ex = ContainerAgentExecutor(
        agent_type="codex",
        config={},
        image="test-image:latest",
        use_kubernetes=True,
    )
    ex.agent_namespace = "preloop-agents"
    return ex


@pytest.fixture(autouse=True)
def clear_placement_env(monkeypatch):
    for name in (
        "AGENT_RUNTIME_CLASS_NAME",
        "AGENT_NODE_SELECTOR",
        "AGENT_TOLERATIONS",
    ):
        monkeypatch.delenv(name, raising=False)


async def _captured_pod_spec(executor, monkeypatch):
    captured = {}

    async def create(job, *, job_name, execution_id):
        captured["job"] = job
        return job_name

    monkeypatch.setattr(executor, "_init_kubernetes_clients", AsyncMock())
    monkeypatch.setattr(
        executor, "_create_kubernetes_job", AsyncMock(side_effect=create)
    )
    await executor._start_kubernetes_pod(
        {
            "execution_id": str(uuid.uuid4()),
            "flow_id": str(uuid.uuid4()),
            "prompt": "does the pod carry placement?",
        }
    )
    return captured["job"].spec.template.spec


async def test_stock_install_omits_placement_keys(executor, monkeypatch):
    spec = await _captured_pod_spec(executor, monkeypatch)
    assert spec.runtime_class_name is None
    assert spec.node_selector is None
    assert spec.tolerations is None


async def test_configured_placement_reaches_the_pod_spec(executor, monkeypatch):
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
                    "tolerationSeconds": 120,
                }
            ]
        ),
    )

    spec = await _captured_pod_spec(executor, monkeypatch)

    assert spec.runtime_class_name == "kata-containers"
    assert spec.node_selector == {"runtime": "kata"}
    assert len(spec.tolerations) == 1
    toleration = spec.tolerations[0]
    assert toleration.key == "dedicated"
    assert toleration.operator == "Equal"
    assert toleration.value == "agents"
    assert toleration.effect == "NoSchedule"
    assert toleration.toleration_seconds == 120


async def test_malformed_placement_is_ignored_not_fatal(executor, monkeypatch):
    monkeypatch.setenv("AGENT_NODE_SELECTOR", "not-json")
    monkeypatch.setenv("AGENT_TOLERATIONS", "{}")

    spec = await _captured_pod_spec(executor, monkeypatch)

    assert spec.runtime_class_name is None
    assert spec.node_selector is None
    assert spec.tolerations is None
