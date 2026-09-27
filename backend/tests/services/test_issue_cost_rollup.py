"""Per-tracker-issue cost and cycle-time rollup (#958)."""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_flow_execution,
    crud_issue,
    crud_issue_cost,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import issue_cost_rollup as rollup_service
from preloop.services.issue_cost_rollup import (
    canonical_issue_key,
    interval_hours,
    parse_event_time,
    parse_trigger_subject,
    published_pr_keys,
)

REPO = "example-org/example-repo"
PR_URL = f"https://github.com/{REPO}/pull/40"
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


class World:
    """One account with a GitHub tracker, a project, two issues and flows."""

    def __init__(self, db: Session, *, name: str = "rollup") -> None:
        suffix = uuid.uuid4().hex[:8]
        self.db = db
        self.account = crud_account.create(
            db,
            obj_in={
                "organization_name": f"{name}-{suffix}",
                "is_active": True,
                "meta_data": {},
            },
        )
        self.tracker = crud_tracker.create(
            db,
            obj_in={
                "name": f"GitHub {suffix}",
                "tracker_type": "github",
                "account_id": self.account.id,
                "api_key": "test_api_key",
                "url": "https://github.com",
                "is_active": True,
            },
        )
        organization = crud_organization.create(
            db,
            obj_in={
                "name": f"Org {suffix}",
                "identifier": f"org-{suffix}",
                "tracker_id": self.tracker.id,
                "is_active": True,
            },
        )
        self.project = crud_project.create(
            db,
            obj_in={
                "name": f"Project {suffix}",
                "identifier": f"project-{suffix}",
                "slug": REPO,
                "organization_id": organization.id,
                "is_active": True,
            },
        )
        self.issue = self.make_issue(12, "Add the export button")
        self.other_issue = self.make_issue(13, "Fix the chart legend")
        self.triage = self.make_flow("triage")
        self.implement = self.make_flow("implement")
        self.review = self.make_flow("review")
        self.audit = self.make_flow("audit")
        db.commit()

    def make_issue(self, number: int, title: str) -> models.Issue:
        return crud_issue.create(
            self.db,
            obj_in={
                "title": title,
                "description": "",
                "status": "open",
                "issue_type": "task",
                "project_id": self.project.id,
                "tracker_id": self.tracker.id,
                "key": f"{REPO}#{number}",
                "external_id": str(1000 + number),
                "external_url": f"https://github.com/{REPO}/issues/{number}",
            },
        )

    def make_flow(self, name: str) -> models.Flow:
        return crud_flow.create(
            db=self.db,
            flow_in=FlowCreate(
                name=f"{name}-{uuid.uuid4().hex[:6]}",
                prompt_template="work",
                trigger_event_source="github",
                trigger_event_types=["issue_labeled"],
                agent_type="openhands",
                agent_config={},
                allowed_mcp_servers=[],
                allowed_mcp_tools=[],
                is_enabled=True,
                account_id=self.account.id,
            ),
            account_id=self.account.id,
        )

    def issue_details(self, number: int = 12) -> dict[str, Any]:
        return {
            "source": "github",
            "tracker_id": str(self.tracker.id),
            "account_id": str(self.account.id),
            "type": "issue_labeled",
            "project_id": str(self.project.id),
            "payload": {
                "action": "labeled",
                "issue": {
                    "number": number,
                    "title": "Add the export button",
                    "html_url": f"https://github.com/{REPO}/issues/{number}",
                },
                "repository": {"full_name": REPO},
            },
        }

    def pr_details(
        self,
        *,
        event_type: str = "pull_request_opened",
        url: str = PR_URL,
        body: str = "",
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        pull = {
            "number": int(url.rsplit("/", 1)[1]),
            "title": "Export button",
            "body": body,
            "html_url": url,
            "head": {"ref": "feature/export"},
        }
        payload: dict[str, Any] = {
            "action": "opened",
            "pull_request": pull,
            "repository": {"full_name": REPO},
        }
        payload.update(extra or {})
        return {
            "source": "github",
            "tracker_id": str(self.tracker.id),
            "account_id": str(self.account.id),
            "type": event_type,
            "payload": payload,
        }

    def run(
        self,
        flow: models.Flow,
        details: dict[str, Any],
        *,
        start: datetime,
        minutes: int = 30,
        tokens: int = 1000,
        cost: Optional[str] = "0.5000",
        status: str = "SUCCEEDED",
        result: Optional[dict[str, Any]] = None,
        parent: Optional[models.FlowExecution] = None,
        record: bool = True,
    ) -> models.FlowExecution:
        execution = crud_flow_execution.create(
            self.db,
            obj_in=FlowExecutionCreate(
                flow_id=flow.id, status="RUNNING", trigger_event_details=details
            ),
        )
        execution.status = status
        execution.start_time = start.replace(tzinfo=None)
        execution.end_time = (start + timedelta(minutes=minutes)).replace(tzinfo=None)
        execution.total_tokens = tokens
        execution.estimated_cost = None if cost is None else Decimal(cost)
        execution.result = result
        if parent is not None:
            execution.parent_execution_id = parent.id
            execution.root_execution_id = parent.id
        self.db.commit()
        self.db.refresh(execution)
        if record:
            self.record(execution)
        return execution

    def record(self, execution: models.FlowExecution) -> None:
        rollup_service.record_execution_finished(self.db, execution)
        self.db.commit()

    def webhook(self, details: dict[str, Any], *, now: datetime) -> None:
        rollup_service.record_pull_request_event(self.db, details, now=now)
        self.db.commit()

    def rollups(self) -> list[models.IssueCostRollup]:
        return list(
            self.db.scalars(
                select(models.IssueCostRollup)
                .where(models.IssueCostRollup.account_id == self.account.id)
                .execution_options(populate_existing=True)
            )
        )

    def rollup(self, number: int = 12) -> models.IssueCostRollup:
        key = f"{REPO}#{number}"
        matches = [row for row in self.rollups() if row.issue_key == key]
        assert len(matches) == 1, [row.issue_key for row in self.rollups()]
        return matches[0]

    def fact(self, execution: models.FlowExecution) -> models.IssueCostExecution:
        fact = crud_issue_cost.get_fact(self.db, execution_id=execution.id)
        assert fact is not None
        self.db.refresh(fact)
        return fact

    def report(self, **kwargs: Any):
        return rollup_service.build_report(
            self.db, account_id=self.account.id, **kwargs
        )


@pytest.fixture
def world(db_session: Session) -> World:
    return World(db_session)


def _issue_lifecycle(world: World) -> tuple[Any, Any, Any]:
    """Triage, implementation (publishes the PR) and review on one issue."""
    triage = world.run(world.triage, world.issue_details(), start=T0, minutes=20)
    implementation = world.run(
        world.implement,
        world.issue_details(),
        start=T0 + timedelta(hours=1),
        minutes=90,
        tokens=5000,
        cost="2.2500",
        result={"pr_url": PR_URL},
        record=False,
    )
    rollup_service.record_publication(
        world.db, implementation, PR_URL, now=T0 + timedelta(hours=2)
    )
    world.db.commit()
    world.record(implementation)
    review = world.run(
        world.review,
        world.pr_details(),
        start=T0 + timedelta(hours=3),
        tokens=700,
        cost="0.1250",
    )
    return triage, implementation, review


# --- pure parsing -----------------------------------------------------------


def test_canonical_issue_key_folds_case() -> None:
    assert canonical_issue_key("Example-Org/Example-Repo#12") == f"{REPO}#12"
    assert canonical_issue_key("proj-7") == "PROJ-7"
    assert canonical_issue_key("repo#abc") is None
    assert canonical_issue_key("") is None


def test_parse_trigger_subject_github_issue_and_pr_comment() -> None:
    issue = parse_trigger_subject(
        {
            "source": "github",
            "payload": {"issue": {"number": 3}, "repository": {"full_name": REPO}},
        }
    )
    assert issue is not None and issue.kind == "issue"
    assert issue.issue_key == f"{REPO}#3"

    comment = parse_trigger_subject(
        {
            "source": "github",
            "payload": {
                "issue": {
                    "number": 40,
                    "html_url": f"https://github.com/{REPO}/issues/40",
                    "pull_request": {"html_url": PR_URL},
                },
                "repository": {"full_name": REPO},
            },
        }
    )
    assert comment is not None and comment.kind == "pull_request"
    assert comment.url == PR_URL


def test_parse_trigger_subject_gitlab_and_jira() -> None:
    note = parse_trigger_subject(
        {
            "source": "gitlab",
            "payload": {
                "object_kind": "note",
                "project": {"path_with_namespace": "group/sub/app"},
                "merge_request": {
                    "iid": 5,
                    "url": "https://gitlab.example.com/group/sub/app/-/merge_requests/5",
                    "description": "Closes #9",
                    "source_branch": "fix-9",
                },
            },
        }
    )
    assert note is not None and note.kind == "pull_request"
    assert rollup_service.closing_issue_keys(note) == ["group/sub/app#9"]

    gitlab_issue = parse_trigger_subject(
        {
            "source": "gitlab",
            "payload": {
                "object_kind": "issue",
                "project": {"path_with_namespace": "Group/App"},
                "object_attributes": {"iid": 4, "title": "Broken"},
            },
        }
    )
    assert gitlab_issue is not None and gitlab_issue.issue_key == "group/app#4"

    jira = parse_trigger_subject(
        {"source": "jira", "payload": {"issue": {"key": "ops-17", "fields": {}}}}
    )
    assert jira is not None and jira.issue_key == "OPS-17"


def test_gitlab_note_prefers_browser_url_over_api_url() -> None:
    web = "https://gitlab.example.com/group/app/-/merge_requests/5"
    note = parse_trigger_subject(
        {
            "source": "gitlab",
            "payload": {
                "object_kind": "note",
                "project": {"path_with_namespace": "group/app"},
                "merge_request": {
                    "iid": 5,
                    "url": "https://gitlab.example.com/api/v4/projects/3/merge_requests/5",
                    "web_url": web,
                },
                "issue": None,
            },
        }
    )
    assert note is not None and note.kind == "pull_request"
    assert note.url == web

    issue = parse_trigger_subject(
        {
            "source": "gitlab",
            "payload": {
                "object_kind": "note",
                "project": {"path_with_namespace": "group/app"},
                "issue": {
                    "iid": 4,
                    "url": "https://gitlab.example.com/api/v4/projects/3/issues/4",
                    "web_url": "https://gitlab.example.com/group/app/-/issues/4",
                },
            },
        }
    )
    assert issue is not None and issue.issue_key == "group/app#4"
    assert issue.url == "https://gitlab.example.com/group/app/-/issues/4"


def test_keys_longer_than_their_columns_are_rejected() -> None:
    assert canonical_issue_key("a/" + "b" * 600 + "#1") is None
    assert canonical_issue_key("X" * 513) is None
    assert canonical_issue_key("X" * 512) == "X" * 512
    long_mr = "https://gitlab.example.com/" + "g/" * 600 + "app/-/merge_requests/5"
    short_mr = "https://gitlab.example.com/group/app/-/merge_requests/5"
    run = type("Run", (), {"result": {"pr_url": long_mr}})()
    assert published_pr_keys(run) == []
    run.result = {"pr_url": short_mr}
    assert published_pr_keys(run) == [short_mr]


def test_closing_issue_keys_ignores_plain_mentions() -> None:
    subject = parse_trigger_subject(
        {
            "source": "github",
            "payload": {
                "pull_request": {
                    "number": 40,
                    "html_url": PR_URL,
                    "body": "Relates to #11. See #10.",
                },
                "repository": {"full_name": REPO},
            },
        }
    )
    assert subject is not None
    assert rollup_service.closing_issue_keys(subject) == []


def test_published_pr_keys_reads_binding_and_isolated_publication() -> None:
    class Execution:
        result = {
            "pr_url": PR_URL + "/",
            "trusted_publication": {
                "repositories": [
                    {"url": PR_URL},
                    {"url": "https://github.com/example-org/other/pull/2"},
                ]
            },
        }

    assert published_pr_keys(Execution()) == [
        PR_URL,
        "https://github.com/example-org/other/pull/2",
    ]


def test_parse_event_time_prefers_payload_but_not_future() -> None:
    now = T0 + timedelta(days=1)
    assert parse_event_time("2026-09-01T09:30:00Z", now=now) == T0 + timedelta(
        minutes=90
    )
    assert parse_event_time("2027-01-01T00:00:00Z", now=now) == now
    assert parse_event_time("not a time", now=now) == now
    assert parse_event_time(None, now=now) == now


def test_interval_hours_blank_not_zero() -> None:
    assert interval_hours(T0, None) is None
    assert interval_hours(None, T0) is None
    assert interval_hours(T0, T0 + timedelta(minutes=90)) == 1.5
    assert interval_hours(T0 + timedelta(hours=1), T0) is None


# --- acceptance ---------------------------------------------------------------


def test_three_executions_on_one_issue_make_one_row(world: World) -> None:
    triage, implementation, review = _issue_lifecycle(world)

    rows = world.rollups()
    assert len(rows) == 1
    row = rows[0]
    assert row.issue_key == f"{REPO}#12"
    assert row.issue_id == world.issue.id
    assert row.project_id == world.project.id
    assert row.run_count == 3
    assert row.failed_run_count == 0
    assert row.total_tokens == 1000 + 5000 + 700
    assert row.estimated_cost == Decimal("2.8750")
    assert row.pr_url == PR_URL
    assert world.fact(triage).link == "trigger_issue"
    assert world.fact(implementation).link == "trigger_issue"
    assert world.fact(review).link == "pull_request"


def test_pr_opened_uses_publication_time_not_triage_end(world: World) -> None:
    _issue_lifecycle(world)

    row = world.rollup()
    assert row.first_event_at == T0
    assert row.pr_opened_at == T0 + timedelta(hours=2)
    report = world.report()
    assert report.issues[0].first_event_to_pr_opened_hours == 2.0
    # Triage ended at T0 + 20 min; that must not be the "PR opened" time.
    assert row.pr_opened_at != T0 + timedelta(minutes=20)


def test_pr_opened_falls_back_to_publishing_run_end(world: World) -> None:
    world.run(
        world.implement,
        world.issue_details(),
        start=T0,
        minutes=45,
        result={"pr_url": PR_URL},
    )
    assert world.rollup().pr_opened_at == T0 + timedelta(minutes=45)


def test_approved_and_merged_use_webhook_times(world: World) -> None:
    _issue_lifecycle(world)
    approved = T0 + timedelta(hours=5)
    merged = T0 + timedelta(hours=6, minutes=30)
    world.webhook(
        world.pr_details(
            event_type="pull_request_review",
            extra={
                "review": {
                    "state": "approved",
                    "submitted_at": approved.isoformat().replace("+00:00", "Z"),
                }
            },
        ),
        now=T0 + timedelta(days=1),
    )
    merged_details = world.pr_details(event_type="pull_request_merged")
    merged_details["payload"]["pull_request"]["merged_at"] = merged.isoformat()
    world.webhook(merged_details, now=T0 + timedelta(days=1))

    row = world.rollup()
    assert row.approved_at == approved
    assert row.merged_at == merged
    issue_row = world.report().issues[0]
    assert issue_row.pr_opened_to_approved_hours == 3.0
    assert issue_row.approved_to_merged_hours == 1.5


def test_review_comment_without_approval_is_not_an_approval(world: World) -> None:
    _issue_lifecycle(world)
    world.webhook(
        world.pr_details(
            event_type="pull_request_review",
            extra={"review": {"state": "commented"}},
        ),
        now=T0 + timedelta(hours=4),
    )
    assert world.rollup().approved_at is None


def test_gitlab_approval_without_time_uses_arrival(db_session: Session) -> None:
    world = World(db_session, name="gitlab")
    world.tracker.tracker_type = "gitlab"
    db_session.commit()
    mr_url = "https://gitlab.example.com/group/app/-/merge_requests/3"
    details = {
        "source": "gitlab",
        "tracker_id": str(world.tracker.id),
        "account_id": str(world.account.id),
        "type": "issue_labeled",
        "payload": {
            "object_kind": "issue",
            "project": {"path_with_namespace": "group/app"},
            "object_attributes": {"iid": 8, "title": "Slow page"},
        },
    }
    world.run(world.implement, details, start=T0, result={"pr_url": mr_url})
    arrival = T0 + timedelta(hours=3)
    world.webhook(
        {
            "source": "gitlab",
            "tracker_id": str(world.tracker.id),
            "account_id": str(world.account.id),
            "type": "merge_request_approved",
            "payload": {
                "object_kind": "merge_request",
                "project": {"path_with_namespace": "group/app"},
                "object_attributes": {"iid": 3, "url": mr_url},
            },
        },
        now=arrival,
    )
    rows = world.rollups()
    assert len(rows) == 1
    assert rows[0].issue_key == "group/app#8"
    assert rows[0].approved_at == arrival


def test_replay_does_not_change_sums(world: World) -> None:
    executions = _issue_lifecycle(world)
    approval = world.pr_details(
        event_type="pull_request_review",
        extra={"review": {"state": "approved"}},
    )
    world.webhook(approval, now=T0 + timedelta(hours=5))
    before = world.rollup()
    snapshot = (
        before.run_count,
        before.total_tokens,
        before.estimated_cost,
        before.pr_opened_at,
        before.approved_at,
    )

    for execution in executions:
        world.record(execution)
    rollup_service.record_publication(
        world.db, executions[1], PR_URL, now=T0 + timedelta(hours=9)
    )
    world.webhook(approval, now=T0 + timedelta(hours=8))
    world.db.commit()

    after = world.rollup()
    assert (
        after.run_count,
        after.total_tokens,
        after.estimated_cost,
        after.pr_opened_at,
        after.approved_at,
    ) == snapshot
    facts = list(
        world.db.scalars(
            select(models.IssueCostExecution).where(
                models.IssueCostExecution.account_id == world.account.id
            )
        )
    )
    assert len(facts) == 3


def test_unlinked_execution_lands_in_unassigned_bucket(world: World) -> None:
    _issue_lifecycle(world)
    scheduled = world.run(
        world.audit,
        {"source": "schedule", "type": "schedule_tick", "payload": {}},
        start=T0 + timedelta(hours=2),
        tokens=300,
        cost="0.0300",
    )

    fact = world.fact(scheduled)
    assert fact.rollup_id is None
    assert fact.link == "unassigned"
    report = world.report()
    assert len(report.issues) == 1
    assert report.issues[0].run_count == 3
    assert report.unassigned.run_count == 1
    assert report.unassigned.estimated_cost == pytest.approx(0.03)
    assert report.unassigned.executions == []
    detailed = world.report(include_execution_ids=True)
    assert [row.execution_id for row in detailed.unassigned.executions] == [
        scheduled.id
    ]


def test_unassigned_totals_are_not_capped_by_the_row_limit(world: World) -> None:
    for offset in range(3):
        world.run(
            world.audit,
            {"source": "schedule", "type": "schedule_tick", "payload": {}},
            start=T0 + timedelta(minutes=offset),
            tokens=100,
            cost="0.0100",
            status="FAILED" if offset == 0 else "SUCCEEDED",
        )
    report = world.report(limit=1)
    assert report.unassigned.run_count == 3
    assert report.unassigned.failed_run_count == 1
    assert report.unassigned.total_tokens == 300
    assert report.unassigned.estimated_cost == pytest.approx(0.03)
    assert report.unassigned.executions == []
    detailed = world.report(limit=1, include_execution_ids=True)
    assert len(detailed.unassigned.executions) == 1
    assert detailed.truncated is True


def test_shared_pr_is_not_merged_into_either_issue(world: World) -> None:
    first = world.run(
        world.implement, world.issue_details(12), start=T0, result={"pr_url": PR_URL}
    )
    second = world.run(
        world.implement,
        world.issue_details(13),
        start=T0 + timedelta(hours=1),
        result={"pr_url": PR_URL},
    )
    review = world.run(world.review, world.pr_details(), start=T0 + timedelta(hours=2))

    one, two = world.rollup(12), world.rollup(13)
    assert one.run_count == 1 and two.run_count == 1
    assert one.pr_url is None and two.pr_url is None
    assert one.pr_opened_at is None and two.pr_opened_at is None
    assert world.fact(first).rollup_id == one.id
    assert world.fact(second).rollup_id == two.id
    review_fact = world.fact(review)
    assert review_fact.rollup_id is None
    assert review_fact.link == "ambiguous"
    assert review_fact.pr_key == PR_URL
    assert world.report().unassigned.run_count == 1


def test_shared_pr_detaches_review_recorded_before_the_conflict(
    world: World,
) -> None:
    world.run(
        world.implement, world.issue_details(12), start=T0, result={"pr_url": PR_URL}
    )
    review = world.run(world.review, world.pr_details(), start=T0 + timedelta(hours=1))
    assert world.fact(review).rollup_id == world.rollup(12).id

    world.run(
        world.implement,
        world.issue_details(13),
        start=T0 + timedelta(hours=2),
        result={"pr_url": PR_URL},
    )
    assert world.fact(review).rollup_id is None
    assert world.rollup(12).run_count == 1


def test_closing_reference_links_an_unclaimed_pr(world: World) -> None:
    review = world.run(
        world.review,
        world.pr_details(body="This fixes #12."),
        start=T0,
    )
    fact = world.fact(review)
    assert fact.link == "closing_reference"
    assert fact.rollup_id == world.rollup(12).id


def test_two_closing_references_are_ambiguous(world: World) -> None:
    review = world.run(
        world.review,
        world.pr_details(body="Fixes #12 and fixes #13"),
        start=T0,
    )
    fact = world.fact(review)
    assert fact.rollup_id is None
    assert fact.link == "ambiguous"
    assert world.rollups() == []


def test_repair_turn_and_delegated_child_inherit_the_issue(world: World) -> None:
    _, implementation, _ = _issue_lifecycle(world)
    repair = world.run(
        world.implement,
        {
            **world.pr_details(event_type="comment_created"),
            "_resume": {
                "execution_id": str(implementation.id),
                "pr_url": PR_URL,
            },
        },
        start=T0 + timedelta(hours=4),
    )
    child = world.run(
        world.review,
        {"source": "flow", "payload": {}},
        start=T0 + timedelta(hours=4, minutes=5),
        parent=implementation,
    )
    assert world.fact(repair).link == "resume"
    assert world.fact(child).link == "delegated"
    assert world.rollup().run_count == 5


def test_lifecycle_envelope_links_directly(world: World) -> None:
    audit = world.run(
        world.audit,
        {
            "source": "lifecycle",
            "payload": {"lifecycle": {"issue_id": str(world.issue.id)}},
        },
        start=T0,
    )
    fact = world.fact(audit)
    assert fact.link == "lifecycle"
    assert fact.rollup_id == world.rollup(12).id


def test_lifecycle_envelope_of_another_account_is_ignored(
    world: World, db_session: Session
) -> None:
    stranger = World(db_session, name="stranger")
    audit = world.run(
        world.audit,
        {"payload": {"lifecycle": {"issue_id": str(stranger.issue.id)}}},
        start=T0,
    )
    assert world.fact(audit).rollup_id is None
    assert stranger.rollups() == []


def test_failed_runs_count_and_unknown_cost_is_not_zero_filled(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, status="FAILED")
    world.run(
        world.triage,
        world.issue_details(),
        start=T0 + timedelta(hours=1),
        cost=None,
        status="STOPPED",
    )
    row = world.rollup()
    assert row.run_count == 2
    assert row.failed_run_count == 1
    assert row.estimated_cost == Decimal("0.5000")


def test_running_execution_is_not_recorded(world: World) -> None:
    execution = world.run(
        world.triage, world.issue_details(), start=T0, status="RUNNING"
    )
    assert crud_issue_cost.get_fact(world.db, execution_id=execution.id) is None


def test_repricing_refreshes_the_issue_row(world: World) -> None:
    triage, _, _ = _issue_lifecycle(world)
    rollup_service.refresh_execution_cost(
        world.db, execution_id=triage.id, estimated_cost=Decimal("1.5000")
    )
    world.db.commit()
    assert world.rollup().estimated_cost == Decimal("3.8750")


def test_recording_leaves_execution_cost_untouched(world: World) -> None:
    triage, implementation, _ = _issue_lifecycle(world)
    world.db.refresh(triage)
    world.db.refresh(implementation)
    assert triage.estimated_cost == Decimal("0.5000")
    assert implementation.estimated_cost == Decimal("2.2500")


def test_rebuild_records_history_once(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, record=False)
    world.run(
        world.review, world.pr_details(), start=T0 + timedelta(hours=1), record=False
    )
    examined, recorded, failed = rollup_service.rebuild(
        world.db,
        account_id=world.account.id,
        start=T0 - timedelta(days=1),
        end=T0 + timedelta(days=1),
    )
    world.db.commit()
    assert (examined, recorded, failed) == (2, 2, 0)
    assert rollup_service.rebuild(
        world.db,
        account_id=world.account.id,
        start=T0 - timedelta(days=1),
        end=T0 + timedelta(days=1),
    ) == (0, 0, 0)


def test_rebuild_skips_a_failing_execution_and_keeps_the_rest(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = world.run(world.triage, world.issue_details(), start=T0, record=False)
    healthy = world.run(
        world.review, world.pr_details(), start=T0 + timedelta(hours=1), record=False
    )
    real = rollup_service.record_execution_finished

    def flaky(db: Session, execution: models.FlowExecution) -> Any:
        if execution.id == broken.id:
            real(db, execution)
            raise RuntimeError("simulated insert failure")
        return real(db, execution)

    monkeypatch.setattr(rollup_service, "record_execution_finished", flaky)
    window = {"start": T0 - timedelta(days=1), "end": T0 + timedelta(days=1)}

    result = rollup_service.rebuild(world.db, account_id=world.account.id, **window)
    world.db.commit()

    assert result == (2, 1, 1)
    assert crud_issue_cost.get_fact(world.db, execution_id=broken.id) is None
    assert world.fact(healthy).execution_id == healthy.id

    monkeypatch.setattr(rollup_service, "record_execution_finished", real)
    assert rollup_service.rebuild(world.db, account_id=world.account.id, **window) == (
        1,
        1,
        0,
    )


def test_lineage_walk_resolves_each_ancestor_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick = {"source": "schedule", "type": "schedule_tick", "payload": {}}
    previous = world.run(world.audit, tick, start=T0, record=False)
    chain = [previous]
    for hop in range(1, 8):
        current = world.run(
            world.audit,
            {**tick, "_resume": {"execution_id": str(previous.id)}},
            start=T0 + timedelta(minutes=hop),
            parent=previous,
            record=False,
        )
        current.retry_of_execution_id = previous.id
        world.db.commit()
        chain.append(current)
        previous = current

    calls: list[Any] = []
    real = crud_issue_cost.get_execution

    def counting(db: Session, **kwargs: Any) -> Any:
        calls.append(kwargs["execution_id"])
        return real(db, **kwargs)

    monkeypatch.setattr(crud_issue_cost, "get_execution", counting)
    attribution = rollup_service.resolve_execution(
        world.db, account_id=world.account.id, execution=chain[-1]
    )

    assert attribution.target is None
    assert len(calls) == len(set(calls)) == len(chain) - 1


def test_webhook_hook_never_raises(world: World) -> None:
    rollup_service.record_pull_request_event_safely(
        world.db, {"type": "pull_request_merged", "account_id": "not-a-uuid"}
    )
    rollup_service.record_pull_request_event_safely(
        world.db,
        {
            "type": "pull_request_merged",
            "account_id": str(world.account.id),
            "payload": {"pull_request": "garbage"},
        },
    )


def test_record_opened_pr_stamps_publication(world: World) -> None:
    from preloop.services.flow_pr_binding import record_opened_pr

    implementation = world.run(
        world.implement,
        world.issue_details(),
        start=T0,
        status="RUNNING",
        record=False,
    )
    record_opened_pr(world.db, implementation.id, PR_URL)
    pull = crud_issue_cost.get_pull_request(
        world.db, account_id=world.account.id, pr_key=PR_URL
    )
    assert pull is not None and pull.opened_at is not None
    assert pull.rollup_id == world.rollup(12).id


# --- report and export ------------------------------------------------------------


def _mixed_world(world: World) -> None:
    _issue_lifecycle(world)
    world.run(
        world.triage,
        world.issue_details(13),
        start=T0 + timedelta(days=2),
        tokens=400,
        cost="0.2000",
    )
    world.run(
        world.review,
        world.issue_details(13),
        start=T0 + timedelta(days=2, hours=1),
        tokens=100,
        cost="0.0100",
    )


def test_project_and_flow_summaries_equal_the_sum_of_rows(world: World) -> None:
    _mixed_world(world)
    report = world.report()
    assert len(report.issues) == 2
    row_cost = sum(Decimal(str(row.estimated_cost)) for row in report.issues)
    row_tokens = sum(row.total_tokens for row in report.issues)
    row_runs = sum(row.run_count for row in report.issues)
    assert len(report.by_project) == 1
    project = report.by_project[0]
    assert project.name == world.project.name
    assert Decimal(str(project.estimated_cost)) == row_cost
    assert project.total_tokens == row_tokens
    assert project.run_count == row_runs
    assert project.issue_count == 2
    assert sum(Decimal(str(item.estimated_cost)) for item in report.by_flow) == (
        row_cost
    )
    assert sum(item.run_count for item in report.by_flow) == row_runs
    review = next(item for item in report.by_flow if item.id == world.review.id)
    assert review.issue_count == 2
    assert review.run_count == 2


def test_flow_filter_restricts_rows_to_that_flows_work(world: World) -> None:
    _mixed_world(world)
    report = world.report(flow_id=world.review.id)
    assert {row.issue_key for row in report.issues} == {f"{REPO}#12", f"{REPO}#13"}
    first = next(row for row in report.issues if row.issue_key.endswith("#12"))
    assert first.run_count == 1
    assert first.estimated_cost == pytest.approx(0.125)
    assert [item.id for item in report.by_flow] == [world.review.id]

    only_implement = world.report(flow_id=world.implement.id)
    assert [row.issue_key for row in only_implement.issues] == [f"{REPO}#12"]


def test_period_and_project_filters(world: World) -> None:
    _mixed_world(world)
    early = world.report(start=T0 - timedelta(hours=1), end=T0 + timedelta(days=1))
    assert [row.issue_key for row in early.issues] == [f"{REPO}#12"]
    assert early.issues[0].run_count == 3
    late = world.report(start=T0 + timedelta(days=1))
    assert [row.issue_key for row in late.issues] == [f"{REPO}#13"]
    none = world.report(project_id=uuid.uuid4())
    assert none.issues == [] and none.by_project == []


def test_report_is_scoped_to_the_account(world: World, db_session: Session) -> None:
    _mixed_world(world)
    stranger = World(db_session, name="stranger")
    assert stranger.report().issues == []
    assert (
        rollup_service.list_issue_executions(
            db_session,
            account_id=stranger.account.id,
            rollup_id=world.rollup().id,
        )
        is None
    )


def test_issue_executions_list_contributing_runs(world: World) -> None:
    triage, implementation, review = _issue_lifecycle(world)
    rows = rollup_service.list_issue_executions(
        world.db, account_id=world.account.id, rollup_id=world.rollup().id
    )
    assert rows is not None
    assert [row.execution_id for row in rows] == [
        triage.id,
        implementation.id,
        review.id,
    ]
    assert rows[1].flow_name == world.implement.name
    assert rows[1].estimated_cost == 2.25
    assert rows[1].start_time == T0 + timedelta(hours=1)


def test_csv_and_json_match_the_table(world: World) -> None:
    _mixed_world(world)
    world.run(
        world.audit, {"source": "schedule", "payload": {}}, start=T0, cost="0.0400"
    )
    table = world.report()
    exported = world.report(include_execution_ids=True)

    rows = list(csv.DictReader(io.StringIO(rollup_service.report_to_csv(exported))))
    assert [row["issue_key"] for row in rows] == [
        row.issue_key for row in table.issues
    ] + [rollup_service.UNASSIGNED_ISSUE_KEY]
    for csv_row, table_row in zip(rows, table.issues, strict=False):
        assert float(csv_row["estimated_cost"]) == table_row.estimated_cost
        assert int(csv_row["total_tokens"]) == table_row.total_tokens
        assert int(csv_row["run_count"]) == table_row.run_count
        assert csv_row["pr_url"] == (table_row.pr_url or "")
        expected_hours = table_row.first_event_to_pr_opened_hours
        assert csv_row["first_event_to_pr_opened_hours"] == (
            "" if expected_hours is None else str(expected_hours)
        )
    assert float(rows[-1]["estimated_cost"]) == table.unassigned.estimated_cost

    document = json.loads(rollup_service.report_to_json(exported))
    assert [row["issue_key"] for row in document["issues"]] == [
        row.issue_key for row in table.issues
    ]
    for json_row, table_row in zip(document["issues"], table.issues, strict=False):
        assert json_row["estimated_cost"] == table_row.estimated_cost
        assert json_row["run_count"] == table_row.run_count
        assert len(json_row["execution_ids"]) == table_row.run_count
    assert len(document["unassigned"]["execution_ids"]) == 1


def test_csv_neutralizes_formulas(world: World) -> None:
    world.issue.title = "=HYPERLINK(evil)"
    world.db.commit()
    world.run(world.triage, world.issue_details(), start=T0)
    text = rollup_service.report_to_csv(world.report())
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["title"] == "'=HYPERLINK(evil)"


def test_gateway_cost_sync_carries_into_the_issue_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.models.crud import crud_api_usage
    from preloop.services.execution_metrics import sync_execution_cost_rollup

    triage, _, _ = _issue_lifecycle(world)
    monkeypatch.setattr(
        crud_api_usage,
        "get_gateway_usage_for_execution",
        lambda db, execution_id: {"api_requests": 2, "estimated_cost": 1.5},
    )

    assert sync_execution_cost_rollup(world.db, str(triage.id)) is True
    world.db.commit()

    assert world.rollup().estimated_cost == Decimal("3.8750")
