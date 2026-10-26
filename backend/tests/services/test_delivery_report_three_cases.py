"""Milestone 1 acceptance on PostgreSQL, with fake forge, tracker and mail.

Run against an Alembic-migrated local DATABASE_URL with
PRELOOP_DISABLE_TELEMETRY=true. Only transport boundaries are replaced: the
orchestrator, CRUD, policy, rendering, distribution and HTTP actions are real.
A strict xfail describes the missing per-member hand-off; no proposals or
member sections are inserted into a report by the test. Member delivery is EE.
"""

from __future__ import annotations

import asyncio
import copy
import json
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_delivery_report_policy,
    crud_flow,
    crud_flow_execution,
    crud_issue,
    crud_project,
    crud_user,
)
from preloop.models.crud.base import CRUDBase
from preloop.models.schemas.flow import FlowUpdate
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionUpdate,
)
from preloop.services import delivery_report as dr
from preloop.services import delivery_report_actions as actions
from preloop.services import delivery_report_email as email
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from tests.endpoints.test_delivery_report_actions import FakeTracker
from tests.endpoints.test_delivery_reports import _flow
from tests.services.test_delivery_report_distribution import _project

FIXTURES = Path(__file__).parents[1] / "fixtures" / "delivery_report"
WINDOW = {"from": "2026-10-12T07:00:00+00:00", "to": "2026-10-19T07:00:00+00:00"}
REPO = "acme/app"
UNMET = "Exports respect the date filter"
EXPORT = f"{REPO}#12"
HOT_PATH = f"{REPO}#103"
BASE = "/api/v1/delivery-reports"


def load_fixture(name: str) -> Any:
    """Read the existing report fixtures, independently for each scenario."""
    return json.loads((FIXTURES / name).read_text())


class MissingProductionBehaviorError(AssertionError):
    """Only this known missing behavior may xfail an acceptance test."""


class WeekTracker(FakeTracker):
    """Reuse the action fake, adding forge reads and observable tracker state."""

    def __init__(self) -> None:
        super().__init__()
        self.prs = {p["number"]: p for p in load_fixture("pull_requests.json")}
        self.issues = {i["key"]: i for i in load_fixture("issues.json")}
        self.reads: list[str] = []
        self.created: list[Any] = []
        for pr in self.prs.values():
            pr["url"] = pr["url"].replace("example.com", "github.com")
            pr.setdefault("head_sha", f"{pr['number']:040x}")
            # The owner reviewed backend changes; another member reviewed UI.
            pr["reviews"] = [
                {
                    "author": "jane-doe" if pr["number"] != 104 else "john-doe",
                    "state": "APPROVED",
                    "submitted_at": "2026-10-16T10:00:00+00:00",
                }
            ]
        self.prs[102]["description"] = "Adds CSV export.\n\nFixes #12"
        self.prs[103].update(
            title="Speed up search indexing",
            description="Updates the search hot path; no regression test was added.",
            changes={
                "files_changed": 1,
                "changed_files": [
                    {
                        "filename": "backend/app/search.py",
                        "additions": 40,
                        "deletions": 8,
                        "patch": "+def rebuild_index():",
                    }
                ],
            },
        )
        for issue in self.issues.values():
            issue["url"] = issue["url"].replace("example.com", "github.com")

    async def get_pull_request(self, number: str) -> dict[str, Any]:
        self.reads.append(number)
        return copy.deepcopy(self.prs[int(number)])

    async def update_issue(self, issue_id: str, issue_data: Any) -> Any:
        result = await super().update_issue(issue_id, issue_data)
        self.issues[issue_id]["status"] = issue_data.status
        return result

    async def create_issue(self, project_key: str, issue_data: Any) -> Any:
        result = await super().create_issue(project_key, issue_data)
        self.created.append(issue_data)
        result.url = f"https://github.com/{project_key}/issues/99"
        return result


@dataclass
class Scenario:
    """The same seed and captured output are used by pytest and the local demo."""

    owner: models.User
    member: models.User
    flow: models.Flow
    tracker: WeekTracker
    execution: models.FlowExecution | None = None
    messages: list[tuple[Any, ...]] = field(default_factory=list)
    model_calls: AsyncMock = field(default_factory=AsyncMock)

    @property
    def report(self) -> dict[str, Any]:
        assert self.execution is not None
        return dict(self.execution.result)

    @property
    def execution_id(self) -> str:
        assert self.execution is not None
        return str(self.execution.id)


