"""Disposable-Postgres lifecycle integration through real provider/controller hooks."""

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.base import CRUDBase
from preloop.models.schemas.flow_execution import FlowExecutionUpdate
from preloop.models.crud import (
    crud_account,
    crud_flow_execution,
    crud_issue,
    crud_issue_lifecycle,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.schemas.issue_lifecycle import Criterion, ReadinessContract
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.issue_lifecycle import IssueLifecycleService
from preloop.services.issue_lifecycle_provider import GitHubLifecycleProvider
from preloop.services.issue_lifecycle_runtime import lifecycle_execution_finished

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO = "example/project"


class FakeGitHub:
    """Fake external transport; production provider pagination/effects stay real."""

    def __init__(self) -> None:
        self.issues = {
            1: {
                "id": 1001,
                "number": 1,
                "title": "Save preferences",
                "body": "Persist user preferences across sessions.",
                "state": "open",
                "html_url": f"https://github.com/{REPO}/issues/1",
                "labels": [{"name": "feature"}],
            }
        }
        self.comments: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.prs: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_after_creation = False
        self.change_before_label = False

    def merge(self, *, manual: bool = False, multiple: bool = False) -> None:
        self.issues[1]["state"] = "closed"
        self.timeline = [{"event": "closed", "commit_id": None if manual else SHA}]
        self.prs = {
            7: {
                "number": 7,
                "merged": True,
                "merge_commit_sha": SHA,
                "html_url": f"https://github.com/{REPO}/pull/7",
                "base": {"repo": {"full_name": REPO}},
            }
        }
        if multiple:
            self.prs[6] = {
                **self.prs[7],
                "number": 6,
                "merge_commit_sha": OTHER_SHA,
                "html_url": f"https://github.com/{REPO}/pull/6",
            }
            self.timeline.insert(
                0,
                {
                    "event": "cross-referenced",
                    "source": {
                        "issue": {
                            "number": 6,
                            "pull_request": {},
                            "repository": {"full_name": REPO},
                        }
                    },
                },
            )
            self.timeline[0]["source"]["issue"]["pull_request"] = {
                "url": "https://api.github.com/example"
            }

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, endpoint))
        root = f"/repos/{REPO}"
        if endpoint == f"{root}/issues" and method == "GET":
            return deepcopy(list(self.issues.values()))
        if endpoint == f"{root}/issues" and method == "POST":
            number = max(self.issues) + 1
            item = {
                **data,
                "number": number,
                "state": "open",
                "html_url": f"https://github.com/{REPO}/issues/{number}",
            }
            self.issues[number] = item
            if self.fail_after_creation:
                self.fail_after_creation = False
                raise TimeoutError("provider effect succeeded but response was lost")
            return deepcopy(item)
        if endpoint == f"{root}/issues/1/timeline":
            return deepcopy(self.timeline)
        if endpoint == f"{root}/commits/{SHA}/pulls":
            return deepcopy(list(self.prs.values()))
        if "/pulls/" in endpoint:
            return deepcopy(self.prs[int(endpoint.rsplit("/", 1)[-1])])
        if endpoint == f"{root}/issues/1/comments":
            if method == "GET":
                return deepcopy(self.comments)
            item = {
                "id": len(self.comments) + 1,
                "body": data["body"],
                "html_url": "https://github.com/example/project/issues/1#issuecomment-1",
            }
            self.comments.append(item)
            return deepcopy(item)
        if "/issues/comments/" in endpoint:
            self.comments[0]["body"] = data["body"]
            return deepcopy(self.comments[0])
        if endpoint.endswith("/labels"):
            if self.change_before_label:
                self.issues[1]["body"] += " New requirement."
            self.issues[1]["labels"].extend({"name": name} for name in data["labels"])
            return self.issues[1]["labels"]
        return deepcopy(self.issues[int(endpoint.rsplit("/", 1)[-1])])


class Capabilities:
    """Substitute only operator environment runtime availability."""

    async def blockers(self, profile: str, commands: list[str]) -> list[str]:
        return (
            []
            if profile == "python-tests" and commands == ["unit"]
            else ["environment_unavailable"]
        )


