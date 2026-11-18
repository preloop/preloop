"""The execution detail read reports which OpenRouter upstream answered."""

from preloop.api.endpoints.flows import _execution_upstream_route
from preloop.models.crud.api_usage import crud_api_usage

from tests.models.crud.test_unpriced_usage_reporting import _log, _setup_execution


def test_execution_detail_summarises_recorded_upstreams(
    db_session, create_account, create_user
):
    account = create_account()
    user = create_user(account=account)
    _, flow, execution = _setup_execution(db_session, account)
    for upstream, finish in (
        ("Provider A", "tool_calls"),
        ("Provider B", "stop"),
        ("Provider A", "stop"),
    ):
        _log(
            db_session,
            account=account,
            user=user,
            flow=flow,
            execution=execution,
            estimated_cost=0.01,
            cost_source="provider",
            meta_data={"upstream_provider": upstream, "finish_reason": finish},
        )

    facts = crud_api_usage.review_request_facts(
        db_session, execution_id=execution.id, account_id=account.id
    )
    assert [fact["finish_reason"] for fact in facts] == ["tool_calls", "stop", "stop"]

    route = _execution_upstream_route(db_session, execution, account_id=account.id)
    assert route is not None
    assert route["upstream_providers"] == [
        {"provider": "Provider A", "request_count": 2},
        {"provider": "Provider B", "request_count": 1},
    ]
    assert route["final_answer_upstream_provider"] == "Provider A"


def test_execution_without_upstreams_has_no_route(
    db_session, create_account, create_user
):
    account = create_account()
    user = create_user(account=account)
    _, flow, execution = _setup_execution(db_session, account)
    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        estimated_cost=0.01,
        cost_source="catalog",
    )
    assert (
        _execution_upstream_route(db_session, execution, account_id=account.id) is None
    )
