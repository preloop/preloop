"""Tests for the gateway usage repricing service."""

from datetime import datetime, timedelta, timezone

import pytest

from preloop.models.crud import crud_ai_model, crud_api_usage
from preloop.models.models.budget import BudgetSpendActivity
from preloop.services import usage_repricing
from preloop.services.model_pricing import CostEstimate
from preloop.services.usage_repricing import reprice_gateway_usage


def _create_model(db_session, test_user, pricing=None):
    meta = {
        "gateway": {
            "enabled": True,
            "model_alias": "openai/gpt-5",
            "provider_adapter": "preloop",
        }
    }
    if pricing:
        meta["pricing"] = pricing
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": meta,
        },
        account_id=test_user.account_id,
    )


def _log_unpriced_row(db_session, test_user, ai_model, tokens=1000):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.4,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=tokens,
        completion_tokens=100,
        total_tokens=tokens + 100,
        estimated_cost=None,
        cost_source="unpriced",
        meta_data={"usage_details": {"prompt_tokens": tokens}},
    )


def _window():
    now = datetime.now(timezone.utc)
    return now - timedelta(days=1), now + timedelta(minutes=5)


def test_reprice_fills_unpriced_rows(db_session, test_user):
    """NULL-cost rows get repriced from stored tokens and current prices."""
    ai_model = _create_model(
        db_session,
        test_user,
        pricing={"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
    )
    row = _log_unpriced_row(db_session, test_user, ai_model)
    start, end = _window()

    result = reprice_gateway_usage(
        db_session, account_id=test_user.account_id, start=start, end=end
    )

    assert result.rows_examined == 1
    assert result.rows_updated == 1
    db_session.refresh(row)
    # 1000 * 0.01/1k + 100 * 0.02/1k = 0.012
    assert row.estimated_cost == 0.012
    assert row.cost_source == "model_config"
    assert (row.meta_data or {}).get("repriced_at")
    assert (row.meta_data or {}).get("previous_estimated_cost") is None


def test_reprice_dry_run_persists_nothing(db_session, test_user):
    """Dry-run reports what would change without writing."""
    ai_model = _create_model(
        db_session, test_user, pricing={"input_price_per_1k": 0.01}
    )
    row = _log_unpriced_row(db_session, test_user, ai_model)
    start, end = _window()

    result = reprice_gateway_usage(
        db_session,
        account_id=test_user.account_id,
        start=start,
        end=end,
        dry_run=True,
    )

    assert result.rows_updated == 1
    assert result.dry_run is True
    db_session.refresh(row)
    assert row.estimated_cost is None


def test_reprice_skips_subscription_rows(db_session, test_user):
    """Subscription-covered rows keep their $0 cost."""
    ai_model = _create_model(
        db_session, test_user, pricing={"input_price_per_1k": 0.01}
    )
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        estimated_cost=0.0,
        cost_source="subscription",
    )
    start, end = _window()

    result = reprice_gateway_usage(
        db_session,
        account_id=test_user.account_id,
        start=start,
        end=end,
        only_unpriced=False,
    )

    assert result.rows_skipped >= 1
    db_session.refresh(row)
    assert row.estimated_cost == 0.0
    assert row.cost_source == "subscription"


def test_reprice_does_not_touch_budget_spend(db_session, test_user):
    """Repricing is analytics-only: no budget spend activity is created."""
    ai_model = _create_model(
        db_session, test_user, pricing={"input_price_per_1k": 0.01}
    )
    _log_unpriced_row(db_session, test_user, ai_model)
    start, end = _window()
    spend_rows_before = (
        db_session.query(BudgetSpendActivity)
        .filter(BudgetSpendActivity.account_id == test_user.account_id)
        .count()
    )

    reprice_gateway_usage(
        db_session, account_id=test_user.account_id, start=start, end=end
    )

    spend_rows_after = (
        db_session.query(BudgetSpendActivity)
        .filter(BudgetSpendActivity.account_id == test_user.account_id)
        .count()
    )
    assert spend_rows_after == spend_rows_before


