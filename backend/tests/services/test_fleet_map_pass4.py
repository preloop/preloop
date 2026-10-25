"""Fleet Map pass 4: prod work-item filtering, people scope, provider containment.

The prod shapes (principal ids, a hook call, a Jira search result, a GitHub
issue) come from the founder's account on 2026-10-21, scrubbed; see
``tests/fixtures/fleet_map/prod_shapes_2026_10_21.json``.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_issue
from preloop.services.fleet_code_activity import build_code_activity
from preloop.services.fleet_map import build_fleet_map
from preloop.services.fleet_principals import base_principal_id, resolve_principal
from preloop.services.fleet_scope import parse_users
from preloop.services.fleet_usage import build_fleet_usage
from preloop.sync.trackers.github import GitHubTracker
from preloop.sync.trackers.jira import JiraTracker
from tests.services import test_fleet_code_activity as base
from tests.services.test_fleet_map_pass3 import _model, _usage

NOW = base.NOW
_call = base._call
fleet = base.fleet

PROD = json.loads(
    (
        Path(__file__).parents[1] / "fixtures/fleet_map/prod_shapes_2026_10_21.json"
    ).read_text()
)
OBSERVED = datetime.fromisoformat(PROD["observed_at"].replace("Z", "+00:00"))


def _principal_session(
    db: Session, account_id: Any, principal_id: str, principal_type: str = "claude_code"
) -> models.RuntimeSession:
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type=principal_type,
        session_source_id=principal_id,
        runtime_principal_type=principal_type,
        runtime_principal_id=principal_id,
        started_at=NOW.replace(tzinfo=None) - timedelta(hours=2),
    )
    db.add(session)
    db.flush()
    return session


def _agent(
    db: Session, account_id: Any, owner: Any, source_id: str, name: str = "Codex CLI"
) -> models.ManagedAgent:
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=account_id,
        owner_user_id=owner,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=source_id,
        display_name=name,
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=NOW.replace(tzinfo=None),
        last_seen_at=NOW.replace(tzinfo=None),
        tags={},
    )
    db.add(agent)
    db.flush()
    return agent


def _person(db: Session, account_id: Any, name: str) -> models.User:
    person = models.User(
        account_id=account_id,
        email=f"{name}-{uuid4().hex[:6]}@example.com",
        username=f"{name}-{uuid4().hex[:6]}",
        full_name=name.title(),
        is_active=True,
        email_verified=True,
        hashed_password="x",
        user_source="local",
    )
    db.add(person)
    db.flush()
    return person


def _hook_ref(ref: str, repo: str = "acme/api", kind: str = "pull_request") -> dict:
    return {
        "origin": "native_hook",
        "work_ref": {
            "via": "cli",
            "kind": kind,
            "action": "commented",
            "ref": ref,
            "repo": repo,
            "host": "github.com",
        },
    }


@contextmanager
def _devices(items: list[dict[str, Any]] | None) -> Iterator[None]:
    """Patch the optional device projection for the map and the usage read."""
    projection = None if items is None else {"truncated": False, "items": items}
    with (
        patch("preloop.services.fleet_map.get_plugin_manager") as for_map,
        patch("preloop.plugins.base.get_plugin_manager") as for_usage,
    ):
        for manager in (for_map, for_usage):
            if projection is None:
                manager.return_value.get_service.return_value = None
            else:
                manager.return_value.get_service.return_value.read.return_value = (
                    projection
                )
        yield


def _device(key: str, owner: Any, *agents: Any) -> dict[str, Any]:
    return {
        "device_key": key,
        "hostname": key,
        "runner_state": None,
        "users": [{"id": str(owner)}],
        "runners": [],
        "findings": [],
        "agents": [
            {"managed_agent_id": str(a.id), "id": f"c-{a.id}", "kind": "claude_code"}
            for a in agents
        ],
        "last_seen_at": NOW.isoformat(),
    }


# ------------------------------------------------- item 10: agent principals


def test_principal_resolution_matches_the_session_explorer() -> None:
    principals = PROD["principals"]
    index = {("claude_code", principals["claude_code_agent_source_id"]): "agent:a"}
    for key in ("claude_code_conversation", "claude_code_subagent"):
        assert resolve_principal(index, "claude_code", principals[key]) == "agent:a"
    assert resolve_principal(index, "codex", principals["codex_thread"]) is None
    assert resolve_principal(index, "claude_code", None) is None
    assert (
        resolve_principal({}, "managed_agent", "a1", by_id={"a1": "agent:a1"})
        == "agent:a1"
    )
    assert (
        base_principal_id(principals["codex_thread"])
        == (principals["codex_agent_source_id"])
    )


def test_hook_calls_of_a_conversation_reach_their_agent(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    """Prod: Claude Code had 80 hook calls per session yet tool_calls 0."""
    user, agent = fleet["user"], fleet["agent"]
    conversation = _principal_session(
        db_session,
        user.account_id,
        f"{agent.session_source_id}:9d0f6a52-6b0e:a41f0c2e9b7d6a5c3",
    )
    hook = PROD["native_hook_call"]
    _call(
        db_session,
        conversation,
        hook["tool_name"],
        hook["minutes_before_observed"],
        status=hook["status"],
        metadata=hook["metadata"],
    )
    db_session.flush()
    activity = build_code_activity(
        db_session, user=user, active_within_minutes=60, now=NOW
    )
    node = f"agent:{agent.id}"
    issue = "work:issue:preloop/preloop#1957"
    item = next(item for item in activity.work if item.id == issue)
    assert node in item.node_ids
    assert any(
        r.kind == "touched" and r.source == node and r.target == issue
        for r in activity.relations
    )
    assert activity.counts[node].issues_updated == 1
    usage = build_fleet_usage(db_session, user=user, active_within_minutes=60, now=NOW)
    assert usage.agents[node].tool_calls >= 1
    assert usage.agents[node].issues_touched == 1


def test_gateway_rows_of_a_conversation_count_for_their_agent(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, agent = fleet["user"], fleet["agent"]
    model = _model(db_session, user.account_id, "conversation-model")
    _usage(
        db_session,
        user.account_id,
        model,
        5,
        runtime_principal_type="claude_code",
        runtime_principal_id=f"{agent.session_source_id}:conversation-1",
    )
    with _devices(None):
        usage = build_fleet_usage(
            db_session, user=user, active_within_minutes=60, now=NOW
        )
        graph = build_fleet_map(
            db_session, user=user, active_within_minutes=60, now=NOW
        )
    assert usage.agents[f"agent:{agent.id}"].request_count == 1
    assert any(
        edge.kind == "uses_model"
        and edge.source == f"agent:{agent.id}"
        and edge.target == "model:conversation-model"
        and edge.provenance == "observed"
        for edge in graph.edges
    )


def test_a_managed_agent_principal_is_its_agent(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, agent = fleet["user"], fleet["agent"]
    session = _principal_session(
        db_session, user.account_id, str(agent.id), principal_type="managed_agent"
    )
    _call(db_session, session, "Bash", 3, status="allowed", metadata=_hook_ref("#61"))
    db_session.flush()
    activity = build_code_activity(
        db_session, user=user, active_within_minutes=60, now=NOW
    )
    item = next(i for i in activity.work if i.id == "work:pull_request:acme/api#61")
    assert item.node_ids == [f"agent:{agent.id}"]


# --------------------------------------------- item 10: tracker timestamps


def _tracker(db: Session, user: models.User, kind: str) -> models.Project:
    tracker = models.Tracker(
        account_id=user.account_id,
        name=f"{kind} tracker",
        tracker_type=kind,
        url="https://example.atlassian.net" if kind == "jira" else "https://github.com",
        api_key="example",
    )
    db.add(tracker)
    db.flush()
    org = models.Organization(
        name="Org", identifier=f"org-{kind}", tracker_id=tracker.id
    )
    db.add(org)
    db.flush()
    project = models.Project(
        name="Proj",
        identifier=f"proj-{kind}",
        slug="preloop/preloop",
        organization_id=org.id,
    )
    db.add(project)
    db.flush()
    return project


def _jira(project: models.Project) -> JiraTracker:
    client = JiraTracker.__new__(JiraTracker)
    client.tracker_id = str(project.organization.tracker_id)
    client.jira_url = "https://subaccount.atlassian.example"
    return client


def test_jira_search_results_keep_jira_times_not_the_sync_time(
    db_session: Session, test_user: models.User
) -> None:
    project = _tracker(db_session, test_user, "jira")
    values = _jira(project).transform_issue(PROD["jira_search_issue"], project)
    assert values["created_at"] == datetime(2026, 10, 18, 9, 12, 44, 120000)
    assert values["last_updated_external"] == datetime(2026, 10, 18, 9, 15, 2, 871000)
    bare = dict(PROD["jira_search_issue"], fields={"summary": "x", "status": "To Do"})
    assert (
        _jira(project).transform_issue(bare, project)["last_updated_external"] is None
    )


def test_a_resynced_jira_issue_is_not_work_in_the_window(
    db_session: Session, test_user: models.User
) -> None:
    """Prod: SUBA-1..9 were stamped 20:26:13 by a sync and listed for 1h and 24h."""
    test_user.is_superuser = True
    project = _tracker(db_session, test_user, "jira")
    values = _jira(project).transform_issue(PROD["jira_search_issue"], project)
    values["tracker_id"] = project.organization.tracker_id
    crud_issue.upsert(db_session, obj_in=values)
    for minutes in (60, 1440):
        activity = build_code_activity(
            db_session, user=test_user, active_within_minutes=minutes, now=OBSERVED
        )
        assert "work:issue:SUBA-1" not in {item.id for item in activity.work}
    # The pre-fix row shape (sync time in every column) is what prod listed.
    row = PROD["jira_prod_row_before_fix"]
    issue = db_session.query(models.Issue).filter_by(key="SUBA-1").one()
    issue.created_at = datetime.fromisoformat(row["created_at"])
    issue.last_updated_external = datetime.fromisoformat(row["updated_at"])
    db_session.flush()
    stale = build_code_activity(
        db_session, user=test_user, active_within_minutes=60, now=OBSERVED
    )
    assert "work:issue:SUBA-1" in {item.id for item in stale.work}


def test_a_closed_github_issue_is_work_when_it_was_closed(
    db_session: Session, test_user: models.User
) -> None:
    """Prod: #1957, closed at 18:44Z, was listed as opened at 13:18Z."""
    test_user.is_superuser = True
    project = _tracker(db_session, test_user, "github")
    client = GitHubTracker.__new__(GitHubTracker)
    client.tracker_id = str(project.organization.tracker_id)
    values = client.transform_issue(PROD["github_issue"], project)
    assert values["last_updated_external"] == datetime(2026, 10, 21, 18, 44, 26)
    values.pop("comments", None)
    crud_issue.upsert(db_session, obj_in=values)
    day = build_code_activity(
        db_session, user=test_user, active_within_minutes=1440, now=OBSERVED
    )
    item = next(i for i in day.work if i.id == "work:issue:preloop/preloop#1957")
    assert item.last_action == "issue_updated"
    assert item.last_at == datetime(2026, 10, 21, 18, 44, 26, tzinfo=UTC)
    hour = build_code_activity(
        db_session, user=test_user, active_within_minutes=60, now=OBSERVED
    )
    assert "work:issue:preloop/preloop#1957" not in {i.id for i in hour.work}