def seed_week(db: Session, owner: models.User) -> Scenario:
    """Persist tracker facts and a prior merge audit, never a report result."""
    owner = crud_user.update(
        db,
        db_obj=owner,
        obj_in={
            "username": "jane-doe",
            "full_name": "Jane Doe",
            "email": "jane.doe@example.com",
        },
    )
    account = crud_account.get(db, id=owner.account_id)
    crud_account.update(db, db_obj=account, obj_in={"primary_user_id": owner.id})
    member = crud_user.create(
        db,
        obj_in={
            "account_id": owner.account_id,
            "email": "john.doe@example.com",
            "username": "john-doe",
            "full_name": "John Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "unused",
            "user_source": "local",
        },
    )
    project = _project(db, owner.account_id)
    project = crud_project.update(
        db,
        db_obj=project,
        obj_in={
            "identifier": REPO,
            "slug": REPO,
            "name": "Example application",
        },
    )
    crud_delivery_report_policy.save_overrides(
        db, account_id=owner.account_id, overrides={"onboarding_assessed_run": False}
    )
    fake = WeekTracker()
    for issue in fake.issues.values():
        crud_issue.create(
            db,
            obj_in={
                "project_id": project.id,
                "tracker_id": project.organization.tracker_id,
                "key": issue["key"],
                "external_id": issue["key"].split("#")[1],
                "external_url": issue["url"],
                "title": issue["title"],
                "description": issue["description"],
                "status": issue["status"],
                "created_at": datetime.fromisoformat(issue["created_at"]).replace(
                    tzinfo=None
                ),
                "updated_at": datetime.fromisoformat(issue["updated_at"]).replace(
                    tzinfo=None
                ),
            },
        )
    for pr in fake.prs.values():
        CRUDBase(models.IssueCostPullRequest).create(
            db,
            obj_in={
                "account_id": owner.account_id,
                "pr_key": pr["url"],
                "opened_at": datetime.fromisoformat(pr["created_at"]),
                "merged_at": datetime.fromisoformat(pr["merged_at"])
                if pr["merged_at"]
                else None,
                "state": "merged" if pr["merged_at"] else "open",
                "head_sha": pr["head_sha"],
                "origin": "human",
            },
        )
    audit_flow = _flow(db, owner.account_id, report=False)
    crud_flow.update(
        db,
        db_obj=audit_flow,
        flow_in=FlowUpdate(agent_config={"lifecycle_kind": "merge_audit"}),
        account_id=owner.account_id,
    )
    audit = load_fixture("merge_audit_executions.json")[0]
    envelope = audit["trigger_event_details"]["payload"]["lifecycle"]
    envelope["issue_url"] = fake.issues[EXPORT]["url"]
    envelope["merges"] = [{"number": 102, "sha": "2" * 40, "url": fake.prs[102]["url"]}]
    criterion = audit["result"]["criteria"][1]
    criterion.update(
        verdict="gap",
        tests=[
            "pytest backend/tests/test_export.py::test_date_filter FAILED: rows outside date range"
        ],
        observations=["Export includes rows outside the requested date range"],
        reason="Date filter is ignored",
    )
    execution = crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=audit_flow.id,
            status="RUNNING",
            trigger_event_details=audit["trigger_event_details"],
        ),
    )
    crud_flow_execution.update(
        db,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(
            **{
                "status": "SUCCEEDED",
                "result": audit["result"],
                "start_time": datetime.fromisoformat(audit["start_time"]),
                "end_time": datetime.fromisoformat(audit["start_time"]),
            }
        ),
    )
    CRUDBase(models.FlowExecution).update(
        db,
        db_obj=execution,
        obj_in={
            "start_time": datetime.fromisoformat(audit["start_time"]),
        },
    )
    flow = _flow(db, owner.account_id, report=True)
    crud_flow.update(
        db,
        db_obj=flow,
        flow_in=FlowUpdate(name="Weekly delivery report"),
        account_id=owner.account_id,
    )
    return Scenario(owner=owner, member=member, flow=flow, tracker=fake)


