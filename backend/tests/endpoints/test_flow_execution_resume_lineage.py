"""List and detail expose resume_of and chain resume_totals."""

from __future__ import annotations

import uuid

import pytest

from preloop.api.endpoints import flows
from preloop.models import schemas
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

from tests.conftest import maybe_await


def _create_flow(db_session, test_user, name="Resume Lineage Flow"):
    return crud_flow.create(
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


def _execution(
    db_session,
    flow,
    *,
    total_tokens: int,
    estimated_cost: float,
    resume_root: str | None = None,
):
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
            flow_id=flow.id,
            status="SUCCEEDED",
            trigger_event_details=details,
        ),
    )
    execution.total_tokens = total_tokens
    execution.estimated_cost = estimated_cost
    db_session.flush()
    return execution


@pytest.mark.asyncio
async def test_list_and_detail_project_resume_chain(db_session, test_user):
    flow = _create_flow(db_session, test_user)
    publisher = _execution(db_session, flow, total_tokens=1000, estimated_cost=0.10)
    repair = _execution(
        db_session,
        flow,
        total_tokens=400,
        estimated_cost=0.04,
        resume_root=str(publisher.id),
    )
    unrelated = _execution(db_session, flow, total_tokens=50, estimated_cost=0.01)

    rows = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    by_id = {
        str(row.id): schemas.FlowExecutionListResponse.model_validate(row)
        for row in rows
    }

    assert by_id[str(repair.id)].resume_of == publisher.id
    assert by_id[str(repair.id)].resume_totals is not None
    assert by_id[str(repair.id)].resume_totals.total_tokens == 1400
    assert by_id[str(repair.id)].resume_totals.estimated_cost == pytest.approx(0.14)

    assert by_id[str(publisher.id)].resume_of is None
    assert by_id[str(publisher.id)].resume_totals is not None
    assert by_id[str(publisher.id)].resume_totals.total_tokens == 1400

    assert by_id[str(unrelated.id)].resume_of is None
    assert by_id[str(unrelated.id)].resume_totals is None

    detail = await maybe_await(
        flows.read_flow_execution(
            db=db_session, execution_id=repair.id, current_user=test_user
        )
    )
    detail_row = schemas.FlowExecutionResponse.model_validate(detail)
    assert detail_row.resume_of == publisher.id
    assert detail_row.resume_totals is not None
    assert detail_row.resume_totals.total_tokens == 1400
    assert detail_row.resume_totals.estimated_cost == pytest.approx(0.14)
