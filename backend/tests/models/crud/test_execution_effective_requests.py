"""The execution detail read shows what reached the model provider."""

from preloop.api.endpoints.flows import _execution_effective_requests
from preloop.models.schemas.flow_execution import ExecutionEffectiveRequest

from tests.models.crud.test_unpriced_usage_reporting import _log, _setup_execution


def test_execution_shows_reasoning_effort_sent(db_session, create_account, create_user):
    account = create_account()
    user = create_user(account=account)
    _, flow, execution = _setup_execution(db_session, account)
    for _ in range(2):
        _log(
            db_session,
            account=account,
            user=user,
            flow=flow,
            execution=execution,
            estimated_cost=0.01,
            cost_source="provider",
            meta_data={
                "upstream_parameters": {
                    "model": "deepseek-flash",
                    "reasoning_effort": "low",
                    "output_format": {"type": "none"},
                    "schema_in_instructions": False,
                    "tool_count": 3,
                }
            },
        )
    entries = _execution_effective_requests(
        db_session, execution, account_id=account.id
    )
    assert entries is not None and len(entries) == 1
    entry = ExecutionEffectiveRequest.model_validate(entries[0])
    assert entry.model == "deepseek-flash"
    assert entry.reasoning_effort == "low"
    assert entry.request_count == 2


def test_execution_without_effort_reports_none(db_session, create_account, create_user):
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
        cost_source="provider",
        meta_data={"upstream_parameters": {"model": "deepseek-flash"}},
    )
    entries = _execution_effective_requests(
        db_session, execution, account_id=account.id
    )
    assert entries is not None
    assert entries[0]["reasoning_effort"] is None


def test_other_accounts_requests_are_not_shown(db_session, create_account, create_user):
    account = create_account()
    other = create_account()
    user = create_user(account=account)
    _, flow, execution = _setup_execution(db_session, account)
    _log(
        db_session,
        account=account,
        user=user,
        flow=flow,
        execution=execution,
        estimated_cost=0.01,
        cost_source="provider",
        meta_data={"upstream_parameters": {"model": "deepseek-flash"}},
    )
    assert (
        _execution_effective_requests(db_session, execution, account_id=other.id)
        is None
    )


def test_execution_detail_reads_request_facts_once(
    db_session, create_account, create_user
):
    from unittest.mock import patch

    from preloop.api.endpoints import flows as flows_endpoint

    account = create_account()
    create_user(account=account)
    _, _, execution = _setup_execution(db_session, account)
    facts = [{"upstream_parameters": {"model": "deepseek-flash"}}]
    with patch.object(
        flows_endpoint.crud_api_usage, "review_request_facts", return_value=facts
    ) as read:
        loaded = flows_endpoint._execution_request_facts(
            db_session, execution, account_id=account.id
        )
        flows_endpoint._execution_upstream_route(
            db_session, execution, account_id=account.id, requests=loaded
        )
        entries = flows_endpoint._execution_effective_requests(
            db_session, execution, account_id=account.id, requests=loaded
        )
    assert read.call_count == 1
    assert entries is not None and entries[0]["model"] == "deepseek-flash"


def test_failed_fact_read_yields_no_summary(db_session, create_account):
    from unittest.mock import patch

    from preloop.api.endpoints import flows as flows_endpoint

    account = create_account()
    _, _, execution = _setup_execution(db_session, account)
    with patch.object(
        flows_endpoint.crud_api_usage,
        "review_request_facts",
        side_effect=RuntimeError("synthetic"),
    ):
        assert (
            _execution_effective_requests(db_session, execution, account_id=account.id)
            is None
        )
