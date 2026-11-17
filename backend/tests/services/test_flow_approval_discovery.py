"""Lean tool discovery preserves every known approval path, without inference."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.flow_approval_discovery import flow_may_use_approval_status
from preloop.services.subject_governance import set_subject_governance


@pytest.fixture
def discovery(mocker: MockerFixture) -> dict[str, Any]:
    """An allowed PR read in an account with an unrelated approval workflow."""
    prefix = "preloop.services.flow_approval_discovery.crud."
    mocker.patch(
        prefix + "crud_approval_request.get_multi_by_execution", return_value=[]
    )
    mocker.patch(
        prefix + "crud_approval_workflow.get_multi_by_account", return_value=[object()]
    )
    mocker.patch(prefix + "crud_approval_workflow.get_default", return_value=None)
    mocker.patch(prefix + "crud_tool_access_rule.has_approval_path", return_value=False)
    return {
        "db": MagicMock(spec=Session),
        "context": UserContext(
            user_id="fixture-user",
            account_id="fixture-account",
            username="fixture",
            flow_id="fixture-flow",
            flow_execution_id="fixture-execution",
        ),
        "allowed": {"get_pull_request"},
        "configs": [],
        "account_meta": {},
        "proxied_names": set(),
    }


def test_unrelated_workflow_does_not_add_tool(discovery: dict[str, Any]) -> None:
    """Having workflows alone is not a reason to advertise approval polling."""
    assert not flow_may_use_approval_status(**discovery)


@pytest.mark.parametrize("tool", ["ask_user", "request_approval", "send_note"])
def test_standalone_approval_tools(discovery: dict[str, Any], tool: str) -> None:
    """Human checkpoints can use a named workflow supplied at call time."""
    discovery["allowed"] = {tool}
    assert flow_may_use_approval_status(**discovery)


def test_pending_or_completed_approval_survives_policy_removal(
    discovery: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    """A resumed execution must still be able to retrieve its approved result."""
    mocker.patch(
        "preloop.services.flow_approval_discovery.crud.crud_approval_request.get_multi_by_execution",
        return_value=[object()],
    )
    mocker.patch(
        "preloop.services.flow_approval_discovery.crud.crud_approval_workflow.get_multi_by_account",
        return_value=[],
    )
    assert flow_may_use_approval_status(**discovery)


def test_no_workflows_means_no_new_approval(
    discovery: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    """Even standalone requests cannot trigger a nonexistent workflow."""
    discovery["allowed"] = {"request_approval"}
    mocker.patch(
        "preloop.services.flow_approval_discovery.crud.crud_approval_workflow.get_multi_by_account",
        return_value=[],
    )
    assert not flow_may_use_approval_status(**discovery)


def test_tool_workflow_and_alias(discovery: dict[str, Any]) -> None:
    """Legacy alias configuration still gates the canonical function."""
    discovery["allowed"] = {"search_issues"}
    discovery["configs"] = [
        models.ToolConfiguration(
            tool_name="search",
            tool_source="builtin",
            approval_workflow_id="fixture-workflow",
        )
    ]
    assert flow_may_use_approval_status(**discovery)
    discovery["allowed"] = {"get_pull_request"}
    assert not flow_may_use_approval_status(**discovery)


@pytest.mark.parametrize("enabled", [True, False])
def test_scoped_rule(discovery: dict[str, Any], enabled: bool) -> None:
    """Per-flow rules retain the helper only when enabled and in scope."""
    discovery["account_meta"] = set_subject_governance(
        {},
        subject_type="flows",
        subject_id="fixture-flow",
        config={
            "tool_rules": {
                "get_pull_request": [
                    {
                        "action": "require_approval",
                        "is_enabled": enabled,
                        "approval_workflow_id": "fixture-workflow",
                    }
                ]
            }
        },
    )
    assert flow_may_use_approval_status(**discovery) is enabled


def test_tool_access_rule(discovery: dict[str, Any], mocker: MockerFixture) -> None:
    """A rule may carry a workflow even when the tool has no default."""
    discovery["configs"] = [
        models.ToolConfiguration(
            id="fixture-config", tool_name="get_pull_request", tool_source="builtin"
        )
    ]
    mocker.patch(
        "preloop.services.flow_approval_discovery.crud.crud_tool_access_rule.has_approval_path",
        return_value=True,
    )
    assert flow_may_use_approval_status(**discovery)


@pytest.mark.parametrize("target", ["tool.args", "tool.result"])
@pytest.mark.parametrize("tool", ["get_pull_request", "get_issue"])
def test_sensitive_data_approval_scope(
    discovery: dict[str, Any],
    target: str,
    tool: str,
) -> None:
    """Argument and result approvals are both possible after discovery."""
    discovery["account_meta"] = {
        "sensitive_data": {
            "rules": [
                {
                    "id": "fixture",
                    "on": [target],
                    "scope": {"tools": [tool]},
                    "action": "require_approval",
                    "approval_workflow": "review",
                }
            ]
        }
    }
    assert flow_may_use_approval_status(**discovery) is (tool == "get_pull_request")


def test_external_policy_is_conservative(discovery: dict[str, Any]) -> None:
    """Resource-owner and grant policy can require approval at dispatch time."""
    discovery["proxied_names"] = {"external_read"}
    assert not flow_may_use_approval_status(**discovery)
    discovery["allowed"] = {"external_read"}
    assert flow_may_use_approval_status(**discovery)


def test_empty_allow_list_never_adds_helper(discovery: dict[str, Any]) -> None:
    """Read-only native reviews must retain their empty MCP surface."""
    discovery["allowed"] = set()
    assert not flow_may_use_approval_status(**discovery)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["default", "lean"])
@pytest.mark.parametrize("available", [True, False, "unavailable"])
async def test_server_applies_discovery_only_to_lean(
    mocker: MockerFixture,
    profile: str,
    available: bool | str,
) -> None:
    """Metadata failure keeps replay, while default never evaluates the new path."""
    from fastmcp import FastMCP
    from fastmcp.tools import Tool
    from sqlalchemy.exc import SQLAlchemyError
    from unittest.mock import AsyncMock

    from preloop.services.dynamic_fastmcp import DynamicFastMCP

    context = UserContext(
        user_id="fixture",
        account_id="fixture",
        username="fixture",
        has_tracker=True,
        tracker_types=["github"],
        allowed_flow_tools=["get_pull_request"],
        codex_tools=profile,
    )
    server = DynamicFastMCP("fixture")
    server.set_user_context_provider(lambda: context)
    mocker.patch.object(
        FastMCP,
        "list_tools",
        new=AsyncMock(
            return_value=[
                Tool(name=name, description="fixture", parameters={})
                for name in ("get_pull_request", "get_approval_status", "create_issue")
            ]
        ),
    )
    mocker.patch(
        "preloop.services.dynamic_fastmcp.get_db",
        side_effect=lambda: iter([MagicMock()]),
    )
    mocker.patch(
        "preloop.services.mcp_tool_discovery._get_proxied_tools_sync", return_value=[]
    )
    mocker.patch(
        "preloop.models.crud.crud_tool_configuration.get_multi_by_account",
        return_value=[],
    )
    mocker.patch(
        "preloop.models.crud.crud_account.get", return_value=MagicMock(meta_data={})
    )
    probe = mocker.patch(
        "preloop.services.flow_approval_discovery.flow_may_use_approval_status",
        return_value=available,
    )
    if available == "unavailable":
        probe.side_effect = SQLAlchemyError("fixture metadata unavailable")
    result = {tool.name for tool in await server.list_tools()}
    assert result == (
        {"get_pull_request"}
        if profile == "lean" and available is False
        else {"get_pull_request", "get_approval_status"}
    )
    assert probe.call_count == (1 if profile == "lean" else 0)


@pytest.mark.parametrize("default_available", [False, True])
def test_access_rules_are_batched_for_allowed_builtin_configs(
    discovery: dict[str, Any], mocker: MockerFixture, default_available: bool
) -> None:
    """Broad allowlists perform one account-scoped access-rule existence lookup."""
    discovery["allowed"] = {"get_issue", "get_pull_request", "search_issues"}
    discovery["configs"] = [
        models.ToolConfiguration(id=identifier, tool_name=name, tool_source=source)
        for identifier, name, source in (
            ("issue-config", "get_issue", "builtin"),
            ("pr-config", "get_pull_request", "builtin"),
            ("search-config", "search", "builtin"),
            ("disallowed-config", "create_issue", "builtin"),
            ("external-config", "get_issue", "mcp"),
        )
    ]
    mocker.patch(
        "preloop.services.flow_approval_discovery.crud.crud_approval_workflow.get_default",
        return_value=object() if default_available else None,
    )
    lookup = mocker.patch(
        "preloop.services.flow_approval_discovery.crud.crud_tool_access_rule.has_approval_path",
        return_value=True,
    )
    assert flow_may_use_approval_status(**discovery)
    lookup.assert_called_once_with(
        discovery["db"],
        config_ids=["issue-config", "pr-config", "search-config"],
        account_id="fixture-account",
        has_default_workflow=default_available,
    )
