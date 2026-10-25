"""Flow and tool references follow the caller's account scope."""

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import flows, tools
from preloop.models import models, schemas
from preloop.models.crud import crud_account, crud_approval_workflow
from preloop.models.schemas.tool_configuration import (
    ToolConfigurationCreate,
    ToolConfigurationUpdate,
)
from tests.conftest import maybe_await


@pytest.fixture(autouse=True)
def _permission_fallback_passes(mocker: Any) -> None:
    """Scope checks are exercised here; the OSS permission fallback is covered
    by the permission test modules, so let it pass for these synthetic users."""
    for module in (flows, tools):
        mocker.patch.object(module, "ensure_permission_in_oss", create=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize("scope", ["own", "global", "other", "ordinary"])
async def test_clone_preset_account_scope(
    mocker: Any, wrapped: bool, scope: str
) -> None:
    account_id = uuid4()
    preset = models.Flow(
        id=uuid4(),
        name="Example preset",
        account_id=None if scope == "global" else account_id,
        is_preset=scope != "ordinary",
        prompt_template="Review changes",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_tools=[],
    )
    if scope == "other":
        preset.account_id = uuid4()
    crud = mocker.patch.object(flows, "crud_flow")
    crud.get.return_value = preset
    crud.get_by_name_and_account.return_value = None
    bind = mocker.patch.object(
        flows, "_resolve_clone_model_binding", return_value=uuid4()
    )
    endpoint = flows.clone_preset if wrapped else inspect.unwrap(flows.clone_preset)
    kwargs = dict(
        db=MagicMock(),
        flow_id=preset.id,
        current_user=SimpleNamespace(account_id=account_id),
    )
    if scope in ("other", "ordinary"):
        with pytest.raises(HTTPException) as caught:
            await maybe_await(endpoint(**kwargs))
        assert caught.value.status_code == 404
        bind.assert_not_called()
        crud.create.assert_not_called()
    else:
        assert await maybe_await(endpoint(**kwargs)) is crud.create.return_value
        assert crud.create.call_args.kwargs["account_id"] == account_id


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "agent_type", ["openhands", "opencode", "claude-code", "codex"]
)
@pytest.mark.parametrize(
    "scope", ["own", "global", "other", "shared", "missing", "null"]
)
async def test_flow_model_account_scope(
    mocker: Any, operation: str, agent_type: str, scope: str
) -> None:
    account_id, model_id = uuid4(), uuid4()
    user = SimpleNamespace(account_id=account_id, is_superuser=False)
    model = models.AIModel(
        id=model_id,
        account_id=None if scope == "global" else account_id,
        provider_name="openai",
        model_identifier="example-model",
        meta_data={},
    )
    if scope in ("other", "shared"):
        model.account_id = uuid4()
    if scope == "shared":
        mocker.patch(
            "preloop.plugins.account_hooks.extra_visible_ids", return_value=[model_id]
        )
    mocker.patch.object(
        flows.crud_ai_model, "get", return_value=None if scope == "missing" else model
    )
    crud = mocker.patch.object(flows, "crud_flow")
    crud.get_by_name_and_account.return_value = None
    crud.get.return_value = models.Flow(
        id=uuid4(),
        name="Example flow",
        account_id=account_id,
        agent_type=agent_type,
        ai_model_id=None,
        agent_config={},
        webhook_config=None,
        source_preset_id=None,
    )
    mocker.patch.object(flows, "log_config_change")
    values = dict(
        agent_type=agent_type, ai_model_id=None if scope == "null" else model_id
    )
    kwargs = dict(db=MagicMock(), current_user=user)
    if operation == "create":
        endpoint = flows.create_flow
        kwargs["flow_in"] = schemas.FlowCreate(
            name="Example flow",
            prompt_template="Review changes",
            agent_config={},
            **values,
        )
    else:
        endpoint = flows.update_flow
        kwargs.update(
            flow_id=crud.get.return_value.id, flow_in=schemas.FlowUpdate(**values)
        )
    if scope in ("other", "shared", "missing"):
        with pytest.raises(HTTPException) as caught:
            await maybe_await(endpoint(**kwargs))
        assert caught.value.status_code == 422
        crud.create.assert_not_called()
        crud.update.assert_not_called()
    else:
        await maybe_await(endpoint(**kwargs))
        getattr(crud, operation).assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["own", "global", "other"])
