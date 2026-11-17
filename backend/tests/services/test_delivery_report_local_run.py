"""A local scheduled-report run, from synced activity to the hosted launch.

Synthetic accounts only. The orchestrator selects the checkout from rows in
PostgreSQL, then the Codex harness builds the real Kubernetes Job and setup
Secret against a mocked cluster. Two shapes: a 440-repository account, and an
80-repository account with the default weekly window (Monday 07:00 UTC).
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from preloop.agents.codex import CodexAgent
from preloop.models import models
from preloop.services import delivery_report as dr
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.utils.execve_limits import (
    check_launch_payload,
    largest_launch_string,
    total_launch_bytes,
)

WINDOW = {"from": "2026-11-16T07:00:00+00:00", "to": "2026-11-23T07:00:00+00:00"}


def _seed(db: Any, account_id: Any, repositories: int, active: set[int]) -> list[str]:
    """Projects with synced push times: active inside the window, else older."""
    tracker = models.Tracker(
        name="Example tracker", tracker_type="github", account_id=account_id
    )
    db.add(tracker)
    db.flush()
    organization = models.Organization(
        name="Example", identifier="example", tracker_id=tracker.id
    )
    db.add(organization)
    db.flush()
    projects = [
        models.Project(
            name=f"Project {i}",
            identifier=str(i),
            slug=f"example/project-{i:03d}",
            organization_id=organization.id,
            meta_data={
                "default_branch": "main",
                "updated_at": (
                    "2026-11-19T12:00:00Z" if i in active else "2026-08-01T00:00:00Z"
                ),
            },
        )
        for i in range(repositories)
    ]
    db.add_all(projects)
    db.flush()
    return [str(project.id) for project in projects]


def _selected(db: Any, account_id: Any) -> list[dict[str, Any]]:
    """Run the orchestrator's report checkout selection for the next window."""
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = db
    orchestrator.execution_log = SimpleNamespace(
        id=uuid4(), start_time=datetime(2026, 11, 23, 7, tzinfo=timezone.utc)
    )
    orchestrator.trigger_event_data = {
        "source": "schedule",
        "payload": {"window": WINDOW, "scheduled_at": WINDOW["to"]},
    }
    orchestrator.flow = SimpleNamespace(
        account_id=account_id,
        agent_config={"report_kind": "delivery_report"},
        git_clone_config={"enabled": True, "repositories": []},
    )
    orchestrator._cached_payload_identity = lambda: None
    orchestrator._delivery_report_run = dr.PolicyRun(
        decision="onboarding", policy=dr.DeliveryReportPolicy()
    )
    return orchestrator._git_clone_config_for_trigger()["repositories"]


async def _hosted_launch(repositories: int) -> dict[str, int]:
    """Build the actual Job and Secret for this many checkouts."""
    from kubernetes_asyncio.client import ApiClient

    from tests.agents.test_delivery_report_launch import report_context

    context = {**report_context(repositories), "flow_id": str(uuid4())}
    agent = CodexAgent({})
    agent.use_kubernetes = True
    agent._init_kubernetes_clients = AsyncMock()
    agent._prepare_environment = AsyncMock(return_value={})
    agent._k8s_batch_api = AsyncMock()
    agent._k8s_batch_api.read_namespaced_job.return_value = SimpleNamespace(
        metadata=SimpleNamespace(uid="synthetic-job-uid")
    )
    agent._k8s_core_api = AsyncMock()
    await agent._start_kubernetes_pod(context)
    job = agent._k8s_batch_api.create_namespaced_job.call_args.kwargs["body"]
    secret = agent._k8s_core_api.create_namespaced_secret.call_args.kwargs["body"]
    container = job.spec.template.spec.containers[0]
    env = {entry.name: entry.value for entry in container.env}
    check_launch_payload(command=container.command, args=container.args, env=env)
    async with ApiClient() as serializer:
        return {
            "argument_environment_bytes": total_launch_bytes(
                container.command, container.args, env
            ),
            "largest_string_bytes": largest_launch_string(
                container.command, container.args, env
            ).size,
            "job_json_bytes": len(
                json.dumps(serializer.sanitize_for_serialization(job)).encode()
            ),
            "secret_json_bytes": len(
                json.dumps(serializer.sanitize_for_serialization(secret)).encode()
            ),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repositories", "active"),
    [
        (440, {0, 17, 101, 230, 377, 439}),
        (440, set(range(440))),
        (80, {0, 3, 9, 27, 41, 66, 79}),
        (80, set(range(80))),
    ],
    ids=["440-some-active", "440-all-active", "80-some-active", "80-all-active"],
)
async def test_next_scheduled_report_launches(
    db_session: Any, test_user: Any, repositories: int, active: set[int]
) -> None:
    """Only active repositories are cloned and the launch fits every limit."""
    ids = _seed(db_session, test_user.account_id, repositories, active)

    selected = _selected(db_session, test_user.account_id)

    assert {entry["project_id"] for entry in selected} == {ids[i] for i in active}
    sizes = await _hosted_launch(len(selected))
    assert sizes["argument_environment_bytes"] < 1024 * 1024
    assert sizes["largest_string_bytes"] < 128 * 1024
    assert sizes["secret_json_bytes"] < 1024 * 1024
    print(json.dumps({"repositories": repositories, "cloned": len(selected), **sizes}))
