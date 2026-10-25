"""Export never fails on stored rows the policy schema would reject.

The console loads ``GET /policies/export`` on every Policies visit. Rows written
through the configuration API can hold values the YAML schema rejects (a
workflow with no timeout or one outside 30s-24h, an MCP server with a
transport the schema does not list). Export must still render what is stored
so the operator can see and fix it, instead of failing with a 500.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from pytest_mock import MockerFixture

from preloop.services.policy import export_current_policy
from preloop.services.policy.loader import export_policy_to_yaml


def _workflow(**overrides: Any) -> MagicMock:
    workflow = MagicMock()
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "Default approval",
        "description": None,
        "timeout_seconds": 300,
        "require_reason": False,
        "is_default": True,
        "workflow_type": "simple",
        "approvals_required": 1,
        "channel_configs": None,
        "approval_mode": "standard",
        "ai_model": None,
        "ai_guidelines": None,
        "ai_context": None,
        "ai_confidence_threshold": 0.8,
        "ai_fallback_behavior": "escalate",
        "escalation_workflow_id": None,
        "async_approval_enabled": False,
    }
    fields.update(overrides)
    for key, value in fields.items():
        setattr(workflow, key, value)
    return workflow


def _server(**overrides: Any) -> MagicMock:
    server = MagicMock()
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "github",
        "url": "https://mcp.example.com/mcp",
        "transport": "streamable-http",
        "auth_type": "none",
        "auth_config": None,
    }
    fields.update(overrides)
    for key, value in fields.items():
        setattr(server, key, value)
    return server


def _patch_store(
    mocker: MockerFixture,
    *,
    workflows: list[MagicMock] | None = None,
    servers: list[MagicMock] | None = None,
) -> None:
    account = MagicMock()
    account.meta_data = {}
    mocker.patch("preloop.models.crud.crud_account.get", return_value=account)
    mocker.patch(
        "preloop.models.crud.crud_mcp_server.get_active_by_account",
        return_value=servers or [],
    )
    mocker.patch(
        "preloop.models.crud.crud_approval_workflow.get_multi_by_account",
        return_value=workflows or [],
    )
    mocker.patch(
        "preloop.models.crud.crud_approval_workflow.export_recipient_names",
        return_value={},
    )
    mocker.patch(
        "preloop.models.crud.crud_tool_configuration.get_multi_by_account",
        return_value=[],
    )
    mocker.patch("preloop.models.crud.resource_share.policy_section", return_value={})
    mocker.patch("preloop.models.crud.access_rule.policy_section", return_value={})


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": None},
        {"timeout_seconds": 10},
        {"timeout_seconds": 172800},
        {"approvals_required": 0},
        {"approval_mode": "ai_driven", "ai_model": None},
    ],
    ids=["no-timeout", "short-timeout", "long-timeout", "zero-quorum", "ai-no-model"],
)
def test_export_renders_a_workflow_the_schema_rejects(
    mocker: MockerFixture, overrides: dict[str, Any]
) -> None:
    _patch_store(mocker, workflows=[_workflow(**overrides)])

    policy = export_current_policy(MagicMock(), str(uuid.uuid4()))
    content = export_policy_to_yaml(policy, account_name="Acme")

    data = yaml.safe_load(content)
    assert data["approval_workflows"][0]["name"] == "Default approval"


def test_export_renders_an_mcp_server_the_schema_rejects(
    mocker: MockerFixture,
) -> None:
    _patch_store(mocker, servers=[_server(transport="websocket")])

    policy = export_current_policy(MagicMock(), str(uuid.uuid4()))
    data = yaml.safe_load(export_policy_to_yaml(policy))

    assert data["mcp_servers"][0]["name"] == "github"
    assert data["mcp_servers"][0]["transport"] == "websocket"


def test_valid_rows_still_validate(mocker: MockerFixture) -> None:
    _patch_store(mocker, workflows=[_workflow()], servers=[_server()])

    policy = export_current_policy(MagicMock(), str(uuid.uuid4()))

    assert policy.approval_workflows[0].timeout_seconds == 300
    assert policy.mcp_servers[0].transport == "streamable-http"


def _tool_config(**overrides: Any) -> MagicMock:
    rule = MagicMock()
    rule.priority = 0
    rule.is_enabled = True
    rule.condition_expression = "   "
    rule.action = "deny"
    rule.description = "stored with a blank expression"
    config = MagicMock()
    fields: dict[str, Any] = {
        "tool_name": "pay",
        "tool_source": "builtin",
        "mcp_server_id": None,
        "access_rules": [rule],
        "is_enabled": True,
        "approval_workflow_id": None,
        "tool_description": None,
        "justification_mode": "sometimes",
        "custom_config": None,
    }
    fields.update(overrides)
    for key, value in fields.items():
        setattr(config, key, value)
    return config


def test_export_renders_a_tool_and_condition_the_schema_rejects(
    mocker: MockerFixture,
) -> None:
    _patch_store(mocker)
    mocker.patch(
        "preloop.models.crud.crud_tool_configuration.get_multi_by_account",
        return_value=[_tool_config()],
    )

    policy = export_current_policy(MagicMock(), str(uuid.uuid4()))
    data = yaml.safe_load(export_policy_to_yaml(policy))

    assert data["tools"][0]["name"] == "pay"
    assert data["tools"][0]["justification"] == "sometimes"
    assert data["tools"][0]["conditions"][0]["action"] == "deny"


def test_rejected_rows_never_log_their_stored_values(
    mocker: MockerFixture,
) -> None:
    secret = "S3CR3T"
    _patch_store(
        mocker,
        servers=[
            _server(
                auth_type="none",
                auth_config={"introspection": {"url": "x"}, "token": secret},
            )
        ],
        workflows=[
            _workflow(
                approval_mode="ai_driven",
                ai_model=None,
                ai_context={"note": secret},
                channel_configs={"webhook": secret},
            )
        ],
    )

    warning = mocker.patch("preloop.services.policy.loader.logger.warning")

    export_current_policy(MagicMock(), str(uuid.uuid4()), include_credentials=True)

    logged = " ".join(call.args[0] % call.args[1:] for call in warning.call_args_list)
    assert "does not validate" in logged
    assert secret not in logged
