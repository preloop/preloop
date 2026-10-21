"""Fleet Map code activity: attribution, the window, caps and the index."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints.fleet_map import router
from preloop.models import models
from preloop.models.crud.fleet_map import CODE_ACTIVITY_TOOLS
from preloop.models.db.session import get_db_session
from preloop.schemas.fleet_map import FleetCodeActivityResponse
from preloop.services import fleet_code_activity
from preloop.services.fleet_code_activity import (
    build_code_activity,
    code_action,
    short_pr,
)

NOW = datetime.now(UTC).replace(microsecond=0)


def test_code_action_classifies_tools() -> None:
    assert code_action("create_pull_request") == "opened"
    assert code_action("create_merge_request") == "opened"
    pr_comment = {"code_target": "pull_request"}
    assert code_action("add_comment", pr_comment) == "commented"
    # An issue or untargeted comment is not pull request activity.
    assert code_action("add_comment") is None
    assert code_action("add_comment", {"code_target": "issue"}) is None
    assert code_action("add_issue_comment") is None
    assert code_action("create_note") is None
    assert code_action("merge_pull_request") == "merged"
    assert code_action("create_pull_request_review") == "reviewed"
    review = {"arguments_summary": {"pull_request": 20, "review_action": 9}}
    assert code_action("update_pull_request", review) == "reviewed"
    # A title or label edit is not a review.
    assert (
        code_action("update_pull_request", {"arguments_summary": {"title": 4}}) is None
    )
    assert code_action("get_pull_request") is None
    assert code_action(None) is None


def test_every_classified_tool_is_in_the_indexed_list() -> None:
    classified = {
        name
        for name in CODE_ACTIVITY_TOOLS
        if code_action(
            name,
            {"arguments_summary": {"review_action": 1}, "code_target": "pull_request"},
        )
    }
    assert classified == set(CODE_ACTIVITY_TOOLS)


def test_short_pr_reads_github_gitlab_and_bitbucket() -> None:
    assert short_pr("https://github.com/acme/api/pull/42") == "acme/api#42"
    assert short_pr("https://gitlab.com/acme/web/-/merge_requests/7") == "acme/web!7"
    assert short_pr("https://bitbucket.org/acme/ops/pull-requests/3") == "acme/ops#3"
    assert short_pr("https://example.com/nothing") is None
    assert short_pr(None) is None


def _session(db: Session, account_id: Any, source_id: str) -> models.RuntimeSession:
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=source_id,
        runtime_principal_type="claude_code",
        runtime_principal_id=source_id,
        started_at=NOW.replace(tzinfo=None) - timedelta(days=1),
    )
    db.add(session)
    db.flush()
    return session


def _call(
    db: Session,
    session: models.RuntimeSession,
    tool: str,
    minutes_ago: float,
    *,
    status: str = "succeeded",
    execution_id: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        models.RuntimeSessionActivity(
            account_id=session.account_id,
            runtime_session_id=session.id,
            flow_execution_id=execution_id,
            activity_type="tool_call",
            tool_name=tool,
            status=status,
            metadata_=metadata or {},
            timestamp=NOW - timedelta(minutes=minutes_ago),
        )
    )


@pytest.fixture
def fleet(db_session: Session, test_user: Any) -> dict[str, Any]:
    """An agent owned by the test user, a flow with a run, and a merge."""
    from preloop.models.crud import crud_flow, crud_flow_execution
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import FlowExecutionCreate

    # A fresh test database has no seeded roles; a superuser reads the fleet.
    test_user.is_superuser = True
    account = test_user.account_id
    source = f"canvas-{uuid4().hex[:8]}"
    session = _session(db_session, account, source)
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
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Pull Request Reviewer",
            prompt_template="review",
            trigger_event_source="github",
            trigger_event_types=["pull_request_opened"],
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
            flow_id=flow.id, status="RUNNING", trigger_event_details={}
        ),
    )
    flow_session = _session(db_session, account, f"flow-{uuid4().hex[:8]}")
    pr_key = "https://github.com/acme/api/pull/42"
    db_session.add(
        models.IssueCostPullRequest(
            account_id=account,
            pr_key=pr_key,
            opened_at=NOW - timedelta(hours=2),
            merged_at=NOW - timedelta(minutes=3),
        )
    )
    db_session.add(
        models.IssueCostExecution(
            account_id=account,
            execution_id=execution.id,
            flow_id=flow.id,
            pr_key=pr_key,
            link="pull_request",
            status="SUCCEEDED",
            start_time=NOW - timedelta(hours=1),
        )
    )
    _call(db_session, session, "create_pull_request", 2)
    _call(
        db_session,
        session,
        "add_comment",
        4,
        metadata={"code_target": "pull_request"},
    )
    _call(db_session, session, "add_comment", 3)  # an issue comment
    _call(db_session, session, "create_pull_request", 300)  # outside 1h
    _call(db_session, session, "create_pull_request", 1, status="refused")
    _call(db_session, session, "get_pull_request", 1)
    _call(
        db_session,
        flow_session,
        "update_pull_request",
        5,
        execution_id=execution.id,
        metadata={"arguments_summary": {"review_action": 9, "review_body": 300}},
    )
    db_session.flush()
    return {
        "agent": agent,
        "flow": flow,
        "user": test_user,
        "session": session,
        "execution": execution,
    }


def test_counts_per_agent_flow_and_owner_in_the_window(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60
    )
    agent = f"agent:{fleet['agent'].id}"
    flow = f"flow:{fleet['flow'].id}"
    owner = f"user:{fleet['user'].id}"
    assert result.counts[agent].model_dump() == {
        "opened": 1,
        "commented": 1,
        "reviewed": 0,
        "merged": 0,
        "issues_opened": 0,
        "issues_updated": 0,
        "issues_commented": 0,
    }
    # The owner's counts are their agents' counts.
    assert result.counts[owner] == result.counts[agent]
    assert result.counts[flow].reviewed == 1
    assert result.counts[flow].merged == 1
    assert [item.id for item in result.flows] == [flow]
    assert result.flows[0].label == "Pull Request Reviewer"
    assert result.flows[0].href == f"/console/flows/{fleet['flow'].id}"
    actions = [(event.action, event.node_id) for event in result.events]
    assert actions[0] == ("opened", agent)
    merged = next(event for event in result.events if event.action == "merged")
    assert merged.pr == "acme/api#42"
    assert merged.url == "https://github.com/acme/api/pull/42"
    assert result.events == sorted(result.events, key=lambda e: e.at, reverse=True)
    assert result.truncated is False


def test_a_wider_window_sees_older_calls(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=24 * 60
    )
    assert result.counts[f"agent:{fleet['agent'].id}"].opened == 2
    # Any time is still a bounded 90 day read.
    unbounded = build_code_activity(db_session, user=fleet["user"])
    assert unbounded.since >= NOW - timedelta(days=90, minutes=1)


def test_other_accounts_see_nothing(db_session: Session, fleet: dict[str, Any]) -> None:
    other = models.User(
        id=uuid4(), account_id=uuid4(), is_superuser=True, username="other"
    )
    with patch.object(fleet_code_activity, "_allowed", return_value=True):
        result = build_code_activity(db_session, user=other, active_within_minutes=60)
    assert result.counts == {}
    assert result.events == []
    assert result.flows == []


def test_call_cap_marks_truncation(db_session: Session, fleet: dict[str, Any]) -> None:
    with patch.object(fleet_code_activity, "CALL_LIMIT", 1):
        result = build_code_activity(
            db_session, user=fleet["user"], active_within_minutes=60
        )
    assert result.truncated is True
    # Only the newest call was read: the 2-minute-old opened PR.
    assert result.counts[f"agent:{fleet['agent'].id}"].opened == 1
    assert result.counts[f"agent:{fleet['agent'].id}"].commented == 0


def test_without_fleet_permission_it_refuses(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    with (
        patch.object(fleet_code_activity, "_allowed", return_value=False),
        pytest.raises(HTTPException) as raised,
    ):
        build_code_activity(db_session, user=fleet["user"], active_within_minutes=60)
    assert raised.value.status_code == 403


def test_the_partial_index_covers_the_read(db_session: Session) -> None:
    """The migrated index carries exactly the read's predicate and key."""
    definition = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_runtime_session_activity_code_tools'"
        )
    ).scalar_one()
    assert '(account_id, "timestamp" DESC)' in definition
    assert "activity_type)::text = 'tool_call'" in definition
    assert "status)::text = 'succeeded'" in definition
    for name in CODE_ACTIVITY_TOOLS:
        assert f"'{name}'" in definition


