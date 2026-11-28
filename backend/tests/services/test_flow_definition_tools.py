"""Dispatch current-flow reads and proposals through the registered MCP server."""

import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from preloop.models import models
from preloop.models.crud import crud_api_key
from preloop.schemas.flow_change import FLOW_DEFINITION_TOOLS
from preloop.services import flow_definition_tools as definitions
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools

pytestmark = pytest.mark.asyncio
NAMES = [tool["name"] for tool in FLOW_DEFINITION_TOOLS]


@dataclass
class Scenario:
    client: Client
    actor: UserContext
    flow: models.Flow
    foreign: models.Flow
    project: str
    policy: AsyncMock

    @property
    def target(self) -> dict[str, str]:
        return {"project_id": self.project, "flow_id": str(self.flow.id)}

    @property
    def proposal(self) -> dict[str, Any]:
        return {
            **self.target,
            "base_revision": 1,
            "changes": {"max_budget": 5.0, "interval_every": 2},
            "reason": "excessive_cost",
            "evidence": {"metric": "cost_usd", "observed_mean": 8.5, "sample_count": 6},
            "expected_outcome": "lower_run_cost",
            "evaluation_window_days": 7,
        }

    async def call(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        """Send a tools/call request and decode the server's structured result."""
        result = await self.client.call_tool(name, {"request": request})
        return result.structured_content


@pytest_asyncio.fixture
async def scope(
    db_session: Session,
    test_user: models.User,
    test_tracker: models.Tracker,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Use the real project, policy scope, flow revision and proposal storage."""
    policy_engine = create_async_engine(
        db_session.get_bind().engine.url.set(drivername="postgresql+asyncpg"),
        poolclass=NullPool,
    )
    monkeypatch.setattr(
        "preloop.models.db.session.get_async_session_factory",
        lambda: async_sessionmaker(policy_engine),
    )
    org = models.Organization(
        name="Example", identifier="example", tracker_id=test_tracker.id
    )
    db_session.add(org)
    db_session.flush()
    project = models.Project(
        name="Project", identifier="project", organization_id=org.id
    )
    db_session.add_all(
        [
            project,
            models.TrackerScopeRule(
                tracker_id=test_tracker.id,
                scope_type="ORGANIZATION",
                rule_type="INCLUDE",
                identifier="example",
            ),
        ]
    )
    db_session.flush()

    def flow(name: str, projects: list[str]) -> models.Flow:
        return models.Flow(
            name=name,
            account_id=test_user.account_id,
            trigger_project_ids=projects,
            trigger_event_source="schedule",
            schedule_config={
                "type": "interval",
                "every": 1,
                "unit": "hours",
                "payload": {"internal": "private-schedule-value"},
            },
            prompt_template="Report usage",
            agent_type="openhands",
            agent_config={
                "limits": {"max_usd": 10},
                "opaque_setting": "private-setting",
            },
            allowed_mcp_tools=[{"name": n} for n in NAMES],
        )

    caller = flow("Review", [str(project.id)])
    target = flow("Report", [str(project.id)])
    foreign = flow("Other", [str(uuid4())])
    shared = flow("Shared", [str(project.id), str(uuid4())])
    db_session.add_all([caller, target, foreign, shared])
    db_session.flush()
    run = models.FlowExecution(flow_id=caller.id, status="RUNNING")
    db_session.add(run)
    db_session.flush()
    key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Flow review",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=["mcp:read", "mcp:write"],
        commit=False,
        context_data={
            "flow_execution_id": str(run.id),
            "flow_id": str(caller.id),
            "allowed_mcp_tools": caller.allowed_mcp_tools,
        },
    )
    actor = UserContext(
        api_key_id=str(key.id),
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        username=test_user.username,
        has_tracker=True,
        flow_id=str(caller.id),
        flow_execution_id=str(run.id),
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(run.id),
        allowed_flow_tools=list(NAMES),
    )
    server = initialize_mcp_with_tools()
    server.set_user_context_provider(lambda: actor)
    monkeypatch.setattr(definitions, "get_current_user_context", lambda: actor)
    monkeypatch.setattr(
        definitions, "get_session_factory", lambda: lambda: nullcontext(db_session)
    )
    policy = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(definitions, "require_approval", policy)
    # The wire dispatcher normally records activity in a separate transaction.
    monkeypatch.setattr(server, "_persist_tool_call_activity", lambda *a, **kw: None)
    async with Client(server) as client:
        yield Scenario(client, actor, target, foreign, str(project.id), policy)


async def test_scoped_wire_list_and_inspect(scope: Scenario) -> None:
    tools = {t.name: t for t in await scope.client.list_tools()}
    assert set(NAMES) <= tools.keys()
    assert {"update_flow", "apply_flow_change"}.isdisjoint(tools)
    listing = await scope.call("inspect_flows", {"project_id": scope.project})
    assert {f["name"] for f in listing["flows"]} == {"Report", "Review"}
    result = await scope.call("inspect_flows", scope.target)
    assert result["revision"] == 1
    assert result["prompt_template"] == "Report usage"
    assert result["run_settings"]["max_budget"] == 10
    assert "private-" not in json.dumps(result)


async def test_durable_review_retry_and_original_base(
    scope: Scenario, db_session: Session
) -> None:
    receipt = await scope.call("propose_flow_change", scope.proposal)
    assert receipt == await scope.call("propose_flow_change", scope.proposal)
    assert db_session.query(models.FlowChangeProposal).count() == 1
    doc = receipt["document"]
    assert doc["diff"] == [
        {"field": "interval_every", "before": 1, "after": 2},
        {"field": "max_budget", "before": 10, "after": 5.0},
    ]
    assert doc["interval_unit"] == "hours"
    assert doc["access_changes"] == []
    assert doc["base_revision"] == scope.flow.config_revision == 1
    assert scope.flow.schedule_config["every"] == 1
    assert doc["proposer_execution_id"] == scope.actor.flow_execution_id
    assert "private-" not in json.dumps(doc)
    scope.flow.timeout_seconds = 600
    db_session.flush()
    review = await scope.call(
        "get_flow_change_proposal", {**scope.target, "digest": receipt["digest"]}
    )
    assert review == {**receipt, "current_revision": 2}
    with pytest.raises(ToolError, match="base revision"):
        await scope.call("propose_flow_change", scope.proposal)


async def test_foreign_scope_and_denied_policy(
    scope: Scenario, db_session: Session
) -> None:
    for target in (
        {"flow_id": str(scope.foreign.id)},
        {"project_id": scope.foreign.trigger_project_ids[0]},
    ):
        with pytest.raises(ToolError, match="unavailable"):
            await scope.call("inspect_flows", {**scope.target, **target})
        with pytest.raises(ToolError, match="unavailable"):
            await scope.call("propose_flow_change", {**scope.proposal, **target})
    scope.policy.return_value = (False, "Denied by tool policy")
    with pytest.raises(ToolError, match="Denied"):
        await scope.call("propose_flow_change", scope.proposal)
    assert db_session.query(models.FlowChangeProposal).count() == 0


async def test_revoked_scope_and_missing_tool(
    scope: Scenario, db_session: Session
) -> None:
    scope.actor.allowed_flow_tools.remove("propose_flow_change")
    with pytest.raises(ToolError):
        await scope.call("propose_flow_change", scope.proposal)
    source = db_session.get(models.Flow, scope.actor.flow_id)
    source.trigger_project_ids = scope.foreign.trigger_project_ids
    db_session.flush()
    with pytest.raises(ToolError, match="unavailable"):
        await scope.call(
            "inspect_flows", {"project_id": scope.foreign.trigger_project_ids[0]}
        )


async def test_invalid_merged_configuration_and_closed_proposals(
    scope: Scenario, db_session: Session
) -> None:
    with pytest.raises(ToolError):
        await scope.call(
            "propose_flow_change",
            {**scope.proposal, "changes": {"custom_commands": {"enabled": True}}},
        )
    with pytest.raises(ToolError):
        await scope.call(
            "propose_flow_change",
            {**scope.proposal, "changes": {"allowed_mcp_tools": []}},
        )
    scope.flow.result_schema = {"type": "invalid"}
    db_session.flush()
    with pytest.raises(ToolError, match="validation"):
        await scope.call("propose_flow_change", {**scope.proposal, "base_revision": 2})
    assert db_session.query(models.FlowChangeProposal).count() == 0


async def test_tampered_document_and_wrong_digest(
    scope: Scenario, db_session: Session
) -> None:
    receipt = await scope.call("propose_flow_change", scope.proposal)
    with pytest.raises(ToolError, match="unavailable"):
        await scope.call(
            "get_flow_change_proposal", {**scope.target, "digest": "0" * 64}
        )
    row = (
        db_session.query(models.FlowChangeProposal)
        .filter_by(digest=receipt["digest"])
        .one()
    )
    row.document = json.dumps({**json.loads(row.document), "evaluation_window_days": 1})
    db_session.flush()
    with pytest.raises(ToolError, match="unavailable"):
        await scope.call(
            "get_flow_change_proposal", {**scope.target, "digest": receipt["digest"]}
        )


async def test_inspection_scrubs_credentials_but_keeps_references(
    scope: Scenario, db_session: Session
) -> None:
    """Redact legacy prompt text and schema defaults without exposing config."""
    reference = str(uuid4())
    marker = "ghp_" + "x" * 36
    scope.flow.prompt_template = "Report with " + marker
    scope.flow.result_schema = {
        "type": "object",
        "properties": {
            "credentials_secret_id": {"const": reference},
            "password": {"default": "private-schema-value"},
        },
    }
    db_session.flush()
    definition = await scope.call("inspect_flows", scope.target)
    serialized = json.dumps(definition)
    assert reference in serialized
    assert marker not in serialized
    assert "private-" not in serialized


async def test_excluded_tracker_and_inactive_member(
    scope: Scenario, db_session: Session, test_user: models.User
) -> None:
    """A cached catalog grants neither tracker scope nor account membership."""
    rule = (
        db_session.query(models.TrackerScopeRule)
        .filter_by(scope_type="ORGANIZATION", identifier="example")
        .one()
    )
    rule.rule_type = "EXCLUDE"
    db_session.flush()
    with pytest.raises(ToolError, match="unavailable"):
        await scope.call("inspect_flows", scope.target)
    rule.rule_type = "INCLUDE"
    test_user.is_active = False
    db_session.flush()
    with pytest.raises(ToolError, match="unavailable"):
        await scope.call("propose_flow_change", scope.proposal)


async def test_wire_proposal_schema_and_bound_principal(scope: Scenario) -> None:
    """Advertise typed review output and reject an unbound assessor."""
    tools = {tool.name: tool for tool in await scope.client.list_tools()}
    assert "document" in tools["propose_flow_change"].output_schema["properties"]
    scope.actor.runtime_principal_id = str(uuid4())
    with pytest.raises(ToolError, match="explicitly authorized"):
        await scope.call("inspect_flows", scope.target)
    scope.policy.assert_not_awaited()


async def test_revoked_credential_and_wrong_grant_source(
    scope: Scenario, db_session: Session
) -> None:
    """A stale authenticated context cannot outlive the underlying grant."""
    key = db_session.get(models.ApiKey, scope.actor.api_key_id)
    key.context_data = {
        **key.context_data,
        "allowed_mcp_tools": [
            {"server_name": "external", "tool_name": "propose_flow_change"}
        ],
    }
    db_session.flush()
    with pytest.raises(ToolError, match="credential unavailable"):
        await scope.call("propose_flow_change", scope.proposal)
    key.is_active = False
    db_session.flush()
    with pytest.raises(ToolError, match="credential unavailable"):
        await scope.call("inspect_flows", scope.target)


async def test_two_pages_after_project_and_visibility_filter(
    scope: Scenario, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [UUID(int=n) for n in range(1, 59)]
    hidden = {ids[3], ids[25], ids[51]}
    db_session.add_all(
        models.Flow(
            id=flow_id,
            name=f"Page case {i}",
            account_id=scope.flow.account_id,
            trigger_project_ids=[scope.project],
            prompt_template="Report",
            agent_config={},
        )
        for i, flow_id in enumerate(ids)
    )
    db_session.flush()
    monkeypatch.setattr(
        definitions,
        "filter_viewable",
        lambda db, user, kind, flows: [f for f in flows if f.id not in hidden],
    )
    one = await scope.call("inspect_flows", {"project_id": scope.project})
    assert len(one["flows"]) == definitions.PAGE_SIZE
    assert one["next_after"] == one["flows"][-1]["flow_id"]
    two = await scope.call(
        "inspect_flows", {"project_id": scope.project, "after": one["next_after"]}
    )
    visible = [UUID(f["flow_id"]) for f in one["flows"] + two["flows"]]
    expected = set(ids).difference(hidden) | {scope.flow.id, UUID(scope.actor.flow_id)}
    assert visible == sorted(expected)
    assert two["next_after"] is None


async def test_timeout_and_iteration_diffs(scope: Scenario) -> None:
    request = {
        **scope.proposal,
        "changes": {"timeout_seconds": 720, "max_iterations": 28},
    }
    receipt = await scope.call("propose_flow_change", request)
    review = await scope.call(
        "get_flow_change_proposal", {**scope.target, "digest": receipt["digest"]}
    )
    assert review["document"]["diff"] == [
        {"field": "max_iterations", "before": None, "after": 28},
        {"field": "timeout_seconds", "before": None, "after": 720},
    ]
    assert review["current_revision"] == request["base_revision"]
    assert (await scope.call("inspect_flows", scope.target))["run_settings"] == {
        "timeout_seconds": None,
        "max_budget": 10,
        "max_iterations": None,
        "interval_every": 1,
    }
