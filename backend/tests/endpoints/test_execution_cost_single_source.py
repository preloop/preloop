"""One estimated cost per run across list, detail and chain total (#1275).

The executions list, the execution detail and the resume-chain total must
read the same figure for a run, including after usage rows are priced or
recorded after the run finished. The stored ``flow_execution.estimated_cost``
rollup (what ``/cost/by-issue`` sums) must converge to the same number.
"""

from __future__ import annotations

import uuid

import pytest

from preloop.api.endpoints import flows
from preloop.models import schemas
from preloop.models.crud import crud_api_usage, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.execution_metrics import sync_finished_execution_cost_rollup

from tests.conftest import maybe_await


def _flow(db_session, test_user):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Cost Single Source Flow",
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


def _execution(db_session, flow, *, stored_cost, resume_root=None):
    details = {"event": "test"}
    if resume_root is not None:
        details["_resume"] = {
            "execution_id": resume_root,
            "resume_root": resume_root,
            "thread_id": str(uuid.uuid4()),
        }
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id, status="SUCCEEDED", trigger_event_details=details
        ),
    )
    # The rollup the orchestrator wrote at completion.
    execution.estimated_cost = stored_cost
    execution.total_tokens = 1100
    db_session.flush()
    return execution


def _usage(db_session, test_user, flow, execution, cost):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.2,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        estimated_cost=cost,
        cost_source="catalog",
        meta_data={},
    )


async def _surfaces(db_session, test_user, flow, execution):
    rows = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    listed = {
        str(row.id): schemas.FlowExecutionListResponse.model_validate(row)
        for row in rows
    }
    detail = schemas.FlowExecutionResponse.model_validate(
        await maybe_await(
            flows.read_flow_execution(
                db=db_session, execution_id=execution.id, current_user=test_user
            )
        )
    )
    return listed, detail


@pytest.mark.asyncio
async def test_priced_at_completion_list_equals_detail(db_session, test_user):
    flow = _flow(db_session, test_user)
    run = _execution(db_session, flow, stored_cost=0.2)
    _usage(db_session, test_user, flow, run, 0.2)

    listed, detail = await _surfaces(db_session, test_user, flow, run)
    assert listed[str(run.id)].estimated_cost == pytest.approx(0.2)
    assert detail.estimated_cost == pytest.approx(0.2)
    assert detail.cost_priced_at is not None


@pytest.mark.asyncio
async def test_late_usage_rows_move_every_surface_together(db_session, test_user):
    """A 2-run chain whose usage rows land after completion."""
    flow = _flow(db_session, test_user)
    publisher = _execution(db_session, flow, stored_cost=0.2)
    repair = _execution(
        db_session, flow, stored_cost=0.19, resume_root=str(publisher.id)
    )
    _usage(db_session, test_user, flow, publisher, 0.2)
    _usage(db_session, test_user, flow, repair, 0.19)
    # Late rows: recorded after both runs finished and wrote their rollup.
    _usage(db_session, test_user, flow, publisher, 0.21)
    _usage(db_session, test_user, flow, repair, 0.18)

    listed, detail = await _surfaces(db_session, test_user, flow, repair)
    pub_list = listed[str(publisher.id)].estimated_cost
    rep_list = listed[str(repair.id)].estimated_cost
    assert pub_list == pytest.approx(0.41)
    assert rep_list == pytest.approx(0.37)
    assert detail.estimated_cost == pytest.approx(rep_list)

    # Chain total == sum of the member runs' displayed costs, on both views.
    assert detail.resume_totals is not None
    assert detail.resume_totals.estimated_cost == pytest.approx(pub_list + rep_list)
    assert listed[str(publisher.id)].resume_totals.estimated_cost == pytest.approx(
        pub_list + rep_list
    )
    assert detail.resume_totals.total_tokens == 4400

    # The stored rollup behind /cost/by-issue converges to the same figure.
    # The read surfaces projected live values onto the identity map, so drop
    # them to read the stored column.
    db_session.expire_all()
    for run, expected in ((publisher, pub_list), (repair, rep_list)):
        assert sync_finished_execution_cost_rollup(db_session, run.id)
        db_session.refresh(run)
        assert float(run.estimated_cost) == pytest.approx(expected)


def test_running_execution_rollup_is_left_to_the_orchestrator(db_session, test_user):
    flow = _flow(db_session, test_user)
    run = _execution(db_session, flow, stored_cost=None)
    run.status = "RUNNING"
    db_session.flush()
    _usage(db_session, test_user, flow, run, 0.3)
    assert sync_finished_execution_cost_rollup(db_session, run.id) is False
