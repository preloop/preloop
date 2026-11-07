"""Cost per work item: attribution, the even split, grouping and drill-down."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints.cost import router
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.services.work_item_cost import build_report, pull_request_key

NOW = datetime.now(UTC).replace(microsecond=0)
REPO = "acme/api"


def test_pull_request_key_reads_github_and_gitlab_urls() -> None:
    assert pull_request_key("https://github.com/acme/api/pull/42") == "acme/api#42"
    assert (
        pull_request_key("https://gitlab.com/acme/web/-/merge_requests/7")
        == "acme/web!7"
    )
    assert pull_request_key("https://github.com/acme/api/issues/3") is None
    assert pull_request_key(None) is None


def _usage(
    db: Session,
    account_id: Any,
    *,
    cost: float,
    tokens: int,
    model: str,
    minutes_ago: float = 30,
    execution_id: Any = None,
    session_id: Any = None,
    user_id: Any = None,
    principal: tuple[str, str] | None = None,
) -> None:
    db.add(
        models.ApiUsage(
            account_id=account_id,
            user_id=user_id,
            endpoint="/v1/messages",
            method="POST",
            status_code=200,
            duration=1.0,
            action_type="model_gateway",
            flow_execution_id=execution_id,
            runtime_session_id=session_id,
            runtime_principal_type=principal[0] if principal else None,
            runtime_principal_id=principal[1] if principal else None,
            model_alias=model,
            prompt_tokens=tokens // 2,
            completion_tokens=tokens - tokens // 2,
            total_tokens=tokens,
            estimated_cost=cost,
            currency="USD",
            cost_source="catalog",
            timestamp=(NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None),
        )
    )


def _work_call(
    db: Session, session: models.RuntimeSession, work_ref: dict[str, Any]
) -> None:
    db.add(
        models.RuntimeSessionActivity(
            account_id=session.account_id,
            runtime_session_id=session.id,
            activity_type="tool_call",
            tool_name="create_pull_request",
            status="succeeded",
            metadata_={"work_ref": work_ref},
            timestamp=NOW - timedelta(minutes=20),
        )
    )


@pytest.fixture
def work(db_session: Session, test_user: Any) -> dict[str, Any]:
    """A flow run on issue #7 and its PR #42; a session opening PR #43 for #7
    and also updating issue #9; one unlinked session; one stale request."""
    from preloop.models.crud import crud_flow, crud_flow_execution
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import FlowExecutionCreate

    test_user.is_superuser = True
    account = test_user.account_id
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Implement issue",
            prompt_template="implement",
            trigger_event_source="github",
            trigger_event_types=["issue_opened"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account,
        ),
        account_id=account,
    )
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="SUCCEEDED", trigger_event_details={}
        ),
    )
    rollup = models.IssueCostRollup(
        account_id=account,
        tracker_id=_tracker(db_session, account),
        issue_key=f"{REPO}#7",
        title="Retry flaky deploys",
        issue_url=f"https://github.com/{REPO}/issues/7",
    )
    db_session.add(rollup)
    db_session.flush()
    db_session.add(
        models.IssueCostExecution(
            account_id=account,
            execution_id=execution.id,
            flow_id=flow.id,
            rollup_id=rollup.id,
            pr_key=f"https://github.com/{REPO}/pull/42",
            link="lifecycle",
            status="SUCCEEDED",
            start_time=NOW - timedelta(hours=1),
        )
    )
    db_session.add(
        models.IssueCostPullRequest(
            account_id=account,
            pr_key=f"https://github.com/{REPO}/pull/43",
            title="Retry deploys with backoff",
            state="merged",
        )
    )
    source = f"cc-{uuid4().hex[:8]}"
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=account,
        owner_user_id=test_user.id,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=source,
        display_name="Claude Code",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=NOW.replace(tzinfo=None),
        last_seen_at=NOW.replace(tzinfo=None),
        tags={},
    )
    db_session.add(agent)
    session = _runtime_session(db_session, account, source)
    idle = _runtime_session(db_session, account, f"idle-{uuid4().hex[:8]}")
    # Opened without a number yet, closing #7; then named as #43.
    _work_call(
        db_session,
        session,
        {
            "via": "builtin",
            "kind": "pull_request",
            "action": "opened",
            "ref": None,
            "repo": REPO,
            "closes": [f"{REPO}#7"],
        },
    )
    _work_call(
        db_session,
        session,
        {
            "via": "builtin",
            "kind": "pull_request",
            "action": "updated",
            "ref": "#43",
            "repo": REPO,
            "host": "github.com",
        },
    )
    _work_call(
        db_session,
        session,
        {
            "via": "cli",
            "kind": "issue",
            "action": "issue_updated",
            "ref": "#9",
            "repo": REPO,
            "host": "github.com",
        },
    )
    principal = ("claude_code", source)
    _usage(
        db_session,
        account,
        cost=1.0,
        tokens=1000,
        model="claude-sonnet-4-5",
        execution_id=execution.id,
        principal=("flow_execution", str(execution.id)),
    )
    _usage(
        db_session,
        account,
        cost=2.0,
        tokens=2000,
        model="gpt-5-codex",
        session_id=session.id,
        user_id=test_user.id,
        principal=principal,
    )
    # Outside the window, and a session that touched no work item.
    _usage(
        db_session,
        account,
        cost=50.0,
        tokens=9000,
        model="gpt-5-codex",
        session_id=session.id,
        minutes_ago=60 * 24 * 40,
        principal=principal,
    )
    _usage(
        db_session, account, cost=7.0, tokens=700, model="gpt-4.1", session_id=idle.id
    )
    db_session.flush()
    return {"user": test_user, "flow": flow, "agent": agent}


