"""Regression tests for the flow_execution.estimated_cost rollup (issue #209).

The stored rollup is written once at execution end, but most gateway usage
rows are priced *later* (live price lookup / repricing backfill). These tests
pin the invariant from the issue's acceptance criteria: the rollup for an
execution equals the sum of the gateway usage rows attributable to it
(``action_type='model_gateway'``, ``flow_execution_id`` match, replay
traffic excluded), and every execution with priced usage rows gets a
non-null rollup — including after repricing runs.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.usage_repricing import reprice_gateway_usage, reprice_single_row


def _create_flow(db_session, test_user):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Cost Rollup Flow",
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


def _create_execution(db_session, flow):
    """Create a finished execution with the legacy placeholder 0.0 rollup."""
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED"),
    )
    execution.end_time = datetime.now(timezone.utc)
    db_session.flush()
    return execution


def _create_priced_model(db_session, test_user):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Rollup Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                },
                "pricing": {
                    "input_price_per_1k": 0.01,
                    "output_price_per_1k": 0.02,
                },
            },
        },
        account_id=test_user.account_id,
    )


def _log_row(
    db_session,
    test_user,
    flow,
    execution,
    *,
    ai_model=None,
    prompt_tokens=1000,
    completion_tokens=100,
    estimated_cost=None,
    cost_source="unpriced",
    purpose=None,
):
    meta = {"usage_details": {"prompt_tokens": prompt_tokens}}
    if purpose:
        meta["purpose"] = purpose
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.3,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        ai_model_id=str(ai_model.id) if ai_model else None,
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        estimated_cost=estimated_cost,
        cost_source=cost_source,
        meta_data=meta,
    )


def _window():
    now = datetime.now(timezone.utc)
    return now - timedelta(days=1), now + timedelta(minutes=5)


def test_reprice_backfill_updates_execution_rollup(db_session, test_user):
    """Repricing unpriced rows must roll the new costs up to the execution.

    This is the production shape of issue #209: the end-of-run rollup ran
    while every gateway row was still unpriced (model missing from the price
    snapshot), the rows were repriced later, and the stored rollup stayed at
    its placeholder while api_usage carried the real spend.
    """
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    ai_model = _create_priced_model(db_session, test_user)
    # Two unpriced rows, as recorded at request time before the price lookup.
    _log_row(db_session, test_user, flow, execution, ai_model=ai_model)
    _log_row(db_session, test_user, flow, execution, ai_model=ai_model)
    assert float(execution.estimated_cost or 0.0) == 0.0

    start, end = _window()
    result = reprice_gateway_usage(
        db_session, account_id=test_user.account_id, start=start, end=end
    )

    assert result.rows_updated == 2
    db_session.refresh(execution)
    # 2 * (1000 * 0.01/1k + 100 * 0.02/1k) = 0.024
    assert execution.estimated_cost is not None
    assert float(execution.estimated_cost) == 0.024


def test_reprice_heals_stale_rollup_of_already_priced_rows(db_session, test_user):
    """A reprice pass over a window also heals rollups whose rows were
    already priced by an earlier backfill (nothing left to reprice)."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    ai_model = _create_priced_model(db_session, test_user)
    _log_row(
        db_session,
        test_user,
        flow,
        execution,
        ai_model=ai_model,
        estimated_cost=0.03,
        cost_source="catalog",
    )
    _log_row(
        db_session,
        test_user,
        flow,
        execution,
        ai_model=ai_model,
        estimated_cost=0.05,
        cost_source="catalog",
    )
    # Stale placeholder written by the end-of-run rollup before repricing.
    execution.estimated_cost = Decimal("0.0")
    db_session.flush()

    start, end = _window()
    result = reprice_gateway_usage(
        db_session, account_id=test_user.account_id, start=start, end=end
    )

    assert result.rows_updated == 0
    db_session.refresh(execution)
    assert execution.estimated_cost is not None
    assert float(execution.estimated_cost) == 0.08


def test_reprice_single_row_updates_execution_rollup(db_session, test_user):
    """The live price lookup's single-row fix must refresh the rollup too."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    ai_model = _create_priced_model(db_session, test_user)
    row = _log_row(db_session, test_user, flow, execution, ai_model=ai_model)

    assert reprice_single_row(db_session, api_usage_id=row.id) is True

    db_session.refresh(execution)
    # 1000 * 0.01/1k + 100 * 0.02/1k = 0.012
    assert execution.estimated_cost is not None
    assert float(execution.estimated_cost) == 0.012


def test_reprice_dry_run_leaves_rollup_untouched(db_session, test_user):
    """Dry-run repricing must not write the rollup either."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    ai_model = _create_priced_model(db_session, test_user)
    _log_row(db_session, test_user, flow, execution, ai_model=ai_model)

    start, end = _window()
    reprice_gateway_usage(
        db_session,
        account_id=test_user.account_id,
        start=start,
        end=end,
        dry_run=True,
    )

    db_session.refresh(execution)
    assert float(execution.estimated_cost or 0.0) == 0.0


def test_rollup_sync_excludes_replay_and_counts_provider_rows(db_session, test_user):
    """The rollup uses the documented attribution rule: replay-validation
    traffic is excluded; any priced row (including provider-reported cost,
    see PR #208) counts."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    ai_model = _create_priced_model(db_session, test_user)
    _log_row(
        db_session,
        test_user,
        flow,
        execution,
        ai_model=ai_model,
        estimated_cost=0.02,
        cost_source="catalog",
    )
    # Replay-validation measurement traffic: real spend, not the user's run.
    _log_row(
        db_session,
        test_user,
        flow,
        execution,
        ai_model=ai_model,
        estimated_cost=5.0,
        cost_source="catalog",
        purpose="replay_validation",
    )

    from preloop.services.execution_metrics import sync_execution_cost_rollup

    assert sync_execution_cost_rollup(db_session, str(execution.id)) is True
    db_session.refresh(execution)
    assert float(execution.estimated_cost) == 0.02


def test_rollup_sync_keeps_null_when_nothing_priced(db_session, test_user):
    """When no attributable row is priced the rollup must stay NULL
    (unknown), never a placeholder 0.0 that reads as free."""
    flow = _create_flow(db_session, test_user)
    execution = _create_execution(db_session, flow)
    ai_model = _create_priced_model(db_session, test_user)
    _log_row(db_session, test_user, flow, execution, ai_model=ai_model)
    execution.estimated_cost = Decimal("0.0")
    db_session.flush()

    from preloop.services.execution_metrics import sync_execution_cost_rollup

    assert sync_execution_cost_rollup(db_session, str(execution.id)) is True
    db_session.refresh(execution)
    assert execution.estimated_cost is None
