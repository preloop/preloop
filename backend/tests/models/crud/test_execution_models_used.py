"""Tests for the execution -> model attribution used by the console.

The executions list and the execution page both answer "what model ran
this?" from gateway usage, aggregated in one query per page of rows
(``crud_api_usage.get_models_used_for_executions``). Usage attaches to an
execution two ways, and both count:

* directly, via ``api_usage.flow_execution_id`` (the flow orchestrator sets
  it on every gateway call it proxies), and
* indirectly, via ``api_usage.runtime_session_id`` where that runtime session
  was opened for the execution (``session_source_type='flow_execution'``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from preloop.models.crud import (
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _create_flow(db_session, test_user):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Model Projection Flow",
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


def _create_execution(db_session, flow, status="SUCCEEDED"):
    return crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status=status),
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


def test_single_model_run_reports_one_alias(db_session, test_user):
    """A run that only talked to one alias reports it with its provider."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    _log_row(
        db_session,
        test_user,
        model_alias="openai/gpt-5",
        flow_execution_id=execution.id,
    )
    _log_row(
        db_session,
        test_user,
        model_alias="openai/gpt-5",
        flow_execution_id=execution.id,
    )

    result = crud_api_usage.get_models_used_for_executions(db_session, [execution.id])

    models = result[str(execution.id)]
    assert [m["model_alias"] for m in models] == ["openai/gpt-5"]
    assert models[0]["provider_name"] == "openai"
    assert models[0]["request_count"] == 2


def test_multi_model_run_orders_by_request_count(db_session, test_user):
    """Several aliases come back most used first, so the cell can show the
    primary model and a '+N' for the rest."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    for _ in range(3):
        _log_row(
            db_session,
            test_user,
            model_alias="anthropic/claude-sonnet-4",
            provider_name="anthropic",
            flow_execution_id=execution.id,
        )
    _log_row(
        db_session,
        test_user,
        model_alias="openai/gpt-5",
        flow_execution_id=execution.id,
    )

    result = crud_api_usage.get_models_used_for_executions(db_session, [execution.id])

    models = result[str(execution.id)]
    assert [m["model_alias"] for m in models] == [
        "anthropic/claude-sonnet-4",
        "openai/gpt-5",
    ]
    assert [m["request_count"] for m in models] == [3, 1]


def test_usage_attached_through_runtime_session_counts(db_session, test_user):
    """Rows that only carry a runtime session still attribute to the run."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference="models-used",
        last_activity_at=datetime.now(UTC),
    )
    _log_row(
        db_session,
        test_user,
        model_alias="openai/gpt-5-codex",
        runtime_session_id=session.id,
    )

    result = crud_api_usage.get_models_used_for_executions(db_session, [execution.id])

    assert [m["model_alias"] for m in result[str(execution.id)]] == [
        "openai/gpt-5-codex"
    ]


def test_run_without_gateway_usage_has_no_entry(db_session, test_user):
    """A run with no session and no gateway traffic is simply absent, which
    the endpoint turns into a null model rather than an error."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)

    result = crud_api_usage.get_models_used_for_executions(db_session, [execution.id])

    assert result == {}


def test_projection_batches_many_executions(db_session, test_user):
    """One call covers a whole page of rows and never mixes them up."""
    flow = _create_flow(db_session, test_user)
    first = _create_execution(db_session, flow)
    second = _create_execution(db_session, flow)
    _log_row(
        db_session,
        test_user,
        model_alias="openai/gpt-5",
        flow_execution_id=first.id,
    )
    _log_row(
        db_session,
        test_user,
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        flow_execution_id=second.id,
    )

    result = crud_api_usage.get_models_used_for_executions(
        db_session, [first.id, second.id, uuid4()]
    )

    assert result[str(first.id)][0]["model_alias"] == "openai/gpt-5"
    assert result[str(second.id)][0]["model_alias"] == "anthropic/claude-sonnet-4"
    assert len(result) == 2


def test_empty_id_list_skips_the_query(db_session):
    """No rows on the page means no query at all."""
    assert crud_api_usage.get_models_used_for_executions(db_session, []) == {}
