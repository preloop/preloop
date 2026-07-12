"""Tests for model gateway usage summaries."""

from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.model_gateway_usage import ModelGatewayUsageService


def test_get_account_summary_aggregates_gateway_usage(db_session, test_user):
    """Account summary should aggregate usage, models, flows, sessions, and budget."""
    account = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(
        db_session,
        db_obj=account,
        obj_in={
            "meta_data": {
                "model_gateway_budget": {
                    "monthly_usd_limit": 5.0,
                    "soft_limit_usd": 4.0,
                }
            }
        },
    )
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Session Usage Flow",
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
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            agent_session_reference="session-abc123",
        ),
    )
    now = datetime.now(timezone.utc)
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference="session-abc123",
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        runtime_session_id=str(runtime_session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        estimated_cost=1.25,
        meta_data={"note": "first"},
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=403,
        duration=0.2,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="anthropic/claude-sonnet-4-5",
        provider_name="anthropic",
        prompt_tokens=20,
        completion_tokens=0,
        total_tokens=20,
        estimated_cost=0.25,
        meta_data={"note": "second"},
    )

    summary = ModelGatewayUsageService(db_session).get_account_summary(
        account=account,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=1),
    )

    assert summary.total_requests == 2
    assert summary.successful_requests == 1
    assert summary.failed_requests == 1
    assert summary.token_usage.total_tokens == 170
    assert summary.estimated_cost == 1.5
    assert summary.budget.current_spend_usd == 1.5
    assert len(summary.usage_by_model) == 2
    assert len(summary.usage_by_session) == 1
    assert summary.usage_by_session[0].runtime_session_id == str(runtime_session.id)
    assert summary.usage_by_session[0].session_source_type == "flow_execution"
    assert summary.usage_by_session[0].session_source_id == str(execution.id)
    assert summary.usage_by_session[0].flow_execution_id == str(execution.id)
    assert summary.usage_by_session[0].flow_name == "Session Usage Flow"
    assert summary.usage_by_session[0].session_reference == "session-abc123"
    assert summary.usage_by_session[0].model_alias == "openai/gpt-5"


def test_get_flow_summary_aggregates_flow_and_execution_usage(db_session, test_user):
    """Flow summary should aggregate flow- and execution-scoped usage."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Usage Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={"model_gateway_budget": {"monthly_usd_limit": 2.0}},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    account = crud_account.get(db_session, id=test_user.account_id)
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED"),
    )

    now = datetime.now(timezone.utc)
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=70,
        completion_tokens=30,
        total_tokens=100,
        estimated_cost=0.9,
    )

    summary = ModelGatewayUsageService(db_session).get_flow_summary(
        account=account,
        flow=flow,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=1),
    )

    assert summary.flow_id == str(flow.id)
    assert summary.total_requests == 1
    assert summary.token_usage.total_tokens == 100
    assert summary.estimated_cost == 0.9
    assert summary.budget.current_spend_usd == 0.9
    assert summary.usage_by_execution[0].flow_execution_id == str(execution.id)


def test_get_account_summary_preserves_legacy_flow_execution_sessions(
    db_session, test_user
):
    """Session summaries should still surface flow-backed rows without RuntimeSession."""
    account = crud_account.get(db_session, id=test_user.account_id)
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Legacy Session Flow",
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
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            agent_session_reference="legacy-session-ref",
        ),
    )
    now = datetime.now(timezone.utc)
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.1,
    )

    summary = ModelGatewayUsageService(db_session).get_account_summary(
        account=account,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=1),
    )

    assert len(summary.usage_by_session) == 1
    assert summary.usage_by_session[0].runtime_session_id is None
    assert summary.usage_by_session[0].session_source_type == "flow_execution"
    assert summary.usage_by_session[0].session_source_id == str(execution.id)
    assert summary.usage_by_session[0].session_reference == "legacy-session-ref"
