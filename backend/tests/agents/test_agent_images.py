"""Tests for the shared default agent image helper."""

import os
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from uuid import uuid4

import pytest

from preloop.agents.aider import AiderAgent
from preloop.agents.codex import CodexAgent
from preloop.agents.gemini import GeminiAgent
from preloop.agents.images import DEFAULT_AGENT_IMAGES, default_agent_image
from preloop.agents.opencode import OpenCodeAgent


def test_default_agent_image_matches_hosted_executors() -> None:
    assert default_agent_image("opencode") == OpenCodeAgent({}).image
    assert default_agent_image("codex") == CodexAgent({}).image
    assert default_agent_image("aider") == AiderAgent({}).image
    assert default_agent_image("gemini") == GeminiAgent({}).image
    assert default_agent_image("OPENCODE") == DEFAULT_AGENT_IMAGES["opencode"]
    assert default_agent_image("unknown") is None


def test_default_agent_image_honors_env_overrides() -> None:
    with patch.dict(
        os.environ,
        {
            "OPENCODE_IMAGE": "custom/opencode:dev",
            "CODEX_IMAGE": "custom/codex:dev",
            "AIDER_IMAGE": "custom/aider:dev",
            "GEMINI_IMAGE": "custom/gemini:dev",
        },
    ):
        assert default_agent_image("opencode") == "custom/opencode:dev"
        assert default_agent_image("codex") == "custom/codex:dev"
        assert default_agent_image("aider") == "custom/aider:dev"
        assert default_agent_image("gemini") == "custom/gemini:dev"
        assert OpenCodeAgent({}).image == "custom/opencode:dev"
        assert CodexAgent({}).image == "custom/codex:dev"


# Effective runtime selection (issue #1058). These assert that the image a
# deployment configures reaches the workload that is actually launched, not
# just an attribute on the agent object.

PERL_IMAGE = (
    "registry.example/preloop-codex-perl@sha256:"
    "0000000000000000000000000000000000000000000000000000000000000001"
)
RUNNER_IMAGE = "registry.example/team/codex-perl:runner"


def _launch_context() -> dict[str, object]:
    return {
        "flow_id": "flow-1058",
        "execution_id": "0f5a0a1e-3a4f-4f0e-9a0a-3c1e2d4b1058",
        "prompt": "Check Perl 5.10 compatibility",
        "agent_config": {},
    }


async def _hosted_docker_image(monkeypatch: pytest.MonkeyPatch) -> str:
    """Launch a hosted Codex container on a mocked daemon; return its Image."""
    from preloop.agents.container import ContainerAgentExecutor

    monkeypatch.setenv("USE_KUBERNETES", "false")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    agent = CodexAgent({})
    assert agent.use_kubernetes is False
    container = AsyncMock()
    type(container).id = PropertyMock(return_value="container-1058")
    with patch("preloop.agents.container.aiodocker.Docker") as docker_cls:
        docker = AsyncMock()
        docker_cls.return_value = docker
        docker.containers.create = AsyncMock(return_value=container)
        await ContainerAgentExecutor._start_docker_container(agent, _launch_context())
    return docker.containers.create.call_args.kwargs["config"]["Image"]


async def _hosted_kubernetes_image(monkeypatch: pytest.MonkeyPatch) -> str:
    """Create a hosted Codex Kubernetes Job on a mocked API; return its image."""
    from preloop.agents.container import ContainerAgentExecutor

    monkeypatch.setenv("USE_KUBERNETES", "true")
    agent = CodexAgent({})
    assert agent.use_kubernetes is True
    agent.agent_namespace = "preloop-agents"
    agent._k8s_batch_api = AsyncMock()
    agent._k8s_core_api = AsyncMock()
    with patch.object(ContainerAgentExecutor, "_init_kubernetes_clients", AsyncMock()):
        await ContainerAgentExecutor._start_kubernetes_pod(agent, _launch_context())
    job = agent._k8s_batch_api.create_namespaced_job.call_args.kwargs["body"]
    (container,) = job.spec.template.spec.containers
    return container.image


