"""Managed legacy publication for Copilot host profiles (#1069)."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.models.crud import crud_flow_runner
from preloop.services.host_exec import (
    ISOLATED_PUBLICATION_UNAVAILABLE,
    PULL_REQUEST_UNAVAILABLE,
    finalize_runner_completion,
    host_exec_unavailable_reason,
    normalize_host_exec_advertisements,
    runner_has_host_exec_profile,
)
from preloop.services.host_exec_delivery import (
    HostExecDeliveryError,
    build_host_exec_publication,
    hydrate_host_exec_job,
)
from preloop.services.host_exec_publication import (
    MULTI_REPOSITORY_PUBLICATION_UNAVAILABLE,
    PUBLICATION_UNSUPPORTED_TRACKER,
    open_and_bind_host_pull_request,
    trusted_host_publication_receipt,
)
from preloop.services.runner_service import lease_job, persistable_job_payload

ONE_REPO = {
    "enabled": True,
    "create_pull_request": True,
    "repositories": [{"repository_url": "https://bitbucket.org/acme/app.git"}],
}
WRITE_TOKEN = "bb_api_token_write_5555"
HEAD = "b" * 40
BRANCH = "preloop/issue-PROJ-7-1a2b3c4d"


def _runner(*caps):
    return SimpleNamespace(
        id=uuid4(),
        status="online",
        free_slots=1,
        capabilities={
            "host_exec_profiles": [
                {"name": "copilot-seat", "capabilities": ["host_exec", *caps]}
            ]
        },
    )


OLD_RUNNER = ("copilot_cli",)
PUBLISHING_RUNNER = ("copilot_cli", "host_publication")


# --- Validation matrix -----------------------------------------------------


def test_copilot_legacy_single_repository_publication_is_admitted():
    assert (
        host_exec_unavailable_reason(git_clone_config=ONE_REPO, agent_type="copilot")
        is None
    )
    legacy = {**ONE_REPO, "publication_mode": "legacy"}
    assert (
        host_exec_unavailable_reason(git_clone_config=legacy, agent_type="copilot")
        is None
    )


@pytest.mark.parametrize(
    ("agent_type", "clone", "mode", "expected"),
    [
        ("cursor", ONE_REPO, None, PULL_REQUEST_UNAVAILABLE),
        (None, ONE_REPO, None, PULL_REQUEST_UNAVAILABLE),
        (
            "copilot",
            {**ONE_REPO, "publication_mode": "isolated"},
            None,
            ISOLATED_PUBLICATION_UNAVAILABLE,
        ),
        ("copilot", ONE_REPO, "isolated", ISOLATED_PUBLICATION_UNAVAILABLE),
        (
            "copilot",
            {
                **ONE_REPO,
                "repositories": [
                    {"repository_url": "https://bitbucket.org/acme/app.git"},
                    {"repository_url": "https://bitbucket.org/acme/lib.git"},
                ],
            },
            None,
            MULTI_REPOSITORY_PUBLICATION_UNAVAILABLE,
        ),
    ],
)
def test_unsupported_publication_shapes_fail_validation(
    agent_type, clone, mode, expected
):
    assert (
        host_exec_unavailable_reason(
            git_clone_config=clone, agent_type=agent_type, publication_mode=mode
        )
        == expected
    )


def test_review_only_copilot_flow_needs_no_publication_capability():
    review = {**ONE_REPO, "create_pull_request": False}
    assert (
        host_exec_unavailable_reason(git_clone_config=review, agent_type="copilot")
        is None
    )
    old = _runner(*OLD_RUNNER)
    assert runner_has_host_exec_profile(old, "copilot-seat", None, "copilot")


def test_publishing_lease_requires_advertised_capability():
    old, capable = _runner(*OLD_RUNNER), _runner(*PUBLISHING_RUNNER)
    assert not runner_has_host_exec_profile(
        old, "copilot-seat", None, "copilot", require_publication=True
    )
    assert runner_has_host_exec_profile(
        capable, "copilot-seat", None, "copilot", require_publication=True
    )


def test_advertisement_keeps_host_publication_capability():
    stored = normalize_host_exec_advertisements(
        [{"name": "copilot-seat", "capabilities": list(PUBLISHING_RUNNER)}]
    )
    assert "host_publication" in stored["host_exec_profiles"][0]["capabilities"]


def _reject(monkeypatch, runners, clone=ONE_REPO, agent_type="copilot"):
    from preloop.api.endpoints.flows import _reject_host_exec_flow

    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kwargs: list(runners)
    )
    _reject_host_exec_flow(
        agent_type=agent_type,
        agent_config={"host_exec_profile": "copilot-seat"},
        runner_pool="office-mac",
        git_clone_config=clone,
        db=MagicMock(),
        account_id=uuid4(),
    )


def test_save_rejects_publishing_flow_when_only_old_runners_exist(monkeypatch):
    with pytest.raises(HTTPException) as caught:
        _reject(monkeypatch, [_runner(*OLD_RUNNER)])
    assert caught.value.status_code == 400
    assert caught.value.detail == PULL_REQUEST_UNAVAILABLE


def test_save_accepts_publishing_flow_with_capable_runner(monkeypatch):
    _reject(monkeypatch, [_runner(*OLD_RUNNER), _runner(*PUBLISHING_RUNNER)])


def test_save_keeps_review_only_flows_without_runner_lookup(monkeypatch):
    def boom(db, **kwargs):
        raise AssertionError("review-only flows must not need a publishing runner")

    monkeypatch.setattr(crud_flow_runner, "find_matching", boom)
    from preloop.api.endpoints.flows import _reject_host_exec_flow

    _reject_host_exec_flow(
        agent_type="copilot",
        agent_config={"host_exec_profile": "copilot-seat"},
        runner_pool="office-mac",
        git_clone_config={**ONE_REPO, "create_pull_request": False},
    )


def test_save_rejects_cursor_publication_even_with_capable_runner(monkeypatch):
    with pytest.raises(HTTPException) as caught:
        _reject(monkeypatch, [_runner(*PUBLISHING_RUNNER)], agent_type="cursor")
    assert caught.value.detail == PULL_REQUEST_UNAVAILABLE


# --- Lease and assignment --------------------------------------------------


def _publishing_payload():
    executor = RemoteRunnerExecutor(
        "copilot", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    return executor._lease_payload(
        execution_id=uuid4(),
        flow_id=uuid4(),
        prompt="implement PROJ-7",
        execution_context={
            "agent_type": "copilot",
            "agent_config": {"host_exec_profile": "copilot-seat"},
            "git_clone_config": ONE_REPO,
        },
    )


def test_publishing_lease_is_data_only():
    payload = _publishing_payload()
    assert payload["host_exec_publication"] == {"mode": "legacy"}
    assert "git_clone_config" not in payload
    stored = persistable_job_payload(payload)
    assert stored["host_exec_publication"] == {"mode": "legacy"}
    assert "token" not in json.dumps(stored)


def test_old_runner_never_takes_a_publishing_lease(monkeypatch):
    old, capable = _runner(*OLD_RUNNER), _runner(*PUBLISHING_RUNNER)
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kwargs: [old, capable]
    )

    def claim(db, *, runner_id):
        if runner_id != capable.id:
            raise AssertionError("old runner must not be claimed")
        return capable

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", claim)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kwargs: SimpleNamespace(reported_status=None, **kwargs),
    )
    payload = {
        "host_exec_profile": "copilot-seat",
        "agent_type": "copilot",
        "host_exec_publication": {"mode": "legacy"},
    }
    kwargs = dict(account_id=uuid4(), pool="local", execution_id=uuid4())
    assert lease_job(MagicMock(), payload=payload, **kwargs) is capable

    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda db, **kw: [old])
    assert lease_job(MagicMock(), payload=payload, **kwargs) is None


# --- Delivery: transient credential and plan -------------------------------


def _jira_context(execution_id, repositories=None):
    return {
        "flow_id": str(uuid4()),
        "flow_name": "Implementer",
        "execution_id": str(execution_id),
        "account_id": uuid4(),
        "git_clone_config": {
            "enabled": True,
            "create_pull_request": True,
            "repositories": repositories
            or [
                {
                    "repository_url": "https://bitbucket.org/acme/app.git",
                    "tracker_id": "bb",
                }
            ],
        },
        "trigger_event_data": {
            "source": "jira",
            "payload": {"issue": {"key": "PROJ-7"}},
        },
        "trigger_project_id": None,
        "trigger_tracker_id": "bb",
        "git_credentials_map": {
            "bb": {"token": WRITE_TOKEN, "tracker_type": "bitbucket"}
        },
    }


def _patch_delivery(monkeypatch, clone, context):
    flow = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        name="Implementer",
        allowed_mcp_tools=None,
        allowed_mcp_servers=None,
        git_clone_config=clone,
        custom_commands=None,
    )
    execution = SimpleNamespace(
        id=context["execution_id"], flow_id=flow.id, trigger_event_details={}
    )
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow_execution.get",
        MagicMock(return_value=execution),
    )
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow.get",
        MagicMock(return_value=flow),
    )
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.FlowExecutionOrchestrator._get_flow_details",
        MagicMock(),
    )
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.FlowExecutionOrchestrator."
        "prepare_host_exec_checkout_context",
        AsyncMock(return_value=context),
    )
    return flow


def _publishing_job(execution_id):
    return {
        "execution_id": str(execution_id),
        "flow_id": str(uuid4()),
        "agent_type": "copilot",
        "prompt": "implement",
        "host_exec_profile": "copilot-seat",
        "agent_config": {"host_exec_profile": "copilot-seat"},
        "completion_protocol": "host_exec",
        "host_exec_publication": {"mode": "legacy"},
    }


@pytest.mark.asyncio
async def test_delivery_adds_transient_publication_plan(monkeypatch, caplog):
    execution_id = uuid4()
    _patch_delivery(monkeypatch, ONE_REPO, _jira_context(execution_id))
    job = _publishing_job(execution_id)
    caplog.set_level(logging.DEBUG)
    hydrated = await hydrate_host_exec_job(MagicMock(), job)
    plan = hydrated["host_exec_publication"]
    repo = hydrated["host_exec_checkout"]["repositories"][0]
    assert plan["branch"] == f"preloop/issue-PROJ-7-{str(execution_id)[:8]}"
    assert plan["path"] == repo["path"]
    assert repo["token"] == WRITE_TOKEN
    assert WRITE_TOKEN not in repo["url"]
    assert WRITE_TOKEN not in json.dumps(plan)
    # The persisted lease is untouched and never carries a credential.
    assert job["host_exec_publication"] == {"mode": "legacy"}
    assert WRITE_TOKEN not in json.dumps(persistable_job_payload(job))
    assert WRITE_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_delivery_refuses_lease_that_no_longer_matches_flow(monkeypatch):
    execution_id = uuid4()
    review = {**ONE_REPO, "create_pull_request": False}
    _patch_delivery(monkeypatch, review, _jira_context(execution_id))
    with pytest.raises(HostExecDeliveryError, match="pull request setting"):
        await hydrate_host_exec_job(MagicMock(), _publishing_job(execution_id))


def test_publication_plan_requires_single_credentialed_repository():
    repo = {"url": "https://bitbucket.org/acme/app.git", "path": "w", "branch": "main"}
    with pytest.raises(HostExecDeliveryError, match="credential"):
        build_host_exec_publication(
            {"repositories": [repo]},
            target_branch=BRANCH,
            flow_name="f",
            execution_id="1a2b3c4d",
        )
    with pytest.raises(HostExecDeliveryError, match="exactly one"):
        build_host_exec_publication(
            {"repositories": [repo, {**repo, "path": "x"}]},
            target_branch=BRANCH,
            flow_name="f",
            execution_id="1a2b3c4d",
        )
    with pytest.raises(HostExecDeliveryError, match="managed"):
        build_host_exec_publication(
            {"repositories": [{**repo, "token": "t"}]},
            target_branch="main",
            flow_name="f",
            execution_id="1a2b3c4d",
        )


# --- Completion receipt ----------------------------------------------------


def _pending():
    return {
        "completion_protocol": "host_exec",
        "agent_type": "copilot",
        "host_exec_profile": "copilot-seat",
        "agent_config": {"host_exec_profile": "copilot-seat"},
        "host_exec_publication": {"mode": "legacy"},
    }


def _complete(receipt=None, result_extra=None):
    message = {
        "type": "complete",
        "status": "SUCCEEDED",
        "exit_code": 0,
        "completion_protocol": "host_exec",
        "host_exec_profile": "copilot-seat",
        "result": {
            "status": "success",
            "harness": "copilot_cli",
            **(result_extra or {}),
        },
    }
    if receipt is not None:
        message["host_publication"] = receipt
    return message


def test_completion_binds_runner_receipt_and_drops_agent_claim():
    forged = {"status": "pushed", "branch": "preloop/evil", "head_sha": "c" * 40}
    status, error, result = finalize_runner_completion(
        _complete(
            {"status": "pushed", "branch": BRANCH, "head_sha": HEAD, "token": "x"},
            {"host_publication": forged},
        ),
        _pending(),
    )
    assert status == "SUCCEEDED", error
    assert result["host_publication"] == {
        "status": "pushed",
        "branch": BRANCH,
        "head_sha": HEAD,
    }


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        {"status": "pushed", "branch": BRANCH},
        {"status": "pushed", "branch": "main", "head_sha": HEAD},
        {"status": "pushed", "branch": "preloop/../main", "head_sha": HEAD},
        {"status": "no_changes", "branch": BRANCH},
    ],
)
def test_publishing_success_without_push_fails(receipt):
    status, error, _ = finalize_runner_completion(_complete(receipt), _pending())
    assert status == "FAILED"
    assert error.startswith("publication_missing")


def test_agent_cannot_forge_receipt_on_a_non_publishing_lease():
    pending = {**_pending()}
    pending.pop("host_exec_publication")
    forged = {"status": "pushed", "branch": BRANCH, "head_sha": HEAD}
    status, _, result = finalize_runner_completion(
        _complete(forged, {"host_publication": forged}), pending
    )
    assert status == "SUCCEEDED"
    assert "host_publication" not in result


def test_failed_push_receipt_is_recoverable():
    receipt = trusted_host_publication_receipt(
        {
            "host_publication": {
                "status": "failed",
                "reason": "push_conflict",
                "branch": BRANCH,
                "head_sha": HEAD,
            }
        }
    )
    assert receipt == {
        "status": "failed",
        "reason": "push_conflict",
        "branch": BRANCH,
        "head_sha": HEAD,
        "recoverable": True,
    }


# --- Control plane opens and binds the PR ----------------------------------


class FakeBitbucket:
    """Forge state: open PRs by source branch, with an optional lost response."""

    tracker_type = "bitbucket"

    def __init__(self, lose_create_response=False):
        self.prs = []
        self.creates = 0
        self.lose_create_response = lose_create_response

    async def list_open_pull_requests_by_source_branch(self, branch):
        return {"items": [pr for pr in self.prs if pr["source_branch"] == branch]}

    async def create_pull_request(
        self, *, title, source_branch, target_branch, description
    ):
        self.creates += 1
        if any(pr["source_branch"] == source_branch for pr in self.prs):
            raise RuntimeError("409 a pull request already exists")
        pr = {
            "url": f"https://bitbucket.org/acme/app/pull-requests/{len(self.prs) + 1}",
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        }
        self.prs.append(pr)
        if self.lose_create_response:
            raise TimeoutError("response lost")
        return pr


@pytest.fixture
def bound(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "preloop.services.flow_pr_binding.record_opened_pr",
        lambda db, execution_id, url, **kwargs: calls.append(
            (execution_id, url, kwargs)
        ),
    )
    return calls


async def _open(client, execution_id):
    return await open_and_bind_host_pull_request(
        MagicMock(),
        execution_id=execution_id,
        client=client,
        branch=BRANCH,
        base_branch="main",
        title="PROJ-7: implement",
        description="body",
    )


@pytest.mark.asyncio
async def test_control_plane_opens_and_binds_one_pull_request(bound):
    forge, execution_id = FakeBitbucket(), uuid4()
    found = await _open(forge, execution_id)
    assert found["source"] == "control_plane_create"
    assert forge.prs[0]["target_branch"] == "main"
    assert bound == [
        (
            execution_id,
            found["url"],
            {"source_branch": BRANCH, "opened_at": None, "raise_errors": True},
        )
    ]


@pytest.mark.asyncio
async def test_lost_create_response_and_retry_leave_exactly_one_pr(bound):
    forge, execution_id = FakeBitbucket(lose_create_response=True), uuid4()
    first = await _open(forge, execution_id)
    forge.lose_create_response = False
    retry = await _open(forge, execution_id)
    assert len(forge.prs) == 1
    assert forge.creates == 1
    assert first["url"] == retry["url"] == forge.prs[0]["url"]
    assert retry["source"] == "branch_lookup"
    assert {url for _, url, _ in bound} == {forge.prs[0]["url"]}


@pytest.mark.asyncio
async def test_create_failure_without_pr_raises_and_binds_nothing(bound):
    forge = FakeBitbucket()
    forge.create_pull_request = AsyncMock(side_effect=RuntimeError("403"))
    with pytest.raises(RuntimeError):
        await _open(forge, uuid4())
    assert bound == []


@pytest.mark.asyncio
async def test_non_bitbucket_tracker_is_refused(bound):
    forge = FakeBitbucket()
    forge.tracker_type = "github"
    with pytest.raises(ValueError, match=PUBLICATION_UNSUPPORTED_TRACKER):
        await _open(forge, uuid4())
    assert forge.creates == 0


# --- Orchestrator terminal step --------------------------------------------


def _orchestrator(monkeypatch, receipt, *, clients, context):
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orch = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orch.db = MagicMock()
    orch.execution_log = SimpleNamespace(
        id=uuid4(),
        result={"status": "success", "host_publication": receipt}
        if receipt
        else {"status": "success"},
    )
    orch.flow = SimpleNamespace(
        name="Implementer", agent_config={"host_exec_profile": "copilot-seat"}
    )
    orch.trigger_event_data = context["trigger_event_data"]
    orch.execution_logger = MagicMock()
    monkeypatch.setattr(
        orch, "prepare_host_exec_checkout_context", AsyncMock(return_value=context)
    )
    monkeypatch.setattr(
        orch, "_publication_tracker_clients", AsyncMock(return_value=clients)
    )
    return orch


@pytest.mark.asyncio
async def test_orchestrator_opens_pr_for_planned_branch(monkeypatch, bound):
    execution_id = uuid4()
    context = _jira_context(execution_id)
    branch = f"preloop/issue-PROJ-7-{str(execution_id)[:8]}"
    forge = FakeBitbucket()
    orch = _orchestrator(
        monkeypatch,
        {"status": "pushed", "branch": branch, "head_sha": HEAD},
        clients=[forge],
        context=context,
    )
    assert await orch._open_host_published_pr("SUCCEEDED") is None
    assert len(forge.prs) == 1 and forge.prs[0]["source_branch"] == branch
    assert orch._opened_pr["url"] == forge.prs[0]["url"]
    assert bound[0][1] == forge.prs[0]["url"]


@pytest.mark.asyncio
async def test_orchestrator_rejects_unplanned_branch(monkeypatch, bound):
    context = _jira_context(uuid4())
    forge = FakeBitbucket()
    orch = _orchestrator(
        monkeypatch,
        {"status": "pushed", "branch": "preloop/someone-else", "head_sha": HEAD},
        clients=[forge],
        context=context,
    )
    error = await orch._open_host_published_pr("SUCCEEDED")
    assert error and error.startswith("publication_failed")
    assert forge.creates == 0 and bound == []


@pytest.mark.asyncio
async def test_orchestrator_reports_actionable_failure_when_create_fails(
    monkeypatch, bound
):
    execution_id = uuid4()
    context = _jira_context(execution_id)
    forge = FakeBitbucket()
    forge.create_pull_request = AsyncMock(side_effect=RuntimeError("401"))
    orch = _orchestrator(
        monkeypatch,
        {
            "status": "pushed",
            "branch": f"preloop/issue-PROJ-7-{str(execution_id)[:8]}",
            "head_sha": HEAD,
        },
        clients=[forge],
        context=context,
    )
    error = await orch._open_host_published_pr("SUCCEEDED")
    assert "branch is kept" in error and WRITE_TOKEN not in error
    assert bound == []


@pytest.mark.asyncio
async def test_orchestrator_ignores_runs_without_a_pushed_receipt(monkeypatch, bound):
    context = _jira_context(uuid4())
    forge = FakeBitbucket()
    orch = _orchestrator(monkeypatch, None, clients=[forge], context=context)
    assert await orch._open_host_published_pr("SUCCEEDED") is None
    assert await orch._open_host_published_pr("FAILED") is None
    assert forge.creates == 0