# ----------------------------------------------------- item 5: users scope


def test_users_parameter_is_validated() -> None:
    assert parse_users(None) is None and parse_users(" ") is None
    one = str(uuid4())
    assert parse_users(f"{one}, {one}") == [one]
    with pytest.raises(HTTPException):
        parse_users("not-a-uuid")
    with pytest.raises(HTTPException):
        parse_users(",".join(str(uuid4()) for _ in range(51)))


def _two_people(db: Session, fleet: dict[str, Any]) -> dict[str, Any]:
    user = fleet["user"]
    other = _person(db, user.account_id, "scotty")
    their_agent = _agent(db, user.account_id, other.id, f"scotty-{uuid4().hex[:6]}")
    their_session = base._session(db, user.account_id, their_agent.session_source_id)
    model = _model(db, user.account_id, "shared-model", "anthropic")
    for agent, minutes in ((fleet["agent"], 6), (their_agent, 5)):
        _usage(
            db,
            user.account_id,
            model,
            minutes,
            runtime_principal_type="claude_code",
            runtime_principal_id=agent.session_source_id,
        )
    own = _model(db, user.account_id, "scotty-model", "mistral")
    _usage(
        db,
        user.account_id,
        own,
        4,
        runtime_principal_type="claude_code",
        runtime_principal_id=their_agent.session_source_id,
    )
    # Both people's agents comment on the same PR; Scotty alone on another.
    _call(db, fleet["session"], "Bash", 3, status="allowed", metadata=_hook_ref("#70"))
    _call(db, their_session, "Bash", 2, status="allowed", metadata=_hook_ref("#70"))
    _call(db, their_session, "Bash", 1, status="allowed", metadata=_hook_ref("#71"))
    db.flush()
    return {"other": other, "their_agent": their_agent}


