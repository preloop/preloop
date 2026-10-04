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


# --- forge "PR opened" time -------------------------------------------------


def _pull(world: World, url: str = PR_URL) -> models.IssueCostPullRequest:
    pull = crud_issue_cost.get_pull_request(
        world.db, account_id=world.account.id, pr_key=url
    )
    assert pull is not None
    world.db.refresh(pull)
    return pull


def test_parse_forge_time_accepts_forge_formats_and_rejects_the_rest() -> None:
    now = T0 + timedelta(days=1)
    assert rollup_service.parse_forge_time("2026-09-01T08:00:00Z", now=now) == T0
    assert rollup_service.parse_forge_time("2026-09-01 08:00:00 UTC", now=now) == T0
    assert rollup_service.parse_forge_time("2026-09-01T10:00:00+02:00", now=now) == T0
    assert rollup_service.parse_forge_time(T0.replace(tzinfo=None), now=now) == T0
    assert rollup_service.parse_forge_time(None, now=now) is None
    assert rollup_service.parse_forge_time("yesterday", now=now) is None
    assert rollup_service.parse_forge_time("2026-09-09T08:00:00Z", now=now) is None


def test_forge_created_at_beats_the_bind_time(world: World) -> None:
    implementation = world.run(
        world.implement, world.issue_details(), start=T0, record=False
    )
    rollup_service.record_publication(
        world.db,
        implementation,
        PR_URL,
        now=T0 + timedelta(hours=3),
        forge_opened_at="2026-09-01T09:15:00Z",
    )
    world.db.commit()
    world.record(implementation)

    pull = _pull(world)
    assert pull.opened_at == T0 + timedelta(hours=1, minutes=15)
    assert pull.opened_at_source == rollup_service.OPENED_FORGE
    row = world.report().issues[0]
    assert row.pr_opened_at_source == "forge"
    assert row.first_event_to_pr_opened_hours == 1.25


def test_bind_time_is_used_and_labelled_when_the_forge_time_is_unknown(
    world: World,
) -> None:
    implementation = world.run(
        world.implement, world.issue_details(), start=T0, record=False
    )
    bound = T0 + timedelta(hours=2)
    rollup_service.record_publication(
        world.db,
        implementation,
        PR_URL,
        now=bound,
        forge_opened_at="2027-01-01T00:00:00Z",  # in the future: not a forge time
    )
    world.db.commit()
    pull = _pull(world)
    assert (pull.opened_at, pull.opened_at_source) == (bound, "bind")

    # A replayed bind never moves the time.
    rollup_service.record_publication(
        world.db, implementation, PR_URL, now=bound + timedelta(hours=1)
    )
    world.db.commit()
    assert _pull(world).opened_at == bound


def test_webhook_created_at_replaces_a_bind_time(world: World) -> None:
    _issue_lifecycle(world)
    assert _pull(world).opened_at_source == "bind"
    details = world.pr_details(event_type="pull_request_synchronize")
    details["payload"]["pull_request"]["created_at"] = "2026-09-01T09:30:00Z"
    world.webhook(details, now=T0 + timedelta(days=1))

    pull = _pull(world)
    assert pull.opened_at == T0 + timedelta(hours=1, minutes=30)
    assert pull.opened_at_source == "forge"
    row = world.rollup()
    assert row.pr_opened_at == T0 + timedelta(hours=1, minutes=30)
    assert row.pr_opened_at_source == "forge"


def test_pr_event_for_an_unknown_pr_creates_nothing(world: World) -> None:
    other = f"https://github.com/{REPO}/pull/99"
    details = world.pr_details(event_type="pull_request_opened", url=other)
    details["payload"]["pull_request"]["created_at"] = "2026-09-01T09:30:00Z"
    world.webhook(details, now=T0 + timedelta(days=1))
    assert (
        crud_issue_cost.get_pull_request(
            world.db, account_id=world.account.id, pr_key=other
        )
        is None
    )


def test_run_end_fallback_is_labelled(world: World) -> None:
    world.run(
        world.implement,
        world.issue_details(),
        start=T0,
        minutes=45,
        result={"pr_url": PR_URL},
    )
    assert _pull(world).opened_at_source == "run_end"
    assert world.rollup().pr_opened_at_source == "run_end"


def test_record_opened_pr_passes_the_forge_time(world: World) -> None:
    from preloop.services.flow_pr_binding import record_opened_pr

    implementation = world.run(
        world.implement,
        world.issue_details(),
        start=T0,
        status="RUNNING",
        record=False,
    )
    record_opened_pr(
        world.db, implementation.id, PR_URL, opened_at="2026-09-01T08:40:00Z"
    )
    pull = _pull(world)
    assert pull.opened_at == T0 + timedelta(minutes=40)
    assert pull.opened_at_source == "forge"


# --- terminal paths that bypass the orchestrator hook ---------------------------