async def test_flow_update_preserves_model_scope_when_binding_is_omitted(
    mocker: Any, scope: str
) -> None:
    account_id, model_id = uuid4(), uuid4()
    model = models.AIModel(
        id=model_id, account_id=None if scope == "global" else account_id
    )
    if scope == "other":
        model.account_id = uuid4()
    load = mocker.patch.object(flows.crud_ai_model, "get", return_value=model)
    crud = mocker.patch.object(flows, "crud_flow")
    crud.get_by_name_and_account.return_value = None
    crud.get.return_value = models.Flow(
        id=uuid4(),
        name="Example flow",
        account_id=account_id,
        agent_type="openhands",
        ai_model_id=model_id,
        agent_config={},
        source_preset_id=None,
        webhook_config=None,
    )
    mocker.patch.object(flows, "log_config_change")
    flow_in = schemas.FlowUpdate(name="Renamed flow")
    assert "ai_model_id" not in flow_in.model_fields_set
    kwargs = dict(
        db=MagicMock(),
        flow_id=crud.get.return_value.id,
        flow_in=flow_in,
        current_user=SimpleNamespace(account_id=account_id),
    )
    if scope == "other":
        with pytest.raises(HTTPException) as caught:
            await maybe_await(flows.update_flow(**kwargs))
        assert caught.value.status_code == 422
        crud.update.assert_not_called()
    else:
        await maybe_await(flows.update_flow(**kwargs))
        crud.update.assert_called_once()
    load.assert_called_once_with(kwargs["db"], id=model_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("binding", ["model_routing", "model_by_label"])
async def test_flow_routing_model_account_scope(
    mocker: Any, operation: str, binding: str
) -> None:
    account_id, model_id = uuid4(), uuid4()
    crud = mocker.patch.object(flows, "crud_flow")
    crud.get_by_name_and_account.return_value = None
    crud.get.return_value = models.Flow(
        id=uuid4(), source_preset_id=None, agent_type="openhands", webhook_config=None
    )
    mocker.patch.object(
        flows.crud_ai_model, "get", return_value=models.AIModel(account_id=uuid4())
    )
    config = {"model_by_label": [{"label": "review", "ai_model_id": str(model_id)}]}
    if binding == "model_routing":
        config = {
            binding: {
                "rules": [
                    {
                        "id": "review",
                        "labels": {"any": ["review"]},
                        "ai_model_id": str(model_id),
                        "agent_type": "openhands",
                    }
                ]
            }
        }
    kwargs = dict(db=MagicMock(), current_user=SimpleNamespace(account_id=account_id))
    if operation == "create":
        endpoint = flows.create_flow
        kwargs["flow_in"] = schemas.FlowCreate(
            name="Example flow", prompt_template="Review changes", agent_config=config
        )
    else:
        endpoint = flows.update_flow
        kwargs.update(
            flow_id=crud.get.return_value.id,
            flow_in=schemas.FlowUpdate(agent_config=config),
        )
    with pytest.raises(HTTPException) as caught:
        await maybe_await(endpoint(**kwargs))
    assert caught.value.status_code == 422
    crud.create.assert_not_called()
    crud.update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize(
    "operation", ["config_create", "config_update", "rule_create", "rule_update"]
)
@pytest.mark.parametrize("scope", ["own", "other", "null"])
async def test_tool_workflow_account_scope(
    mocker: Any, wrapped: bool, operation: str, scope: str
) -> None:
    await _tool_reference_case(
        mocker, wrapped, operation, scope, "approval_workflow_id"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["config_create", "config_update"])
@pytest.mark.parametrize("scope", ["own", "other", "null"])
async def test_tool_server_account_scope(
    mocker: Any, operation: str, scope: str
) -> None:
    await _tool_reference_case(mocker, True, operation, scope, "mcp_server_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["config_create", "config_update"])
async def test_tool_server_visibility_keeps_configuration_owner_scoped(
    mocker: Any, operation: str
) -> None:
    visible = mocker.patch.object(
        tools.crud_mcp_server,
        "get_visible",
        return_value=models.MCPServer(account_id=uuid4()),
    )
    await _tool_reference_case(mocker, True, operation, "other", "mcp_server_id")
    visible.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["own", "other"])
async def test_tool_agent_account_scope(mocker: Any, scope: str) -> None:
    account_id, agent_id = uuid4(), uuid4()
    configs = mocker.patch.object(tools, "crud_tool_configuration")
    configs.get_multi_by_account.return_value = []
    load = mocker.patch.object(
        tools.crud_managed_agent,
        "get_for_account",
        return_value=None
        if scope == "other"
        else models.ManagedAgent(account_id=account_id),
    )
    mocker.patch.object(tools.ToolConfigurationResponse, "model_validate")
    kwargs = dict(
        db=MagicMock(),
        account=SimpleNamespace(id=account_id),
        current_user=SimpleNamespace(account_id=account_id),
        config_data=ToolConfigurationCreate(
            tool_name="example",
            account_id=str(account_id),
            managed_agent_id=str(agent_id),
        ),
    )
    if scope == "other":
        with pytest.raises(HTTPException) as caught:
            await tools.create_tool_configuration(**kwargs)
        assert caught.value.status_code == 404
        configs.create.assert_not_called()
    else:
        await tools.create_tool_configuration(**kwargs)
        configs.create.assert_called_once()
    load.assert_called_once_with(
        kwargs["db"], account_id=str(account_id), agent_id=str(agent_id)
    )


async def _tool_reference_case(
    mocker: Any, wrapped: bool, operation: str, scope: str, field: str
) -> None:
    account_id, reference_id, config_id = uuid4(), uuid4(), uuid4()
    db = MagicMock()
    configs = mocker.patch.object(tools, "crud_tool_configuration")
    configs.get_multi_by_account.return_value = []
    rules = mocker.patch.object(tools, "crud_tool_access_rule")
    loader = mocker.patch.object(
        tools.crud_approval_workflow
        if field == "approval_workflow_id"
        else tools.crud_mcp_server,
        "get",
        return_value=None
        if scope == "other"
        else SimpleNamespace(account_id=account_id),
    )
    mocker.patch.object(tools, "log_config_change")
    mocker.patch.object(tools.ToolConfigurationResponse, "model_validate")
    mocker.patch.object(tools, "AccessRuleResponse")
    values = {field: None if scope == "null" else str(reference_id)}
    kwargs = dict(
        db=db,
        account=SimpleNamespace(id=account_id),
        current_user=SimpleNamespace(account_id=account_id),
    )
    if operation == "config_create":
        endpoint = tools.create_tool_configuration
        kwargs["config_data"] = ToolConfigurationCreate(
            tool_name="example", account_id=str(account_id), **values
        )
    elif operation == "config_update":
        endpoint = tools.update_tool_configuration
        kwargs.update(
            config_id=config_id, config_update=ToolConfigurationUpdate(**values)
        )
    elif operation == "rule_create":
        endpoint = tools.create_access_rule
        kwargs.update(
            config_id=config_id,
            rule_in=tools.AccessRuleCreate(action="require_approval", **values),
        )
    else:
        endpoint = tools.update_access_rule
        kwargs.update(rule_id=uuid4(), rule_in=tools.AccessRuleUpdate(**values))
    if not wrapped:
        endpoint = inspect.unwrap(endpoint)
    if scope == "other":
        with pytest.raises(HTTPException) as caught:
            await maybe_await(endpoint(**kwargs))
        assert caught.value.status_code == 404
        for crud in (configs, rules):
            crud.create.assert_not_called()
            crud.update.assert_not_called()
        db.commit.assert_not_called()
    else:
        await maybe_await(endpoint(**kwargs))
        crud = configs if operation.startswith("config") else rules
        getattr(
            crud, "create" if operation.endswith("create") else "update"
        ).assert_called_once()
    if scope == "null":
        loader.assert_not_called()
    else:
        loader.assert_called_once_with(db, id=reference_id, account_id=str(account_id))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["config_create", "config_update", "rule_create", "rule_update"]
)
@pytest.mark.parametrize("scope", ["own", "other", "null"])
async def test_tool_workflow_persistence_scope(
    db_session: Session, test_user: models.User, operation: str, scope: str
) -> None:
    account = crud_account.get(db_session, id=test_user.account_id)
    workflow_id = None
    if scope != "null":
        owner = account
        if scope == "other":
            owner = crud_account.create(
                db_session, obj_in={"organization_name": "Example organization"}
            )
        workflow = crud_approval_workflow.create(
            db_session, account_id=str(owner.id), obj_in={"name": "Example workflow"}
        )
        workflow_id = workflow.id
    config = None
    if operation != "config_create":
        config = tools.crud_tool_configuration.create(
            db_session,
            config_in=ToolConfigurationCreate(
                tool_name="example", account_id=str(account.id)
            ),
        )
    kwargs = dict(db=db_session, account=account, current_user=test_user)
    values = {"approval_workflow_id": str(workflow_id) if workflow_id else None}
    rule = None
    if operation == "config_create":
        endpoint = tools.create_tool_configuration
        kwargs["config_data"] = ToolConfigurationCreate(
            tool_name="example", account_id=str(account.id), **values
        )
    elif operation == "config_update":
        endpoint = tools.update_tool_configuration
        kwargs.update(
            config_id=config.id, config_update=ToolConfigurationUpdate(**values)
        )
    elif operation == "rule_create":
        endpoint = tools.create_access_rule
        kwargs.update(
            config_id=config.id,
            rule_in=tools.AccessRuleCreate(action="require_approval", **values),
        )
    else:
        rule = tools.crud_tool_access_rule.create(
            db_session,
            obj_in={
                "account_id": str(account.id),
                "tool_configuration_id": str(config.id),
                "action": "require_approval",
            },
        )
        endpoint = tools.update_access_rule
        kwargs.update(rule_id=rule.id, rule_in=tools.AccessRuleUpdate(**values))
    if scope == "other":
        with pytest.raises(HTTPException) as caught:
            await endpoint(**kwargs)
        assert caught.value.status_code == 404
        configs = tools.crud_tool_configuration.get_multi_by_account(
            db_session, account_id=str(account.id)
        )
        assert len(configs) == (0 if operation == "config_create" else 1)
        if config:
            db_session.refresh(config)
            assert config.approval_workflow_id is None
            rules = tools.crud_tool_access_rule.get_multi_by_config(
                db_session, config_id=str(config.id), account_id=str(account.id)
            )
            assert len(rules) == (1 if operation == "rule_update" else 0)
            assert all(row.approval_workflow_id is None for row in rules)
    else:
        result = await endpoint(**kwargs)
        assert str(result.approval_workflow_id) == str(workflow_id)