def test_a_user_without_devices_scopes_to_their_agents_models_and_work(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, agent = fleet["user"], fleet["agent"]
    extra = _two_people(db_session, fleet)
    me, them = f"agent:{agent.id}", f"agent:{extra['their_agent'].id}"
    with _devices(None):
        graph = build_fleet_map(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(user.id)],
        )
        activity = build_code_activity(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(user.id)],
        )
        usage = build_fleet_usage(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(user.id)],
        )
    ids = {node.id for node in graph.nodes}
    assert graph.users == [f"user:{user.id}"]
    assert {me, f"user:{user.id}", "model:shared-model", "provider:anthropic"} <= ids
    assert them not in ids and f"user:{extra['other'].id}" not in ids
    assert "model:scotty-model" not in ids and "provider:mistral" not in ids
    assert all(e.source in ids and e.target in ids for e in graph.edges)

    work = {item.id: item for item in activity.work}
    assert work["work:pull_request:acme/api#70"].node_ids == [me]
    assert "work:pull_request:acme/api#71" not in work
    assert activity.flows == [] and activity.users == [f"user:{user.id}"]
    assert set(activity.counts) <= {me, f"user:{user.id}"}
    assert all(event.node_id == me for event in activity.events)

    assert set(usage.agents) == {me} and set(usage.people) == {f"user:{user.id}"}
    assert usage.flows == {} and usage.users == [f"user:{user.id}"]
    # Only this person's agent's request on the shared model is counted.
    assert usage.models["model:shared-model"].request_count == 1
    assert "model:scotty-model" not in usage.models
    assert "work:pull_request:acme/api#71" not in usage.work


