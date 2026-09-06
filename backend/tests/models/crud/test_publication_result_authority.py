"""Terminal writes preserve current controller state under a real database lock."""

from uuid import uuid4

import pytest
from sqlalchemy import update

from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionUpdate,
)


@pytest.mark.parametrize("phase", ["verifying", "complete"])
def test_stale_execution_result_cannot_replace_durable_publication(
    db_session, create_account, phase
):
    account = create_account()
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name=f"Publication result {uuid4()}",
            prompt_template="Test",
            trigger_event_source="manual",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account.id,
        ),
        account_id=account.id,
    )
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    execution.result = {"_private_publication": {"phase": "agent"}}
    db_session.flush()
    receipt = {"url": "https://github.com/example/project/pull/1", "head_sha": "a" * 40}
    protected = {"phase": phase, "nonce": "b" * 64, "receipt": receipt}
    current = {"_private_publication": protected, "trusted_publication": receipt}
    # Model another controller write while this ORM instance remains stale.
    db_session.execute(
        update(models.FlowExecution)
        .where(models.FlowExecution.id == execution.id)
        .values(result=current)
        .execution_options(synchronize_session=False)
    )
    assert execution.result["_private_publication"]["phase"] == "agent"
    crud_flow_execution.update(
        db_session,
        execution,
        FlowExecutionUpdate(
            status="SUCCEEDED",
            result={
                "summary": "finished",
                "_private_publication": {"phase": "complete"},
                "trusted_publication": {"url": "forged"},
            },
        ),
    )
    db_session.expire(execution, ["result"])
    assert execution.result["_private_publication"] == protected
    assert execution.result["summary"] == "finished"
    if phase == "complete":
        assert execution.result["trusted_publication"] == receipt
    else:
        assert "trusted_publication" not in execution.result