def _tracker(db: Session, account_id: Any) -> Any:
    tracker = models.Tracker(
        account_id=account_id,
        name=f"github-{uuid4().hex[:6]}",
        tracker_type="github",
        api_key="unused",
        is_active=True,
    )
    db.add(tracker)
    db.flush()
    return tracker.id


def _runtime_session(
    db: Session, account_id: Any, source: str
) -> models.RuntimeSession:
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=source,
        runtime_principal_type="claude_code",
        runtime_principal_id=source,
        started_at=(NOW - timedelta(hours=2)).replace(tzinfo=None),
    )
    db.add(session)
    db.flush()
    return session


def _report(db: Session, user: Any) -> Any:
    return build_report(
        db,
        account_id=user.account_id,
        user=user,
        start=NOW - timedelta(days=7),
        end=NOW,
    )


def test_groups_issue_with_its_pull_requests_and_splits_sessions(
    db_session: Session, work: dict[str, Any]
) -> None:
    report = _report(db_session, work["user"])
    by_key = {group.key: group for group in report.groups}
    assert set(by_key) == {f"{REPO}#7", f"{REPO}#9"}
    issue = by_key[f"{REPO}#7"]
    assert issue.kind == "issue"
    assert issue.title == "Retry flaky deploys"
    # The flow run ($1) on #7 and PR #42, plus half the session ($1) on #43.
    assert issue.estimated_cost == pytest.approx(2.0)
    assert issue.total_tokens == 2000
    assert issue.execution_count == 1
    assert issue.session_count == 1
    items = {(item.kind, item.key): item for item in issue.items}
    assert items[("issue", f"{REPO}#7")].estimated_cost == pytest.approx(0.5)
    assert items[("pull_request", f"{REPO}#42")].estimated_cost == pytest.approx(0.5)
    pr = items[("pull_request", f"{REPO}#43")]
    assert pr.estimated_cost == pytest.approx(1.0)
    assert pr.title == "Retry deploys with backoff"
    assert pr.state == "merged"
    assert by_key[f"{REPO}#9"].estimated_cost == pytest.approx(1.0)
    # Nothing counted twice; the stale request and the idle session are out.
    assert report.estimated_cost == pytest.approx(3.0)
    assert report.total_groups == 2
    assert report.groups[0].key == f"{REPO}#7"


def test_drill_down_names_agents_flows_models_and_users(
    db_session: Session, work: dict[str, Any]
) -> None:
    issue = next(
        g for g in _report(db_session, work["user"]).groups if g.key.endswith("#7")
    )
    agents = {s.label: s.estimated_cost for s in issue.agents}
    assert agents["Claude Code"] == pytest.approx(1.0)
    # A flow run's own principal is folded into one row, not one per run.
    assert agents["Flow runs"] == pytest.approx(1.0)
    assert {s.label for s in issue.flows} == {"Implement issue"}
    models_ = {s.label: s.estimated_cost for s in issue.models}
    assert models_ == {
        "claude-sonnet-4-5": pytest.approx(1.0),
        "gpt-5-codex": pytest.approx(1.0),
    }
    users = {s.key: s.estimated_cost for s in issue.users}
    assert users[str(work["user"].id)] == pytest.approx(1.0)
    assert users["none"] == pytest.approx(1.0)


def test_other_accounts_see_nothing(db_session: Session, work: dict[str, Any]) -> None:
    stranger = models.Account(organization_name=f"other-{uuid4().hex[:6]}")
    db_session.add(stranger)
    db_session.flush()
    report = build_report(
        db_session,
        account_id=stranger.id,
        user=work["user"],
        start=NOW - timedelta(days=7),
        end=NOW,
    )
    assert report.groups == [] and report.total_groups == 0


def test_endpoint_pages_groups(db_session: Session, work: dict[str, Any]) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: work["user"]
    client = TestClient(app)
    response = client.get("/api/v1/cost/work-items", params={"limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_groups"] == 2
    assert [g["key"] for g in body["groups"]] == [f"{REPO}#7"]
    assert body["estimated_cost"] == pytest.approx(3.0)
    bad = client.get(
        "/api/v1/cost/work-items",
        params={"start_date": NOW.isoformat(), "end_date": NOW.isoformat()},
    )
    assert bad.status_code == 422


def test_unnumbered_create_does_not_join_every_pull_request_it_touched(
    db_session: Session, work: dict[str, Any]
) -> None:
    """A contributor that opened a PR closing #20 and also commented on two
    named PRs cannot say which was created: no group is merged by guess."""
    account = work["user"].account_id
    session = _runtime_session(db_session, account, f"amb-{uuid4().hex[:8]}")
    _work_call(
        db_session,
        session,
        {
            "via": "builtin",
            "kind": "pull_request",
            "action": "opened",
            "ref": None,
            "repo": REPO,
            "closes": [f"{REPO}#20"],
        },
    )
    for number in ("#50", "#51"):
        _work_call(
            db_session,
            session,
            {
                "via": "builtin",
                "kind": "pull_request",
                "action": "commented",
                "ref": number,
                "repo": REPO,
                "host": "github.com",
            },
        )
    _usage(
        db_session,
        account,
        cost=4.0,
        tokens=400,
        model="gpt-4.1",
        session_id=session.id,
    )
    db_session.flush()
    groups = {g.key: g for g in _report(db_session, work["user"]).groups}
    assert f"{REPO}#20" not in groups
    assert groups[f"{REPO}#50"].estimated_cost == pytest.approx(2.0)
    assert groups[f"{REPO}#51"].estimated_cost == pytest.approx(2.0)
