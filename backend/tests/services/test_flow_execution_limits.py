"""Per-execution token, turn and USD ceilings (issue #840).

The acceptance criteria these pin:

* a run at its ceiling has its next gateway request refused and ends FAILED
  with the ``budget_exceeded`` category naming the ceiling;
* usage under the ceiling is untouched (the request is allowed, the run keeps
  running);
* flows without ``limits`` behave exactly as before.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_execution_limits import (
    ExecutionBudgetExceededError,
    ExecutionLimits,
    ExecutionUsage,
    enforce_execution_limits_for_id,
    evaluate_execution_limits,
    parse_execution_limits,
)
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_BUDGET_EXCEEDED,
    derive_failure_category,
)


def _create_flow(db_session: Session, test_user, *, limits=None):
    agent_config = {}
    if limits is not None:
        agent_config["limits"] = limits
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Budgeted Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config=agent_config,
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )


def _create_execution(db_session: Session, flow, *, status="RUNNING"):
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status=status),
    )
    db_session.flush()
    return execution


def _log_usage(
    db_session: Session,
    test_user,
    flow,
    execution,
    *,
    total_tokens=100,
    estimated_cost=None,
    cost_source="unpriced",
):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.3,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=total_tokens,
        completion_tokens=0,
        total_tokens=total_tokens,
        estimated_cost=estimated_cost,
        cost_source=cost_source,
    )


# --- parsing and evaluation -------------------------------------------------


def test_parse_limits_reads_positive_values_and_ignores_junk():
    limits = parse_execution_limits(
        {"limits": {"max_total_tokens": "1000", "max_usd": 2, "max_turns": 5}}
    )
    assert limits == ExecutionLimits(max_total_tokens=1000, max_usd=2.0, max_turns=5)
    assert parse_execution_limits(None).is_empty
    assert parse_execution_limits({}).is_empty
    assert parse_execution_limits({"limits": "nope"}).is_empty
    assert parse_execution_limits(
        {"limits": {"max_total_tokens": 0, "max_usd": -1, "max_turns": "x"}}
    ).is_empty


def test_evaluate_allows_under_and_refuses_at_the_ceiling():
    limits = ExecutionLimits(max_total_tokens=100, max_usd=1.0, max_turns=3)
    assert evaluate_execution_limits(limits, ExecutionUsage(99, 0.5, 2)) is None

    tokens = evaluate_execution_limits(limits, ExecutionUsage(100, 0.5, 2))
    assert tokens is not None and tokens.kind == "max_total_tokens"
    assert "100 tokens used of 100 allowed" in tokens.message

    usd = evaluate_execution_limits(limits, ExecutionUsage(99, 1.0, 2))
    assert usd is not None and usd.kind == "max_usd"
    assert "$1.00 spent of $1.00 allowed" in usd.message

    turns = evaluate_execution_limits(limits, ExecutionUsage(99, 0.5, 3))
    assert turns is not None and turns.kind == "max_turns"
    assert "3 turns used of 3 allowed" in turns.message


def test_unpriced_usd_is_not_an_exceeded_usd_ceiling():
    """A cost nobody can compute cannot be shown to have crossed a limit."""
    limits = ExecutionLimits(max_usd=5.0)
    assert evaluate_execution_limits(limits, ExecutionUsage(10, None, 1)) is None


def test_parse_returns_empty_for_flows_without_limits():
    assert parse_execution_limits({"sandbox_type": "exec"}).is_empty


# --- enforcement against a real execution -----------------------------------


def test_token_ceiling_refuses_and_marks_execution_failed(
    db_session: Session, test_user
):
    flow = _create_flow(db_session, test_user, limits={"max_total_tokens": 100})
    execution = _create_execution(db_session, flow)
    _log_usage(db_session, test_user, flow, execution, total_tokens=150)
    db_session.commit()

    with pytest.raises(ExecutionBudgetExceededError) as raised:
        enforce_execution_limits_for_id(db_session, execution_id=execution.id)

    assert "Execution budget exceeded" in raised.value.message
    assert "100 allowed" in raised.value.message
    db_session.refresh(execution)
    assert execution.status == "FAILED"
    assert execution.failure_category == FAILURE_CATEGORY_BUDGET_EXCEEDED
    assert "token ceiling" in execution.error_message
    assert execution.end_time is not None


def test_usage_under_the_ceiling_is_untouched(db_session: Session, test_user):
    flow = _create_flow(db_session, test_user, limits={"max_total_tokens": 100})
    execution = _create_execution(db_session, flow)
    _log_usage(db_session, test_user, flow, execution, total_tokens=50)
    db_session.commit()

    # No exception, and the run keeps its in-flight status.
    enforce_execution_limits_for_id(db_session, execution_id=execution.id)
    db_session.refresh(execution)
    assert execution.status == "RUNNING"
    assert execution.failure_category is None


def test_flow_without_limits_is_never_refused(db_session: Session, test_user):
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    _log_usage(db_session, test_user, flow, execution, total_tokens=10_000_000)
    db_session.commit()

    enforce_execution_limits_for_id(db_session, execution_id=execution.id)
    db_session.refresh(execution)
    assert execution.status == "RUNNING"
    assert execution.failure_category is None


def test_usd_ceiling_uses_priced_usage(db_session: Session, test_user):
    flow = _create_flow(db_session, test_user, limits={"max_usd": 5.0})
    execution = _create_execution(db_session, flow)
    _log_usage(
        db_session,
        test_user,
        flow,
        execution,
        total_tokens=100,
        estimated_cost=6.0,
        cost_source="catalog",
    )
    db_session.commit()

    with pytest.raises(ExecutionBudgetExceededError) as raised:
        enforce_execution_limits_for_id(db_session, execution_id=execution.id)
    assert "USD ceiling" in raised.value.message
    db_session.refresh(execution)
    assert execution.failure_category == FAILURE_CATEGORY_BUDGET_EXCEEDED


def test_usd_ceiling_does_not_block_an_unpriced_run(db_session: Session, test_user):
    flow = _create_flow(db_session, test_user, limits={"max_usd": 5.0})
    execution = _create_execution(db_session, flow)
    _log_usage(
        db_session,
        test_user,
        flow,
        execution,
        total_tokens=10_000_000,
        estimated_cost=None,
        cost_source="unpriced",
    )
    db_session.commit()

    enforce_execution_limits_for_id(db_session, execution_id=execution.id)
    db_session.refresh(execution)
    assert execution.status == "RUNNING"


def test_turn_ceiling_counts_one_turn_per_gateway_request(
    db_session: Session, test_user
):
    flow = _create_flow(db_session, test_user, limits={"max_turns": 2})
    execution = _create_execution(db_session, flow)
    _log_usage(db_session, test_user, flow, execution, total_tokens=10)
    _log_usage(db_session, test_user, flow, execution, total_tokens=10)
    db_session.commit()

    with pytest.raises(ExecutionBudgetExceededError) as raised:
        enforce_execution_limits_for_id(db_session, execution_id=execution.id)
    assert "turn ceiling" in raised.value.message


def test_describe_execution_limits_reports_ceilings_and_usage(
    db_session: Session, test_user
):
    from preloop.services.flow_execution_limits import describe_execution_limits

    flow = _create_flow(db_session, test_user, limits={"max_usd": 5.0})
    execution = _create_execution(db_session, flow)
    _log_usage(
        db_session,
        test_user,
        flow,
        execution,
        total_tokens=250,
        estimated_cost=1.5,
        cost_source="catalog",
    )
    db_session.commit()

    limits, usage = describe_execution_limits(db_session, execution)
    assert limits.as_dict() == {"max_usd": 5.0}
    assert usage.total_tokens == 250
    assert usage.cost_usd == 1.5
    assert usage.turns == 1


def test_terminal_execution_is_never_rewritten(db_session: Session, test_user):
    flow = _create_flow(db_session, test_user, limits={"max_total_tokens": 1})
    execution = _create_execution(db_session, flow, status="SUCCEEDED")
    execution.end_time = datetime.now(timezone.utc)
    _log_usage(db_session, test_user, flow, execution, total_tokens=100)
    db_session.commit()

    # A late request is still refused (the ceiling is spent), but a recorded
    # success keeps its own outcome rather than being rewritten to a failure.
    with pytest.raises(ExecutionBudgetExceededError):
        enforce_execution_limits_for_id(db_session, execution_id=execution.id)
    db_session.refresh(execution)
    assert execution.status == "SUCCEEDED"
    assert execution.failure_category is None


# --- failure classification and schema --------------------------------------


def test_message_classifies_as_budget_exceeded():
    category = derive_failure_category(
        status="FAILED",
        error_message=(
            "Execution budget exceeded: execution token ceiling reached: "
            "2100000 tokens used of 2000000 allowed"
        ),
    )
    assert category == FAILURE_CATEGORY_BUDGET_EXCEEDED


@pytest.mark.parametrize(
    "limits",
    [
        {"max_total_tokens": 0},
        {"max_usd": -1},
        {"max_turns": 0},
        {"unknown_ceiling": 5},
    ],
)
def test_flow_schema_rejects_bad_limits(test_user, limits):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FlowCreate(
            name="Bad Limits",
            prompt_template="Test",
            agent_config={"limits": limits},
            account_id=test_user.account_id,
        )


def test_flow_schema_accepts_valid_limits(test_user):
    flow = FlowCreate(
        name="Good Limits",
        prompt_template="Test",
        agent_config={
            "limits": {"max_total_tokens": 1000, "max_usd": 2.5, "max_turns": 7}
        },
        account_id=test_user.account_id,
    )
    assert flow.agent_config["limits"]["max_total_tokens"] == 1000


# --- gateway integration ----------------------------------------------------


def _gateway_auth_context(test_user, execution_id):
    import json

    from preloop.services.gateway_execution import GatewayApiKeySnapshot
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext

    return ModelGatewayAuthContext(
        token="synthetic",
        user=test_user,
        api_key=GatewayApiKeySnapshot(
            id=test_user.id,
            account_id=test_user.account_id,
            user_id=test_user.id,
            name="Flow Execution synthetic",
            context_json=json.dumps({"flow_execution_id": str(execution_id)}),
        ),
    )


def test_gateway_endpoint_refuses_over_ceiling_execution(
    db_session: Session, test_user, monkeypatch
):
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from preloop.api.app import create_app
    from preloop.api.deps import get_budget_enforcer
    from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
    from preloop.config import settings
    from preloop.models.crud import crud_ai_model
    from preloop.models.db.session import get_db_session
    from preloop.plugins.base import PluginManager
    from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "gateway")
    monkeypatch.setattr(settings, "disable_rbac", True)
    monkeypatch.setattr("preloop.plugins.get_plugin_manager", PluginManager)
    monkeypatch.setattr("preloop.plugins.base._plugin_manager", None)

    ai_model = crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Ceiling Gateway Model",
            "provider_name": "openai",
            "model_identifier": "synthetic-ceiling-model",
            "api_key": "unused-synthetic-key",
            "meta_data": {"gateway": {"enabled": True}, "pricing": {}},
        },
    )
    requested_alias = resolve_ai_model_runtime(ai_model).model_gateway_model_alias
    flow = _create_flow(db_session, test_user, limits={"max_total_tokens": 10})
    execution = _create_execution(db_session, flow)
    _log_usage(db_session, test_user, flow, execution, total_tokens=50)
    db_session.commit()

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        _gateway_auth_context(test_user, execution.id)
    )
    app.dependency_overrides[get_budget_enforcer] = lambda: None

    with (
        patch("preloop.services.openai_gateway.litellm.completion") as provider,
        TestClient(app) as client,
    ):
        response = client.post(
            "/openai/v1/chat/completions",
            json={
                "model": requested_alias,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 403, response.text
    body = response.json()
    assert "Execution budget exceeded" in str(body)
    provider.assert_not_called()

    db_session.refresh(execution)
    assert execution.status == "FAILED"
    assert execution.failure_category == FAILURE_CATEGORY_BUDGET_EXCEEDED
