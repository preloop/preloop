"""Fleet Map pass 3: window evidence, flow models, work relations, node totals."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.fleet_code_activity import build_code_activity, code_action
from preloop.services.fleet_map import build_fleet_map
from preloop.services.fleet_usage import build_fleet_usage
from preloop.services.fleet_work_refs import (
    classify_shell_command,
    classify_tool_call,
    work_key,
    work_url,
)
from tests.services import test_fleet_code_activity as base

NOW = base.NOW
_call = base._call
_work_issue = base._work_issue
# The shared fixture: an agent with a session, a flow with a run, a merged PR.
fleet = base.fleet

REMOTE = "github.com/acme/api"


# ---------------------------------------------------------------- classifiers


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("gh pr create --title T --body 'Closes #7'", ("pull_request", "opened", None)),
        ("cd x && gh pr comment 42 --body hi", ("pull_request", "commented", "#42")),
        ("gh pr review 42 --approve", ("pull_request", "reviewed", "#42")),
        ("gh pr review -r 42 -b no", ("pull_request", "reviewed", "#42")),
        ("GH_TOKEN=x gh pr merge 8 --squash", ("pull_request", "merged", "#8")),
        ("gh pr close 9", ("pull_request", "pull_request_updated", "#9")),
        ("gh pr edit 9 --title x", ("pull_request", "pull_request_updated", "#9")),
        ("gh issue create -t T -b B", ("issue", "issue_opened", None)),
        ("gh issue comment 3 -b hi", ("issue", "commented", "#3")),
        ("gh issue close 3", ("issue", "issue_updated", "#3")),
        ("glab mr create -t T", ("pull_request", "opened", None)),
        ("glab mr note 12 -m hi", ("pull_request", "commented", "!12")),
        ("glab mr approve 12", ("pull_request", "reviewed", "!12")),
        ("glab mr merge !12", ("pull_request", "merged", "!12")),
        ("glab issue note 4 -m hi", ("issue", "commented", "#4")),
        (
            "gh api repos/acme/api/pulls/5/reviews -f event=APPROVE",
            ("pull_request", "reviewed", "#5"),
        ),
        ("gh api -X POST repos/acme/api/issues", ("issue", "issue_opened", None)),
    ],
)
def test_gh_and_glab_write_verbs_are_classified(
    command: str, expected: tuple[str, str, str | None]
) -> None:
    found = classify_shell_command(command, remote=REMOTE)
    assert found is not None
    assert (found["kind"], found["action"], found["ref"]) == expected
    assert found["via"] == "cli"
    assert found["repo"] == "acme/api"


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 3",
        "gh pr list",
        "gh pr checks 3",
        "gh pr diff 3",
        "gh issue view 3",
        "glab mr view 3",
        "gh api repos/acme/api/pulls/5",
        "echo gh pr create",
        "ls -la",
        "",
    ],
)
def test_reads_and_other_commands_are_not_work(command: str) -> None:
    assert classify_shell_command(command, remote=REMOTE) is None


def test_cli_reference_repo_and_url_forms() -> None:
    url = classify_shell_command(
        "gh issue comment https://github.com/Other/Repo/issues/3 --body secret-body"
    )
    assert url is not None and (url["repo"], url["ref"]) == ("other/repo", "#3")
    assert work_url(url) == "https://github.com/other/repo/issues/3"
    named = classify_shell_command("gh pr merge 8 -R octo/cat", remote=REMOTE)
    assert named is not None and named["repo"] == "octo/cat"
    # The work tree's host is not assumed for another repository.
    assert "host" not in named
    gitlab = classify_shell_command(
        "glab mr note 12 -m hi", remote="gitlab.example.com/grp/sub/proj"
    )
    assert gitlab is not None and work_key(gitlab) == (
        "pull_request",
        "grp/sub/proj!12",
    )
    assert (
        work_url(gitlab)
        == "https://gitlab.example.com/grp/sub/proj/-/merge_requests/12"
    )


def test_cli_classification_keeps_only_verb_reference_and_repo() -> None:
    found = classify_shell_command(
        "gh pr create --title 'Ship it' --body 'Fixes #7 and mentions #9. token=abc'",
        remote=REMOTE,
    )
    assert found is not None
    assert found["closes"] == ["acme/api#7"] and found["mentions"] == ["acme/api#9"]
    flat = repr(found)
    assert "Ship it" not in flat and "token" not in flat and "--body" not in flat
    assert set(found) <= {
        "via",
        "kind",
        "action",
        "ref",
        "repo",
        "host",
        "tool",
        "closes",
        "mentions",
        "review_state",
    }


def test_builtin_tools_are_classified_and_reads_excluded() -> None:
    review = classify_tool_call(
        "preloop-mcp",
        "update_pull_request",
        {
            "pull_request": "https://github.com/acme/api/pull/9",
            "review_action": "approve",
        },
    )
    assert review is not None
    assert (review["action"], review["ref"], review["review_state"]) == (
        "reviewed",
        "#9",
        "approved",
    )
    edit = classify_tool_call(
        "preloop-mcp",
        "update_pull_request",
        {"pull_request": "acme/api#9", "title": "x"},
    )
    assert edit is not None and edit["action"] == "pull_request_updated"
    issue = classify_tool_call("preloop-mcp", "update_issue", {"issue": "acme/api#3"})
    assert issue is not None and work_key(issue) == ("issue", "acme/api#3")
    comment = classify_tool_call(
        "preloop-mcp",
        "update_comment",
        {"target": "https://gitlab.com/g/p/-/merge_requests/4", "comment_id": "1"},
    )
    assert comment is not None and work_key(comment) == ("pull_request", "g/p!4")
    opened = classify_tool_call(
        "preloop-mcp",
        "create_pull_request",
        {"project": "acme/api", "title": "t", "description": "Resolves #5"},
    )
    assert opened is not None and opened["closes"] == ["acme/api#5"]
    created = classify_tool_call(
        "preloop-mcp",
        "create_issue",
        {"project": "acme/api", "title": "t", "description": "d"},
    )
    assert created is not None and created["action"] == "issue_opened"
    for read in ("get_issue", "get_pull_request", "search_issues"):
        assert classify_tool_call("preloop-mcp", read, {"issue": "acme/api#3"}) is None


def test_github_and_gitlab_mcp_tables() -> None:
    gh = classify_tool_call(
        "github",
        "create_pull_request_review",
        {"owner": "acme", "repo": "api", "pullNumber": 4, "event": "REQUEST_CHANGES"},
    )
    assert gh is not None and gh["via"] == "github_mcp"
    assert work_key(gh) == ("pull_request", "acme/api#4")
    assert gh["review_state"] == "changes_requested"
    issue_comment = classify_tool_call(
        "GitHub MCP",
        "add_issue_comment",
        {"owner": "acme", "repo": "api", "issue_number": 3},
    )
    assert issue_comment is not None and work_key(issue_comment) == (
        "issue",
        "acme/api#3",
    )
    # A collision prefix in front of the upstream name still matches.
    prefixed = classify_tool_call(
        "github",
        "gh_merge_pull_request",
        {"owner": "acme", "repo": "api", "pullNumber": 2},
    )
    assert prefixed is not None and prefixed["action"] == "merged"
    gl = classify_tool_call(
        "gitlab",
        "create_note",
        {"project_id": "g/p", "noteable_type": "merge_request", "noteable_iid": 3},
    )
    assert gl is not None and work_key(gl) == ("pull_request", "g/p!3")
    approve = classify_tool_call(
        "gitlab", "approve_merge_request", {"project_id": "g/p", "merge_request_iid": 3}
    )
    assert approve is not None and approve["action"] == "reviewed"
    assert classify_tool_call("github", "get_pull_request", {"owner": "a"}) is None
    assert classify_tool_call("jira", "create_issue", {"project": "X"}) is None


def test_code_action_reads_the_classified_reference() -> None:
    pr = {"work_ref": {"kind": "pull_request", "action": "commented", "ref": "#1"}}
    assert code_action("Bash", pr) == "commented"
    issue = {"work_ref": {"kind": "issue", "action": "commented", "ref": "#1"}}
    assert code_action("add_comment", issue) is None
    edit = {"work_ref": {"kind": "pull_request", "action": "pull_request_updated"}}
    assert code_action("update_pull_request", edit) is None


def test_native_hook_records_only_the_parsed_reference() -> None:
    from preloop.api.endpoints.agent_permission import (
        AgentPermissionCheckRequest,
        _native_work_ref,
    )

    payload = AgentPermissionCheckRequest(
        tool_name="Bash",
        tool_input={"command": "gh pr comment 42 --body 'private words'"},
        source="claude_code",
        repository={"remote": REMOTE},
    )
    found = _native_work_ref(payload)
    assert found is not None and work_key(found) == ("pull_request", "acme/api#42")
    assert "private" not in repr(found)
    other = AgentPermissionCheckRequest(
        tool_name="Read",
        tool_input={"command": "gh pr comment 42"},
        source="claude_code",
    )
    assert _native_work_ref(other) is None


def test_native_hook_row_carries_work_ref_and_broadcasts(monkeypatch: Any) -> None:
    from preloop.api.endpoints import agent_permission
    from preloop.api.endpoints.agent_permission import AgentPermissionCheckRequest

    captured: dict[str, Any] = {}

    class _Activity:
        id = "activity-1"
        timestamp = NOW

    def log_tool_call(db: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _Activity()

    class _Factory:
        def __call__(self) -> "_Factory":
            return self

        def __enter__(self) -> None:
            return None

        def __exit__(self, *args: Any) -> None:
            return None

    identity = agent_permission.PermissionIdentity.__new__(
        agent_permission.PermissionIdentity
    )
    for name, value in {
        "account_id": "acct",
        "api_key_id": None,
        "managed_agent_id": None,
        "managed_agent_name": "agent",
        "runtime_principal_type": "claude_code",
        "runtime_principal_id": "p",
        "flow_id": None,
    }.items():
        object.__setattr__(identity, name, value)
    payload = AgentPermissionCheckRequest(
        tool_name="Bash",
        tool_input={"command": "gh issue close 3 --comment 'secret'"},
        source="claude_code",
        repository={"remote": REMOTE},
    )
    with (
        patch(
            "preloop.models.crud.crud_runtime_session_activity.log_tool_call",
            side_effect=log_tool_call,
        ),
        patch.object(agent_permission, "get_session_factory", return_value=_Factory()),
        patch("preloop.services.account_realtime.emit_account_event") as emitted,
    ):
        agent_permission._record_native_tool_call(
            identity,
            runtime_session_id="session-1",
            payload=payload,
            decision="allow",
            reason=None,
            approval_request_id=None,
            timed_out=False,
            elapsed_ms=3,
        )
    work_ref = captured["metadata"]["work_ref"]
    assert work_key(work_ref) == ("issue", "acme/api#3")
    assert "secret" not in repr(captured["metadata"])
    topics = [
        call.args[0]["topic"]
        if isinstance(call.args[0], dict)
        else getattr(call.args[0], "topic", None)
        for call in emitted.call_args_list
    ]
    assert len(emitted.call_args_list) == 2, topics


# ------------------------------------------------------------ window evidence


def _model(
    db: Session, account_id: Any, identifier: str, provider: str = "openai"
) -> Any:
    row = models.AIModel(
        account_id=account_id,
        name=identifier,
        model_identifier=identifier,
        provider_name=provider,
    )
    db.add(row)
    db.flush()
    return row


def _usage(
    db: Session, account_id: Any, model: Any, minutes_ago: float, **changes: Any
) -> None:
    values: dict[str, Any] = {
        "account_id": account_id,
        "ai_model_id": model.id,
        "endpoint": "/v1/chat/completions",
        "method": "POST",
        "status_code": 200,
        "duration": 0.1,
        "action_type": "model_gateway",
        "timestamp": (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None),
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "estimated_cost": 0.01,
        "currency": "USD",
    }
    db.add(models.ApiUsage(**(values | changes)))
    db.flush()


def _map(db: Session, user: Any, **kwargs: Any) -> dict[str, Any]:
    with patch("preloop.services.fleet_map.get_plugin_manager") as manager:
        manager.return_value.get_service.return_value = None
        graph = build_fleet_map(db, user=user, now=NOW, **kwargs)
    return {node.id: node for node in graph.nodes} | {"_edges": graph.edges}


def test_models_need_gateway_activity_inside_the_window(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, agent = fleet["user"], fleet["agent"]
    used = _model(db_session, user.account_id, "used-model", "openai")
    stale = _model(db_session, user.account_id, "stale-model", "mistral")
    _model(db_session, user.account_id, "never-model", "groq")
    _usage(
        db_session,
        user.account_id,
        used,
        30,
        runtime_principal_type="claude_code",
        runtime_principal_id=agent.session_source_id,
    )
    _usage(db_session, user.account_id, stale, 61)  # just outside one hour
    nodes = _map(db_session, user, active_within_minutes=60)
    assert "model:used-model" in nodes and "provider:openai" in nodes
    assert "model:stale-model" not in nodes and "provider:mistral" not in nodes
    assert "model:never-model" not in nodes
    observed = [
        e
        for e in nodes["_edges"]
        if e.kind == "uses_model"
        and e.source == f"agent:{agent.id}"
        and e.target == "model:used-model"
    ]
    assert observed and observed[0].provenance == "observed"
    assert nodes["model:used-model"].last_active_at is not None
    wider = _map(db_session, user, active_within_minutes=120)
    assert "model:stale-model" in wider
    padded = _map(db_session, user, active_within_minutes=60, include_idle=True)
    assert padded["model:never-model"].idle and padded["model:stale-model"].idle
    assert not padded["model:used-model"].idle
    anytime = _map(db_session, user)
    assert "model:never-model" in anytime and anytime["model:never-model"].idle


def test_people_show_their_stored_avatar(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user = fleet["user"]
    user.avatar_url = "https://lh3.googleusercontent.com/a/example"
    db_session.flush()
    nodes = _map(db_session, user)
    assert nodes[f"user:{user.id}"].avatar_url == user.avatar_url
    user.avatar_url = "javascript:alert(1)"
    db_session.flush()
    assert _map(db_session, user)[f"user:{user.id}"].avatar_url is None


def test_provider_links_to_its_filtered_models_page(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user = fleet["user"]
    model = _model(db_session, user.account_id, "linked-model", "openai")
    _usage(db_session, user.account_id, model, 5)
    nodes = _map(db_session, user, active_within_minutes=60)
    assert nodes["provider:openai"].href == "/console/ai-models?provider=openai"
    assert nodes["model:linked-model"].href == f"/console/ai-models/{model.id}"


# ---------------------------------------------------------------- flow models


def test_flow_is_connected_to_the_models_its_runs_called(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from preloop.models.crud import crud_flow_execution
    from preloop.models.schemas.flow_execution import FlowExecutionCreate

    user, flow = fleet["user"], fleet["flow"]
    configured = _model(db_session, user.account_id, "configured-model")
    routed = _model(db_session, user.account_id, "routed-model", "anthropic")
    flow.ai_model_id = configured.id
    run = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="RUNNING", trigger_event_details={}
        ),
    )
    # A real gateway row of a flow run carries the execution; flow_id may be
    # missing on older credentials.
    _usage(db_session, user.account_id, routed, 10, flow_execution_id=run.id)
    _usage(db_session, user.account_id, routed, 9, flow_execution_id=run.id)
    _usage(db_session, user.account_id, configured, 8, flow_id=flow.id)
    result = build_code_activity(db_session, user=user, active_within_minutes=60)
    item = next(f for f in result.flows if f.id == f"flow:{flow.id}")
    assert item.model_node_ids == ["model:routed-model", "model:configured-model"]
    assert item.model_node_id == "model:routed-model"
    assert item.run_count >= 1


def test_a_running_flow_without_code_activity_is_listed(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from preloop.models.crud import crud_flow, crud_flow_execution
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import FlowExecutionCreate

    user = fleet["user"]
    quiet = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Quiet flow",
            prompt_template="x",
            trigger_event_source="github",
            trigger_event_types=["issue_opened"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=user.account_id,
        ),
        account_id=user.account_id,
    )
    model = _model(db_session, user.account_id, "quiet-model")
    quiet.ai_model_id = model.id
    crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=quiet.id, status="RUNNING", trigger_event_details={}
        ),
    )
    db_session.flush()
    result = build_code_activity(db_session, user=user, active_within_minutes=60)
    item = next(f for f in result.flows if f.id == f"flow:{quiet.id}")
    # No ledger rows yet: the configured model is the fallback.
    assert item.model_node_ids == ["model:quiet-model"]


# ------------------------------------------------------------ work relations


def _ref(**values: Any) -> dict[str, Any]:
    return {"work_ref": {"via": "cli", "host": "github.com"} | values}


def test_cli_and_mcp_calls_touch_work_items_with_actions(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    session = fleet["session"]
    _call(
        db_session,
        session,
        "Bash",
        6,
        status="allowed",
        metadata=_ref(
            kind="pull_request",
            action="opened",
            ref="#50",
            repo="acme/api",
            closes=["acme/api#7"],
            mentions=["acme/api#9"],
        ),
    )
    _call(
        db_session,
        session,
        "Bash",
        5,
        status="approved",
        metadata=_ref(
            kind="pull_request", action="commented", ref="#50", repo="acme/api"
        ),
    )
    _call(
        db_session,
        session,
        "Bash",
        4,
        status="denied",  # never ran
        metadata=_ref(kind="pull_request", action="merged", ref="#50", repo="acme/api"),
    )
    _call(
        db_session,
        session,
        "add_issue_comment",
        3,
        metadata={
            "work_ref": {
                "via": "github_mcp",
                "kind": "issue",
                "action": "commented",
                "ref": "#9",
                "repo": "acme/api",
            }
        },
    )
    _call(
        db_session,
        session,
        "Bash",
        200,  # outside the window
        status="allowed",
        metadata=_ref(kind="issue", action="issue_opened", ref="#99", repo="acme/api"),
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    agent = f"agent:{fleet['agent'].id}"
    items = {item.id: item for item in result.work}
    pr = items["work:pull_request:acme/api#50"]
    assert pr.href == "https://github.com/acme/api/pull/50"
    assert "work:issue:acme/api#99" not in items
    relations = {r.id: r for r in result.relations}
    touched = relations[f"touched:{agent}:work:pull_request:acme/api#50"]
    assert touched.count == 2 and set(touched.actions) == {"opened", "commented"}
    assert touched.provenance == "cli"
    issue_touch = relations[f"touched:{agent}:work:issue:acme/api#9"]
    assert issue_touch.provenance == "mcp"
    # The closing reference was parsed, but the issue itself was not touched in
    # the window, so the relation has no visible target.
    assert (
        "resolves:work:pull_request:acme/api#50:work:issue:acme/api#7" not in relations
    )
    assert (
        relations["mentions:work:pull_request:acme/api#50:work:issue:acme/api#9"].kind
        == "mentions"
    )
    counts = result.counts[agent]
    assert counts.opened >= 1 and counts.commented >= 1 and counts.merged == 0
    assert counts.issues_commented == 1
    event = next(
        e for e in result.events if e.work_id == "work:pull_request:acme/api#50"
    )
    assert event.source == "cli"


def test_rollup_pair_resolves_and_run_subjects_trigger_and_produce(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, flow = fleet["user"], fleet["flow"]
    _work_issue(db_session, user, 12, minutes=10)
    rollup = models.IssueCostRollup(
        account_id=user.account_id,
        tracker_id=db_session.query(models.Tracker)
        .filter_by(account_id=user.account_id)
        .first()
        .id,
        issue_key="example/widgets#12",
        issue_url="https://github.com/example/widgets/issues/12",
    )
    db_session.add(rollup)
    db_session.flush()
    db_session.add(
        models.IssueCostPullRequest(
            account_id=user.account_id,
            pr_key="https://github.com/example/widgets/pull/13",
            rollup_id=rollup.id,
            opened_at=NOW - timedelta(minutes=8),
        )
    )
    db_session.add(
        models.FlowExecution(
            flow_id=flow.id,
            status="SUCCEEDED",
            start_time=(NOW - timedelta(minutes=7)).replace(tzinfo=None),
            trigger_event_details={
                "type": "issue_opened",
                "_subject": {
                    "reference": "#12",
                    "repo": "example/widgets",
                    "url": "https://github.com/example/widgets/issues/12",
                },
            },
            result={"pr_url": "https://github.com/example/widgets/pull/13"},
        )
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=user, active_within_minutes=60, now=NOW
    )
    kinds = {(r.kind, r.source, r.target) for r in result.relations}
    issue, pr = "work:issue:example/widgets#12", "work:pull_request:example/widgets#13"
    assert ("resolves", pr, issue) in kinds
    assert ("triggered", f"flow:{flow.id}", issue) in kinds
    assert ("produced", f"flow:{flow.id}", pr) in kinds
    assert len(result.relations) == len({r.id for r in result.relations})


def test_a_synced_issue_is_not_active_because_its_row_was_rewritten(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    issue = _work_issue(db_session, fleet["user"], 21, minutes=60 * 24 * 5)
    issue.updated_at = (NOW - timedelta(minutes=2)).replace(tzinfo=None)
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    assert "example/widgets#21" not in {item.key for item in result.work}


# --------------------------------------------------------------- node totals


def test_usage_totals_cover_agents_people_flows_and_work(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, agent, flow = fleet["user"], fleet["agent"], fleet["flow"]
    model = _model(db_session, user.account_id, "totals-model")
    _usage(
        db_session,
        user.account_id,
        model,
        10,
        runtime_principal_type="claude_code",
        runtime_principal_id=agent.session_source_id,
    )
    _usage(
        db_session, user.account_id, model, 9, flow_execution_id=fleet["execution"].id
    )
    _usage(db_session, user.account_id, model, 8, user_id=user.id)  # direct key use
    _usage(db_session, user.account_id, model, 120)  # outside
    _call(
        db_session,
        fleet["session"],
        "Bash",
        6,
        status="allowed",
        metadata=_ref(kind="issue", action="commented", ref="#3", repo="acme/api"),
    )
    db_session.flush()
    usage = build_fleet_usage(db_session, user=user, active_within_minutes=60, now=NOW)
    agent_totals = usage.agents[f"agent:{agent.id}"]
    assert agent_totals.request_count == 1 and agent_totals.input_tokens == 100
    assert agent_totals.cost_is_estimate and agent_totals.currency == "USD"
    assert agent_totals.prs_opened == 1 and agent_totals.review_comments == 1
    assert agent_totals.tool_calls >= 3 and agent_totals.issues_touched == 1
    person = usage.people[f"user:{user.id}"]
    assert person.request_count == 2  # the agent's request plus the direct one
    flow_totals = usage.flows[f"flow:{flow.id}"]
    assert flow_totals.request_count == 1 and flow_totals.prs_reviewed == 1
    # The flow run is linked to acme/api#42; its gateway usage lands there.
    assert usage.work["work:pull_request:acme/api#42"].request_count == 1
    assert usage.work["work:issue:acme/api#3"].tool_calls == 1


def test_usage_counts_approvals_per_agent_and_flow(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from types import SimpleNamespace

    agent, flow = fleet["agent"], fleet["flow"]
    rows = [
        SimpleNamespace(
            managed_agent_id=agent.id, flow_id=None, status="approved", count=2
        ),
        SimpleNamespace(
            managed_agent_id=agent.id, flow_id=None, status="declined", count=1
        ),
        SimpleNamespace(
            managed_agent_id=agent.id, flow_id=None, status="expired", count=1
        ),
        SimpleNamespace(
            managed_agent_id=agent.id, flow_id=None, status="cancelled", count=5
        ),
        SimpleNamespace(
            managed_agent_id=None, flow_id=flow.id, status="pending", count=1
        ),
        SimpleNamespace(
            managed_agent_id="00000000-0000-0000-0000-000000000000",
            flow_id=None,
            status="approved",
            count=9,
        ),  # not visible
    ]
    with patch("preloop.models.crud.fleet_activity.approval_counts", return_value=rows):
        usage = build_fleet_usage(
            db_session, user=fleet["user"], active_within_minutes=60, now=NOW
        )
    totals = usage.agents[f"agent:{agent.id}"]
    assert (
        totals.approvals_requested,
        totals.approvals_approved,
        totals.approvals_declined,
    ) == (4, 2, 2)
    assert usage.flows[f"flow:{flow.id}"].approvals_requested == 1
    assert "agent:00000000-0000-0000-0000-000000000000" not in usage.agents


def test_approval_counts_query_runs(db_session: Session, fleet: dict[str, Any]) -> None:
    from preloop.models.crud.fleet_activity import approval_counts

    assert (
        approval_counts(
            db_session,
            account_id=fleet["user"].account_id,
            since=NOW - timedelta(hours=1),
            until=NOW,
            limit=10,
        )
        == []
    )


def test_a_model_used_only_by_a_flow_run_stays_on_the_windowed_map(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user = fleet["user"]
    model = _model(db_session, user.account_id, "flow-only-model", "anthropic")
    _usage(
        db_session, user.account_id, model, 5, flow_execution_id=fleet["execution"].id
    )
    nodes = _map(db_session, user, active_within_minutes=60)
    assert "model:flow-only-model" in nodes and "provider:anthropic" in nodes


def test_device_totals_sum_the_agents_it_hosts(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    user, agent = fleet["user"], fleet["agent"]
    model = _model(db_session, user.account_id, "device-model")
    _usage(
        db_session,
        user.account_id,
        model,
        5,
        runtime_principal_type="claude_code",
        runtime_principal_id=agent.session_source_id,
    )
    projection = {
        "truncated": False,
        "items": [
            {"device_key": "fp:laptop", "agents": [{"managed_agent_id": str(agent.id)}]}
        ],
    }
    with patch("preloop.plugins.base.get_plugin_manager") as manager:
        manager.return_value.get_service.return_value.read.return_value = projection
        usage = build_fleet_usage(
            db_session, user=user, active_within_minutes=60, now=NOW
        )
    device = usage.devices["device:fp:laptop"]
    assert device.request_count == usage.agents[f"agent:{agent.id}"].request_count == 1


# ------------------------------------------------------- review regressions


def test_node_source_cap_marks_usage_truncated_even_when_models_fit(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from types import SimpleNamespace

    from preloop.services import fleet_usage

    rows = [
        SimpleNamespace(
            principal_type="x", principal_id=str(i), flow_id=None, tool_calls=1
        )
        for i in range(3)
    ]
    with (
        patch.object(fleet_usage, "NODE_LIMIT", 2),
        patch("preloop.models.crud.fleet_activity.tool_call_counts", return_value=rows),
    ):
        usage = build_fleet_usage(
            db_session, user=fleet["user"], active_within_minutes=60, now=NOW
        )
    assert usage.truncated and usage.partial


def test_unresolved_reference_does_not_hide_a_tracker_key_issue(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    work_ref = classify_tool_call("preloop-mcp", "update_issue", {"issue": "proj-123"})
    assert work_ref is not None and work_key(work_ref) == ("issue", "PROJ-123")
    _call(
        db_session, fleet["session"], "update_issue", 3, metadata={"work_ref": work_ref}
    )
    db_session.flush()
    result = build_code_activity(
        db_session, user=fleet["user"], active_within_minutes=60, now=NOW
    )
    assert "work:issue:PROJ-123" in {item.id for item in result.work}
    assert any(r.target == "work:issue:PROJ-123" for r in result.relations)


def test_a_slug_comment_target_is_a_pull_request() -> None:
    found = classify_tool_call("preloop-mcp", "add_comment", {"target": "acme/api#42"})
    assert found is not None and work_key(found) == ("pull_request", "acme/api#42")


def test_gh_api_input_body_is_an_implicit_write() -> None:
    found = classify_shell_command(
        "gh api repos/acme/api/issues/1/comments --input body.json"
    )
    assert found is not None and found["action"] == "commented"


def test_flow_cap_keeps_the_most_recent_and_says_truncated(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    from preloop.models.crud import crud_flow, crud_flow_execution
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import FlowExecutionCreate
    from preloop.services import fleet_code_activity

    user = fleet["user"]
    newer = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Newer flow",
            prompt_template="x",
            trigger_event_source="github",
            trigger_event_types=["issue_opened"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=user.account_id,
        ),
        account_id=user.account_id,
    )
    crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=newer.id, status="RUNNING", trigger_event_details={}
        ),
    )
    db_session.flush()
    with patch.object(fleet_code_activity, "FLOW_LIMIT", 1):
        result = build_code_activity(
            db_session, user=fleet["user"], active_within_minutes=60
        )
    assert [f.id for f in result.flows] == [f"flow:{newer.id}"] and result.truncated


def test_flows_the_authorizer_denies_are_not_returned(
    db_session: Session, fleet: dict[str, Any]
) -> None:
    flow = f"flow:{fleet['flow'].id}"
    model = _model(db_session, fleet["user"].account_id, "hidden-flow-model")
    _usage(db_session, fleet["user"].account_id, model, 5, flow_id=fleet["flow"].id)
    deny = lambda db, user, kind, rows: [] if kind == "flow" else list(rows)  # noqa: E731
    with patch("preloop.plugins.account_hooks.filter_viewable", side_effect=deny):
        result = build_code_activity(
            db_session, user=fleet["user"], active_within_minutes=60
        )
        usage = build_fleet_usage(
            db_session, user=fleet["user"], active_within_minutes=60
        )
    assert flow not in {f.id for f in result.flows}
    assert flow not in result.counts
    assert all(flow not in item.node_ids for item in result.work)
    assert all(r.source != flow for r in result.relations)
    assert all(e.node_id != flow for e in result.events)
    assert flow not in usage.flows
