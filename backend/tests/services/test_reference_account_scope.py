"""Stored references retain account scope during execution."""

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_approval_workflow
from preloop.models.crud.approval_workflow import get_approval_workflow_async
from preloop.models.models.flow_execution import (
    MATRIX_OVERRIDES_KEY,
    ROUTING_RECORD_KEY,
)
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.flow_presets_service import clone_preset_for_account


@pytest.mark.parametrize("scope", ["own", "global", "other"])
@pytest.mark.parametrize("fallback", [True, False])
def test_execution_model_account_scope(
    mocker: Any, monkeypatch: Any, caplog: Any, scope: str, fallback: bool
) -> None:
    account_id, model_id = uuid4(), uuid4()
    flow = models.Flow(
        id=uuid4(),
        name="Example flow",
        account_id=account_id,
        ai_model_id=model_id,
        agent_type="openhands",
    )
    model = models.AIModel(
        id=model_id,
        name="Example model",
        model_identifier="example-model",
        account_id=None if scope == "global" else account_id,
    )
    if scope == "other":
        model.account_id = uuid4()
    mocker.patch("preloop.services.flow_orchestrator.crud_flow.get", return_value=flow)
    mocker.patch(
        "preloop.services.flow_orchestrator.resolve_execution_agent_selection",
        return_value=("openhands", uuid4() if fallback else model_id),
    )
    load = mocker.patch(
        "preloop.services.flow_orchestrator.crud_ai_model.get",
        side_effect=[None, model] if fallback else [model],
    )
    orch = FlowExecutionOrchestrator(
        db=MagicMock(), flow_id=flow.id, trigger_event_data={}, nats_client=AsyncMock()
    )
    monkeypatch.setattr(logging.getLogger("preloop"), "propagate", True)
    if scope == "other":
        with (
            caplog.at_level(logging.WARNING),
            pytest.raises(ValueError, match="AI model"),
        ):
            orch._get_flow_details()
        assert orch.ai_model is None
        assert any(
            record.levelno == logging.WARNING and "account" in record.message
            for record in caplog.records
        )
    else:
        orch._get_flow_details()
        assert orch.ai_model is model
    assert load.call_count == (2 if fallback else 1)


@pytest.mark.parametrize("selection_key", [ROUTING_RECORD_KEY, MATRIX_OVERRIDES_KEY])
@pytest.mark.parametrize("scope", ["own", "global", "other", "missing"])
def test_execution_override_model_account_scope(
    mocker: Any, selection_key: str, scope: str
) -> None:
    account_id, default_id, override_id = uuid4(), uuid4(), uuid4()
    flow = models.Flow(
        id=uuid4(),
        name="Example flow",
        account_id=account_id,
        agent_type="openhands",
        ai_model_id=default_id,
    )
    default = models.AIModel(id=default_id, account_id=account_id)
    override = models.AIModel(
        id=override_id, account_id=None if scope == "global" else account_id
    )
    if scope == "other":
        override.account_id = uuid4()
    mocker.patch("preloop.services.flow_orchestrator.crud_flow.get", return_value=flow)
    load = mocker.patch(
        "preloop.services.flow_orchestrator.crud_ai_model.get",
        side_effect=[None, default] if scope == "missing" else [override],
    )
    orch = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=flow.id,
        nats_client=AsyncMock(),
        trigger_event_data={
            selection_key: {"ai_model_id": str(override_id), "agent_type": "openhands"}
        },
    )
    if scope == "other":
        with pytest.raises(ValueError, match="AI model"):
            orch._get_flow_details()
        assert orch.ai_model is None
        load.assert_called_once_with(orch.db, id=str(override_id))
    else:
        orch._get_flow_details()
        assert orch.ai_model is (default if scope == "missing" else override)
        assert load.call_args_list[0].kwargs["id"] == str(override_id)
        if scope == "missing":
            assert load.call_args_list[1].kwargs["id"] == str(default_id)


@pytest.mark.parametrize("scope", ["own", "global", "other", "ordinary"])
def test_clone_service_account_scope(mocker: Any, scope: str) -> None:
    account_id = uuid4()
    preset = models.Flow(
        id=uuid4(),
        name="Example preset",
        is_preset=scope != "ordinary",
        account_id=None if scope == "global" else account_id,
        prompt_template="Review changes",
        agent_type="openhands",
        agent_config={},
    )
    if scope == "other":
        preset.account_id = uuid4()
    crud = mocker.patch("preloop.services.flow_presets_service.crud_flow")
    crud.get_by_name_and_account.return_value = None
    if scope in ("other", "ordinary"):
        with pytest.raises(HTTPException) as caught:
            clone_preset_for_account(
                MagicMock(), preset, account_id, bound_ai_model_id=uuid4()
            )
        assert caught.value.status_code == 404
        crud.create.assert_not_called()
    else:
        clone_preset_for_account(
            MagicMock(), preset, account_id, bound_ai_model_id=uuid4()
        )
        crud.create.assert_called_once()


@pytest.mark.asyncio
async def test_async_workflow_loader_requires_account_scope() -> None:
    account_id, workflow_id = uuid4(), uuid4()
    db = SimpleNamespace(execute=AsyncMock())
    await get_approval_workflow_async(
        db, workflow_id=workflow_id, account_id=account_id
    )
    statement = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "approval_workflow.id =" in str(statement)
    assert "approval_workflow.account_id =" in str(statement)
    assert set(statement.params.values()) == {workflow_id, account_id}


@pytest.mark.asyncio
async def test_async_workflow_loader_account_results(
    db_session: Session, test_user: models.User
) -> None:
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Example organization"}
    )
    own = crud_approval_workflow.create(
        db_session,
        account_id=str(test_user.account_id),
        obj_in={"name": "Own workflow"},
    )
    other = crud_approval_workflow.create(
        db_session, account_id=str(other_account.id), obj_in={"name": "Other workflow"}
    )
    db = SimpleNamespace(execute=AsyncMock(side_effect=db_session.execute))
    assert (
        await get_approval_workflow_async(
            db, workflow_id=own.id, account_id=test_user.account_id
        )
        is own
    )
    assert (
        await get_approval_workflow_async(
            db, workflow_id=other.id, account_id=test_user.account_id
        )
        is None
    )