def test_a_shared_device_brings_in_every_agent_it_hosts(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, agent = fleet["user"], fleet["agent"]
    extra = _two_people(db_session, fleet)
    them = f"agent:{extra['their_agent'].id}"
    devices = [_device("fp:shared", user.id, agent, extra["their_agent"])]
    with _devices(devices):
        graph = build_fleet_map(db_session, user=user, now=NOW, user_ids=[str(user.id)])
        usage = build_fleet_usage(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(user.id)],
        )
    ids = {node.id for node in graph.nodes}
    assert {"device:fp:shared", them, "model:scotty-model"} <= ids
    # Scotty is reachable through the device, not selected.
    assert f"user:{extra['other'].id}" not in ids
    assert them in usage.agents and "device:fp:shared" in usage.devices
    assert usage.models["model:shared-model"].request_count == 2


def test_work_touched_by_two_users_lists_the_selected_actors(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, agent = fleet["user"], fleet["agent"]
    extra = _two_people(db_session, fleet)
    me, them = f"agent:{agent.id}", f"agent:{extra['their_agent'].id}"
    with _devices(None):
        both = build_code_activity(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(user.id), str(extra["other"].id)],
        )
        theirs = build_code_activity(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(extra["other"].id)],
        )
    shared = next(i for i in both.work if i.id == "work:pull_request:acme/api#70")
    assert set(shared.node_ids) == {me, them}
    assert [i.id for i in both.work].count("work:pull_request:acme/api#70") == 1
    only = next(i for i in theirs.work if i.id == "work:pull_request:acme/api#70")
    assert only.node_ids == [them]
    assert {r.source for r in theirs.relations if r.kind == "touched"} == {them}