@pytest.fixture
def rig(
    db_session: Session,
) -> tuple[IssueLifecycleService, FakeGitHub, models.Flow, models.Flow]:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Example", "is_active": True}
    )
    tracker = crud_tracker.create(
        db_session,
        obj_in={
            "name": "Example tracker",
            "tracker_type": "github",
            "account_id": account.id,
            "api_key": "fake-local-only",
        },
    )
    org = crud_organization.create(
        db_session,
        obj_in={"name": "example", "identifier": "example", "tracker_id": tracker.id},
    )
    project = crud_project.create(
        db_session,
        obj_in={"name": "project", "identifier": REPO, "organization_id": org.id},
    )
    issue = crud_issue.create(
        db_session,
        obj_in={
            "title": "Save preferences",
            "description": "Persist user preferences across sessions.",
            "status": "open",
            "external_id": "1001",
            "key": f"{REPO}#1",
            "project_id": project.id,
            "tracker_id": tracker.id,
        },
    )
    config = {
        "name": "Implementation",
        "account_id": account.id,
        "agent_type": "codex",
        "agent_config": {},
        "prompt_template": "Implement",
        "is_enabled": True,
        "allowed_mcp_servers": [],
        "allowed_mcp_tools": [],
    }
    implementer = CRUDBase(models.Flow).create(db_session, obj_in=config)
    audit = CRUDBase(models.Flow).create(
        db_session,
        obj_in={
            **config,
            "name": "Audit",
            "agent_config": {"lifecycle_kind": "merge_audit"},
            "git_clone_config": {
                "enabled": True,
                "create_pull_request": False,
                "repositories": [
                    {"project_id": str(project.id), "tracker_id": str(tracker.id)}
                ],
            },
        },
    )
    policy = {
        "ready_enabled": True,
        "implementation_flow_id": str(implementer.id),
        "audit_flow_id": str(audit.id),
        "create_follow_ups": True,
    }
    crud_project.update(
        db_session, db_obj=project, obj_in={"settings": {"issue_lifecycle": policy}}
    )
    transport = FakeGitHub()
    service = IssueLifecycleService(
        db_session,
        account_id=account.id,
        issue=issue,
        provider=GitHubLifecycleProvider(transport, REPO),
        capabilities=Capabilities(),
        policy=policy,
    )
    return service, transport, implementer, audit


async def contract_for(
    service: IssueLifecycleService, count: int = 1
) -> ReadinessContract:
    snapshot = await service.provider.issue(1)
    return ReadinessContract(
        issue_revision=snapshot.revision,
        problem="Preferences disappear",
        user_outcome="Preferences persist",
        constraints=["Use existing storage"],
        non_goals=["Cross-device sync"],
        criteria=[
            Criterion(
                id=f"C{i}",
                outcome="Preference survives restart",
                verification="Restart and assert persisted value",
            )
            for i in range(count)
        ],
        code_entry_points=["app/preferences.py"],
        test_entry_points=["tests/test_preferences.py"],
        environment_profile="python-tests",
        test_commands=["unit"],
    )