async def run_week(
    db: Session, scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the real orchestrator; substitute only forge/tracker/mail transports."""

    async def tracker_client(*args: Any, **kwargs: Any) -> WeekTracker:
        return scenario.tracker

    def capture_email(*args: Any, **kwargs: Any) -> bool:
        scenario.messages.append(args)
        return True

    monkeypatch.setattr("preloop.api.common.get_tracker_client", tracker_client)
    monkeypatch.setattr(actions, "get_tracker_client", tracker_client)
    monkeypatch.setattr("preloop.utils.email.send_email", capture_email)
    scenario.model_calls = AsyncMock(side_effect=AssertionError("Model call forbidden"))
    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_run_agent_with_retries", scenario.model_calls
    )
    nats = Mock(is_connected=True, publish=AsyncMock())
    orchestrator = FlowExecutionOrchestrator(
        db=db,
        flow_id=scenario.flow.id,
        trigger_event_data={"payload": {"window": WINDOW}},
        nats_client=nats,
    )
    assert dr.is_delivery_report_flow(scenario.flow), scenario.flow.agent_config
    plan = dr.plan_policy_run(db, flow=scenario.flow)
    assert not plan.calls_model, plan
    await orchestrator.run()
    scenario.execution = orchestrator.execution_log
    assert scenario.execution is not None
    assert scenario.execution.status == "SUCCEEDED", scenario.execution.error_message
    scenario.model_calls.assert_not_called()


@pytest.fixture
def week(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Scenario]:
    """One isolated week, with no outbound socket connections allowed."""
    attempts: list[Any] = []

    def deny_network(sock: socket.socket, address: Any) -> None:
        attempts.append(address)
        raise AssertionError(f"Unexpected network connection: {address}")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    scenario = seed_week(db_session, test_user)
    asyncio.run(run_week(db_session, scenario, monkeypatch))
    assert attempts == []
    yield scenario
    assert attempts == []


def test_deterministic_week_reaches_console_with_audit_and_risk(
    week: Scenario,
    client: TestClient,
) -> None:
    """Passing substrate: fetch, persisted audit, report, counts and HTTP reads."""
    assert set(week.tracker.reads) == {"101", "102", "103", "104"}
    assert week.tracker.calls == []  # default policy never writes during a run
    assert week.report["assessment"]["decision"] == "deterministic"
    assert week.report["summary"]["shipped"] == 4
    assert week.report["spend"]["total_tokens"] == 0
    detail = client.get(f"{BASE}/{week.execution_id}")
    assert detail.status_code == 200, detail.text
    report = detail.json()["report"]
    closed = next(i for i in report["closed_issues"] if i["ref"] == EXPORT)
    assert closed["closed_by"] == [f"{REPO}#102"]
    assert closed["purpose"]["source"] == "audit"
    assert closed["purpose"]["verdict"] == "partial"
    unmet = next(c for c in closed["purpose"]["criteria"] if c["text"] == UNMET)
    assert unmet["met"] is False and unmet["evidence"]
    needs = next(
        i
        for i in report["needs_you"]
        if i["ref"] == EXPORT and i["kind"] == "closed_unverified"
    )
    assert needs["kind"] == "closed_unverified" and needs["unmet_criteria"] == [UNMET]
    risk = next(i for i in report["shipped"] if i["ref"] == HOT_PATH)
    flag = next(
        f for f in risk["risk_flags"] if f["flag"] == dr.RISK_HOT_PATH_WITHOUT_TESTS
    )
    assert "backend/app/search.py" in " ".join(flag["evidence"])
    rows = client.get(BASE).json()["items"]
    assert len(rows) == 1 and rows[0]["id"] == str(week.execution_id)
    assert rows[0]["shipped"] == 4


def test_case_1_fixes_close_reopens_in_one_call(
    week: Scenario, client: TestClient
) -> None:
    assert week.report["reopen_requests"], json.dumps(week.report["closed_issues"])
    response = client.post(
        f"{BASE}/{week.execution_id}/actions", json={"kind": "reopen", "ref": EXPORT}
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"]["allowed_by"] == "click"
    assert week.tracker.issues[EXPORT]["status"] == "open"
    updates = [c for c in week.tracker.calls if c[0] == "update_issue"]
    comments = [c for c in week.tracker.calls if c[0] == "add_comment"]
    assert len(updates) == len(comments) == 1
    assert UNMET in comments[0][2]
    assert "2" * 40 in comments[0][2]
    repeat = client.post(
        f"{BASE}/{week.execution_id}/actions", json={"kind": "reopen", "ref": EXPORT}
    )
    assert repeat.status_code == 409
    assert len(week.tracker.calls) == 2


def test_case_2_hot_path_opens_labelled_follow_up(
    week: Scenario, client: TestClient
) -> None:
    proposals = week.report["follow_ups"]
    assert proposals, "deterministic hot-path risk has no follow-up proposal"
    index = next(
        n for n, proposal in enumerate(proposals) if proposal["ref"] == HOT_PATH
    )
    proposal = proposals[index]
    assert "backend/app/search.py" in " ".join(proposal["evidence"])
    response = client.post(
        f"{BASE}/{week.execution_id}/actions",
        json={"kind": "open_issue", "index": index},
    )
    assert response.status_code == 200, response.text
    assert len(week.tracker.created) == 1
    created = week.tracker.created[0]
    assert (
        response.json()["decision"]["issue_url"]
        == f"https://github.com/{REPO}/issues/99"
    )
    assert created.labels == ["preloop:proposed"]
    assert "backend/app/search.py" in created.description
    assert email.report_url(week.execution_id) in created.description
    assert response.json()["decision"]["allowed_by"] == "click"
    repeated = client.post(
        f"{BASE}/{week.execution_id}/actions",
        json={"kind": "open_issue", "index": index},
    )
    assert repeated.status_code == 409
    assert len(week.tracker.created) == 1


def test_owner_receives_one_report_link_once(
    week: Scenario, db_session: Session
) -> None:
    """OSS sends the account owner one report, plus the mandatory opt-out link."""
    assert len(week.messages) == 1
    recipient, subject, text, body = week.messages[0]
    assert recipient == week.owner.email and recipient != week.member.email
    link = email.report_url(week.execution_id)
    assert text.count(link) == 1
    assert body.count(f'href="{link}"') == 1
    assert "Stop these emails" in body
    assert UNMET in text and "hot_path_without_tests" in text
    # The owner digest is account-wide in OSS, including the UI-only change.
    assert "Report card styling" in text
    again = asyncio.run(
        email.distribute_report(
            db_session,
            flow=week.flow,
            execution=week.execution,
            report=week.report,
            policy=dr.DEFAULT_POLICY,
        )
    )
    assert again is None and len(week.messages) == 1


@pytest.mark.xfail(
    strict=True,
    raises=MissingProductionBehaviorError,
    reason=(
        "delivery_report_facts.details_from_forge and CRUD pull_request_facts drop reviews; "
        "delivery_report.py:assemble_report produces no per_person data. Member relevance "
        "and short updates are EE; OSS render_email is an account-wide owner digest."
    ),
)
def test_case_3_member_data_selects_only_reviewed_areas(week: Scenario) -> None:
    # Verify the supplied engagement is real fixture input, not authored report data.
    assert week.tracker.prs[103]["reviews"][0]["author"] == "jane-doe"
    assert week.tracker.prs[104]["reviews"][0]["author"] == "john-doe"
    people = week.report.get("per_person")
    if not people:
        raise MissingProductionBehaviorError(
            "No per-person relevance data survives deterministic report assembly"
        )
    person = next(p for p in people if p["user_id"] == str(week.owner.id))
    assert set(person["areas"]) == {"backend"}
    refs = {bullet["ref"] for bullet in person["bullets"]}
    assert HOT_PATH in refs and f"{REPO}#104" not in refs


@pytest.mark.parametrize("change", ["issue_edit", "new_commit"])
def test_generated_reopen_refuses_stale_evidence(
    week: Scenario, client: TestClient, db_session: Session, change: str
) -> None:
    """A real generated request uses the existing action freshness checks."""
    if change == "issue_edit":
        issue = crud_issue.get_by_external_url(
            db_session,
            external_url=week.tracker.issues[EXPORT]["url"],
            account_id=str(week.owner.account_id),
        )
        crud_issue.update(
            db_session,
            db_obj=issue,
            obj_in={
                "description": "## Acceptance\n- A different export contract\n",
            },
        )
    else:
        from preloop.models.crud import crud_issue_cost

        pull = crud_issue_cost.get_pull_request(
            db_session,
            account_id=week.owner.account_id,
            pr_key=week.tracker.prs[102]["url"],
        )
        crud_issue_cost.record_pull_request_identity(
            db_session, row=pull, head_sha="f" * 40
        )
        db_session.commit()
    detail = client.get(f"{BASE}/{week.execution_id}").json()
    assert EXPORT in detail["stale_reopen_requests"]
    response = client.post(
        f"{BASE}/{week.execution_id}/actions", json={"kind": "reopen", "ref": EXPORT}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "stale"
    assert week.tracker.calls == []


@pytest.mark.parametrize(
    "variant",
    [
        "covered",
        "unknown",
        "no_evidence",
        "wrong_revision",
        "no_revision",
        "different_closing_pr",
        "edited_issue",
        "changed_criteria",
        "no_head",
    ],
)
def test_reopen_proposals_require_a_current_cited_audit(variant: str) -> None:
    """Unsafe or unverifiable audit joins must not become actionable requests."""
    fake = WeekTracker()
    issue = fake.issues[EXPORT]
    pr = fake.prs[102]
    audit = {
        "issue_ref": EXPORT,
        "issue_url": issue["url"],
        "merge_sha": "2" * 40,
        "assessed_at": "2026-10-14T12:30:00+00:00",
        "merges": [{"url": pr["url"], "sha": "2" * 40}],
        "purpose": {
            "criteria": [
                {
                    "text": "CSV export includes headers",
                    "met": True,
                    "evidence": ["headers test"],
                },
                {"text": UNMET, "met": False, "evidence": ["date-filter test failed"]},
            ]
        },
    }
    # First prove this fixture qualifies; each variant removes one requirement.
    good = dr.assemble_report(
        window=WINDOW, pull_requests=[pr], issues=[issue], merge_audits=[audit]
    )
    assert len(good["reopen_requests"]) == 1
    criterion = audit["purpose"]["criteria"][1]
    if variant == "covered":
        criterion["met"] = True
    elif variant == "unknown":
        criterion["met"] = None
    elif variant == "no_evidence":
        criterion["evidence"] = []
    elif variant == "wrong_revision":
        audit["merge_sha"] = "f" * 40
    elif variant == "no_revision":
        pr.pop("merge_commit_sha")
        audit["merges"] = []
    elif variant == "different_closing_pr":
        pr.pop("merge_commit_sha")
        audit["merges"][0]["url"] = f"https://github.com/{REPO}/pull/999"
    elif variant == "edited_issue":
        issue["updated_at"] = "2026-10-14T13:00:00+00:00"
    elif variant == "changed_criteria":
        issue["description"] += "\n## Acceptance\n- A new requirement\n"
        # The triage section is authoritative; edit the actual criterion there.
        issue["description"] = issue["description"].replace(
            UNMET, "A new export criterion"
        )
    elif variant == "no_head":
        pr.pop("head_sha")
    report = dr.assemble_report(
        window=WINDOW, pull_requests=[pr], issues=[issue], merge_audits=[audit]
    )
    assert report["reopen_requests"] == []


def test_every_deterministic_risk_proposes_its_own_evidence() -> None:
    """Multiple flags stay distinct; model-supplied flags cannot add proposals."""
    pr = load_fixture("pull_requests.json")[2]
    flags = dr.detect_risk_flags(pr)
    assert {f["flag"] for f in flags} >= {
        dr.RISK_HOT_PATH_WITHOUT_TESTS,
        dr.RISK_MIGRATION_WITHOUT_DOWNGRADE,
    }
    report = dr.assemble_report(
        window=WINDOW,
        pull_requests=[pr],
        issues=[],
        assessments={
            HOT_PATH: {"risk_flags": [{"flag": "model_guess", "evidence": ["claim"]}]}
        },
    )
    assert {(p["ref"], p["risk_flag"]) for p in report["follow_ups"]} == {
        (HOT_PATH, f["flag"]) for f in flags
    }
    for proposal in report["follow_ups"]:
        expected = next(f for f in flags if f["flag"] == proposal["risk_flag"])
        assert proposal["evidence"] == expected["evidence"]
