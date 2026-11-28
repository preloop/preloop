"""``ci_health_report`` over the registered MCP server, with the ledger in flow state."""

from contextlib import contextmanager, nullcontext
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from preloop.models import models
from preloop.models.crud import crud_api_key
from preloop.models.crud import flow_state as crud_flow_state
from preloop.services import flow_state_tools
from preloop.services.ci_health import ledger as ledger_mod
from preloop.services.ci_health import tool as ci_tool
from preloop.services.ci_health.transport import Budget
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools
from tests.services.ci_health.conftest import FakeCi, make_providers

TOOLS = ["ci_health_report", "get_flow_state"]


@pytest_asyncio.fixture
async def run(
    db_session: Session,
    test_user: models.User,
    test_tracker: models.Tracker,
    fake_ci: FakeCi,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    policy_engine = create_async_engine(
        db_session.get_bind().engine.url.set(drivername="postgresql+asyncpg"),
        poolclass=NullPool,
    )
    monkeypatch.setattr(
        "preloop.models.db.session.get_async_session_factory",
        lambda: async_sessionmaker(policy_engine),
    )
    org = models.Organization(
        name="example-org", identifier="example-org", tracker_id=test_tracker.id
    )
    db_session.add(org)
    db_session.flush()
    project = models.Project(
        name="app", identifier="9001", slug="example-org/app", organization_id=org.id
    )
    db_session.add(project)
    db_session.flush()
    flow = models.Flow(
        name="CI health",
        account_id=test_user.account_id,
        trigger_event_source="schedule",
        schedule_config={"type": "interval", "every": 2, "unit": "hours"},
        prompt_template="Review CI",
        agent_type="codex",
        agent_config={},
        git_clone_config={
            "enabled": True,
            "repositories": [
                {"tracker_id": str(test_tracker.id), "project_id": str(project.id)}
            ],
        },
        allowed_mcp_tools=[{"name": n} for n in TOOLS],
    )
    db_session.add(flow)
    db_session.flush()
    earlier = models.FlowExecution(
        flow_id=flow.id,
        status="SUCCEEDED",
        result={
            "status": "success",
            "pr_url": "https://github.com/example-org/app/pull/7",
            "change": {"estimated_runner_minutes_saved_per_week": 30},
        },
    )
    execution = models.FlowExecution(
        flow_id=flow.id,
        status="RUNNING",
        trigger_event_details={
            "payload": {
                "precheck": {
                    "decision": "run",
                    "checks": {
                        "new_failures": {
                            "matched": True,
                            "items": [
                                {
                                    "url": "https://github.com/example-org/app/"
                                    "actions/runs/101"
                                }
                            ],
                        }
                    },
                }
            }
        },
    )
    db_session.add_all([earlier, execution])
    db_session.flush()
    key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Flow run",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=["mcp:read", "mcp:write"],
        commit=False,
        context_data={
            "flow_execution_id": str(execution.id),
            "flow_id": str(flow.id),
            "allowed_mcp_tools": flow.allowed_mcp_tools,
        },
    )
    actor = UserContext(
        api_key_id=str(key.id),
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        username=test_user.username,
        has_tracker=True,
        tracker_types=["github"],
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        allowed_flow_tools=list(TOOLS),
    )
    server = initialize_mcp_with_tools()
    server.set_user_context_provider(lambda: actor)
    monkeypatch.setattr(flow_state_tools, "get_current_user_context", lambda: actor)
    factory = lambda: lambda: nullcontext(db_session)  # noqa: E731
    monkeypatch.setattr(flow_state_tools, "get_session_factory", factory)
    open_sessions = {"n": 0}

    @contextmanager
    def counted() -> Any:
        open_sessions["n"] += 1
        try:
            yield db_session
        finally:
            open_sessions["n"] -= 1

    monkeypatch.setattr(ci_tool, "get_session_factory", lambda: counted)
    real_collect = ci_tool.collect

    async def collect_without_session(*args: Any, **kwargs: Any) -> Any:
        # No database connection is held while the provider is read.
        assert open_sessions["n"] == 0
        return await real_collect(*args, **kwargs)

    monkeypatch.setattr(ci_tool, "collect", collect_without_session)
    policy = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(flow_state_tools, "require_approval", policy)
    monkeypatch.setattr(server, "_persist_tool_call_activity", lambda *a, **kw: None)
    tracker_client = AsyncMock()
    monkeypatch.setattr(
        "preloop.api.common.get_tracker_client", AsyncMock(return_value=tracker_client)
    )

    def adapter(client: Any, budget: Budget) -> Any:
        assert client is tracker_client
        github, _ = make_providers(fake_ci, budget)
        return github

    monkeypatch.setattr(ci_tool, "_adapter", adapter)
    fake_ci.pulls["7"] = {"state": "open", "merged_at": None}
    async with Client(server) as client:
        yield client, flow, test_user, policy, actor


