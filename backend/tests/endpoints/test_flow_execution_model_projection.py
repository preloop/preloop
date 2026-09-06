"""The executions list and detail endpoints project the model that ran a run.

Wave 7 puts a Model column in the executions table and a Model field in the
execution summary strip, so both responses have to carry the alias without a
second round trip. The projection is derived from gateway usage, not stored
on the execution row.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from preloop.api.endpoints import flows
from preloop.models import schemas
from preloop.models.crud import (
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

from tests.conftest import maybe_await


def _create_flow(db_session, test_user, name="Model Column Flow"):
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


def _log_row(
    db_session,
    test_user,
    *,
    model_alias,
    provider_name="openai",
    flow_execution_id=None,
    runtime_session_id=None,
):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=str(test_user.account_id),
        flow_execution_id=str(flow_execution_id) if flow_execution_id else None,
        runtime_session_id=str(runtime_session_id) if runtime_session_id else None,
        model_alias=model_alias,
        provider_name=provider_name,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.001,
    )


@pytest.mark.asyncio
async def test_execution_list_carries_model_projection(db_session, test_user):
    """A single-model run shows one alias; a multi-model run keeps the rest."""
    flow = _create_flow(db_session, test_user)
    single = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    multi = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    _log_row(
        db_session, test_user, model_alias="openai/gpt-5", flow_execution_id=single.id
    )
    for _ in range(2):
        _log_row(
            db_session,
            test_user,
            model_alias="anthropic/claude-sonnet-4",
            provider_name="anthropic",
            flow_execution_id=multi.id,
        )
    _log_row(
        db_session, test_user, model_alias="openai/gpt-5", flow_execution_id=multi.id
    )

    result = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    rows = {
        str(row.id): schemas.FlowExecutionListResponse.model_validate(row)
        for row in result
    }

    single_row = rows[str(single.id)]
    assert single_row.model_alias == "openai/gpt-5"
    assert single_row.provider_name == "openai"
    assert len(single_row.models_used) == 1

    multi_row = rows[str(multi.id)]
    assert multi_row.model_alias == "anthropic/claude-sonnet-4"
    assert [m.model_alias for m in multi_row.models_used] == [
        "anthropic/claude-sonnet-4",
        "openai/gpt-5",
    ]
    assert multi_row.models_used[0].request_count == 2


@pytest.mark.asyncio
async def test_execution_without_usage_has_null_model(db_session, test_user):
    """A run that never called the gateway reports no model, not an error."""
    flow = _create_flow(db_session, test_user, name="No Usage Flow")
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )

    result = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    row = schemas.FlowExecutionListResponse.model_validate(result[0])

    assert str(row.id) == str(execution.id)
    assert row.model_alias is None
    assert row.provider_name is None
    assert row.models_used == []


@pytest.mark.asyncio
async def test_execution_detail_carries_model_projection(db_session, test_user):
    """The detail response projects the same fields, including usage that is
    only attributable through the run's runtime session."""
    flow = _create_flow(db_session, test_user, name="Detail Model Flow")
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference="detail-model",
        last_activity_at=datetime.now(UTC),
    )
    _log_row(
        db_session,
        test_user,
        model_alias="openai/gpt-5-codex",
        runtime_session_id=session.id,
    )

    result = await maybe_await(
        flows.read_flow_execution(
            db=db_session, execution_id=execution.id, current_user=test_user
        )
    )
    detail = schemas.FlowExecutionResponse.model_validate(result)

    assert detail.model_alias == "openai/gpt-5-codex"
    assert [m.model_alias for m in detail.models_used] == ["openai/gpt-5-codex"]


@pytest.mark.asyncio
async def test_execution_list_carries_token_split(db_session, test_user):
    """The executions list shows tokens before cost, split in and out with cache."""
    flow = _create_flow(db_session, test_user, name="Token Split Flow")
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=str(test_user.account_id),
        flow_execution_id=str(execution.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=1_000,
        completion_tokens=250,
        total_tokens=1_250,
        cache_read_tokens=800,
        cache_creation_tokens=50,
        estimated_cost=0.02,
    )

    result = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    row = schemas.FlowExecutionListResponse.model_validate(result[0])

    assert row.token_usage is not None
    assert row.token_usage.input_tokens == 1_000
    assert row.token_usage.output_tokens == 250
    assert row.token_usage.total_tokens == 1_250
    assert row.token_usage.cache_read_tokens == 800
    assert row.token_usage.cache_write_tokens == 50
    assert row.token_usage.uncached_input_tokens == 150
    assert row.token_usage.cache_hit_ratio == 0.8421


@pytest.mark.asyncio
async def test_execution_without_usage_has_null_token_usage(db_session, test_user):
    """No gateway traffic means unknown tokens, which is not zero tokens."""
    flow = _create_flow(db_session, test_user, name="No Token Usage Flow")
    crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )

    result = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    row = schemas.FlowExecutionListResponse.model_validate(result[0])

    assert row.token_usage is None
