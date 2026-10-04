"""Synthetic Jira and Bitbucket Cloud reconciliation fixture (#1064).

One Jira ticket, one Bitbucket Cloud pull request and the executions that
worked on them, recorded through the same attribution and CRUD paths the
webhook worker and the runner use. Nothing here assigns a rollup total: the
service derives every figure, so the tests can compare those figures with
the timeline below.

Timeline (UTC, 2026-09-01):

* 08:00 Jira ticket created (``fields.created``). Not measured by the
  report: ``first_event_at`` is the earliest attributed execution start.
* 09:00 implementation starts (fails, cost 1.00).
* 09:20 a distinct retry of it publishes the pull request (cost 0.25).
* 10:00 Bitbucket ``created_on`` of the pull request (forge time). Preloop
  bound the pull request at 10:05, so the forge time must win.
* 10:30 review triggered by ``pullrequest:created`` (cost 0.50).
* 11:00 repair turn resuming the review (cost 0.10).
* 11:30 seat-backed review of a comment, no per-run cost (null).
* 12:00 ``pullrequest:approved``, delivered twice and after the merge.
* 13:00 ``pullrequest:fulfilled``, delivered twice.

A sixth execution on an unrelated pull request without a closing reference
lands in the unassigned bucket (cost 0.07).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_flow,
    crud_flow_execution,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import issue_cost_rollup
from preloop.sync.event_normalizer import extract_filter_fields, normalize_event_type

DAY = datetime(2026, 9, 1, tzinfo=UTC)
TICKET_CREATED = DAY + timedelta(hours=8)
IMPLEMENTATION_START = DAY + timedelta(hours=9)
PR_OPENED = DAY + timedelta(hours=10)
APPROVED = DAY + timedelta(hours=12)
MERGED = DAY + timedelta(hours=13)

JIRA_KEY = "REC-7"
WORKSPACE_REPO = "acme-synthetic/widgets"
PR_URL = f"https://bitbucket.org/{WORKSPACE_REPO}/pull-requests/5"
OTHER_PR_URL = f"https://bitbucket.org/{WORKSPACE_REPO}/pull-requests/9"
POINTS_FIELD = "customfield_10016"


def _iso(value: datetime) -> str:
    return value.isoformat()


@dataclass
class Reconciliation:
    """Everything the fixture created, for assertions."""

    account_id: uuid.UUID
    jira_tracker: models.Tracker
    bitbucket_tracker: models.Tracker
    project: models.Project
    implement: models.Flow
    review: models.Flow
    repair: models.Flow
    executions: dict[str, models.FlowExecution] = field(default_factory=dict)

    @property
    def issue_execution_ids(self) -> set[uuid.UUID]:
        return {
            execution.id
            for name, execution in self.executions.items()
            if name != "unassigned"
        }


def _flow(db: Session, account_id: uuid.UUID, name: str, source: str) -> models.Flow:
    return crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"{name}-{uuid.uuid4().hex[:6]}",
            prompt_template="work",
            trigger_event_source=source,
            trigger_event_types=["issue_updated"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account_id,
        ),
        account_id=account_id,
    )


def jira_trigger(fixture: Reconciliation) -> dict[str, Any]:
    """Trigger details of a Jira ``jira:issue_updated`` delivery.

    The estimate states zero story points and no original estimate: the
    report must keep the zero and leave the hours empty.
    """
    return {
        "source": "jira",
        "tracker_id": str(fixture.jira_tracker.id),
        "account_id": str(fixture.account_id),
        "project_id": str(fixture.project.id),
        "type": "issue_updated",
        "payload": {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": JIRA_KEY,
                "fields": {
                    "summary": "Export the widget report",
                    "created": _iso(TICKET_CREATED),
                    "labels": [],
                    POINTS_FIELD: 0,
                },
            },
        },
    }


def bitbucket_pull_request(
    url: str = PR_URL,
    *,
    state: str = "OPEN",
    updated: Optional[datetime] = None,
    description: str = f"Implements {JIRA_KEY}.",
) -> dict[str, Any]:
    """A Bitbucket Cloud ``pullrequest`` object as webhooks deliver it."""
    number = int(url.rsplit("/", 1)[1])
    return {
        "id": number,
        "title": f"{JIRA_KEY}: export the widget report",
        "description": description,
        "state": state,
        "draft": False,
        "created_on": _iso(PR_OPENED),
        "updated_on": _iso(updated or PR_OPENED),
        "author": {"nickname": "preloop-bot"},
        "reviewers": [],
        "source": {"branch": {"name": f"feature/{JIRA_KEY}"}},
        "destination": {"branch": {"name": "main"}},
        "links": {"html": {"href": url}},
    }


def bitbucket_event(
    fixture: Reconciliation,
    event_key: str,
    payload_extra: Optional[dict[str, Any]] = None,
    *,
    pull: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Normalized event data, built the way ``process_webhook_event`` does."""
    payload: dict[str, Any] = {
        "actor": {"nickname": "reviewer"},
        "repository": {"full_name": WORKSPACE_REPO},
        "pullrequest": pull or bitbucket_pull_request(),
    }
    payload.update(payload_extra or {})
    tracker = fixture.bitbucket_tracker
    return {
        "source": tracker.tracker_type,
        "tracker_id": str(tracker.id),
        "type": normalize_event_type(tracker.tracker_type, event_key, payload),
        "payload": {
            **payload,
            **extract_filter_fields(tracker.tracker_type, event_key, payload),
        },
        "account_id": str(fixture.account_id),
    }