def test_route_validates_the_window() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    user = models.User(id=uuid4(), account_id=uuid4(), is_superuser=True)
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    with patch(
        "preloop.api.endpoints.fleet_map.build_code_activity",
        return_value=FleetCodeActivityResponse(since=NOW),
    ) as build:
        with TestClient(app) as client:
            assert (
                client.get(
                    "/api/v1/fleet/code-activity?active_within_minutes=0"
                ).status_code
                == 422
            )
            response = client.get("/api/v1/fleet/code-activity?active_within_minutes=5")
            assert response.status_code == 200
            assert response.json()["counts"] == {}
            assert build.call_args.kwargs["active_within_minutes"] == 5


def test_a_code_tool_call_is_broadcast_on_code_activity() -> None:
    with patch("preloop.services.account_realtime.emit_account_event") as emit:
        fleet_code_activity.emit_code_tool_event(
            account_id="acct",
            tool_name="create_pull_request",
            metadata={},
            managed_agent_id="agent-1",
            execution_id=None,
            runtime_session_id="sess-1",
            timestamp="2026-10-18T10:00:00+00:00",
        )
        fleet_code_activity.emit_code_tool_event(
            account_id="acct", tool_name="get_pull_request", metadata={}
        )
    assert emit.call_count == 1
    event = emit.call_args.args[0]
    assert event["topic"] == "code_activity"
    assert event["type"] == "code_activity"
    assert event["payload"]["action"] == "opened"
    assert event["payload"]["managed_agent_id"] == "agent-1"
    assert event["runtime_session_id"] == "sess-1"


