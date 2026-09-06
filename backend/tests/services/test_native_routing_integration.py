"""Combined routing/native default keeps local model requests outside cloud auth."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow
from preloop.models.schemas.flow import FlowCreate
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.model_routing import prepare_execution_routing


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_policy", [False, True])
async def test_native_default_reaches_local_model_map_and_rejects_resume(
    db_session: Session, test_user: models.User, empty_policy: bool
) -> None:
    model = models.AIModel(
        name="Local model",
        provider_name="cursor",
        model_identifier="team-fast",
        account_id=test_user.account_id,
    )
    db_session.add(model)
    db_session.flush()
    config = {"host_exec_profile": "cursor-ask"}
    if empty_policy:
        config["model_routing"] = {"version": 1, "rules": []}
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name=f"Native routing {uuid4()}",
            prompt_template="question",
            agent_type="cursor",
            ai_model_id=model.id,
            agent_config=config,
            runner_pool="private",
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    details = prepare_execution_routing(db_session, flow, {})
    orch = FlowExecutionOrchestrator(
        db_session, flow_id=flow.id, trigger_event_data=details, nats_client=AsyncMock()
    )
    orch._get_flow_details()
    orch.execution_log = SimpleNamespace(id=uuid4())
    mint = MagicMock(
        side_effect=AssertionError("native must not mint cloud credentials")
    )
    with patch.object(orch, "_create_temporary_api_token", mint):
        context = await orch._prepare_execution_context(resolved_prompt="question")
        assert context["model_identifier"] == "team-fast"
        assert context["agent_config"] == {"host_exec_profile": "cursor-ask"}
        assert "account_api_token" not in context
        orch.trigger_event_data = {**details, "_resume": {"execution_id": str(uuid4())}}
        with pytest.raises(ValueError, match="native resume"):
            await orch._prepare_execution_context(resolved_prompt="question")
    mint.assert_not_called()