def test_reprice_single_row_after_live_lookup(db_session, test_user):
    """A single unpriced row is fixed once the model gains a price."""
    import litellm

    from preloop.services.usage_repricing import reprice_single_row

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Live Lookup Model",
            "provider_name": "openai",
            "model_identifier": "live-lookup-model-x",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "live-lookup-model-x",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="live-lookup-model-x",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        estimated_cost=None,
        cost_source="unpriced",
    )

    # Row cannot be priced yet (model unknown to the price map).
    assert reprice_single_row(db_session, api_usage_id=row.id) is False

    litellm.register_model(
        {
            "live-lookup-model-x": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 0.00001,
                "output_cost_per_token": 0.00002,
            }
        }
    )
    try:
        assert reprice_single_row(db_session, api_usage_id=row.id) is True
        db_session.refresh(row)
        # 1000 * 0.00001 + 100 * 0.00002 = 0.012
        assert row.estimated_cost == 0.012
        assert row.cost_source == "catalog"
        assert (row.meta_data or {}).get("repriced_by") == "live_price_lookup"
    finally:
        litellm.model_cost.pop("live-lookup-model-x", None)


def test_reprice_consults_live_lookup_for_models_missing_from_the_catalog(
    db_session, test_user, monkeypatch
):
    """A model absent from the local snapshot is priced via the live lookup.

    Without this, a backfill re-derives "unpriced" for every such row and
    reports updated=0, so the rows can never become priceable by repricing.
    """
    ai_model = _create_model(db_session, test_user)
    _log_unpriced_row(db_session, test_user, ai_model)

    calls = []

    def _fake_lookup(candidates):
        calls.append(list(candidates))
        # Simulate upstream registration making the model priceable.
        monkeypatch.setattr(
            usage_repricing,
            "estimate_ai_model_usage_cost_detailed",
            lambda *a, **k: CostEstimate(cost=0.25, source="catalog"),
        )
        return candidates[0]

    monkeypatch.setattr(
        usage_repricing,
        "estimate_ai_model_usage_cost_detailed",
        lambda *a, **k: CostEstimate(cost=None, source="unpriced"),
    )
    monkeypatch.setattr(usage_repricing, "lookup_model_price_now", _fake_lookup)

    start, end = _window()
    result = reprice_gateway_usage(
        db_session, account_id=str(test_user.account_id), start=start, end=end
    )

    assert calls, "an unpriced row must trigger the live price lookup"
    assert result.rows_updated == 1
    assert result.cost_after == 0.25


def test_reprice_looks_up_each_model_once_not_once_per_row(
    db_session, test_user, monkeypatch
):
    """The lookup is cached per model so a large backfill stays cheap."""
    ai_model = _create_model(db_session, test_user)
    for _ in range(5):
        _log_unpriced_row(db_session, test_user, ai_model)

    calls = []
    monkeypatch.setattr(
        usage_repricing,
        "estimate_ai_model_usage_cost_detailed",
        lambda *a, **k: CostEstimate(cost=None, source="unpriced"),
    )
    monkeypatch.setattr(
        usage_repricing,
        "lookup_model_price_now",
        lambda candidates: calls.append(1) or None,
    )

    start, end = _window()
    reprice_gateway_usage(
        db_session, account_id=str(test_user.account_id), start=start, end=end
    )

    assert len(calls) == 1, f"expected one lookup for one model, got {len(calls)}"


def test_reprice_survives_a_failing_live_lookup(db_session, test_user, monkeypatch):
    """A lookup error must not abort the backfill."""
    ai_model = _create_model(db_session, test_user)
    _log_unpriced_row(db_session, test_user, ai_model)

    def _boom(candidates):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(usage_repricing, "lookup_model_price_now", _boom)

    start, end = _window()
    result = reprice_gateway_usage(
        db_session, account_id=str(test_user.account_id), start=start, end=end
    )

    assert result.rows_examined == 1