def test_a_merge_is_broadcast_to_its_flows(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    with patch("preloop.services.account_realtime.emit_account_event") as emit:
        fleet_code_activity.emit_merge_event(
            db_session,
            account_id=fleet["user"].account_id,
            pr_key="https://github.com/acme/api/pull/42",
            at=NOW,
        )
    payload = emit.call_args.args[0]["payload"]
    assert payload["action"] == "merged"
    assert payload["flow_ids"] == [str(fleet["flow"].id)]
    assert payload["pr"] == "acme/api#42"


def test_code_activity_is_a_subscribable_topic() -> None:
    from preloop.services.websocket_manager import WebSocketManager

    assert WebSocketManager.normalize_topic("code_activity") == "code_activity"


def test_a_flow_is_linked_to_its_model_node(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from types import SimpleNamespace

    from preloop.models.crud import fleet_map as fleet_crud

    model_id = uuid4()
    real = fleet_crud.flows_for_executions

    def with_model(*args: Any, **kwargs: Any) -> list[Any]:
        return [(e, f, n, model_id) for e, f, n, _ in real(*args, **kwargs)]

    model = SimpleNamespace(
        id=model_id, model_identifier="anthropic/claude-sonnet-4-5", name="x"
    )
    with (
        patch.object(fleet_crud, "flows_for_executions", side_effect=with_model),
        patch.object(fleet_crud, "models_by_id", return_value=[model]),
    ):
        result = build_code_activity(
            db_session, user=fleet["user"], active_within_minutes=60
        )
    assert result.flows[0].model_node_id == "model:claude-sonnet-4-5"


def test_pull_request_target_reads_only_what_identifies_a_pr() -> None:
    from preloop.services.fleet_code_activity import pull_request_target

    assert pull_request_target({"target": "https://github.com/a/b/pull/3"})
    assert pull_request_target({"target": "https://gitlab.com/a/b/-/merge_requests/9"})
    assert pull_request_target({"target": "group/project!12"})
    assert pull_request_target({"target": "a/b#3", "path": "x.py", "line": 4})
    assert not pull_request_target({"target": "https://github.com/a/b/issues/3"})
    assert not pull_request_target({"target": "PROJ-12"})
    assert not pull_request_target(None)


def test_a_flow_merge_is_counted_once_with_the_forge_webhook(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    execution_id = (
        db_session.execute(
            text(
                "SELECT execution_id FROM issue_cost_execution "
                "WHERE flow_id = :f LIMIT 1"
            ),
            {"f": fleet["flow"].id},
        )
    ).scalar_one()
    session = _session(db_session, fleet["user"].account_id, f"m-{uuid4().hex[:6]}")
    _call(db_session, session, "merge_pull_request", 3, execution_id=execution_id)
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60
    )
    assert result.counts[f"flow:{fleet['flow'].id}"].merged == 1
    merged = [e for e in result.events if e.action == "merged"]
    assert len(merged) == 1
    assert merged[0].pr == "acme/api#42"


def test_a_flow_tool_merge_is_not_announced_twice() -> None:
    with patch("preloop.services.account_realtime.emit_account_event") as emit:
        fleet_code_activity.emit_code_tool_event(
            account_id="acct",
            tool_name="merge_pull_request",
            metadata={},
            flow_id="flow-1",
        )
        fleet_code_activity.emit_code_tool_event(
            account_id="acct",
            tool_name="merge_pull_request",
            metadata={},
            managed_agent_id="agent-1",
        )
    assert emit.call_count == 1
    assert emit.call_args.args[0]["payload"]["managed_agent_id"] == "agent-1"


def test_the_pr_key_index_covers_the_merge_join(db_session: Session) -> None:
    definition = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_issue_cost_execution_account_pr_key'"
        )
    ).scalar_one()
    assert "(account_id, pr_key)" in definition
    assert "pr_key IS NOT NULL" in definition


