"""Feedback continuation for Copilot host profiles (#1069)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.models.crud import crud_flow_execution, crud_flow_runner
from preloop.services.host_exec import finalize_runner_completion
from preloop.services.host_exec_continuation import (
    HostContinuationError,
    host_continuation_session,
    record_host_continuation_session,
    resolve_host_continuation,
)
from preloop.services.host_exec_delivery import (
    HostExecDeliveryError,
    hydrate_host_exec_job,
)
from preloop.services.runner_service import lease_job, workspace_owner_runner_id

SESSION = "5f2c7a8e-1b3d-4e6f-9a0b-c1d2e3f4a5b6"
HEAD = "c" * 40
BRANCH = "preloop/issue-PROJ-7-1a2b3c4d"
ONE_REPO = {
    "enabled": True,
    "create_pull_request": True,
    "repositories": [{"repository_url": "https://bitbucket.org/acme/app.git"}],
}


def _pending(**extra):
    return {
        "completion_protocol": "host_exec",
        "agent_type": "copilot",
        "host_exec_profile": "copilot-seat",
        "agent_config": {"host_exec_profile": "copilot-seat"},
        "model_identifier": "team-default",
        "host_exec_publication": {"mode": "legacy"},
        **extra,
    }


def _complete(session=SESSION, receipt_status="pushed"):
    receipt = {"status": receipt_status, "branch": BRANCH}
    if receipt_status == "pushed":
        receipt["head_sha"] = HEAD
    return {
        "type": "complete",
        "status": "SUCCEEDED",
        "exit_code": 0,
        "completion_protocol": "host_exec",
        "host_exec_profile": "copilot-seat",
        "result": {
            "status": "success",
            "harness": "copilot_cli",
            "session_id": session,
        },
        "host_publication": receipt,
    }


# --- Session persistence ----------------------------------------------------


def test_publishing_run_records_validated_copilot_session():
    session = host_continuation_session(
        result={"harness": "copilot_cli", "session_id": SESSION.upper()},
        pending_job=_pending(),
    )
    assert session == {
        "agent_type": "copilot",
        "harness": "copilot_cli",
        "session_id": SESSION,
        "host_exec_profile": "copilot-seat",
        "model_identifier": "team-default",
    }
    review = _pending()
    review.pop("host_exec_publication")
    assert (
        host_continuation_session(
            result={"harness": "copilot_cli", "session_id": SESSION}, pending_job=review
        )
        is None
    )
    assert (
        host_continuation_session(
            result={"harness": "copilot_cli", "session_id": "../x"},
            pending_job=_pending(),
        )
        is None
    )


def test_session_recorded_only_on_success(monkeypatch):
    calls = []
    monkeypatch.setattr(
        crud_flow_execution,
        "set_cli_session",
        lambda db, *, db_obj, cli_session: calls.append(cli_session),
    )
    result = {"harness": "copilot_cli", "session_id": SESSION}
    record_host_continuation_session(
        MagicMock(), object(), status="FAILED", result=result, pending_job=_pending()
    )
    assert calls == []
    record_host_continuation_session(
        MagicMock(), object(), status="SUCCEEDED", result=result, pending_job=_pending()
    )
    assert calls[0]["session_id"] == SESSION


# --- Continuation validation -------------------------------------------------


def _rows(
    monkeypatch, *, session=None, result=None, caps=("host_continuation",), flow=None
):
    flow = flow or SimpleNamespace(id=uuid4(), account_id=uuid4())
    runner = SimpleNamespace(
        id=uuid4(),
        capabilities={
            "host_exec_profiles": [
                {
                    "name": "copilot-seat",
                    "capabilities": ["host_exec", "copilot_cli", *caps],
                }
            ]
        },
    )
    prior = SimpleNamespace(
        id=uuid4(),
        flow_id=flow.id,
        runner_id=runner.id,
        cli_session={
            "harness": "copilot_cli",
            "session_id": SESSION,
            "host_exec_profile": "copilot-seat",
            "model_identifier": "team-default",
        }
        if session is None
        else session,
        result={
            "pr_url": "https://bitbucket.org/acme/app/pull-requests/1",
            "host_publication": {
                "status": "pushed",
                "branch": BRANCH,
                "head_sha": HEAD,
            },
        }
        if result is None
        else result,
    )
    monkeypatch.setattr(
        crud_flow_execution,
        "get",
        lambda db, *, id, account_id=None, **kw: prior if id == prior.id else None,
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "get",
        lambda db, *, id, **kw: runner if id == runner.id else None,
    )
    resume = {"execution_id": str(prior.id), "source_branch": BRANCH}
    return flow, prior, runner, resume


def test_continuation_resolves_on_originating_runner(monkeypatch):
    flow, prior, runner, resume = _rows(monkeypatch)
    resolved = resolve_host_continuation(
        MagicMock(),
        flow=flow,
        resume=resume,
        profile="copilot-seat",
        model_identifier="team-default",
    )
    assert resolved == {
        "session_id": SESSION,
        "execution_id": str(prior.id),
        "runner_id": str(runner.id),
        "source_branch": BRANCH,
    }


@pytest.mark.parametrize(
    ("setup", "kwargs", "reason"),
    [
        ({"session": {}}, {}, "no Copilot session"),
        ({}, {"profile": "copilot-other"}, "host profile changed"),
        ({}, {"model_identifier": "other-model"}, "model changed"),
        ({"result": {"host_publication": {"status": "pushed"}}}, {}, "not confirmed"),
        ({"caps": ()}, {}, "host_continuation"),
    ],
)
def test_continuation_cannot_run_without_its_session_runner_or_pr(
    monkeypatch, setup, kwargs, reason
):
    flow, _, _, resume = _rows(monkeypatch, **setup)
    args = {"profile": "copilot-seat", "model_identifier": "team-default", **kwargs}
    with pytest.raises(HostContinuationError, match=f"^resume_unavailable: .*{reason}"):
        resolve_host_continuation(MagicMock(), flow=flow, resume=resume, **args)


def test_continuation_from_another_flow_is_refused(monkeypatch):
    _, _, _, resume = _rows(monkeypatch)
    other = SimpleNamespace(id=uuid4(), account_id=uuid4())
    with pytest.raises(HostContinuationError, match="does not belong"):
        resolve_host_continuation(
            MagicMock(),
            flow=other,
            resume=resume,
            profile="copilot-seat",
            model_identifier="team-default",
        )


# --- Completion ---------------------------------------------------------------


def _resume_pending():
    return _pending(
        host_exec_resume={"session_id": SESSION, "execution_id": str(uuid4())}
    )


def test_continuation_completion_requires_the_resumed_session():
    status, error, _ = finalize_runner_completion(_complete(), _resume_pending())
    assert status == "SUCCEEDED", error
    forged = "9d9d9d9d-1111-4222-8333-444455556666"
    status, error, _ = finalize_runner_completion(
        _complete(session=forged), _resume_pending()
    )
    assert status == "FAILED"
    assert error.startswith("resume_identity_mismatch")


def test_continuation_without_new_commit_still_succeeds():
    status, error, result = finalize_runner_completion(
        _complete(receipt_status="no_changes"), _resume_pending()
    )
    assert status == "SUCCEEDED", error
    assert result["host_publication"]["status"] == "no_changes"
    # A first implementation with no changes is still a failure.
    status, _, _ = finalize_runner_completion(
        _complete(receipt_status="no_changes"), _pending()
    )
    assert status == "FAILED"


# --- Lease, pinning and assignment ------------------------------------------


def test_lease_carries_control_plane_resume_and_pins_runner(monkeypatch):
    flow, prior, runner, resume = _rows(monkeypatch)
    execution = SimpleNamespace(trigger_event_details={"_resume": resume})
    executor = RemoteRunnerExecutor(
        "copilot",
        {},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
        execution=execution,
    )
    payload = executor._lease_payload(
        execution_id=uuid4(),
        flow_id=flow.id,
        prompt="address feedback",
        execution_context={
            "agent_type": "copilot",
            "agent_config": {"host_exec_profile": "copilot-seat"},
            "git_clone_config": ONE_REPO,
            "host_exec_resume": {"session_id": SESSION, "execution_id": str(prior.id)},
        },
    )
    assert payload["host_exec_resume"] == {
        "session_id": SESSION,
        "execution_id": str(prior.id),
    }
    assert "resume_from" not in payload
    assert workspace_owner_runner_id(MagicMock(), payload=payload) == runner.id


def test_unvalidated_resume_is_still_refused():
    execution = SimpleNamespace(
        trigger_event_details={"_resume": {"execution_id": str(uuid4())}}
    )
    executor = RemoteRunnerExecutor(
        "copilot",
        {},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
        execution=execution,
    )
    with pytest.raises(ValueError, match="does not resume"):
        executor._lease_payload(
            execution_id=uuid4(),
            flow_id=uuid4(),
            prompt="x",
            execution_context={
                "agent_type": "copilot",
                "agent_config": {"host_exec_profile": "copilot-seat"},
                "git_clone_config": {**ONE_REPO, "create_pull_request": False},
            },
        )


def test_continuation_lease_skips_runner_without_continuation(monkeypatch):
    def runner(*caps):
        return SimpleNamespace(
            id=uuid4(),
            status="online",
            free_slots=1,
            capabilities={
                "host_exec_profiles": [
                    {
                        "name": "copilot-seat",
                        "capabilities": ["host_exec", "copilot_cli", *caps],
                    }
                ]
            },
        )

    publish_only = runner("host_publication")
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kwargs: [publish_only]
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "claim_free_slot",
        lambda db, *, runner_id: (_ for _ in ()).throw(AssertionError("claimed")),
    )
    payload = {
        "host_exec_profile": "copilot-seat",
        "agent_type": "copilot",
        "host_exec_publication": {"mode": "legacy"},
        "host_exec_resume": {"session_id": SESSION, "execution_id": str(uuid4())},
    }
    assert (
        lease_job(
            MagicMock(),
            account_id=uuid4(),
            pool="local",
            execution_id=uuid4(),
            payload=payload,
        )
        is None
    )


# --- Delivery -----------------------------------------------------------------


def _patch_delivery(monkeypatch, trigger, context):
    flow = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        name="Implementer",
        allowed_mcp_tools=None,
        allowed_mcp_servers=None,
        git_clone_config=ONE_REPO,
        custom_commands=None,
    )
    execution = SimpleNamespace(
        id=context["execution_id"], flow_id=flow.id, trigger_event_details=trigger
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


def _continuation_context(execution_id, trigger):
    return {
        "flow_id": str(uuid4()),
        "flow_name": "Implementer",
        "execution_id": str(execution_id),
        "account_id": uuid4(),
        "git_clone_config": {
            "enabled": True,
            "create_pull_request": True,
            "repositories": [
                {
                    "repository_url": "https://bitbucket.org/acme/app.git",
                    "tracker_id": "bb",
                }
            ],
        },
        "trigger_event_data": trigger,
        "trigger_project_id": None,
        "trigger_tracker_id": "bb",
        "git_credentials_map": {"bb": {"token": "tok", "tracker_type": "bitbucket"}},
    }


def _continuation_job(execution_id):
    return {
        "execution_id": str(execution_id),
        "flow_id": str(uuid4()),
        "agent_type": "copilot",
        "prompt": "address feedback",
        "host_exec_profile": "copilot-seat",
        "agent_config": {"host_exec_profile": "copilot-seat"},
        "completion_protocol": "host_exec",
        "host_exec_publication": {"mode": "legacy"},
        "host_exec_resume": {"session_id": SESSION, "execution_id": str(uuid4())},
    }


@pytest.mark.asyncio
async def test_continuation_delivery_pushes_the_existing_pr_branch(monkeypatch):
    execution_id = uuid4()
    trigger = {
        "source": "jira",
        "_resume": {"execution_id": str(uuid4()), "source_branch": BRANCH},
    }
    _patch_delivery(monkeypatch, trigger, _continuation_context(execution_id, trigger))
    hydrated = await hydrate_host_exec_job(MagicMock(), _continuation_job(execution_id))
    plan = hydrated["host_exec_publication"]
    repo = hydrated["host_exec_checkout"]["repositories"][0]
    assert plan["continuation"] is True
    assert plan["branch"] == BRANCH == repo["branch"]
    assert hydrated["host_exec_resume"]["session_id"] == SESSION


@pytest.mark.asyncio
async def test_continuation_lease_without_resume_trigger_is_refused(monkeypatch):
    execution_id = uuid4()
    trigger = {"source": "jira"}
    _patch_delivery(monkeypatch, trigger, _continuation_context(execution_id, trigger))
    with pytest.raises(HostExecDeliveryError, match="resume_unavailable"):
        await hydrate_host_exec_job(MagicMock(), _continuation_job(execution_id))


@pytest.mark.asyncio
async def test_continuation_binds_the_existing_pr_without_opening_another(monkeypatch):
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    bound = []
    monkeypatch.setattr(
        "preloop.services.flow_pr_binding.record_opened_pr",
        lambda db, execution_id, url, **kw: bound.append((execution_id, url)),
    )
    pr_url = "https://bitbucket.org/acme/app/pull-requests/1"
    forge = SimpleNamespace(
        tracker_type="bitbucket",
        list_open_pull_requests_by_source_branch=AsyncMock(
            return_value={"items": [{"url": pr_url, "source_branch": BRANCH}]}
        ),
        create_pull_request=AsyncMock(side_effect=AssertionError("second PR")),
    )
    execution_id = uuid4()
    trigger = {
        "source": "jira",
        "_resume": {"execution_id": str(uuid4()), "source_branch": BRANCH},
    }
    orch = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orch.db = MagicMock()
    orch.execution_log = SimpleNamespace(
        id=execution_id,
        result={
            "host_publication": {"status": "pushed", "branch": BRANCH, "head_sha": HEAD}
        },
    )
    orch.flow = SimpleNamespace(
        name="Implementer", agent_config={"host_exec_profile": "copilot-seat"}
    )
    orch.trigger_event_data = trigger
    orch.execution_logger = MagicMock()
    monkeypatch.setattr(
        orch,
        "prepare_host_exec_checkout_context",
        AsyncMock(return_value=_continuation_context(execution_id, trigger)),
    )
    monkeypatch.setattr(
        orch, "_publication_tracker_clients", AsyncMock(return_value=[forge])
    )
    assert await orch._open_host_published_pr("SUCCEEDED") is None
    assert bound == [(execution_id, pr_url)]
    forge.create_pull_request.assert_not_called()


def test_delayed_lease_revalidates_continuation_from_rows(monkeypatch):
    """A lease rebuilt without orchestrator context validates from the rows.

    The flow has no copilot_model alias, so the lease and the recorded
    session carry the catalog model; the comparison uses the same value.
    """
    flow = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        agent_config={"host_exec_profile": "copilot-seat"},
        ai_model=SimpleNamespace(model_identifier="gpt-5.1"),
        git_clone_config=ONE_REPO,
    )
    session = {
        "harness": "copilot_cli",
        "session_id": SESSION,
        "host_exec_profile": "copilot-seat",
        "model_identifier": "gpt-5.1",
    }
    _, prior, runner, resume = _rows(monkeypatch, session=session, flow=flow)
    execution = SimpleNamespace(trigger_event_details={"_resume": resume})
    executor = RemoteRunnerExecutor(
        "copilot",
        {},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
        flow=flow,
        execution=execution,
    )
    payload = executor._lease_payload(
        execution_id=uuid4(), flow_id=flow.id, prompt="address feedback", flow=flow
    )
    assert payload["host_exec_resume"] == {
        "session_id": SESSION,
        "execution_id": str(prior.id),
    }
    assert workspace_owner_runner_id(MagicMock(), payload=payload) == runner.id

    flow.ai_model = SimpleNamespace(model_identifier="other-model")
    with pytest.raises(HostContinuationError, match="model changed"):
        executor._lease_payload(
            execution_id=uuid4(), flow_id=flow.id, prompt="again", flow=flow
        )


def test_continuation_after_a_no_change_continuation_is_admitted(monkeypatch):
    flow, _, _, resume = _rows(
        monkeypatch,
        result={
            "pr_url": "https://bitbucket.org/acme/app/pull-requests/1",
            "host_publication": {"status": "no_changes", "branch": BRANCH},
        },
    )
    resolved = resolve_host_continuation(
        MagicMock(),
        flow=flow,
        resume=resume,
        profile="copilot-seat",
        model_identifier="team-default",
    )
    assert resolved["session_id"] == SESSION


def _continuation_orchestrator(monkeypatch, receipt, forge, trigger):
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    execution_id = uuid4()
    orch = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orch.db = MagicMock()
    orch.execution_log = SimpleNamespace(
        id=execution_id, result={"host_publication": receipt}
    )
    orch.flow = SimpleNamespace(
        name="Implementer", agent_config={"host_exec_profile": "copilot-seat"}
    )
    orch.trigger_event_data = trigger
    orch.execution_logger = MagicMock()
    monkeypatch.setattr(
        orch,
        "prepare_host_exec_checkout_context",
        AsyncMock(return_value=_continuation_context(execution_id, trigger)),
    )
    monkeypatch.setattr(
        orch, "_publication_tracker_clients", AsyncMock(return_value=[forge])
    )
    return orch, execution_id


@pytest.mark.asyncio
async def test_no_change_continuation_binds_existing_pr_and_never_creates(monkeypatch):
    bound = []
    monkeypatch.setattr(
        "preloop.services.flow_pr_binding.record_opened_pr",
        lambda db, execution_id, url, **kw: bound.append((execution_id, url)),
    )
    pr_url = "https://bitbucket.org/acme/app/pull-requests/1"
    forge = SimpleNamespace(
        tracker_type="bitbucket",
        list_open_pull_requests_by_source_branch=AsyncMock(
            return_value={"items": [{"url": pr_url, "source_branch": BRANCH}]}
        ),
        create_pull_request=AsyncMock(side_effect=AssertionError("second PR")),
    )
    trigger = {
        "source": "jira",
        "_resume": {"execution_id": str(uuid4()), "source_branch": BRANCH},
    }
    receipt = {"status": "no_changes", "branch": BRANCH}
    orch, execution_id = _continuation_orchestrator(
        monkeypatch, receipt, forge, trigger
    )
    assert await orch._open_host_published_pr("SUCCEEDED") is None
    assert bound == [(execution_id, pr_url)]

    # The PR was closed meanwhile: no create, an actionable failure.
    forge.list_open_pull_requests_by_source_branch = AsyncMock(
        return_value={"items": []}
    )
    orch, _ = _continuation_orchestrator(monkeypatch, receipt, forge, trigger)
    error = await orch._open_host_published_pr("SUCCEEDED")
    assert error and error.startswith("publication_failed")
    forge.create_pull_request.assert_not_called()

    # A first run with no changes never reaches binding at all.
    orch, _ = _continuation_orchestrator(
        monkeypatch, receipt, forge, {"source": "jira"}
    )
    assert await orch._open_host_published_pr("SUCCEEDED") is None


def test_delayed_lease_carries_the_alias_it_validated(monkeypatch):
    """The model the delayed lease carries is the one admission compared."""
    flow = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        agent_config={
            "host_exec_profile": "copilot-seat",
            "copilot_model": "team-default",
        },
        ai_model=SimpleNamespace(model_identifier="gpt-5.1"),
        git_clone_config=ONE_REPO,
    )
    _, prior, _, resume = _rows(monkeypatch, flow=flow)
    executor = RemoteRunnerExecutor(
        "copilot",
        {},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
        flow=flow,
        execution=SimpleNamespace(trigger_event_details={"_resume": resume}),
    )
    payload = executor._lease_payload(
        execution_id=uuid4(), flow_id=flow.id, prompt="address feedback", flow=flow
    )
    assert payload["model_identifier"] == "team-default"
    assert payload["host_exec_resume"]["execution_id"] == str(prior.id)