async def _call(client: Client, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool("ci_health_report", arguments)
    return result.structured_content


@pytest.mark.asyncio
async def test_report_and_persisted_ledger(run: Any, db_session: Session) -> None:
    client, flow, user, _, _ = run
    report = await _call(
        client, {"critical_path_jobs": ["test*"], "max_failed_logs": 1}
    )
    assert [r["repository"] for r in report["repositories"]] == ["example-org/app"]
    assert report["changed_since_last_run"] is True
    # The pre-check's matched run is read first.
    assert report["precheck_focus"] == [
        "https://github.com/example-org/app/actions/runs/101"
    ]
    assert report["repositories"][0]["failures"][0]["url"].endswith("/job/1011")
    assert report["ledger"]["saved"] is True
    assert report["ledger"]["pull_requests"][0]["url"].endswith("/pull/7")
    assert (
        report["ledger"]["pull_requests"][0]["estimated_minutes_saved_per_week"] == 30
    )
    row = crud_flow_state.get_state(
        db_session,
        account_id=user.account_id,
        flow_id=flow.id,
        key=ledger_mod.LEDGER_KEY,
    )
    assert row is not None and row.version == 1
    signature_id = report["ledger"]["top"][0]["id"]

    again = await _call(
        client,
        {
            "ledger_updates": [
                {
                    "signature_id": signature_id,
                    "status": "ignored",
                    "note": "known upstream outage",
                }
            ]
        },
    )
    assert again["changed_since_last_run"] is False
    assert again["ledger"]["version"] == 2
    db_session.refresh(row)
    assert row.value["signatures"][signature_id]["status"] == "ignored"
    # The agent can read the ledger, but cannot overwrite it.
    read = await client.call_tool("get_flow_state", {"key": ledger_mod.LEDGER_KEY})
    assert read.structured_content["reserved"] is True


@pytest.mark.asyncio
async def test_unknown_repositories_and_bad_arguments(run: Any) -> None:
    client, _, _, _, _ = run
    report = await _call(
        client, {"repositories": ["example-org/app", "example-org/unsynced"]}
    )
    assert report["unavailable"] == [
        {"repository": "example-org/unsynced", "reason": "not synced"}
    ]
    with pytest.raises(ToolError, match="ci_health_no_repository"):
        await _call(client, {"repositories": ["example-org/unsynced"]})
    with pytest.raises(ToolError):
        await _call(client, {"window_hours": 0})


@pytest.mark.asyncio
async def test_policy_records_replayable_arguments(run: Any) -> None:
    """Ledger updates reach the policy record, so an approved call replays."""
    client, _, _, policy, _ = run
    first = await _call(client, {"max_failed_logs": 0})
    signature_id = first["ledger"]["top"][0]["id"]
    update = {"signature_id": signature_id, "status": "ignored", "note": "known"}
    await _call(client, {"ledger_updates": [update]})
    recorded = policy.call_args.kwargs["arguments"]
    assert recorded["ledger_updates"] == [{**update, "linked": None}]
    replayed = await _call(client, {k: v for k, v in recorded.items() if v is not None})
    assert replayed["ledger"]["saved"] is True


def test_tool_requires_a_github_or_gitlab_tracker() -> None:
    from preloop.schemas.ci_health import CI_HEALTH_REPORT_TOOL

    assert CI_HEALTH_REPORT_TOOL["required_tracker_types"] == ["github", "gitlab"]


@pytest.mark.asyncio
async def test_hidden_without_a_github_or_gitlab_tracker(run: Any) -> None:
    client, _, _, _, actor = run
    assert "ci_health_report" in {t.name for t in await client.list_tools()}
    actor.tracker_types = ["jira"]
    actor.mcp_tools_cache = None
    with pytest.raises(ToolError, match="not available"):
        await _call(client, {})