def test_work_lists_a_stored_pr_without_inventing_title_state_or_actor(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    db_session.add(
        models.IssueCostPullRequest(
            account_id=fleet["user"].account_id,
            pr_key="https://github.com/example/widgets/pull/7",
            opened_at=NOW - timedelta(minutes=10),
        )
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    item = next(item for item in result.work if item.key == "example/widgets#7")
    assert item.kind == "pull_request"
    assert item.reference == "#7"
    assert item.repo == "example/widgets"
    assert item.title is None
    assert item.state is None
    assert item.node_ids == []
    assert item.review_state is None
    assert item.last_at == NOW - timedelta(minutes=10)
    assert item.model_dump(mode="json")["last_at"].endswith("Z")
    assert result.work_truncated is False


def _work_issue(
    db: Session,
    user: models.User,
    number: int = 7,
    *,
    minutes: float = 10,
    state: str = "open",
    labels: list[str] | None = None,
) -> models.Issue:
    """Create a synthetic tracker snapshot with explicit UTC base timestamps."""
    tracker = models.Tracker(
        account_id=user.account_id,
        name="Example tracker",
        tracker_type="github",
        url="https://github.com",
        api_key="example",
    )
    db.add(tracker)
    db.flush()
    org = models.Organization(
        name="Example", identifier="example", tracker_id=tracker.id
    )
    db.add(org)
    db.flush()
    project = models.Project(
        name="Widgets", identifier="widgets", organization_id=org.id
    )
    db.add(project)
    db.flush()
    stamp = (NOW - timedelta(minutes=minutes)).replace(tzinfo=None)
    issue = models.Issue(
        project_id=project.id,
        tracker_id=tracker.id,
        title="Stored issue title",
        key=f"Example/Widgets#{number}",
        external_id=str(number),
        external_url=f"https://github.com/example/widgets/issues/{number}",
        status=state,
        meta_data={"labels": labels or []},
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(issue)
    db.flush()
    return issue


def test_work_distinguishes_kinds_dedupes_tools_and_bounds_metadata(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    _work_issue(db_session, fleet["user"], labels=["x" * 50] * 8)
    session = _session(db_session, fleet["user"].account_id, "work-tools")
    for tool, target in (
        (
            "update_issue",
            "https://name:password@github.com/example/widgets/issues/7?token=secret#fragment",
        ),
        ("create_pull_request", "https://github.com/example/widgets/pull/7"),
        ("create_pull_request_review", "Example/Widgets#7"),
    ):
        _call(
            db_session,
            session,
            tool,
            2,
            metadata={
                "arguments_summary": {
                    "target": target,
                    "title": "Untrusted title",
                    "state": "closed",
                },
            },
        )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    rows = [item for item in result.work if item.key == "example/widgets#7"]
    assert {item.kind for item in rows} == {"issue", "pull_request"}
    assert len(rows) == 2
    issue = next(item for item in rows if item.kind == "issue")
    assert issue.title == "Stored issue title"
    assert issue.state == "open"
    assert issue.url == "https://github.com/example/widgets/issues/7"
    assert issue.labels == ["x" * 40] * 6
    assert issue.last_action == "issue_updated"
    assert issue.last_at == NOW - timedelta(minutes=2)
    pr = next(item for item in rows if item.kind == "pull_request")
    assert pr.title is None
    assert pr.state is None
    assert pr.review_state is None


def test_work_window_includes_boundary_and_excludes_future_and_old_rows(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    account = fleet["user"].account_id
    for number, minutes in ((10, 60), (11, 60.01), (12, -1), (13, 90 * 24 * 60 + 1)):
        db_session.add(
            models.IssueCostPullRequest(
                account_id=account,
                pr_key=f"https://github.com/example/widgets/pull/{number}",
                opened_at=NOW - timedelta(minutes=minutes),
            )
        )
    _work_issue(db_session, fleet["user"], 10, minutes=60)
    _work_issue(db_session, fleet["user"], 11, minutes=60.01)
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    keys = {(item.kind, item.key) for item in result.work}
    assert ("issue", "example/widgets#10") in keys
    assert ("pull_request", "example/widgets#10") in keys
    assert not any(key.endswith(("#11", "#12", "#13")) for _, key in keys)
    any_time = build_code_activity(db_session, user=fleet["user"], now=NOW)
    assert any_time.since == NOW - timedelta(days=90)
    assert not any(item.key.endswith(("#12", "#13")) for item in any_time.work)


def test_work_cap_is_distinct_and_newest_first(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from preloop.services import fleet_work

    for number in range(10, 15):
        db_session.add(
            models.IssueCostPullRequest(
                account_id=fleet["user"].account_id,
                pr_key=f"https://github.com/example/widgets/pull/{number}",
                opened_at=NOW - timedelta(seconds=number),
            )
        )
    db_session.flush()
    with patch.object(fleet_work, "WORK_LIMIT", 3):
        result = build_code_activity(
            db_session, user=fleet["user"], active_within_minutes=60, now=NOW
        )
    assert [item.key for item in result.work] == [
        f"example/widgets#{n}" for n in (10, 11, 12)
    ]
    assert result.work_truncated is True


def test_work_issue_unknown_state_and_lifecycle_actor_link(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    issue = _work_issue(db_session, fleet["user"], state="in progress", minutes=100)
    execution = (
        db_session.query(models.IssueCostExecution)
        .filter_by(flow_id=fleet["flow"].id)
        .one()
    )
    stamp = (NOW - timedelta(minutes=3)).replace(tzinfo=None)
    db_session.add(
        models.IssueLifecycle(
            account_id=fleet["user"].account_id,
            issue_id=issue.id,
            kind="triage",
            revision="example-revision",
            state="assessed",
            execution_id=execution.execution_id,
            created_at=stamp,
            updated_at=stamp,
        )
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    item = next(item for item in result.work if item.kind == "issue")
    assert item.state is None
    assert item.node_ids == [f"flow:{fleet['flow'].id}"]
    assert item.last_at == NOW - timedelta(minutes=3)
    assert item.last_action == "issue_updated"


@pytest.mark.parametrize(
    "link,ambiguous",
    [("unassigned", False), ("ambiguous", False), ("pull_request", True)],
)
def test_work_does_not_promote_ambiguous_or_unassigned_links(
    db_session: Session,
    fleet: dict[str, Any],
    link: str,
    ambiguous: bool,
) -> None:
    fact = (
        db_session.query(models.IssueCostExecution)
        .filter_by(flow_id=fleet["flow"].id)
        .one()
    )
    fact.link = link
    fact.start_time = NOW - timedelta(minutes=5)
    pr = (
        db_session.query(models.IssueCostPullRequest)
        .filter_by(pr_key=fact.pr_key)
        .one()
    )
    pr.ambiguous = ambiguous
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    item = next(item for item in result.work if item.kind == "pull_request")
    assert item.node_ids == []
    assert item.related_issue is None


def test_work_is_account_scoped(db_session: Session, fleet: dict[str, Any]) -> None:
    _work_issue(db_session, fleet["user"])
    other = models.User(
        id=uuid4(), account_id=uuid4(), is_superuser=True, username="other"
    )
    with patch.object(fleet_code_activity, "_allowed", return_value=True):
        result = build_code_activity(
            db_session, user=other, active_within_minutes=60, now=NOW
        )
    assert result.work == []
    assert result.work_truncated is False


def test_work_tool_actor_is_the_managed_agent_and_not_its_owner(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    session = (
        db_session.query(models.RuntimeSession)
        .filter_by(session_source_id=fleet["agent"].session_source_id)
        .one()
    )
    _call(
        db_session,
        session,
        "update_issue",
        1,
        metadata={
            "arguments_summary": {"issue_id": "example/widgets#99"},
        },
    )
    # Normal size-only summaries, failed calls and ambiguous issue comments
    # cannot identify another work item.
    _call(
        db_session,
        session,
        "create_issue",
        1,
        metadata={"arguments_summary": {"issue_id": 25}},
    )
    _call(
        db_session,
        session,
        "update_issue",
        1,
        status="failed",
        metadata={"arguments_summary": {"issue_id": "example/widgets#100"}},
    )
    _call(
        db_session,
        session,
        "add_comment",
        1,
        metadata={"arguments_summary": {"target": "example/widgets#101"}},
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    item = next(item for item in result.work if item.kind == "issue")
    assert item.key == "example/widgets#99"
    assert item.node_ids == [f"agent:{fleet['agent'].id}"]
    assert item.title is None
    assert item.state is None
    assert not any(item.key.endswith(("#100", "#101")) for item in result.work)


def test_work_projects_run_subjects_and_publications_without_cost_facts(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    db_session.add(
        models.FlowExecution(
            flow_id=fleet["flow"].id,
            status="RUNNING",
            start_time=(NOW - timedelta(minutes=5)).replace(tzinfo=None),
            trigger_event_details={
                "type": "issue_labeled",
                "_subject": {
                    "reference": "#8",
                    "repo": "example/widgets",
                    "title": "Stored subject",
                    "url": "https://github.com/example/widgets/issues/8",
                },
                "payload": {"body": "Do not project this"},
            },
            result={
                "pr_url": "https://gitlab.com/example/widgets/-/merge_requests/9",
                "body": "Do not project this either",
            },
        )
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    issue = next(item for item in result.work if item.key == "example/widgets#8")
    pr = next(item for item in result.work if item.key == "example/widgets!9")
    assert issue.title == "Stored subject"
    assert issue.node_ids == pr.node_ids == [f"flow:{fleet['flow'].id}"]
    assert pr.reference == "!9"
    assert (
        pr.related_issue is None
    )  # Publication and trigger alone do not prove a closing reference.
    assert "Do not project" not in result.model_dump_json()


def test_work_actor_visibility_follows_the_graph(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    def allowed(db: Any, user: Any, permission: str, owner: bool) -> bool:
        return permission in {"view_device_fleet", "view_agents"}

    with patch.object(fleet_code_activity, "_allowed", side_effect=allowed):
        result = build_code_activity(
            db_session, user=fleet["user"], active_within_minutes=60, now=NOW
        )
    assert result.work
    assert all(not item.node_ids for item in result.work)
    assert result.flows == []


def test_work_sources_are_bounded_and_each_uses_one_query(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from sqlalchemy import event
    from preloop.models.crud import fleet_work

    _work_issue(db_session, fleet["user"])
    statements: list[str] = []

    def record(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        for source in (
            fleet_work.list_pull_requests,
            fleet_work.list_issues,
            fleet_work.list_run_links,
            fleet_work.list_execution_subjects,
        ):
            statements.clear()
            rows = source(
                db_session,
                account_id=str(fleet["user"].account_id),
                since=NOW - timedelta(hours=1),
                until=NOW,
                limit=1,
            )
            assert len(rows) <= 1
            assert len(statements) == 1
            assert "LIMIT" in statements[0]
    finally:
        event.remove(bind, "before_cursor_execute", record)


def test_work_tool_index_covers_the_source(db_session: Session) -> None:
    from preloop.models.crud.fleet_map import WORK_ACTIVITY_TOOLS

    definition = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_runtime_session_activity_work_tools'"
        )
    ).scalar_one()
    assert '(account_id, "timestamp" DESC)' in definition
    assert "activity_type)::text = 'tool_call'" in definition
    assert "status)::text = 'succeeded'" in definition
    for tool in WORK_ACTIVITY_TOOLS:
        assert f"'{tool}'" in definition


def test_work_old_snapshots_enrich_recent_targets_in_the_same_source_queries(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    _work_issue(db_session, fleet["user"], 88, minutes=100, state="closed")
    db_session.add(
        models.IssueCostPullRequest(
            account_id=fleet["user"].account_id,
            pr_key="https://gitlab.com/example/widgets/-/merge_requests/89",
            opened_at=NOW - timedelta(days=3),
            merged_at=NOW - timedelta(days=2),
        )
    )
    session = _session(db_session, fleet["user"].account_id, "old-snapshots")
    _call(
        db_session,
        session,
        "update_issue",
        1,
        metadata={"arguments_summary": {"issue_id": "example/widgets#88"}},
    )
    _call(
        db_session,
        session,
        "update_merge_request",
        1,
        metadata={"arguments_summary": {"target": "example/widgets!89"}},
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    issue = next(item for item in result.work if item.key == "example/widgets#88")
    pr = next(item for item in result.work if item.key == "example/widgets!89")
    assert issue.title == "Stored issue title"
    assert issue.state == "closed"
    assert issue.last_action == "issue_updated"
    assert pr.state == "merged"
    assert pr.last_action == "pull_request_updated"
    assert issue.last_at == pr.last_at == NOW - timedelta(minutes=1)


def test_work_actor_ids_are_distinct_recent_first_and_capped_at_five(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    ids: list[str] = []
    for index in range(6):
        source = f"example-work-actor-{index}"
        agent = models.ManagedAgent(
            account_id=fleet["user"].account_id,
            owner_user_id=fleet["user"].id,
            agent_kind="claude_code",
            display_name="Example agent",
            session_source_type="claude_code",
            session_source_id=source,
            lifecycle_updated_at=NOW.replace(tzinfo=None),
            last_seen_at=NOW.replace(tzinfo=None),
            tags={},
        )
        db_session.add(agent)
        db_session.flush()
        ids.append(f"agent:{agent.id}")
        session = _session(db_session, fleet["user"].account_id, source)
        _call(
            db_session,
            session,
            "update_issue",
            index + 1,
            metadata={"arguments_summary": {"issue_id": "example/widgets#77"}},
        )
        _call(
            db_session,
            session,
            "update_issue",
            index + 10,
            metadata={"arguments_summary": {"issue_id": "example/widgets#77"}},
        )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    item = next(item for item in result.work if item.key == "example/widgets#77")
    assert item.node_ids == ids[:5]


def test_work_hard_cap_is_two_hundred(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    for number in range(201):
        db_session.add(
            models.IssueCostPullRequest(
                account_id=fleet["user"].account_id,
                pr_key=f"https://github.com/example/widgets/pull/{number + 1}",
                opened_at=NOW - timedelta(seconds=number),
            )
        )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    assert len(result.work) == 200
    assert len({(item.kind, item.key) for item in result.work}) == 200
    assert result.work_truncated is True
    assert result.work == sorted(
        result.work, key=lambda item: (-item.last_at.timestamp(), item.kind, item.key)
    )


def test_work_update_pull_request_review_agrees_with_counts_and_events(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    session = (
        db_session.query(models.RuntimeSession)
        .filter_by(session_source_id=fleet["agent"].session_source_id)
        .one()
    )
    _call(
        db_session,
        session,
        "update_pull_request",
        1,
        metadata={
            "arguments_summary": {"target": "example/widgets#4", "review_action": 9},
        },
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    item = next(item for item in result.work if item.key == "example/widgets#4")
    assert item.last_action == "reviewed"
    assert item.review_state is None
    assert result.counts[f"agent:{fleet['agent'].id}"].reviewed == 1
    assert any(
        event.action == "reviewed" and event.node_id == f"agent:{fleet['agent'].id}"
        for event in result.events
    )


def test_work_real_size_only_summaries_never_invent_a_target_or_actor(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from preloop.services.dynamic_fastmcp import _summarize_arguments

    session = (
        db_session.query(models.RuntimeSession)
        .filter_by(session_source_id=fleet["agent"].session_source_id)
        .one()
    )
    summary = _summarize_arguments(
        {
            "issue_id": "example/widgets#55",
            "url": "https://github.com/example/widgets/issues/55",
            "title": "This is not persisted tracker metadata",
            "body": "Private issue body",
        }
    )
    assert all(isinstance(value, int) for value in summary.values())
    _call(
        db_session, session, "update_issue", 1, metadata={"arguments_summary": summary}
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    assert not any(item.key == "example/widgets#55" for item in result.work)
    assert all(
        f"agent:{fleet['agent'].id}" not in item.node_ids for item in result.work
    )
    assert "Private issue body" not in result.model_dump_json()


def test_work_active_pr_cap_cannot_crowd_a_merge_out_of_counts(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    for number in range(501):
        db_session.add(
            models.IssueCostPullRequest(
                account_id=fleet["user"].account_id,
                pr_key=f"https://github.com/example/widgets/pull/{number + 1}",
                opened_at=NOW - timedelta(seconds=1),
            )
        )
    db_session.flush()
    from preloop.models.crud import fleet_work

    windows = fleet_work.list_pull_requests(
        db_session,
        account_id=str(fleet["user"].account_id),
        since=NOW - timedelta(hours=1),
        until=NOW,
        limit=1,
    )
    assert len(windows) == 2
    assert sum(row.active for row in windows) == 1
    assert sum(row.for_merges for row in windows) == 1
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    assert result.counts[f"flow:{fleet['flow'].id}"].merged == 1
    assert any(
        event.pr == "acme/api#42" and event.action == "merged"
        for event in result.events
    )
    assert result.work_truncated is True