def approval_event(fixture: Reconciliation, at: datetime) -> dict[str, Any]:
    return bitbucket_event(
        fixture,
        "pullrequest:approved",
        {"approval": {"date": _iso(at), "user": {"nickname": "reviewer"}}},
        pull=bitbucket_pull_request(updated=at),
    )


def merge_event(fixture: Reconciliation, at: datetime) -> dict[str, Any]:
    return bitbucket_event(
        fixture,
        "pullrequest:fulfilled",
        pull=bitbucket_pull_request(state="MERGED", updated=at),
    )


def _run(
    db: Session,
    flow: models.Flow,
    details: dict[str, Any],
    *,
    start: datetime,
    minutes: int,
    tokens: int,
    cost: Optional[str],
    status: str = "SUCCEEDED",
    result: Optional[dict[str, Any]] = None,
    retry_of: Optional[models.FlowExecution] = None,
    record: bool = True,
) -> models.FlowExecution:
    execution = crud_flow_execution.create(
        db,
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
    if retry_of is not None:
        execution.retry_of_execution_id = retry_of.id
    db.commit()
    db.refresh(execution)
    if record:
        issue_cost_rollup.record_execution_finished(db, execution)
        db.commit()
    return execution


def _webhook(db: Session, event_data: dict[str, Any], *, now: datetime) -> None:
    issue_cost_rollup.record_pull_request_event(db, event_data, now=now)
    db.commit()


def seed_reconciliation(
    db: Session, account_id: uuid.UUID, *, seat_backed: bool = True
) -> Reconciliation:
    """Record the whole timeline for one account.

    Args:
        db: Database session.
        account_id: Account that owns the trackers and flows.
        seat_backed: Add the fifth, unpriced execution.

    Returns:
        The created objects.
    """
    suffix = uuid.uuid4().hex[:8]
    jira = crud_tracker.create(
        db,
        obj_in={
            "name": f"Jira {suffix}",
            "tracker_type": "jira",
            "account_id": account_id,
            "api_key": "synthetic",
            "url": "https://synthetic.atlassian.net",
            "is_active": True,
            "meta_data": {"issue_estimate": {"points_field": POINTS_FIELD}},
        },
    )
    bitbucket = crud_tracker.create(
        db,
        obj_in={
            "name": f"Bitbucket {suffix}",
            "tracker_type": "bitbucket",
            "account_id": account_id,
            "api_key": "synthetic",
            "url": "https://bitbucket.org",
            "is_active": True,
        },
    )
    organization = crud_organization.create(
        db,
        obj_in={
            "name": f"Org {suffix}",
            "identifier": f"org-{suffix}",
            "tracker_id": jira.id,
            "is_active": True,
        },
    )
    project = crud_project.create(
        db,
        obj_in={
            "name": f"Widgets {suffix}",
            "identifier": f"REC-{suffix}",
            "slug": "REC",
            "organization_id": organization.id,
            "is_active": True,
        },
    )
    fixture = Reconciliation(
        account_id=account_id,
        jira_tracker=jira,
        bitbucket_tracker=bitbucket,
        project=project,
        implement=_flow(db, account_id, "implement", "jira"),
        review=_flow(db, account_id, "review", "bitbucket"),
        repair=_flow(db, account_id, "repair", "bitbucket"),
    )
    db.commit()
    runs = fixture.executions

    # Implementation fails after spending real money.
    runs["failed"] = _run(
        db,
        fixture.implement,
        jira_trigger(fixture),
        start=IMPLEMENTATION_START,
        minutes=15,
        tokens=4000,
        cost="1.00",
        status="FAILED",
    )
    # A distinct retry publishes the pull request. The runner binds it at
    # 10:05 without the forge time; the forge says 10:00.
    retry = _run(
        db,
        fixture.implement,
        jira_trigger(fixture),
        start=IMPLEMENTATION_START + timedelta(minutes=20),
        minutes=50,
        tokens=1500,
        cost="0.25",
        result={"pr_url": PR_URL},
        retry_of=runs["failed"],
        record=False,
    )
    runs["retry"] = retry
    issue_cost_rollup.record_publication(
        db, retry, PR_URL, now=PR_OPENED + timedelta(minutes=5)
    )
    db.commit()
    issue_cost_rollup.record_execution_finished(db, retry)
    db.commit()

    # The review is triggered by the Bitbucket "created" delivery, which
    # also carries the forge created_on and replaces the bind time.
    opened = bitbucket_event(fixture, "pullrequest:created")
    _webhook(db, opened, now=PR_OPENED + timedelta(minutes=6))
    runs["review"] = _run(
        db,
        fixture.review,
        opened,
        start=PR_OPENED + timedelta(minutes=30),
        minutes=10,
        tokens=800,
        cost="0.50",
    )
    comment = bitbucket_event(
        fixture,
        "pullrequest:comment_created",
        {"comment": {"content": {"raw": "Please rename the column."}}},
    )
    runs["repair"] = _run(
        db,
        fixture.repair,
        {
            **comment,
            "_resume": {"execution_id": str(runs["review"].id), "pr_url": PR_URL},
        },
        start=PR_OPENED + timedelta(hours=1),
        minutes=10,
        tokens=300,
        cost="0.10",
    )
    if seat_backed:
        runs["seat"] = _run(
            db,
            fixture.review,
            comment,
            start=PR_OPENED + timedelta(hours=1, minutes=30),
            minutes=10,
            tokens=600,
            cost=None,
        )

    # Merge arrives before approval, both are redelivered, and a late
    # "updated" delivery repeats created_on: no milestone moves.
    _webhook(db, merge_event(fixture, MERGED), now=MERGED)
    _webhook(db, approval_event(fixture, APPROVED), now=MERGED + timedelta(minutes=1))
    _webhook(db, merge_event(fixture, MERGED), now=MERGED + timedelta(minutes=2))
    _webhook(db, approval_event(fixture, APPROVED), now=MERGED + timedelta(minutes=3))
    _webhook(
        db,
        bitbucket_event(
            fixture,
            "pullrequest:updated",
            pull=bitbucket_pull_request(updated=MERGED + timedelta(minutes=4)),
        ),
        now=MERGED + timedelta(minutes=4),
    )

    # Replay the completion of the retry: still one fact.
    issue_cost_rollup.record_execution_finished(db, retry)
    db.commit()

    # Work on an unrelated pull request with no closing reference.
    runs["unassigned"] = _run(
        db,
        fixture.review,
        bitbucket_event(
            fixture,
            "pullrequest:created",
            pull=bitbucket_pull_request(OTHER_PR_URL, description="Chore."),
        ),
        start=PR_OPENED,
        minutes=5,
        tokens=200,
        cost="0.07",
    )
    return fixture