def test_reprice_single_row_uses_stored_provider_cost(db_session, test_user):
    """A stored unpriced row whose usage payload carries the provider cost is
    repriced from it — the per-row entry point a historical backfill will call.

    Rows recorded before usage accounting was requested may still have
    ``cost_details`` in their persisted ``usage_details`` (some prod rows do);
    repricing them must adopt the provider figure and mark
    ``cost_source='provider'``.
    """
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenRouter Auto",
            "provider_name": "openrouter",
            "model_identifier": "openrouter/auto-beta",
            "api_endpoint": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openrouter/auto-beta",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.4,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openrouter/auto-beta",
        provider_name="openrouter",
        prompt_tokens=973,
        completion_tokens=15,
        total_tokens=988,
        estimated_cost=None,
        cost_source="unpriced",
        meta_data={
            "usage_details": {
                "prompt_tokens": 973,
                "completion_tokens": 15,
                "cost_details": {"upstream_inference_cost": 0.00001946},
            }
        },
    )

    updated = usage_repricing.reprice_single_row(db_session, api_usage_id=row.id)

    assert updated is True
    db_session.refresh(row)
    assert row.cost_source == "provider"
    assert row.estimated_cost == 0.00001946


def test_reprice_single_row_does_not_double_count_credits_shape(db_session, test_user):
    """Credits-based OpenRouter rows persist cost AND an identical
    cost_details.upstream_inference_cost; repricing must adopt the charge
    once, not their sum (#224). reprice_single_row funnels through
    provider_reported_cost, so this pins the whole path.
    """
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenRouter Auto",
            "provider_name": "openrouter",
            "model_identifier": "openrouter/auto-beta",
            "api_endpoint": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openrouter/auto-beta",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.4,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openrouter/auto-beta",
        provider_name="openrouter",
        prompt_tokens=42,
        completion_tokens=7,
        total_tokens=49,
        estimated_cost=None,
        cost_source="unpriced",
        meta_data={
            "usage_details": {
                "prompt_tokens": 42,
                "completion_tokens": 7,
                "cost": 0.000001979964,
                "cost_details": {"upstream_inference_cost": 0.000001979964},
            }
        },
    )

    updated = usage_repricing.reprice_single_row(db_session, api_usage_id=row.id)

    assert updated is True
    db_session.refresh(row)
    assert row.cost_source == "provider"
    assert row.estimated_cost == pytest.approx(0.000001979964)


def test_reprice_examines_zero_cost_rows_tagged_unpriced(db_session, test_user):
    """A row tagged 'unpriced' but carrying a stray $0 cost (legacy write) is
    selected by only_unpriced and healed — previously invisible because the
    filter matched NULL costs only."""
    ai_model = _create_model(
        db_session,
        test_user,
        pricing={"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
    )
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.4,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        estimated_cost=0.0,
        cost_source="unpriced",
    )
    start, end = _window()

    result = reprice_gateway_usage(
        db_session, account_id=test_user.account_id, start=start, end=end
    )

    assert result.rows_examined == 1
    assert result.rows_updated == 1
    db_session.refresh(row)
    assert row.estimated_cost == 0.012
    assert row.cost_source == "model_config"


@pytest.mark.parametrize("source", ["provider", "reconciled", "imported"])
def test_reprice_never_overwrites_provider_side_actuals(db_session, test_user, source):
    """provider/reconciled/imported costs are actuals; even a full
    only_unpriced=False recompute must not replace them with estimates."""
    ai_model = _create_model(
        db_session,
        test_user,
        pricing={"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
    )
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.4,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        estimated_cost=0.5,
        cost_source=source,
    )
    start, end = _window()

    result = reprice_gateway_usage(
        db_session,
        account_id=test_user.account_id,
        start=start,
        end=end,
        only_unpriced=False,
    )

    assert result.rows_skipped >= 1
    db_session.refresh(row)
    assert row.estimated_cost == 0.5
    assert row.cost_source == source


@pytest.mark.parametrize("source", ["provider", "reconciled", "imported"])
def test_reprice_single_row_refuses_protected_sources(db_session, test_user, source):
    """The single-row heal path honors the same protection as the bulk pass."""
    ai_model = _create_model(
        db_session, test_user, pricing={"input_price_per_1k": 0.01}
    )
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.4,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        estimated_cost=0.5,
        cost_source=source,
    )

    assert usage_repricing.reprice_single_row(db_session, api_usage_id=row.id) is False
    db_session.refresh(row)
    assert row.estimated_cost == 0.5
    assert row.cost_source == source
