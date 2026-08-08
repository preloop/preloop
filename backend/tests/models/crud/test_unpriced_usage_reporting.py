"""Unpriced gateway usage must never be reported as a confident $0.00.

User-reported (2026-08-08): PR review flows costing real
money displayed as costing nothing. The gateway metered tokens correctly but
the model was missing from the price catalog, so every row landed with
``cost_source='unpriced'`` and ``estimated_cost=NULL`` — which a ``sum()``
then coalesced into a confident ``0.0``.

These tests pin the reporting contract:

  1. Unpriced rows expose their token volume, not a zero cost.
  2. An aggregate mixing priced and unpriced rows reports the priced subtotal
     plus an explicit unpriced qualifier, never one misleadingly-low number.
  3. ``cost_source='subscription'`` remains a legitimate, complete $0.00.
"""

import uuid

from preloop.models.crud import crud_ai_model, crud_flow, crud_flow_execution
from preloop.models.crud.api_usage import crud_api_usage
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _setup_execution(db_session, account, model_identifier="deepseek/deepseek-v4"):
    """Create an account-scoped model, flow and execution for usage rows."""
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Unpriced Model {uuid.uuid4()}",
            "provider_name": "openai-compatible",
            "model_identifier": model_identifier,
            "api_key": "provider-secret",
        },
        account_id=account.id,
    )
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"Unpriced Flow {uuid.uuid4()}",
            prompt_template="Test",
            trigger_event_source="manual",
            trigger_event_types=["test"],
            ai_model_id=ai_model.id,
            agent_type="gemini",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account.id,
        ),
        account_id=account.id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="RUNNING"),
    )
    return ai_model, flow, execution


def _log(db_session, *, account, user, flow, execution, **overrides):
    """Log one gateway usage row against the execution."""
    params = dict(
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user.id),
        account_id=str(account.id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai-compatible/deepseek/deepseek-v4-flash-0731",
        provider_name="openai-compatible",
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
    )
    params.update(overrides)
    return crud_api_usage.log_gateway_request(db_session, **params)


def test_unpriced_execution_reports_tokens_instead_of_zero_cost(
    db_session, create_account, create_user
):
    """An all-unpriced execution reports None cost and its unpriced tokens."""
    account = create_account()
    user = create_user(account=account)
    _, flow, execution = _setup_execution(db_session, account)

    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        estimated_cost=None,
        cost_source="unpriced",
    )

    usage = crud_api_usage.get_gateway_usage_for_execution(
        db_session, str(execution.id)
    )

    # The core complaint: this must not read as "this cost you $0.00".
    assert usage["estimated_cost"] is None
    assert usage["has_pricing"] is False
    assert usage["unpriced_requests"] == 1
    assert usage["unpriced_tokens"] == 1200


def test_mixed_execution_keeps_priced_subtotal_and_flags_unpriced(
    db_session, create_account, create_user
):
    """A mixed aggregate exposes the unpriced remainder, not just a subtotal."""
    account = create_account()
    user = create_user(account=account)
    _, flow, execution = _setup_execution(db_session, account)

    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        estimated_cost=0.25,
        cost_source="catalog",
    )
    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        estimated_cost=None,
        cost_source="unpriced",
        total_tokens=5000,
    )

    usage = crud_api_usage.get_gateway_usage_for_execution(
        db_session, str(execution.id)
    )

    # The priced part is still a real number, but the total is incomplete.
    assert usage["estimated_cost"] == 0.25
    assert usage["has_pricing"] is True
    assert usage["cost_is_partial"] is True
    assert usage["unpriced_requests"] == 1
    assert usage["unpriced_tokens"] == 5000


def test_subscription_zero_is_a_complete_zero(db_session, create_account, create_user):
    """Subscription-covered traffic is genuinely $0.00 and not flagged."""
    account = create_account()
    user = create_user(account=account)
    _, flow, execution = _setup_execution(db_session, account)

    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        estimated_cost=0.0,
        cost_source="subscription",
    )

    usage = crud_api_usage.get_gateway_usage_for_execution(
        db_session, str(execution.id)
    )

    assert usage["estimated_cost"] == 0.0
    assert usage["cost_is_partial"] is False
    assert usage["unpriced_requests"] == 0
    assert usage["unpriced_tokens"] == 0
