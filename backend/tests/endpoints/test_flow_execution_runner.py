"""The executions list and detail endpoints project where a run executed."""

from __future__ import annotations

import pytest

from preloop.api.endpoints import flows
from preloop.models import schemas
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow_runner import FlowRunner
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.runner_service import HOSTED_RUNNER_NAME, hash_runner_token

from tests.conftest import maybe_await


def _create_flow(db_session, test_user, name="Runner Column Flow", runner_pool=None):
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    if runner_pool:
        flow.runner_pool = runner_pool
        db_session.add(flow)
        db_session.flush()
    return flow


def _create_runner(db_session, test_user, name="Office Mac"):
    runner = FlowRunner(
        account_id=test_user.account_id,
        name=name,
        token_hash=hash_runner_token("test-runner-token"),
        labels=["gpu"],
        status="offline",
    )
    db_session.add(runner)
    db_session.flush()
    return runner


@pytest.mark.asyncio
async def test_execution_list_and_detail_project_hosted_and_private(
    db_session, test_user
):
    """Hosted runs still name the executor; private runs join the runner name."""
    flow = _create_flow(db_session, test_user, runner_pool="gpu")
    hosted = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    runner = _create_runner(db_session, test_user)
    private = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    private.runner_id = runner.id
    private.agent_session_reference = f"runner:{runner.id}:{private.id}"
    db_session.add(private)
    db_session.flush()

    listed = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    rows = {
        str(row.id): schemas.FlowExecutionListResponse.model_validate(row)
        for row in listed
    }

    hosted_row = rows[str(hosted.id)]
    assert hosted_row.runner.kind == "hosted"
    assert hosted_row.runner.name == HOSTED_RUNNER_NAME

    private_row = rows[str(private.id)]
    assert private_row.runner.kind == "private"
    assert private_row.runner.name == "Office Mac"

    detail = schemas.FlowExecutionResponse.model_validate(
        await maybe_await(
            flows.read_flow_execution(
                db=db_session, execution_id=private.id, current_user=test_user
            )
        )
    )
    assert detail.runner.kind == "private"
    assert detail.runner.id == runner.id
    assert detail.runner.name == "Office Mac"
    assert detail.runner.pool == "gpu"