def test_stale_execution_monitor_records_the_issue_fact(world: World) -> None:
    from preloop.services.execution_monitor import ExecutionMonitor

    execution = world.run(
        world.triage, world.issue_details(), start=T0, status="FAILED", record=False
    )
    assert crud_issue_cost.get_fact(world.db, execution_id=execution.id) is None

    ExecutionMonitor._record_issue_costs(world.db, [execution.id])

    assert world.fact(execution).status == "FAILED"
    assert world.rollup().failed_run_count == 1


class _KeepOpen:
    """The test session, with ``close`` disabled for code that closes it."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    def close(self) -> None:
        return None


def test_crashed_local_dispatch_records_the_issue_fact(world: World) -> None:
    from preloop.services.flow_trigger_service import _record_local_run_failure

    execution = world.run(
        world.triage, world.issue_details(), start=T0, status="RUNNING", record=False
    )
    execution.agent_session_reference = None
    world.db.commit()

    marked = _record_local_run_failure(
        lambda: _KeepOpen(world.db), execution.id, RuntimeError("boom")
    )

    assert marked is True
    assert world.fact(execution).status == "FAILED"
    assert world.rollup().run_count == 1


# --- estimates ----------------------------------------------------------------


def _configure_estimates(world: World, config: dict[str, Any]) -> None:
    world.tracker.meta_data = {"issue_estimate": config}
    world.db.commit()


def test_estimate_comes_from_the_trigger_payload_labels(world: World) -> None:
    _configure_estimates(
        world, {"hours_label_prefix": "estimate:", "points_label_prefix": "sp:"}
    )
    details = world.issue_details()
    details["payload"]["issue"]["labels"] = [
        {"name": "estimate:6h"},
        {"name": "sp:3"},
    ]
    world.run(world.triage, details, start=T0)

    row = world.rollup()
    assert (row.estimate_hours, row.estimate_hours_source) == (
        Decimal("6.00"),
        "label:estimate:",
    )
    assert (row.estimate_points, row.estimate_points_source) == (
        Decimal("3.00"),
        "label:sp:",
    )
    issue_row = world.report().issues[0]
    assert issue_row.estimate_hours == 6.0
    assert issue_row.estimate_points_source == "label:sp:"


def test_no_estimate_stays_empty(world: World) -> None:
    details = world.issue_details()
    details["payload"]["issue"]["labels"] = [{"name": "estimate:6h"}]
    world.run(world.triage, details, start=T0)  # no prefix configured

    row = world.rollup()
    assert row.estimate_hours is None and row.estimate_points is None
    issue_row = world.report().issues[0]
    assert issue_row.estimate_hours is None
    assert issue_row.estimate_hours_source is None


def test_estimate_is_not_cleared_by_a_later_payload_without_one(
    world: World,
) -> None:
    _configure_estimates(world, {"points_label_prefix": "sp:"})
    details = world.issue_details()
    details["payload"]["issue"]["labels"] = [{"name": "sp:5"}]
    world.run(world.triage, details, start=T0)
    world.run(world.review, world.issue_details(), start=T0 + timedelta(hours=1))
    assert world.rollup().estimate_points == Decimal("5.00")


def test_a_pr_payload_does_not_set_the_issue_estimate(world: World) -> None:
    _configure_estimates(world, {"points_label_prefix": "sp:"})
    world.run(world.triage, world.issue_details(), start=T0)
    details = world.pr_details(extra={})
    details["payload"]["pull_request"]["labels"] = [{"name": "sp:8"}]
    world.run(world.review, details, start=T0 + timedelta(hours=1))
    assert world.rollup().estimate_points is None


def test_synced_issue_row_estimate_wins_over_the_payload(world: World) -> None:
    _configure_estimates(world, {"points_label_prefix": "sp:"})
    world.issue.meta_data = {"labels": ["sp:8"], "estimate_fields": {}}
    world.db.commit()
    details = world.issue_details()
    details["payload"]["issue"]["labels"] = [{"name": "sp:5"}]
    world.run(world.triage, details, start=T0)

    row = world.rollup()
    assert row.estimate_points == Decimal("8.00")
    assert row.estimate_points_source == "label:sp:"


def test_jira_original_estimate_is_read_from_the_synced_issue(
    db_session: Session,
) -> None:
    world = World(db_session, name="jira-estimate")
    world.tracker.tracker_type = "jira"
    world.tracker.url = "https://jira.example.com"
    world.issue.key = "PROJ-12"
    world.issue.meta_data = {"estimate_fields": {"timeoriginalestimate": 14400}}
    db_session.commit()
    details = {
        "source": "jira",
        "tracker_id": str(world.tracker.id),
        "account_id": str(world.account.id),
        "type": "issue_updated",
        "project_id": str(world.project.id),
        "payload": {
            "issue": {
                "key": "PROJ-12",
                "self": "https://jira.example.com/rest/api/2/issue/1012",
                "fields": {"summary": "Add the export button"},
            }
        },
    }
    world.run(world.triage, details, start=T0)

    rows = world.rollups()
    assert len(rows) == 1
    assert rows[0].estimate_hours == Decimal("4.00")
    assert rows[0].estimate_hours_source == "jira:timeoriginalestimate"
    assert rows[0].estimate_points is None


def test_estimate_columns_in_csv_and_json(world: World) -> None:
    _configure_estimates(world, {"hours_label_prefix": "estimate:"})
    details = world.issue_details()
    details["payload"]["issue"]["labels"] = [{"name": "estimate:2.5h"}]
    world.run(world.triage, details, start=T0)
    world.run(
        world.audit, {"source": "schedule", "payload": {}}, start=T0, cost="0.0400"
    )
    report = world.report(include_execution_ids=True)

    rows = list(csv.DictReader(io.StringIO(rollup_service.report_to_csv(report))))
    assert list(rows[0].keys()) == list(rollup_service.CSV_COLUMNS)
    assert rows[0]["estimate_hours"] == "2.5"
    assert rows[0]["estimate_hours_source"] == "label:estimate:"
    assert rows[0]["estimate_points"] == ""
    assert rows[0]["pr_opened_at_source"] == ""
    assert rows[-1]["issue_key"] == rollup_service.UNASSIGNED_ISSUE_KEY
    assert rows[-1]["estimate_hours"] == ""

    document = json.loads(rollup_service.report_to_json(report))
    issue = document["issues"][0]
    assert issue["estimate_hours"] == 2.5
    assert issue["estimate_hours_source"] == "label:estimate:"
    assert issue["estimate_points"] is None
    assert issue["pr_opened_at_source"] is None


# --- scheduled rebuild --------------------------------------------------------


def test_scheduled_rebuild_records_what_the_hooks_missed(world: World) -> None:
    missed = world.run(world.triage, world.issue_details(), start=T0, record=False)
    world.run(world.review, world.issue_details(), start=T0 + timedelta(hours=1))
    old = world.run(
        world.audit,
        {"source": "schedule", "payload": {}},
        start=T0 - timedelta(days=10),
        record=False,
    )

    summary = rollup_service.scheduled_rebuild(
        world.db, lookback=timedelta(hours=72), now=T0 + timedelta(days=1)
    )

    assert summary.recorded >= 1 and summary.failed == 0
    assert world.fact(missed).rollup_id == world.rollup().id
    assert world.rollup().run_count == 2
    # Outside the lookback: left for the rebuild endpoint.
    assert crud_issue_cost.get_fact(world.db, execution_id=old.id) is None

    again = rollup_service.scheduled_rebuild(
        world.db, lookback=timedelta(hours=72), now=T0 + timedelta(days=1)
    )
    assert world.rollup().run_count == 2
    assert again.accounts_skipped == 0
    assert crud_issue_cost.get_fact(world.db, execution_id=missed.id) is not None


def test_scheduled_rebuild_skips_an_account_another_replica_holds(
    world: World, db_engine: Any
) -> None:
    from sqlalchemy import text

    missed = world.run(world.triage, world.issue_details(), start=T0, record=False)
    key = rollup_service.rebuild_lock_key(world.account.id)
    with db_engine.connect() as other:
        other.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"), {"key": key}
        )
        try:
            summary = rollup_service.scheduled_rebuild(
                world.db, lookback=timedelta(hours=72), now=T0 + timedelta(days=1)
            )
        finally:
            other.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": key},
            )

    assert summary.accounts_skipped >= 1
    assert crud_issue_cost.get_fact(world.db, execution_id=missed.id) is None


def test_scheduled_rebuild_refreshes_estimates_from_synced_issues(
    world: World,
) -> None:
    _configure_estimates(world, {"points_label_prefix": "sp:"})
    world.run(world.triage, world.issue_details(), start=T0)
    assert world.rollup().estimate_points is None

    # The tracker sync later stores the estimate label on the issue row.
    world.issue.meta_data = {"labels": ["sp:13"]}
    world.db.commit()
    summary = rollup_service.scheduled_rebuild(
        world.db, lookback=timedelta(hours=72), now=T0 + timedelta(days=1)
    )

    assert summary.estimates_changed >= 1
    assert world.rollup().estimate_points == Decimal("13.00")


def test_scheduled_rebuild_reads_each_tracker_config_once_per_pass(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_estimates(world, {"points_label_prefix": "sp:"})
    for number in (12, 13):
        world.run(world.triage, world.issue_details(number), start=T0, record=False)
    world.run(world.review, world.issue_details(12), start=T0, record=False)

    reads: list[uuid.UUID] = []
    original = crud_issue_cost.tracker_estimate_settings

    def counting(db: Any, *, tracker_id: uuid.UUID) -> Any:
        reads.append(tracker_id)
        return original(db, tracker_id=tracker_id)

    monkeypatch.setattr(crud_issue_cost, "tracker_estimate_settings", counting)
    summary = rollup_service.scheduled_rebuild(
        world.db, lookback=timedelta(hours=72), now=T0 + timedelta(days=1)
    )

    assert summary.recorded >= 3 and summary.estimates_checked >= 2
    # One tracker, one read for the whole pass.
    assert reads == [world.tracker.id]
    # The cache lives only for the pass.
    assert rollup_service._TRACKER_SETTINGS_CACHE not in world.db.info
    rollup_service.observe_estimate(world.db, rollup=world.rollup(12))
    assert len(reads) == 2


def test_scheduled_rebuild_counts_a_failed_account_apart_from_a_locked_one(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.run(world.triage, world.issue_details(), start=T0, record=False)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(rollup_service, "rebuild", boom)
    summary = rollup_service.scheduled_rebuild(
        world.db, lookback=timedelta(hours=72), now=T0 + timedelta(days=1)
    )

    assert summary.accounts_failed >= 1
    assert summary.accounts_skipped == 0
    assert summary.as_dict()["accounts_failed"] == summary.accounts_failed


def test_scheduled_rebuild_sweeper_pass_uses_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import issue_cost_rebuild_sweeper as sweeper_module

    seen: dict[str, Any] = {}

    class _Session:
        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(sweeper_module, "get_db_session", lambda: iter([_Session()]))
    monkeypatch.setattr(sweeper_module.settings, "issue_cost_rebuild_lookback_hours", 5)
    monkeypatch.setattr(
        sweeper_module.settings, "issue_cost_rebuild_max_executions_per_account", 7
    )

    def fake(db: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return rollup_service.ScheduledRebuildSummary()

    monkeypatch.setattr(sweeper_module.issue_cost_rollup, "scheduled_rebuild", fake)

    sweeper_module.run_scheduled_rebuild_once()

    assert seen == {
        "lookback": timedelta(hours=5),
        "per_account_limit": 7,
        "closed": True,
    }


# --- unassigned drill-down ----------------------------------------------------


def test_unassigned_executions_add_up_to_the_bucket(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0)
    first = world.run(
        world.audit, {"source": "schedule", "payload": {}}, start=T0, cost="0.0400"
    )
    second = world.run(
        world.audit,
        {"source": "schedule", "payload": {}},
        start=T0 + timedelta(hours=1),
        cost="0.0600",
    )
    report = world.report()
    rows = rollup_service.list_unassigned_executions(
        world.db, account_id=world.account.id
    )
    assert [row.execution_id for row in rows] == [first.id, second.id]
    assert sum(row.estimated_cost or 0 for row in rows) == pytest.approx(
        report.unassigned.estimated_cost
    )
    assert {row.link for row in rows} == {"unassigned"}
    assert (
        rollup_service.list_unassigned_executions(
            world.db, account_id=world.account.id, start=T0 + timedelta(minutes=30)
        )[0].execution_id
        == second.id
    )
    assert (
        rollup_service.list_unassigned_executions(
            world.db, account_id=world.account.id, flow_id=world.triage.id
        )
        == []
    )


@pytest.mark.asyncio
async def test_rebuild_sweeper_runs_passes_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from preloop.services import issue_cost_rebuild_sweeper as sweeper_module

    passes: list[int] = []
    monkeypatch.setattr(sweeper_module, "background_passes_allowed", lambda: True)
    monkeypatch.setattr(
        sweeper_module, "run_scheduled_rebuild_once", lambda: passes.append(1)
    )
    sweeper = sweeper_module.IssueCostRebuildSweeper(check_interval_seconds=0)

    await sweeper.start()
    for _ in range(50):
        if passes:
            break
        await asyncio.sleep(0.01)
    await sweeper.stop()

    assert passes and not sweeper.running


@pytest.mark.asyncio
async def test_rebuild_sweeper_does_not_start_on_a_role_without_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import issue_cost_rebuild_sweeper as sweeper_module

    monkeypatch.setattr(sweeper_module, "background_passes_allowed", lambda: False)
    sweeper = sweeper_module.IssueCostRebuildSweeper(check_interval_seconds=60)
    await sweeper.start()
    assert not sweeper.running


# --- cost coverage (#1057) ----------------------------------------------------


def test_cost_coverage_counts_a_known_zero_as_priced() -> None:
    assert rollup_service.cost_coverage(2, 0) == "complete"
    assert rollup_service.cost_coverage(1, 0) == "complete"
    assert rollup_service.cost_coverage(1, 1) == "partial"
    assert rollup_service.cost_coverage(0, 3) == "unknown"
    # An empty bucket is unknown: nothing about it is priced.
    assert rollup_service.cost_coverage(0, 0) == "unknown"


def test_attributed_cost_is_the_subtotal_only_when_complete() -> None:
    assert rollup_service.attributed_cost("complete", 2.0) == 2.0
    assert rollup_service.attributed_cost("complete", 0.0) == 0.0
    assert rollup_service.attributed_cost("partial", 2.0) is None
    assert rollup_service.attributed_cost("unknown", 0.0) is None


def test_unknown_only_costs_are_unknown_and_never_free(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, cost=None)
    world.run(
        world.review, world.issue_details(), start=T0 + timedelta(hours=1), cost=None
    )

    row = world.report().issues[0]

    # The legacy subtotal stays 0.0, which is exactly why coverage exists.
    assert row.estimated_cost == 0.0
    assert row.cost_coverage == "unknown"
    assert row.known_cost_run_count == 0
    assert row.unknown_cost_run_count == 2
    assert row.run_count == 2
    assert row.attributed_cost_usd is None


def test_known_only_costs_are_complete(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, cost="2.0000")

    row = world.report().issues[0]

    assert row.cost_coverage == "complete"
    assert row.known_cost_run_count == 1
    assert row.unknown_cost_run_count == 0
    assert row.estimated_cost == 2.0
    assert row.attributed_cost_usd == 2.0


def test_a_priced_zero_run_is_complete_not_unknown(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, cost="0.0000")

    row = world.report().issues[0]

    assert row.estimated_cost == 0.0
    assert row.cost_coverage == "complete"
    assert row.known_cost_run_count == 1
    assert row.unknown_cost_run_count == 0
    # A known zero is still an attributable total.
    assert row.attributed_cost_usd == 0.0


def test_mixed_costs_are_partial_with_no_attributed_total(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, cost="2.0000")
    world.run(
        world.review, world.issue_details(), start=T0 + timedelta(hours=1), cost=None
    )

    row = world.report().issues[0]

    assert row.estimated_cost == 2.0
    assert row.cost_coverage == "partial"
    assert (row.known_cost_run_count, row.unknown_cost_run_count) == (1, 1)
    assert row.attributed_cost_usd is None


def test_summaries_and_the_unassigned_bucket_share_the_definition(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, cost="2.0000")
    world.run(
        world.review, world.issue_details(), start=T0 + timedelta(hours=1), cost=None
    )
    world.run(world.audit, {"source": "schedule", "payload": {}}, start=T0, cost=None)
    world.run(
        world.audit,
        {"source": "schedule", "payload": {}},
        start=T0 + timedelta(hours=1),
        cost="0.0400",
    )

    report = world.report()

    project = report.by_project[0]
    assert project.cost_coverage == "partial"
    assert (project.known_cost_run_count, project.unknown_cost_run_count) == (1, 1)
    assert project.attributed_cost_usd is None
    flows = {item.id: item for item in report.by_flow}
    assert flows[world.triage.id].cost_coverage == "complete"
    assert flows[world.triage.id].attributed_cost_usd == 2.0
    assert flows[world.review.id].cost_coverage == "unknown"
    assert flows[world.review.id].attributed_cost_usd is None
    # Unassigned runs are not one of the report's flows; they land in the
    # bucket instead, which carries the same definitions.
    bucket = report.unassigned
    assert bucket.cost_coverage == "partial"
    assert (bucket.known_cost_run_count, bucket.unknown_cost_run_count) == (1, 1)
    assert bucket.attributed_cost_usd is None
    # Every run is counted once, in exactly one bucket.
    assert (
        project.run_count
        == sum(item.run_count for item in report.by_flow)
        == report.unassigned.run_count
        == 2
    )
    assert (
        project.known_cost_run_count + project.unknown_cost_run_count
        == project.run_count
    )
    assert (
        bucket.known_cost_run_count + bucket.unknown_cost_run_count == bucket.run_count
    )


def test_coverage_follows_the_flow_filter(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, cost="2.0000")
    world.run(
        world.review, world.issue_details(), start=T0 + timedelta(hours=1), cost=None
    )

    triage_only = world.report(flow_id=world.triage.id)
    assert triage_only.issues[0].cost_coverage == "complete"
    assert triage_only.issues[0].attributed_cost_usd == 2.0
    review_only = world.report(flow_id=world.review.id)
    assert review_only.issues[0].cost_coverage == "unknown"
    assert review_only.issues[0].estimated_cost == 0.0
    assert review_only.issues[0].attributed_cost_usd is None


def test_an_empty_bucket_is_unknown_with_zero_counts(world: World) -> None:
    bucket = world.report().unassigned

    assert bucket.run_count == 0
    assert bucket.cost_coverage == "unknown"
    assert bucket.known_cost_run_count == 0
    assert bucket.unknown_cost_run_count == 0
    assert bucket.attributed_cost_usd is None


def test_recording_the_same_fact_twice_does_not_move_the_counts(world: World) -> None:
    execution = world.run(world.triage, world.issue_details(), start=T0, cost="2.0000")
    before = world.report().issues[0]
    world.record(execution)

    after = world.report().issues[0]

    after_counts = (after.known_cost_run_count, after.unknown_cost_run_count)
    before_counts = (before.known_cost_run_count, before.unknown_cost_run_count)

    assert after_counts == before_counts == (1, 0)
    assert after.cost_coverage == "complete"
    assert after.run_count == 1


def test_a_null_to_known_correction_updates_coverage(world: World) -> None:
    execution = world.run(world.triage, world.issue_details(), start=T0, cost=None)
    assert world.report().issues[0].cost_coverage == "unknown"

    rollup_service.refresh_execution_cost(
        world.db, execution_id=execution.id, estimated_cost=Decimal("0.7500")
    )
    world.db.commit()

    row = world.report().issues[0]
    assert row.cost_coverage == "complete"
    assert (row.known_cost_run_count, row.unknown_cost_run_count) == (1, 0)
    assert row.attributed_cost_usd == 0.75


def test_coverage_stays_within_the_account(world: World, db_session: Session) -> None:
    stranger = World(db_session, name="stranger")
    stranger.run(stranger.triage, stranger.issue_details(), start=T0, cost=None)

    report = world.report()

    assert report.issues == []
    assert report.by_project == []
    assert report.unassigned.run_count == 0
    assert report.unassigned.cost_coverage == "unknown"
    assert report.unassigned.attributed_cost_usd is None


#: The CSV columns a consumer of the report already relies on; the coverage
#: columns are appended after them, never in place of one.
LEGACY_CSV_COLUMNS = rollup_service.CSV_COLUMNS[:-4]


def test_csv_and_json_agree_on_coverage_and_keep_legacy_columns(world: World) -> None:
    world.run(world.triage, world.issue_details(), start=T0, cost="2.0000")
    world.run(
        world.review, world.issue_details(), start=T0 + timedelta(hours=1), cost=None
    )
    world.run(world.audit, {"source": "schedule", "payload": {}}, start=T0, cost=None)
    export = world.report(include_execution_ids=True)

    reader = csv.DictReader(io.StringIO(rollup_service.report_to_csv(export)))
    header = tuple(reader.fieldnames or ())

    # The legacy columns keep their names, order and position; the coverage
    # columns are appended, so a consumer reading estimated_cost by name is
    # unaffected.
    assert header == rollup_service.CSV_COLUMNS
    assert header[: len(LEGACY_CSV_COLUMNS)] == LEGACY_CSV_COLUMNS
    rows = list(reader)
    by_key = {row["issue_key"]: row for row in rows}
    issue = by_key[f"{REPO}#12"]
    unassigned = by_key[rollup_service.UNASSIGNED_ISSUE_KEY]
    document = json.loads(rollup_service.report_to_json(export))
    json_by_key = {row["issue_key"]: row for row in document["issues"]}
    json_issue = json_by_key[f"{REPO}#12"]

    assert issue["cost_coverage"] == "partial"
    assert issue["known_cost_run_count"] == "1"
    assert issue["unknown_cost_run_count"] == "1"
    # A nullable attributed cost is an empty cell, never 0.
    assert issue["attributed_cost_usd"] == ""
    assert float(issue["estimated_cost"]) == 2.0
    assert json_issue["cost_coverage"] == "partial"
    assert json_issue["known_cost_run_count"] == 1
    assert json_issue["unknown_cost_run_count"] == 1
    assert json_issue["attributed_cost_usd"] is None
    assert unassigned["cost_coverage"] == "unknown"
    assert unassigned["attributed_cost_usd"] == ""
    assert document["unassigned"]["cost_coverage"] == "unknown"
    assert document["unassigned"]["known_cost_run_count"] == 0
    assert document["unassigned"]["unknown_cost_run_count"] == 1
    assert document["unassigned"]["attributed_cost_usd"] is None
    assert document["by_project"][0]["cost_coverage"] == "partial"
    assert document["by_project"][0]["attributed_cost_usd"] is None


def test_csv_writes_the_attributed_total_when_coverage_is_complete(
    world: World,
) -> None:
    world.run(world.triage, world.issue_details(), start=T0, cost="2.0000")
    export = world.report()

    row = next(csv.DictReader(io.StringIO(rollup_service.report_to_csv(export))))

    assert row["cost_coverage"] == "complete"
    assert float(row["attributed_cost_usd"]) == 2.0


def test_a_daily_import_is_never_charged_to_a_ticket(world: World) -> None:
    """An imported day of subscription spend stays out of the issue totals.

    The daily GitHub import is an account-level, per-seat view with no ticket
    attribution. Even when its login and day match an execution, it must not
    turn an unpriced run into a priced one.
    """
    from preloop.models.crud import crud_provider_billing_snapshot
    from preloop.models.crud.copilot_import import (
        COPILOT_PROVIDER,
        LINE_ITEM_PREMIUM_REQUEST,
    )
    from preloop.models.crud.provider_billing import IMPORTED_USAGE_SOURCE

    world.run(world.triage, world.issue_details(), start=T0, cost=None)
    day = T0.replace(hour=0, minute=0, second=0)
    crud_provider_billing_snapshot.upsert_snapshots(
        world.db,
        account_id=world.account.id,
        rows=[
            {
                "provider": COPILOT_PROVIDER,
                "granularity": "1d",
                "bucket_start": day,
                "bucket_end": day + timedelta(days=1),
                "line_item": LINE_ITEM_PREMIUM_REQUEST,
                "user_login": "jane-doe",
                "usage_source": IMPORTED_USAGE_SOURCE,
                "cost_amount": 42.0,
                "currency": "USD",
                "fetched_at": T0,
            }
        ],
    )
    world.db.commit()

    row = world.report().issues[0]

    assert row.estimated_cost == 0.0
    assert row.cost_coverage == "unknown"
    assert row.unknown_cost_run_count == 1
    assert row.attributed_cost_usd is None


# --- Jira and Bitbucket reconciliation (#1064) -------------------------------

from tests import issue_cost_reconciliation as rec  # noqa: E402


@pytest.fixture
def reconciliation(world: World) -> rec.Reconciliation:
    return rec.seed_reconciliation(world.db, world.account.id)


def _rec_row(world: World, **kwargs: Any):
    report = world.report(**kwargs)
    rows = [row for row in report.issues if row.issue_key == rec.JIRA_KEY]
    return report, rows


def test_bitbucket_pull_request_payload_is_a_pull_request_subject() -> None:
    subject = parse_trigger_subject(
        {
            "source": "bitbucket",
            "payload": {
                "repository": {"full_name": rec.WORKSPACE_REPO},
                "pullrequest": rec.bitbucket_pull_request(description="Fixes REC-7"),
            },
        }
    )
    assert subject is not None
    assert subject.kind == "pull_request"
    assert subject.url == rec.PR_URL
    assert subject.platform == "bitbucket"
    assert rollup_service.closing_issue_keys(subject) == ["REC-7"]


def test_reconciliation_milestones_and_intervals(
    world: World, reconciliation: rec.Reconciliation
) -> None:
    _, rows = _rec_row(world)
    assert len(rows) == 1
    row = rows[0]
    # The first event is the earliest attributed execution start (09:00),
    # never the Jira creation time (08:00).
    assert row.first_event_at == rec.IMPLEMENTATION_START
    assert row.first_event_at != rec.TICKET_CREATED
    assert row.pr_opened_at == rec.PR_OPENED
    assert row.pr_opened_at_source == "forge"
    assert row.approved_at == rec.APPROVED
    assert row.merged_at == rec.MERGED
    assert row.first_event_to_pr_opened_hours == 1.0
    assert row.pr_opened_to_approved_hours == 2.0
    assert row.approved_to_merged_hours == 1.0
    assert row.pr_url == rec.PR_URL


def test_reconciliation_costs_runs_and_coverage(
    world: World, reconciliation: rec.Reconciliation
) -> None:
    report, rows = _rec_row(world, include_execution_ids=True)
    row = rows[0]
    runs = reconciliation.executions
    # Four priced runs (1.00 + 0.25 + 0.50 + 0.10) and one seat-backed run.
    assert row.estimated_cost == 1.85
    assert row.run_count == 5
    assert row.failed_run_count == 1
    assert (row.known_cost_run_count, row.unknown_cost_run_count) == (4, 1)
    assert row.cost_coverage == "partial"
    assert row.attributed_cost_usd is None
    assert row.total_tokens == 4000 + 1500 + 800 + 300 + 600
    assert set(row.execution_ids or []) == reconciliation.issue_execution_ids
    assert len(row.execution_ids or []) == 5
    links = {
        name: world.fact(execution).link
        for name, execution in runs.items()
        if name != "unassigned"
    }
    assert links == {
        "failed": "trigger_issue",
        "retry": "retry",
        "review": "pull_request",
        "repair": "resume",
        "seat": "pull_request",
    }
    # Missing estimate stays null, a stated zero stays zero.
    assert row.estimate_hours is None
    assert row.estimate_points == 0.0
    assert row.estimate_points_source == f"jira:{rec.POINTS_FIELD}"

    project = next(p for p in report.by_project if p.id == reconciliation.project.id)
    assert project.estimated_cost == 1.85
    assert project.run_count == 5
    flows = {summary.id: summary for summary in report.by_flow}
    assert flows[reconciliation.implement.id].estimated_cost == 1.25
    assert flows[reconciliation.implement.id].run_count == 2
    assert flows[reconciliation.review.id].estimated_cost == 0.5
    assert flows[reconciliation.review.id].run_count == 2
    assert flows[reconciliation.review.id].cost_coverage == "partial"
    assert flows[reconciliation.repair.id].estimated_cost == 0.1
    assert round(sum(summary.estimated_cost for summary in flows.values()), 2) == 1.85
    assert sum(summary.run_count for summary in flows.values()) == 5
    assert report.unassigned.estimated_cost == 0.07
    assert report.unassigned.run_count == 1
    assert [item.execution_id for item in report.unassigned.executions] == [
        runs["unassigned"].id
    ]


def test_reconciliation_four_priced_runs_are_complete(
    world: World,
) -> None:
    rec.seed_reconciliation(world.db, world.account.id, seat_backed=False)
    row = _rec_row(world)[1][0]
    assert row.estimated_cost == 1.85
    assert row.run_count == 4
    assert row.cost_coverage == "complete"
    assert row.attributed_cost_usd == 1.85


def test_reconciliation_events_are_not_executions(
    world: World, reconciliation: rec.Reconciliation
) -> None:
    facts = world.db.scalars(
        select(models.IssueCostExecution).where(
            models.IssueCostExecution.account_id == world.account.id
        )
    ).all()
    # Five issue executions and one unassigned: approvals, merges and the
    # replayed completion added no facts.
    assert len(facts) == 6
    assert len({fact.execution_id for fact in facts}) == 6
    pulls = world.db.scalars(
        select(models.IssueCostPullRequest).where(
            models.IssueCostPullRequest.account_id == world.account.id
        )
    ).all()
    # A "created" delivery for a pull request nobody published or claimed
    # does not create a pull request row either.
    assert [pull.pr_key for pull in pulls] == [rec.PR_URL]


def test_reconciliation_period_selects_by_first_event(
    world: World, reconciliation: rec.Reconciliation
) -> None:
    # The merge (13:00) is inside this window but the first event is not.
    _, rows = _rec_row(world, start=rec.PR_OPENED, end=rec.MERGED + timedelta(hours=1))
    assert rows == []
    # Start is inclusive, end exclusive.
    assert len(_rec_row(world, start=rec.IMPLEMENTATION_START)[1]) == 1
    assert _rec_row(world, end=rec.IMPLEMENTATION_START)[1] == []
    # The ticket creation time does not select the issue either.
    assert (
        _rec_row(world, start=rec.TICKET_CREATED, end=rec.IMPLEMENTATION_START)[1] == []
    )


def test_reconciliation_flow_filter_uses_only_that_flows_facts(
    world: World, reconciliation: rec.Reconciliation
) -> None:
    report, rows = _rec_row(
        world, flow_id=reconciliation.review.id, include_execution_ids=True
    )
    row = rows[0]
    assert row.estimated_cost == 0.5
    assert row.run_count == 2
    assert row.cost_coverage == "partial"
    assert set(row.execution_ids or []) == {
        reconciliation.executions["review"].id,
        reconciliation.executions["seat"].id,
    }
    # Milestones stay those of the issue, not of the selected flow.
    assert row.first_event_at == rec.IMPLEMENTATION_START
    assert [summary.id for summary in report.by_flow] == [reconciliation.review.id]


def test_reconciliation_csv_and_json_agree(
    world: World, reconciliation: rec.Reconciliation
) -> None:
    report = world.report(include_execution_ids=True)
    csv_rows = {
        row["issue_key"]: row
        for row in csv.DictReader(io.StringIO(rollup_service.report_to_csv(report)))
    }
    document = json.loads(rollup_service.report_to_json(report))
    json_row = next(r for r in document["issues"] if r["issue_key"] == rec.JIRA_KEY)
    csv_row = csv_rows[rec.JIRA_KEY]

    for column in ("first_event_at", "pr_opened_at", "approved_at", "merged_at"):
        from_csv = datetime.fromisoformat(csv_row[column])
        from_json = datetime.fromisoformat(json_row[column].replace("Z", "+00:00"))
        assert from_csv == from_json
        assert from_csv.utcoffset() == timedelta(0)
    assert datetime.fromisoformat(csv_row["first_event_at"]) == rec.IMPLEMENTATION_START
    for column, expected in (
        ("first_event_to_pr_opened_hours", 1.0),
        ("pr_opened_to_approved_hours", 2.0),
        ("approved_to_merged_hours", 1.0),
        ("estimated_cost", 1.85),
        ("estimate_points", 0.0),
    ):
        assert float(csv_row[column]) == json_row[column] == expected
    for column in ("run_count", "known_cost_run_count", "unknown_cost_run_count"):
        assert int(csv_row[column]) == json_row[column]
    assert (json_row["run_count"], json_row["unknown_cost_run_count"]) == (5, 1)
    # Nullable values are null in JSON and empty in CSV, never zero.
    for column in ("attributed_cost_usd", "estimate_hours", "estimate_hours_source"):
        assert json_row[column] is None
        assert csv_row[column] == ""
    assert csv_row["pr_opened_at_source"] == json_row["pr_opened_at_source"] == "forge"
    assert csv_row["cost_coverage"] == json_row["cost_coverage"] == "partial"
    unassigned = csv_rows[rollup_service.UNASSIGNED_ISSUE_KEY]
    assert (
        float(unassigned["estimated_cost"]) == document["unassigned"]["estimated_cost"]
    )
    assert document["unassigned"]["execution_ids"] == [
        str(reconciliation.executions["unassigned"].id)
    ]


def test_reconciliation_is_scoped_to_its_account(
    world: World, reconciliation: rec.Reconciliation, db_session: Session
) -> None:
    other = World(db_session, name="other")
    assert other.report().issues == []
    assert other.report().unassigned.run_count == 0
    # Bitbucket events naming the account's pull request but sent for
    # another account change nothing on this account's row.
    foreign = rec.approval_event(reconciliation, rec.APPROVED - timedelta(hours=1))
    foreign["account_id"] = str(other.account.id)
    rollup_service.record_pull_request_event(
        db_session, foreign, now=rec.APPROVED - timedelta(hours=1)
    )
    db_session.commit()
    assert _rec_row(world)[1][0].approved_at == rec.APPROVED