def test_an_unknown_user_scopes_to_nothing(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    with _devices(None):
        graph = build_fleet_map(
            db_session, user=fleet["user"], now=NOW, user_ids=[str(uuid4())]
        )
    assert graph.users == [] and graph.nodes == [] and graph.edges == []


# ------------------------------------------- item 6: provider containment


def test_models_carry_their_provider_and_providers_their_models(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user = fleet["user"]
    secret = uuid4()
    db_session.add(
        models.SecretReference(
            id=secret,
            account_id=user.account_id,
            name="OpenAI key",
            backend_type="local_encrypted",
            secret_kind="ai_model_credentials",
            status="active",
            encrypted_value="x",
        )
    )
    db_session.flush()
    for identifier in ("gpt-x", "gpt-y"):
        row = _model(db_session, user.account_id, identifier, "openai")
        row.credentials_secret_id = secret
    _model(db_session, user.account_id, "claude-z", "anthropic")
    db_session.flush()
    with _devices(None):
        graph = build_fleet_map(db_session, user=user, now=NOW)
    nodes = {node.id: node for node in graph.nodes}
    account = f"provider_account:{secret}"
    assert nodes["model:gpt-x"].provider_id == "provider:openai"
    assert nodes["model:gpt-x"].provider_account_ids == [account]
    assert nodes["model:claude-z"].provider_id == "provider:anthropic"
    assert nodes["model:claude-z"].provider_account_ids == []
    assert nodes["provider:openai"].model_ids == ["model:gpt-x", "model:gpt-y"]
    assert nodes["provider:openai"].account_ids == [account]
    assert nodes[account].provider_id == "provider:openai"
    assert nodes[account].model_ids == ["model:gpt-x", "model:gpt-y"]
    assert nodes["provider:anthropic"].model_ids == ["model:claude-z"]


def test_a_model_under_two_providers_is_placed_by_most_configurations(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user = fleet["user"]
    _model(db_session, user.account_id, "vendor/shared-m", "openrouter")
    _model(db_session, user.account_id, "shared-m", "zeta")
    _model(db_session, user.account_id, "other/shared-m", "zeta")
    with _devices(None):
        graph = build_fleet_map(db_session, user=user, now=NOW)
    nodes = {node.id: node for node in graph.nodes}
    model = nodes["model:shared-m"]
    assert model.provider_ids == ["provider:openrouter", "provider:zeta"]
    assert model.provider_id == "provider:zeta"
    assert nodes["provider:zeta"].model_ids == ["model:shared-m"]
    assert nodes["provider:openrouter"].model_ids == []


def test_out_of_scope_activity_cannot_crowd_the_scoped_caps(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    """Review: the scope is applied before the call, event and work caps."""
    from preloop.services import fleet_code_activity, fleet_work

    user, agent = fleet["user"], fleet["agent"]
    extra = _two_people(db_session, fleet)
    their_session = base._session(
        db_session, user.account_id, f"{extra['their_agent'].session_source_id}:c2"
    )
    for number in range(100, 160):
        _call(
            db_session,
            their_session,
            "Bash",
            0.5,
            status="allowed",
            metadata=_hook_ref(f"#{number}"),
        )
    _call(db_session, fleet["session"], "Bash", 50, metadata=_hook_ref("#99"))
    db_session.flush()
    with (
        _devices(None),
        patch.object(fleet_code_activity, "CALL_LIMIT", 20),
        patch.object(fleet_code_activity, "EVENT_LIMIT", 5),
        patch.object(fleet_work, "WORK_LIMIT", 10),
    ):
        scoped = build_code_activity(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(user.id)],
        )
    me = f"agent:{agent.id}"
    assert "work:pull_request:acme/api#99" in {item.id for item in scoped.work}
    assert any(e.work_id == "work:pull_request:acme/api#99" for e in scoped.events)
    assert all(e.node_id == me for e in scoped.events)


def test_direct_only_usage_keeps_scoped_usage_keyed_like_the_map(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    """Review: totals are only returned for model nodes in the scoped map."""
    user = fleet["user"]
    person = _person(db_session, user.account_id, "uhura")
    model = _model(db_session, user.account_id, "direct-only-model", "groq")
    _usage(db_session, user.account_id, model, 3, user_id=person.id)
    with _devices(None):
        graph = build_fleet_map(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(person.id)],
        )
        usage = build_fleet_usage(
            db_session,
            user=user,
            active_within_minutes=60,
            now=NOW,
            user_ids=[str(person.id)],
        )
    ids = {node.id for node in graph.nodes}
    assert set(usage.models) <= ids and set(usage.providers) <= ids
    assert usage.people[f"user:{person.id}"].request_count == 1


def test_a_resync_heals_a_sync_stamped_jira_row(
    db_session: Session, test_user: models.User
) -> None:
    """Review B1: the stored sync stamp is later than Jira's own time."""
    from preloop.sync.scanner.core import stamped_times_to_heal

    project = _tracker(db_session, test_user, "jira")
    values = _jira(project).transform_issue(PROD["jira_search_issue"], project)
    row = PROD["jira_prod_row_before_fix"]
    stored = models.Issue(
        project_id=project.id,
        tracker_id=project.organization.tracker_id,
        key="SUBA-1",
        external_id="10001",
        title="t",
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_updated_external=datetime.fromisoformat(row["updated_at"]),
    )
    healed = stamped_times_to_heal(stored, values)
    assert healed == {
        "last_updated_external": datetime(2026, 10, 18, 9, 15, 2, 871000),
        "updated_at": datetime(2026, 10, 18, 9, 15, 2, 871000),
        "created_at": datetime(2026, 10, 18, 9, 12, 44, 120000),
    }
    # A local edit after the bad sync keeps its own updated_at.
    edited = datetime(2026, 10, 21, 20, 40)
    stored.updated_at = edited
    assert (stamped_times_to_heal(stored, values) or {})["updated_at"] == edited
    stored.updated_at = datetime.fromisoformat(row["updated_at"])
    # A row already holding the provider time needs nothing.
    stored.last_updated_external = healed["last_updated_external"]
    assert stamped_times_to_heal(stored, values) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("local_edit", [False, True])
async def test_a_full_scan_rewrites_the_stamped_jira_row(
    db_session: Session, test_user: models.User, local_edit: bool
) -> None:
    """Review: the heal runs inside scan_issues and is persisted."""
    from unittest.mock import AsyncMock

    from preloop.sync.scanner.core import TrackerClient

    project = _tracker(db_session, test_user, "jira")
    row = PROD["jira_prod_row_before_fix"]
    stamp = datetime.fromisoformat(row["updated_at"])
    db_session.add(
        models.Issue(
            project_id=project.id,
            tracker_id=project.organization.tracker_id,
            key="SUBA-1",
            external_id="10001",
            title="Subaccount tracker issue SUBA-1",
            status="Backlog",
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=stamp,
            last_updated_external=stamp,
        )
    )
    db_session.flush()
    edited = datetime(2026, 10, 21, 20, 40)
    if local_edit:
        # A local edit after the bad sync (raw write: onupdate would stamp now).
        db_session.execute(
            models.Issue.__table__.update()
            .where(models.Issue.key == "SUBA-1")
            .values(updated_at=edited)
        )
        db_session.flush()
    tracker = db_session.get(models.Tracker, project.organization.tracker_id)
    scanner = TrackerClient(tracker, initialize_client=False)
    jira = _jira(project)
    stub = AsyncMock()
    stub.get_issues.return_value = [PROD["jira_search_issue"]]
    stub.transform_issue = jira.transform_issue
    scanner.client = stub
    await scanner.scan_issues(db_session, project.organization, project)
    stored = db_session.query(models.Issue).filter_by(key="SUBA-1").one()
    db_session.refresh(stored)
    assert stored.last_updated_external == datetime(2026, 10, 18, 9, 15, 2, 871000)
    assert stored.updated_at == (
        edited if local_edit else datetime(2026, 10, 18, 9, 15, 2, 871000)
    )
    assert stored.created_at == datetime(2026, 10, 18, 9, 12, 44, 120000)