def audit_result(
    contract: ReadinessContract, verdict: str = "complete"
) -> dict[str, Any]:
    return {
        "status": "success",
        "checked_out_sha": SHA,
        "issue_revision": contract.issue_revision,
        "criteria": [
            {
                "criterion_id": item.id,
                "verdict": verdict,
                "code": ["app/preferences.py:20"],
                "tests": ["pytest tests/test_preferences.py: passed"],
                "observations": ["Stored preference survives process restart"],
                "reason": "Verified persistence",
            }
            for item in contract.criteria
        ],
        "follow_ups": [
            {
                "criterion_id": item.id,
                "kind": "defect",
                "title": f"Fix {item.id}",
                "description": "Persistence gap reproduced; retain value across process restart.",
                "evidence": ["evidence/tests.txt"],
            }
            for item in contract.criteria
        ]
        if verdict == "gap"
        else [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant,expected",
    [
        ("vague", "missing_problem"),
        ("missing_environment", "environment_unavailable"),
        ("dependency", "blocked_dependency:2"),
        ("conflict", "conflict:two incompatible persistence requirements"),
    ],
)
async def test_readiness_blocks_material_gaps(
    rig: tuple, variant: str, expected: str
) -> None:
    service, transport, _, _ = rig
    contract = await contract_for(service)
    if variant == "vague":
        contract = ReadinessContract(issue_revision=contract.issue_revision)
    elif variant == "missing_environment":
        contract.environment_profile = "missing"
    elif variant == "dependency":
        transport.issues[2] = {**transport.issues[1], "number": 2}
        contract.dependencies = [2]
    else:
        contract.conflicts = ["two incompatible persistence requirements"]
    assert expected in (await service.refine(contract))["blockers"]
    assert (await service.ready(contract.issue_revision))["state"] == "blocked"
    assert not any(method == "POST" for method, _ in transport.calls)


@pytest.mark.asyncio
async def test_ready_and_duplicate_webhook_create_one_authorized_pickup(
    rig: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, transport, flow, _ = rig
    contract = await contract_for(service)
    assert not (await service.refine(contract))["blockers"]
    assert (await service.ready(contract.issue_revision))["state"] == "ready"
    assert (await service.ready(contract.issue_revision))["state"] == "ready"
    assert transport.issues[1]["labels"] == [
        {"name": "feature"},
        {"name": "agent-ready"},
    ]
    monkeypatch.setattr(
        "preloop.services.issue_lifecycle_runtime.build_lifecycle_service",
        AsyncMock(return_value=service),
    )
    monkeypatch.setattr(
        "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
        lambda: True,
    )
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "preloop.services.flow_execution_dispatcher.dispatch_execute", dispatch
    )
    event = {
        "type": "issue_labeled",
        "project_id": str(service.issue.project_id),
        "payload": {"issue": {"id": 1001}},
    }
    trigger = FlowTriggerService(service.db)
    first = await trigger._start_flow_execution(flow, event, None)
    second = await trigger._start_flow_execution(flow, event, None)
    assert first.id == second.id
    assert len({str(call.args[0]) for call in dispatch.await_args_list}) == 1
    assert (
        first.trigger_event_details["lifecycle_pickup"]["issue_revision"]
        == contract.issue_revision
    )
    assert first.cli_session is None


@pytest.mark.asyncio
async def test_scope_change_cannot_dispatch_and_is_visible(rig: tuple) -> None:
    service, transport, flow, _ = rig
    contract = await contract_for(service)
    await service.refine(contract)
    transport.change_before_label = True
    assert (await service.ready(contract.issue_revision))[
        "state"
    ] == "needs_reconciliation"
    dispatch = AsyncMock()
    assert await service.schedule_pickup(flow, {}, dispatch) is None
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_readiness_never_writes_label(rig: tuple) -> None:
    service, transport, _, _ = rig
    service.policy["ready_enabled"] = False
    with pytest.raises(ValueError, match="not_authorized"):
        await service.ready("revision")
    assert not transport.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["complete", "gap", "unknown"])
async def test_merge_to_terminal_hook_one_audit_and_deduplicated_followups(
    rig: tuple, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service, 5)
    await service.refine(contract)
    transport.merge(multiple=True)
    monkeypatch.setattr(
        "preloop.services.issue_lifecycle_runtime.build_lifecycle_service",
        AsyncMock(return_value=service),
    )
    monkeypatch.setattr(
        "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "preloop.services.flow_execution_dispatcher.dispatch_execute",
        AsyncMock(return_value=True),
    )
    trigger = FlowTriggerService(service.db)
    event = {
        "type": "issue_closed",
        "project_id": str(service.issue.project_id),
        "payload": {"issue": {"id": 1001}},
    }
    execution = await trigger._start_flow_execution(flow, event, None)
    assert execution.trigger_event_details["payload"]["sha"] == SHA
    assert len(execution.trigger_event_details["payload"]["lifecycle"]["merges"]) == 2
    assert execution.cli_session is None
    assert execution.retry_of_execution_id is None
    assert (await trigger._start_flow_execution(flow, event, None)).id == execution.id
    execution = crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(
            status="SUCCEEDED", result=audit_result(contract, verdict)
        ),
    )
    await lifecycle_execution_finished(service.db, execution, flow)
    await lifecycle_execution_finished(service.db, execution, flow)
    row = crud_issue_lifecycle.get(
        service.db,
        account_id=service.account_id,
        issue_id=service.issue.id,
        kind="merge_audit",
        revision=SHA,
    )
    assert row.state == "published" and row.data["verdict"] == verdict
    assert len(transport.comments) == 1
    assert len(transport.issues) == (4 if verdict == "gap" else 1)
    for issue in list(transport.issues.values())[1:]:
        assert issue["labels"] == []
        assert SHA in issue["body"]
    assert transport.issues[1]["state"] == "closed"


