"""Discovery through the authenticated projects API and real scoped queries."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models

END = datetime(2026, 11, 12, tzinfo=UTC)
RECENT = END - timedelta(days=1)
URL = "/api/v1/projects/discovery"


def discover(client: TestClient, **params: Any) -> dict[str, Any]:
    """Read a reproducible seven-day window."""
    response = client.get(URL, params={"as_of": END.isoformat(), "days": 7, **params})
    assert response.status_code == 200, response.text
    return response.json()


def session(
    db: Session, account_id: UUID, *, source: str = "agent", at: datetime = RECENT
) -> models.RuntimeSession:
    """Store a conversation at its original activity time."""
    row = models.RuntimeSession(
        account_id=account_id,
        session_source_type=source,
        session_source_id=str(uuid4()),
        started_at=at.replace(tzinfo=None),
        cwd="/work/example",
    )
    db.add(row)
    db.flush()
    return row


def work(
    db: Session,
    row: models.RuntimeSession,
    host: str | None = "github.com",
    path: str = "team/example.git",
) -> None:
    """Record a successful tool call with the compact Fleet Map work reference."""
    db.add(
        models.RuntimeSessionActivity(
            account_id=row.account_id,
            runtime_session_id=row.id,
            activity_type="tool_call",
            status="succeeded",
            timestamp=RECENT,
            metadata_={
                "work_ref": {
                    "kind": "pull_request",
                    "action": "merged",
                    "repo": path,
                    "ref": "#7",
                    "host": host,
                }
            },
        )
    )
    db.flush()


def project(db: Session, tracker: models.Tracker, slug: str) -> models.Project:
    """Create a project visible under the normal tracker scope rules."""
    org = models.Organization(
        tracker_id=tracker.id, name="Team", identifier=str(uuid4())
    )
    db.add(org)
    db.flush()
    db.add(
        models.TrackerScopeRule(
            tracker_id=tracker.id,
            scope_type="ORGANIZATION",
            rule_type="INCLUDE",
            identifier=org.identifier,
        )
    )
    row = models.Project(
        name="Example", identifier=str(uuid4()), slug=slug, organization_id=org.id
    )
    db.add(row)
    db.flush()
    return row


def test_agent_only_and_multiple_repositories(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
) -> None:
    row = session(db_session, test_user.account_id)
    work(db_session, row)
    work(db_session, row, path="team/library")
    result = discover(client)
    assert result["coverage"] == {"sessions": "observed", "tracker": "unknown"}
    candidates = result["candidates"]
    assert [c["repository"] for c in candidates] == [
        "github.com/team/example",
        "github.com/team/library",
    ]
    assert all(c["project_id"] is None and c["evidence_count"] == 1 for c in candidates)
    assert all(c["evidence"][0]["session_id"] == str(row.id) for c in candidates)
    assert discover(client) == result
    assert discover(client, limit=1)["truncated"]


def test_session_directory_is_only_a_hint(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    test_tracker: models.Tracker,
) -> None:
    matches = [
        project(db_session, test_tracker, f"{owner}/example")
        for owner in ("one", "two")
    ]
    row = session(db_session, test_user.account_id)
    row.cwd = "/work/example/packages/client"
    db_session.flush()
    (candidate,) = discover(client)["candidates"]
    assert candidate["repository"] is None
    assert candidate["project_id"] is None
    assert set(candidate["possible_project_ids"]) == {str(p.id) for p in matches}
    assert candidate["evidence"][0]["ref"] == f"session:{row.id}"


def test_mapped_repo_deduplicates_shared_merge(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    test_tracker: models.Tracker,
) -> None:
    p = project(db_session, test_tracker, "team/new-name")
    p.meta_data = {"former_slugs": ["team/Example"]}
    row = session(db_session, test_user.account_id)
    work(db_session, row)
    work(db_session, row)
    db_session.add(
        models.IssueCostPullRequest(
            account_id=test_user.account_id,
            pr_key="https://github.com/team/new-name/pull/7",
            merged_at=RECENT,
            origin="human",
        )
    )
    db_session.flush()
    result = discover(client)
    (candidate,) = result["candidates"]
    assert candidate["project_id"] == str(p.id)
    assert candidate["repository"] == "github.com/team/new-name"
    assert candidate["evidence_count"] == 1
    (evidence,) = candidate["evidence"]
    assert evidence["sources"] == ["sessions", "tracker"]
    assert evidence["at"] == "2026-11-11T00:00:00Z"


def test_tracker_uses_provider_dates(
    client: TestClient,
    db_session: Session,
    test_tracker: models.Tracker,
) -> None:
    old = project(db_session, test_tracker, "team/old")
    recent = project(db_session, test_tracker, "team/recent")
    for p, at in [(old, END - timedelta(days=100)), (old, None), (recent, RECENT)]:
        db_session.add(
            models.Issue(
                tracker_id=test_tracker.id,
                project_id=p.id,
                title="Example issue",
                external_id=str(uuid4()),
                last_updated_external=at.replace(tzinfo=None) if at else None,
                last_synced=END.replace(tzinfo=None),
            )
        )
    db_session.flush()
    result = discover(client)
    assert result["coverage"] == {"sessions": "unknown", "tracker": "observed"}
    (candidate,) = result["candidates"]
    assert candidate["project_id"] == str(recent.id)
    assert candidate["evidence_count"] == 1


def test_empty_stale_factory_and_foreign_activity(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    test_viewer_user: models.User,
) -> None:
    empty = discover(client)
    assert empty["candidates"] == []
    assert empty["coverage"] == {"sessions": "unknown", "tracker": "unknown"}
    session(db_session, test_user.account_id, at=END - timedelta(days=30))
    work(db_session, session(db_session, test_user.account_id, source="flow_execution"))
    work(db_session, session(db_session, test_viewer_user.account_id))
    db_session.add(
        models.IssueCostPullRequest(
            account_id=test_viewer_user.account_id,
            pr_key="https://github.com/team/example/pull/7",
            merged_at=RECENT,
        )
    )
    db_session.flush()
    assert discover(client) == empty


@pytest.mark.parametrize("params", [{"days": 0}, {"days": 91}, {"limit": 51}])
def test_discovery_bounds(client: TestClient, params: dict[str, int]) -> None:
    assert client.get(URL, params=params).status_code == 422


def test_hostless_reference_keeps_ambiguous_choices(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    test_tracker: models.Tracker,
) -> None:
    first = project(db_session, test_tracker, "group/sub/example")
    second = project(db_session, test_tracker, "group/sub/renamed")
    second.meta_data = {"former_slugs": ["group/sub/example"]}
    row = session(db_session, test_user.account_id)
    work(db_session, row, host=None, path="group/sub/Example.git")
    (candidate,) = discover(client)["candidates"]
    assert candidate["project_id"] is candidate["repository"] is None
    assert candidate["possible_project_ids"] == sorted([str(first.id), str(second.id)])


def test_push_ranking_and_invisible_merges(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    test_tracker: models.Tracker,
    test_viewer_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.models.crud import project_discovery

    # An excluded or foreign PR must not consume the visible input budget.
    monkeypatch.setattr(project_discovery, "FACT_LIMIT", 1)
    p = project(db_session, test_tracker, "team/older")
    p.meta_data = {"updated_at": (RECENT - timedelta(days=1)).isoformat()}
    recent = project(db_session, test_tracker, "team/current")
    for account, path, at, origin in [
        (test_user.account_id, "team/excluded", END, "human"),
        (test_viewer_user.account_id, "team/current", END, "human"),
        (test_user.account_id, "team/older", END, "preloop_flow"),
        (test_user.account_id, "team/current", RECENT, "human"),
    ]:
        db_session.add(
            models.IssueCostPullRequest(
                account_id=account,
                pr_key=f"https://github.com/{path}/pull/5",
                merged_at=at,
                origin=origin,
            )
        )
    db_session.flush()
    result = discover(client)
    assert not result["truncated"]
    assert [c["project_id"] for c in result["candidates"]] == [
        str(recent.id),
        str(p.id),
    ]
    assert [c["evidence"][0]["reason"] for c in result["candidates"]] == [
        "pull_request_merged",
        "repository_push",
    ]


def test_runner_checkout_uses_tracker_identity(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    test_tracker: models.Tracker,
) -> None:
    """Map a checked-out repository even before a forge tool has been called."""
    p = project(db_session, test_tracker, "team/example")
    row = session(db_session, test_user.account_id, source="runner_session")
    runner = models.FlowRunner(
        account_id=test_user.account_id,
        registered_by_user_id=test_user.id,
        name="Example runner",
        hostname="runner.example.com",
        token_hash="test-hash",
        status="online",
        last_heartbeat=RECENT,
    )
    db_session.add(runner)
    db_session.flush()
    db_session.add(
        models.RunnerRemoteSession(
            runner_id=runner.id,
            runtime_session_id=row.id,
            account_id=test_user.account_id,
            actor_user_id=test_user.id,
            harness="copilot_cli",
            requested_at=RECENT.replace(tzinfo=None),
            workspace_kind="tracker_checkout",
            workspace={
                "repository": "team/example",
                "tracker_id": str(test_tracker.id),
            },
        )
    )
    db_session.flush()
    (candidate,) = discover(client)["candidates"]
    assert candidate["project_id"] == str(p.id)
    assert candidate["repository_id"] == f"github:github.com:{p.identifier}"
    assert candidate["evidence"][0]["ref"] == f"session:{row.id}"
    assert candidate["coverage"] == {"sessions": "observed", "tracker": "unknown"}


def test_directories_are_scoped_to_the_reporting_machine(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
) -> None:
    for host in ["first", "second", "first"]:
        session(db_session, test_user.account_id).desktop_endpoint_id = host
    db_session.flush()
    candidates = discover(client)["candidates"]
    assert [c["evidence_count"] for c in candidates] == [2, 1]
    assert all(c["repository"] is None for c in candidates)
