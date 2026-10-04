"""Tool summaries preserve policy semantics without sending input definitions."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.api.endpoints import tools
from preloop.models import models


@pytest.fixture
def catalogue(mocker: Any) -> tuple[Any, ...]:
    """Include external, native, configured and agent-scoped tool variants."""
    account = SimpleNamespace(id=uuid4())
    servers = [models.MCPServer(id=uuid4(), name=f"Server {i}") for i in range(20)]
    external = [
        models.MCPTool(
            mcp_server_id=server.id,
            name=f"external_{i}",
            description="External tool",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "x" * 4096}},
            },
        )
        for i, server in enumerate(servers)
    ]
    config_id, workflow_id, agent_id = uuid4(), uuid4(), uuid4()
    configs = [
        SimpleNamespace(
            id=config_id,
            tool_name="external_0",
            tool_source="mcp",
            mcp_server_id=servers[0].id,
            managed_agent_id=None,
            is_enabled=False,
            approval_workflow_id=workflow_id,
            justification_mode="required",
        ),
        SimpleNamespace(
            id=uuid4(),
            tool_name="permission_prompt",
            tool_source="builtin",
            mcp_server_id=None,
            managed_agent_id=agent_id,
            is_enabled=True,
        ),
    ]
    rule = SimpleNamespace(
        id=uuid4(),
        tool_configuration_id=config_id,
        action="require_approval",
        condition_expression="amount > 10",
        condition_type="cel",
        priority=1,
        description="Review large calls",
        is_enabled=True,
        approval_workflow_id=workflow_id,
    )
    mocker.patch.object(
        tools.crud_tool_configuration, "get_multi_by_account", return_value=configs
    )
    mocker.patch.object(
        tools.crud_tool_access_rule, "get_multi_by_account", return_value=[rule]
    )
    mocker.patch.object(tools.crud_tracker, "get_for_account", return_value=[])
    mocker.patch.object(
        tools.crud_mcp_server, "get_active_by_account", return_value=servers
    )
    batch = mocker.patch.object(
        tools.crud_mcp_tool, "get_by_servers_for_account", return_value=external
    )
    individual = mocker.patch.object(
        tools.crud_mcp_tool, "get_by_server", side_effect=AssertionError("N+1 lookup")
    )
    return account, servers, batch, individual, agent_id


def test_summary_preserves_policy_and_token_estimates_without_schemas(
    catalogue: tuple[Any, ...],
) -> None:
    account, servers, batch, individual, agent_id = catalogue
    db, user = MagicMock(), MagicMock()
    full = tools.list_all_tools(account=account, current_user=user, db=db)
    batch.reset_mock()
    summary = tools.list_tool_summaries(account=account, current_user=user, db=db)
    batch.assert_called_once_with(
        db, account_id=str(account.id), server_ids=[s.id for s in servers]
    )
    individual.assert_not_called()
    assert len(summary) == len(full)
    for original, projected in zip(full, summary, strict=True):
        row = projected.model_dump()
        assert "schema" not in row
        assert "parameters" not in row
        for key, value in original.items():
            if key not in {"schema", "parameters"}:
                assert row[key] == value
    external = next(row for row in summary if row.name == "external_0")
    assert external.is_enabled is False
    assert external.has_approval_condition is True
    assert external.justification_mode == "required"
    assert external.access_rules[0]["condition_expression"] == "amount > 10"
    assert external.schema_tokens_estimate > 100
    prompt = next(row for row in summary if row.name == "permission_prompt")
    assert prompt.is_enabled is False
    assert prompt.enabled_for_agents == [str(agent_id)]
    assert (
        len(json.dumps([row.model_dump() for row in summary]))
        < len(json.dumps(full)) / 2
    )


def test_summary_has_typed_openapi_response() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(tools.router)
    schema = app.openapi()
    summary_response = schema["paths"]["/tools/summary"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert summary_response["items"]["$ref"].endswith("/ToolSummaryResponse")
    properties = schema["components"]["schemas"]["ToolSummaryResponse"]["properties"]
    assert "schema" not in properties
    assert "parameters" not in properties
