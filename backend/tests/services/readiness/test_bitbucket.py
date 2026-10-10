"""Synthetic provider reads exercise the actual configured-policy evaluator."""

from typing import Any

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from preloop.schemas.readiness import GateEvidence, ReadinessPolicy
from preloop.services.readiness.bitbucket import observe_bitbucket
from preloop.sync.trackers.bitbucket import BitbucketTracker


class Probe:
    async def assess(
        self, repository: str, source_sha: str, target_sha: str
    ) -> GateEvidence:
        return GateEvidence(
            name="conflict",
            state="pass",
            source="local_fixture",
            retrieved_at=datetime.now(UTC),
            source_sha=source_sha,
            target_sha=target_sha,
            probe_version="fixture",
            strategy="ort",
        )


def policy(**changes) -> Any:
    return ReadinessPolicy(
        version=uuid4(),
        required_build_keys=("required",),
        minimum_approvals=1,
        changes_requests_block=True,
        unresolved_tasks_block=True,
        **changes,
    )


def tracker(
    *,
    statuses: Any = None,
    tasks: Any = None,
    pr_changes: Any = None,
    move: Any = False,
    pagination: Any = False,
) -> Any:
    count = 0
    requests = []

    def handle(request: Any) -> Any:
        nonlocal count
        requests.append(request)
        if request.url.path.endswith("/pullrequests/1"):
            count += 1
            pr = {
                "state": "OPEN",
                "draft": False,
                "participants": [
                    {
                        "user": {"uuid": "reviewer"},
                        "approved": True,
                        "state": "approved",
                    }
                ],
                "source": {"commit": {"hash": "a" * 40}},
                "destination": {"commit": {"hash": "b" * 40}},
            }
            pr.update(pr_changes or {})
            if move and count > 1:
                pr["destination"]["commit"]["hash"] = "c" * 40
            return httpx.Response(200, json=pr)
        if request.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"values": tasks or []})
        if request.url.path.endswith("/statuses"):
            if pagination and "page" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "values": [{"key": "optional", "state": "FAILED"}],
                        "next": str(request.url) + "?page=2",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "values": statuses
                    if statuses is not None
                    else [{"key": "required", "state": "SUCCESSFUL"}]
                },
            )
        raise AssertionError(request.url)

    return BitbucketTracker(
        str(uuid4()),
        "synthetic-token",
        {"workspace": "example", "repository": "repo"},
        transport=httpx.MockTransport(handle),
    ), requests


async def run(client: Any, *, selected_policy: Any = None) -> Any:
    return await observe_bitbucket(
        client,
        Probe(),
        account_id=uuid4(),
        tracker_id=uuid4(),
        repository="example/repo",
        pr_id=1,
        policy=selected_policy or policy(),
    )


@pytest.mark.asyncio
async def test_required_pending_not_ready_and_optional_failure_ignored() -> Any:
    client, requests = tracker(statuses=[{"key": "required", "state": "INPROGRESS"}])
    observation = await run(client)
    assert observation.state == "not_ready"
    assert "pending_build" in observation.reasons
    client, requests = tracker(pagination=True)
    observation = await run(client)
    assert observation.state == "ready"
    assert observation.coverage == "complete"
    assert observation.forge_coverage == "unknown"
    assert any("page=2" in str(request.url) for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,reason,state",
    [
        ({"statuses": []}, "missing_required_key", "unknown"),
        ({"tasks": [{"state": "UNRESOLVED"}]}, "unresolved_task", "not_ready"),
        ({"pr_changes": {"draft": True}}, "draft", "not_ready"),
        ({"pr_changes": {"draft": None}}, "draft_unavailable", "unknown"),
        ({"pr_changes": {"participants": []}}, "insufficient_approvals", "not_ready"),
        ({"move": True}, "commits_changed", "unknown"),
    ],
)
async def test_distinct_failure_and_unknown_reasons(
    changes: Any, reason: Any, state: Any
) -> Any:
    client, _ = tracker(**changes)
    observation = await run(client)
    assert observation.state == state
    assert reason in observation.reasons


@pytest.mark.asyncio
async def test_no_policy_reads_nothing() -> Any:
    client, requests = tracker()
    observation = await observe_bitbucket(
        client,
        Probe(),
        account_id=uuid4(),
        tracker_id=uuid4(),
        repository="example/repo",
        pr_id=1,
        policy=None,
    )
    assert observation.state == "unknown"
    assert observation.reasons == ("policy_unconfigured",)
    assert requests == []


@pytest.mark.asyncio
async def test_zero_required_approvals_and_checks_still_checks_conflict() -> Any:
    client, requests = tracker(pr_changes={"participants": []})
    selected = ReadinessPolicy(
        version=uuid4(),
        required_build_keys=(),
        minimum_approvals=0,
        changes_requests_block=False,
        unresolved_tasks_block=False,
    )
    observation = await run(client, selected_policy=selected)
    assert observation.state == "ready"
    assert {g.name for g in observation.gates} == {
        "open",
        "non_draft",
        "approvals",
        "conflict",
    }
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_provider_enforced_is_explicitly_unsupported_without_reads() -> Any:
    client, requests = tracker()
    observation = await observe_bitbucket(
        client,
        Probe(),
        account_id=uuid4(),
        tracker_id=uuid4(),
        repository="example/repo",
        pr_id=1,
        policy=policy(),
        scope="provider_enforced",
    )
    assert observation.scope == "provider_enforced"
    assert observation.coverage == "unsupported"
    assert observation.state == "unknown"
    assert observation.reasons == ("provider_enforced_unsupported",)
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize(
    "endpoint", [{"commit": None}, {"commit": []}, None, "malformed"]
)
async def test_malformed_commit_identity_becomes_unknown(
    side: str, endpoint: Any
) -> Any:
    client, requests = tracker(pr_changes={side: endpoint})
    result = await run(client)
    assert result.state == "unknown"
    assert "identity_unavailable" in result.reasons
    assert len(requests) == 1