@pytest.mark.asyncio
async def test_manual_completed_close_is_not_merge_authority(rig: tuple) -> None:
    service, transport, _, flow = rig
    transport.merge(manual=True)
    dispatch = AsyncMock()
    assert await service.schedule_audit(flow, dispatch) is None
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_success_local_failure_recovers_existing_followup(
    rig: tuple,
) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service)
    await service.refine(contract)
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(
            status="SUCCEEDED", result=audit_result(contract, "gap")
        ),
    )
    service.db.commit()  # The production terminal hook receives a durable result.
    transport.fail_after_creation = True
    with pytest.raises(TimeoutError):
        await service.finish_audit(execution)
    assert len(transport.issues) == 2
    result = await service.finish_audit(execution)
    assert len(result["follow_ups"]) == 1
    assert len(transport.issues) == 2
    assert len(transport.comments) == 1


@pytest.mark.asyncio
async def test_exact_sha_and_bound_execution_required(rig: tuple) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service)
    await service.refine(contract)
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    result = {**audit_result(contract), "checked_out_sha": OTHER_SHA}
    crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(status="SUCCEEDED", result=result),
    )
    with pytest.raises(ValueError, match="revision_mismatch"):
        await service.finish_audit(execution)
    assert not transport.comments
    impostor = models.FlowExecution(
        id=uuid4(),
        trigger_event_details=execution.trigger_event_details,
        status="SUCCEEDED",
        result=result,
    )
    with pytest.raises(ValueError, match="not_bound"):
        await service.finish_audit(impostor)


@pytest.mark.asyncio
async def test_deployment_acceptance_remains_unknown_without_probe(rig: tuple) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service)
    contract.criteria[0].deployment_required = True
    await service.refine(contract)
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(status="SUCCEEDED", result=audit_result(contract)),
    )
    result = await service.finish_audit(execution)
    assert result["verdict"] == "unknown"
    assert result["deployment_verification"]["merge_sha"] == SHA
    assert result["deployment_verification"]["criteria"] == ["C0"]
    assert all(endpoint.startswith("/repos/") for _, endpoint in transport.calls)


@pytest.mark.asyncio
async def test_existing_human_followup_is_reused_and_policy_blocks_creation(
    rig: tuple,
) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service)
    await service.refine(contract)
    transport.issues[2] = {
        "number": 2,
        "title": "Existing persistence defect",
        "body": f"Source: https://github.com/{REPO}/issues/1\nAcceptance criterion: C0",
        "state": "open",
        "html_url": f"https://github.com/{REPO}/issues/2",
        "labels": [{"name": "bug"}],
    }
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(
            status="SUCCEEDED", result=audit_result(contract, "gap")
        ),
    )
    result = await service.finish_audit(execution)
    assert list(result["follow_ups"].values()) == [transport.issues[2]["html_url"]]
    assert len(transport.issues) == 2
    assert transport.issues[2]["labels"] == [{"name": "bug"}]
    assert not any(
        method == "POST" and endpoint.endswith("/issues")
        for method, endpoint in transport.calls
    )


@pytest.mark.asyncio
async def test_no_evidence_cannot_claim_complete_or_file_unknown_gap(
    rig: tuple,
) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service)
    await service.refine(contract)
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    result = audit_result(contract, "gap")
    result["criteria"][0].update(code=[], tests=[], observations=[])
    crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(status="SUCCEEDED", result=result),
    )
    summary = await service.finish_audit(execution)
    assert summary["verdict"] == "unknown"
    assert summary["evidence"]["criteria"][0]["verdict"] == "unknown"
    assert len(transport.issues) == 1


