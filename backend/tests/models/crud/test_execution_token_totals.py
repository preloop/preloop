"""An execution's total tokens are never smaller than its prompt tokens.

A hosted Gemini run reported 309k total tokens against 556k prompt tokens.
The gateway stores input cache inclusive (#1955) but keeps the provider's own
``total_tokens`` verbatim, and a provider total that leaves the cached prompt
out (or a row with no total at all) then sums below the prompt. The execution
figures take, per row, the larger of the stored total and prompt + completion.
Stored rows and prices are left alone.
"""

import uuid

from preloop.models.crud import crud_ai_model, crud_flow, crud_flow_execution
from preloop.models.crud.api_usage import crud_api_usage
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.execution_metrics import get_execution_totals


def _execution(db_session, account):
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Gemini {uuid.uuid4()}",
            "provider_name": "gemini",
            "model_identifier": "gemini-2.5-pro",
            "api_key": "provider-secret",
        },
        account_id=account.id,
    )
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"Review {uuid.uuid4()}",
            prompt_template="Test",
            trigger_event_source="manual",
            trigger_event_types=["test"],
            ai_model_id=ai_model.id,
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
    return flow, execution


def _log(db_session, *, account, user, flow, execution, **usage):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user.id),
        account_id=str(account.id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="gemini/gemini-2.5-pro",
        provider_name="gemini",
        estimated_cost=0.01,
        **usage,
    )


def test_execution_total_covers_prompt_and_completion(
    db_session, create_account, create_user
):
    account = create_account()
    user = create_user(account=account)
    flow, execution = _execution(db_session, account)
    # Gemini shape: the prompt counts the cached prefix, the provider total
    # does not.
    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        prompt_tokens=278_000,
        completion_tokens=2_000,
        total_tokens=154_500,
        cache_read_tokens=125_500,
    )
    # A row recorded without any total.
    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        prompt_tokens=1_000,
        completion_tokens=100,
        total_tokens=None,
    )
    # A consistent row is counted as reported.
    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        prompt_tokens=500,
        completion_tokens=50,
        total_tokens=600,
    )

    usage = crud_api_usage.get_gateway_usage_for_execution(db_session, execution.id)
    tokens = usage["token_usage"]
    assert tokens["input_tokens"] == 279_500
    assert tokens["total_tokens"] == 280_000 + 1_100 + 600
    assert tokens["total_tokens"] >= tokens["input_tokens"] + tokens["output_tokens"]
    assert usage["estimated_cost"] == 0.03

    listed = get_execution_totals(db_session, [execution.id])[str(execution.id)]
    assert listed["token_usage"]["total_tokens"] == tokens["total_tokens"]
    assert listed["estimated_cost"] == 0.03