@pytest.mark.asyncio
async def test_codex_image_reaches_hosted_docker_container(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_IMAGE", PERL_IMAGE)
    assert await _hosted_docker_image(monkeypatch) == PERL_IMAGE


@pytest.mark.asyncio
async def test_unset_codex_image_keeps_hosted_docker_default(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_IMAGE", raising=False)
    assert await _hosted_docker_image(monkeypatch) == DEFAULT_AGENT_IMAGES["codex"]


@pytest.mark.asyncio
async def test_codex_image_reaches_hosted_kubernetes_job(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_IMAGE", PERL_IMAGE)
    assert await _hosted_kubernetes_image(monkeypatch) == PERL_IMAGE


@pytest.mark.asyncio
async def test_unset_codex_image_keeps_hosted_kubernetes_default(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_IMAGE", raising=False)
    assert await _hosted_kubernetes_image(monkeypatch) == DEFAULT_AGENT_IMAGES["codex"]


def _private_lease(agent_type: str, agent_config: dict[str, object]) -> dict:
    from preloop.agents.remote_runner import RemoteRunnerExecutor

    executor = RemoteRunnerExecutor(
        agent_type, {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    return executor._lease_payload(
        execution_id=uuid4(),
        flow_id=uuid4(),
        prompt="review",
        execution_context={"agent_type": agent_type, "agent_config": agent_config},
    )


def test_server_codex_image_reaches_private_runner_lease(monkeypatch) -> None:
    """With no flow override the lease carries the server's CODEX_IMAGE."""
    monkeypatch.setenv("CODEX_IMAGE", PERL_IMAGE)
    assert _private_lease("codex", {})["agent_config"]["image"] == PERL_IMAGE


def test_unset_codex_image_keeps_private_runner_default(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_IMAGE", raising=False)
    payload = _private_lease("codex", {})
    assert payload["agent_config"]["image"] == DEFAULT_AGENT_IMAGES["codex"]


@pytest.mark.parametrize("key", ["image", "docker_image"])
def test_private_runner_override_beats_codex_image(monkeypatch, key) -> None:
    """agent_config image (or its legacy alias) wins over CODEX_IMAGE."""
    from preloop.agents.images import effective_agent_image
    from preloop.agents.remote_runner import payload_for_log

    monkeypatch.setenv("CODEX_IMAGE", PERL_IMAGE)
    payload = _private_lease("codex", {key: RUNNER_IMAGE})
    assert effective_agent_image(payload["agent_config"]) == RUNNER_IMAGE
    assert payload["agent_config"].get("image") in (None, RUNNER_IMAGE)
    assert payload_for_log(payload)["image"] == RUNNER_IMAGE


def test_host_profile_carries_no_image(monkeypatch) -> None:
    """Native host profiles run on the host: no image, even with CODEX_IMAGE."""
    from preloop.agents.images import effective_agent_image

    monkeypatch.setenv("CODEX_IMAGE", PERL_IMAGE)
    payload = _private_lease(
        "cursor", {"host_exec_profile": "cursor-ask", "image": RUNNER_IMAGE}
    )
    assert payload["host_exec_profile"] == "cursor-ask"
    assert effective_agent_image(payload["agent_config"]) is None


@pytest.mark.asyncio
async def test_hosted_codex_ignores_private_runner_image_key(monkeypatch) -> None:
    """agent_config.image is a private-runner override; hosted uses CODEX_IMAGE."""
    from preloop.agents.container import ContainerAgentExecutor

    monkeypatch.setenv("CODEX_IMAGE", PERL_IMAGE)
    monkeypatch.setenv("USE_KUBERNETES", "false")
    agent = CodexAgent({"image": RUNNER_IMAGE, "docker_image": RUNNER_IMAGE})
    container = AsyncMock()
    type(container).id = PropertyMock(return_value="container-1058")
    with patch("preloop.agents.container.aiodocker.Docker") as docker_cls:
        docker = AsyncMock()
        docker_cls.return_value = docker
        docker.containers.create = AsyncMock(return_value=container)
        await ContainerAgentExecutor._start_docker_container(agent, _launch_context())
    assert docker.containers.create.call_args.kwargs["config"]["Image"] == PERL_IMAGE