@pytest.mark.asyncio
async def test_no_followup_policy_leaves_gap_in_audit(rig: tuple) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service)
    await service.refine(contract)
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(
            status="SUCCEEDED", result=audit_result(contract, "gap")
        ),
    )
    service.policy["create_follow_ups"] = False
    result = await service.finish_audit(execution)
    assert result["verdict"] == "gap"
    assert not result["follow_ups"]
    assert len(transport.issues) == 1


@pytest.mark.asyncio
async def test_deployment_audit_is_separate_approved_and_pinned(rig: tuple) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service)
    contract.criteria[0].deployment_required = True
    await service.refine(contract)
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(status="SUCCEEDED", result=audit_result(contract)),
    )
    await service.finish_audit(execution)
    deploy_flow = CRUDBase(models.Flow).create(
        service.db,
        obj_in={
            "name": "Staging verification",
            "account_id": service.account_id,
            "agent_type": "codex",
            "agent_config": {"lifecycle_kind": "deployment_audit"},
            "prompt_template": "Verify only the approved deployment scope",
            "is_enabled": True,
            "allowed_mcp_tools": [],
            "allowed_mcp_servers": [],
            "git_clone_config": flow.git_clone_config,
        },
    )
    kwargs = {
        "merge_sha": SHA,
        "target": "staging",
        "deployed_revision": OTHER_SHA,
        "deployment_evidence": "https://ci.example.com/deployments/42",
        "flow": deploy_flow,
        "dispatch": AsyncMock(),
    }
    with pytest.raises(ValueError, match="not_authorized"):
        await service.schedule_deployment_audit(**kwargs)
    service.policy["deployment_targets"] = {
        "staging": {
            "flow_id": str(deploy_flow.id),
            "approved_scope": [
                "GET https://staging.example.com/preferences for synthetic test account"
            ],
        }
    }
    deployed = await service.schedule_deployment_audit(**kwargs)
    assert deployed.id != execution.id
    assert deployed.trigger_event_details["payload"]["sha"] == OTHER_SHA
    assert (await service.schedule_deployment_audit(**kwargs)).id == deployed.id
    result = {**audit_result(contract), "checked_out_sha": OTHER_SHA}
    crud_flow_execution.update(
        service.db,
        db_obj=deployed,
        obj_in=FlowExecutionUpdate(status="SUCCEEDED", result=result),
    )
    summary = await service.finish_audit(deployed)
    assert summary["verdict"] == "complete"
    assert summary["deployment_verification"] is None
    assert len(transport.comments) == 2
    assert "Deployed revision" in transport.comments[-1]["body"]


def test_http_entrypoints_preserve_tenant_scope_and_authorization(
    rig: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from preloop.api.auth import get_current_active_user
    from preloop.api.endpoints.issue_lifecycle import router
    from preloop.models.db.session import get_db_session

    service, transport, _, _ = rig
    transport.tracker_type = "github"
    monkeypatch.setattr(
        "preloop.services.issue_lifecycle_runtime.get_tracker_client",
        AsyncMock(return_value=transport),
    )
    monkeypatch.setattr(
        "preloop.services.issue_lifecycle_runtime.FlowEnvironmentCapabilities.blockers",
        AsyncMock(return_value=[]),
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: service.db
    actor = models.User(
        id=uuid4(),
        account_id=service.account_id,
        username="example-user",
        is_active=True,
    )
    app.dependency_overrides[get_current_active_user] = lambda: actor
    with TestClient(app) as client:
        path = f"/api/v1/issues/{service.issue.id}/lifecycle"
        response = client.get(path)
        assert response.status_code == 200
        revision = response.json()["issue_revision"]
        contract = {
            "issue_revision": revision,
            "problem": "Preferences disappear",
            "user_outcome": "Retain preferences",
            "criteria": [
                {
                    "id": "C0",
                    "outcome": "Retain value",
                    "verification": "Restart and inspect persisted value",
                }
            ],
            "code_entry_points": ["app/preferences.py"],
            "test_entry_points": ["tests/test_preferences.py"],
            "environment_profile": "python-tests",
            "test_commands": ["unit"],
        }
        assert client.post(path + "/refine", json=contract).status_code == 200
        assert (
            client.post(path + "/ready", json={"issue_revision": revision}).json()[
                "state"
            ]
            == "ready"
        )
        actor.account_id = uuid4()
        response = client.get(path)
        assert response.status_code == 409
        assert response.json()["detail"] == "lifecycle_issue_not_found"
        del app.dependency_overrides[get_current_active_user]
        assert client.get(path).status_code in {401, 403}


@pytest.mark.asyncio
async def test_refinement_terminal_hook_stores_reviewable_contract_without_label(
    rig: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, transport, _, flow = rig
    contract = await contract_for(service)
    monkeypatch.setattr(
        "preloop.services.issue_lifecycle_runtime.build_lifecycle_service",
        AsyncMock(return_value=service),
    )
    flow.agent_config = {"lifecycle_kind": "refinement"}
    execution = models.FlowExecution(
        status="SUCCEEDED",
        trigger_event_details={
            "lifecycle_refinement": {
                "issue_id": str(service.issue.id),
                "issue_revision": contract.issue_revision,
            }
        },
        result={"status": "success", "readiness_contract": contract.model_dump()},
    )
    await lifecycle_execution_finished(service.db, execution, flow)
    row = crud_issue_lifecycle.get(
        service.db,
        account_id=service.account_id,
        issue_id=service.issue.id,
        kind="readiness",
        revision=contract.issue_revision,
    )
    assert row.state == "reviewable"
    assert not any(method == "POST" for method, _ in transport.calls)


@pytest.mark.asyncio
async def test_concurrent_ready_requests_serialize_without_blocking_event_loop(
    db_engine: Any,
) -> None:
    import asyncio

    # Separate committed connections exercise real PostgreSQL contention. This
    # test database is disposable; regular db_session fixtures are savepoints
    # on one connection and cannot exercise concurrent ownership correctly.
    with Session(db_engine) as first_db, Session(db_engine) as second_db:
        service, transport, _, _ = rig.__wrapped__(first_db)
        contract = await contract_for(service)
        await service.refine(contract)
        issue = crud_issue_lifecycle.get_issue(
            second_db, account_id=service.account_id, issue_id=service.issue.id
        )
        second = IssueLifecycleService(
            second_db,
            account_id=service.account_id,
            issue=issue,
            provider=service.provider,
            capabilities=Capabilities(),
            policy=service.policy,
        )
        original = transport._request
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(
            method: str,
            endpoint: str,
            data: dict[str, Any] | None = None,
            params: dict[str, Any] | None = None,
        ) -> Any:
            if method == "POST" and endpoint.endswith("/labels"):
                entered.set()
                await release.wait()
            return await original(method, endpoint, data, params)

        transport._request = delayed
        first_task = asyncio.create_task(service.ready(contract.issue_revision))
        await asyncio.wait_for(entered.wait(), 2)
        second_task = asyncio.create_task(second.ready(contract.issue_revision))
        await asyncio.sleep(0.1)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first_task, second_task), 3)
        assert all(result["state"] == "ready" for result in results)
        assert (
            sum(
                method == "POST" and endpoint.endswith("/labels")
                for method, endpoint in transport.calls
            )
            == 1
        )


@pytest.mark.asyncio
async def test_failed_runner_publishes_unknown_without_inventing_checkout(
    rig: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    service, transport, _, flow = rig
    contract = await contract_for(service)
    await service.refine(contract)
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    crud_flow_execution.update(
        service.db, db_obj=execution, obj_in=FlowExecutionUpdate(status="FAILED")
    )
    service.db.commit()
    monkeypatch.setattr(
        "preloop.services.issue_lifecycle_runtime.build_lifecycle_service",
        AsyncMock(return_value=service),
    )
    # Exercise the universal terminal notification path, including startup
    # failures where the main agent-result completion path never runs.
    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.db, orchestrator.flow, orchestrator.execution_log = (
        service.db,
        flow,
        execution,
    )
    await orchestrator._notify_terminal("FAILED")
    row = crud_issue_lifecycle.get(
        service.db,
        account_id=service.account_id,
        issue_id=service.issue.id,
        kind="merge_audit",
        revision=SHA,
    )
    assert row.state == "published"
    assert row.data["verdict"] == "unknown"
    assert row.data["evidence"]["checked_out_sha"] is None
    assert len(transport.issues) == 1 and len(transport.comments) == 1


@pytest.mark.asyncio
async def test_lost_label_response_does_not_lose_authorized_pickup(rig: tuple) -> None:
    service, transport, flow, _ = rig
    contract = await contract_for(service)
    await service.refine(contract)
    original = transport._request

    async def lose_response(
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        result = await original(method, endpoint, data, params)
        if method == "POST" and endpoint.endswith("/labels"):
            raise TimeoutError("label applied; acknowledgment lost")
        return result

    transport._request = lose_response
    with pytest.raises(TimeoutError):
        await service.ready(contract.issue_revision)
    row = crud_issue_lifecycle.get(
        service.db,
        account_id=service.account_id,
        issue_id=service.issue.id,
        kind="pickup",
        revision="once",
    )
    assert row.state == "label_pending"
    execution = await service.schedule_pickup(
        flow, {"type": "issue_labeled"}, AsyncMock()
    )
    assert execution is not None
    assert (await service.ready(contract.issue_revision))["state"] == "dispatched"


@pytest.mark.asyncio
async def test_explicit_ready_reconciles_only_unlaunched_current_revision(
    rig: tuple,
) -> None:
    service, transport, flow, _ = rig
    old = await contract_for(service)
    await service.refine(old)
    await service.ready(old.issue_revision)
    transport.issues[1]["body"] += " New scope."
    current = await contract_for(service)
    await service.refine(current)
    stale = await service.ready(old.issue_revision)
    assert stale["state"] == "blocked"
    assert "issue_scope_changed" in stale["blockers"]
    assert (await service.ready(current.issue_revision))["state"] == "ready"
    prior = service._get("pickup", old.issue_revision)
    assert prior.state == "superseded"
    assert prior.data["contract"] == old.model_dump()
    dispatch = AsyncMock()
    first = await service.schedule_pickup(flow, {}, dispatch)
    assert first is not None
    assert (await service.ready(current.issue_revision))["state"] == "dispatched"
    transport.issues[1]["body"] += " Yet another scope."
    next_contract = await contract_for(service)
    await service.refine(next_contract)
    blocked = await service.ready(next_contract.issue_revision)
    assert blocked["state"] == "needs_reconciliation"
    assert "pickup_reconciliation_required" in blocked["blockers"]
    assert service._get("pickup", "once").execution_id == first.id
    assert await service.schedule_pickup(flow, {}, dispatch) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("exhaustion", ["time", "requests"])
async def test_audit_publication_budget_rolls_back_and_can_reconcile(
    rig: tuple, monkeypatch: pytest.MonkeyPatch, exhaustion: str
) -> None:
    import asyncio
    from preloop.services import issue_lifecycle_provider

    service, transport, _, flow = rig
    contract = await contract_for(service)
    await service.refine(contract)
    transport.merge()
    execution = await service.schedule_audit(flow, AsyncMock())
    crud_flow_execution.update(
        service.db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(
            status="SUCCEEDED", result=audit_result(contract, "gap")
        ),
    )
    service.db.commit()
    original = transport._request

    async def stalled(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(1)
        return await original(*args, **kwargs)

    with monkeypatch.context() as patcher:
        if exhaustion == "time":
            patcher.setattr(
                issue_lifecycle_provider, "PUBLICATION_TIMEOUT_SECONDS", 0.02
            )
            patcher.setattr(transport, "_request", stalled)
            error = "publication_deadline_exceeded"
        else:
            patcher.setattr(issue_lifecycle_provider, "PUBLICATION_MAX_REQUESTS", 1)
            error = "publication_request_budget_exhausted"
        with pytest.raises(ValueError, match=error):
            await service.finish_audit(execution)
    assert service._get("merge_audit", SHA).state == "queued"
    assert len(transport.issues) == 1
    assert not transport.comments
    result = await service.finish_audit(execution)
    assert len(result["follow_ups"]) == 1
    assert len(transport.issues) == 2
