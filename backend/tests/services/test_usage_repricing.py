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


@pytest.mark.parametrize("source", ["unpriced", "override", "model_config"])
@pytest.mark.parametrize("single", [False, True])
def test_reprice_repairs_free_override_metadata(
    db_session, test_user, source, single
) -> None:
    """A historic zero still needs provenance and pricing availability healed."""
    from preloop.models.crud import crud_model_price_override

    ai_model = _create_model(db_session, test_user)
    override = crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "ai_model_id": ai_model.id,
            "model_alias": "openai/gpt-5",
            "input_price_per_1k": 0.0,
            "output_price_per_1k": 0.0,
            "is_active": True,
        },
    )
    row = _log_unpriced_row(db_session, test_user, ai_model)
    row.estimated_cost = 0.0
    row.cost_source = source
    row.meta_data = {
        "pricing_override_id": None,
        "budget": {"pricing_available": False, "allowed": True, "spent": 4.2},
    }
    db_session.commit()
    start, end = _window()
    if single:
        assert usage_repricing.reprice_single_row(db_session, api_usage_id=row.id)
    else:
        result = reprice_gateway_usage(
            db_session, account_id=test_user.account_id, start=start, end=end
        )
        assert result.rows_updated == 1
    db_session.refresh(row)
    assert row.estimated_cost == 0.0
    assert row.cost_source == "override"
    assert row.meta_data["pricing_override_id"] == str(override.id)
    assert row.meta_data["budget"] == {
        "pricing_available": True,
        "allowed": True,
        "spent": 4.2,
    }
    assert not usage_repricing.reprice_single_row(db_session, api_usage_id=row.id)


def test_free_alias_backfill_leaves_dynamic_route_unpriced(
    db_session, test_user, monkeypatch
) -> None:
    """Free aliases heal the aggregate; dynamic routing remains unresolved."""
    from preloop.models.crud import crud_model_price_override

    monkeypatch.setattr(usage_repricing, "lookup_model_price_now", lambda _: False)
    free_rows = []
    for index in range(5):
        alias = f"openai-compatible/custom-free-{index}"
        model = _create_model(db_session, test_user)
        model.provider_name = "openai-compatible"
        model.model_identifier = f"custom-free-{index}"
        model.meta_data = {}
        db_session.commit()
        override = crud_model_price_override.create_for_account(
            db_session,
            account_id=test_user.account_id,
            obj_in={
                "ai_model_id": model.id,
                "model_alias": alias,
                "input_price_per_1k": 0.0,
                "output_price_per_1k": 0.0,
                "is_active": True,
            },
        )
        for _ in range(35):
            row = _log_unpriced_row(db_session, test_user, model)
            row.model_alias = alias
            row.meta_data = {"budget": {"pricing_available": False}}
            db_session.commit()
            free_rows.append((row, str(override.id)))
    dynamic = _create_model(db_session, test_user)
    dynamic.provider_name = "openai-compatible"
    dynamic.model_identifier = "openrouter/auto-beta"
    dynamic.meta_data = {}
    db_session.commit()
    dynamic_rows = [_log_unpriced_row(db_session, test_user, dynamic) for _ in range(5)]
    for row in dynamic_rows:
        row.model_alias = "openrouter/auto-beta"
    db_session.commit()
    start, end = _window()
    summary_kwargs = dict(
        account_id=str(test_user.account_id), start_date=start, end_date=end
    )
    assert (
        crud_api_usage.get_gateway_usage_summary(db_session, **summary_kwargs)[
            "unpriced_requests"
        ]
        == 180
    )
    preview = reprice_gateway_usage(
        db_session,
        account_id=test_user.account_id,
        start=start,
        end=end,
        dry_run=True,
        batch_size=20,
    )
    assert preview.rows_updated == 175
    assert (
        crud_api_usage.get_gateway_usage_summary(db_session, **summary_kwargs)[
            "unpriced_requests"
        ]
        == 180
    )
    applied = reprice_gateway_usage(
        db_session, account_id=test_user.account_id, start=start, end=end, batch_size=20
    )
    assert applied.rows_updated == 175
    assert (
        crud_api_usage.get_gateway_usage_summary(db_session, **summary_kwargs)[
            "unpriced_requests"
        ]
        == 5
    )
    for row, override_id in free_rows:
        db_session.refresh(row)
        assert row.estimated_cost == 0.0
        assert row.meta_data["pricing_override_id"] == override_id
        assert row.meta_data["budget"]["pricing_available"] is True
    for row in dynamic_rows:
        db_session.refresh(row)
        assert row.estimated_cost is None
        assert row.cost_source == "unpriced"
    assert (
        reprice_gateway_usage(
            db_session, account_id=test_user.account_id, start=start, end=end
        ).rows_updated
        == 0
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_reprice_recovers_per_request_actuals_without_free_route_override(
    db_session, test_user, monkeypatch, dry_run
) -> None:
    """Generation actuals repair eligible dynamic requests without inventing $0."""
    from types import SimpleNamespace

    model = _create_model(db_session, test_user)
    model.provider_name = "openai-compatible"
    model.model_identifier = "openrouter/auto-beta"
    model.api_endpoint = "https://openrouter.ai/api/v1"
    model.meta_data = {}
    db_session.commit()
    rows = [_log_unpriced_row(db_session, test_user, model) for _ in range(3)]
    for index, row in enumerate(rows):
        row.model_alias = "openrouter/auto-beta"
        row.upstream_request_id = f"gen-example-{index}"
        row.meta_data = {
            "budget": {"pricing_available": False, "allowed": True},
            "usage_details": {"prompt_tokens": 1000},
        }
    db_session.commit()
    monkeypatch.setattr(usage_repricing, "lookup_model_price_now", lambda _: False)
    monkeypatch.setattr(
        usage_repricing,
        "estimate_ai_model_usage_cost_detailed",
        lambda *a, **k: CostEstimate(cost=None, source="unpriced"),
    )
    actuals = {str(rows[0].id): 0.025, str(rows[1].id): 0.0}

    class Lookup:
        def __init__(self, db, *, account_id):
            assert account_id == str(test_user.account_id)
            self.summary = {"attempted": 0, "recovered": 0}

        def lookup(self, *, ai_model, usage_row):
            self.summary["attempted"] += 1
            cost = actuals.get(str(usage_row.id))
            if cost is None:
                return None
            self.summary["recovered"] += 1
            generation_id = usage_row.upstream_request_id
            return SimpleNamespace(
                cost=cost,
                usage_details={"cost": cost},
                provenance={
                    "generation_id": generation_id,
                    "source": "openrouter_generation",
                },
            )

    monkeypatch.setattr(usage_repricing, "OpenRouterGenerationCostLookup", Lookup)
    start, end = _window()
    result = reprice_gateway_usage(
        db_session,
        account_id=test_user.account_id,
        start=start,
        end=end,
        dry_run=dry_run,
    )
    assert result.rows_updated == 2
    assert result.provider_lookup == {"attempted": 3, "recovered": 2}
    for index, row in enumerate(rows):
        db_session.refresh(row)
        if dry_run or index == 2:
            assert row.estimated_cost is None
            assert row.cost_source == "unpriced"
        else:
            assert row.estimated_cost == actuals[str(row.id)]
            assert row.cost_source == "provider"
            assert row.meta_data["budget"] == {
                "pricing_available": True,
                "allowed": True,
            }
            assert (
                row.meta_data["provider_cost_lookup"]["generation_id"]
                == f"gen-example-{index}"
            )
            assert row.meta_data["usage_details"]["prompt_tokens"] == 1000
            assert row.meta_data["usage_details"]["cost"] == actuals[str(row.id)]
    if not dry_run:
        second = reprice_gateway_usage(
            db_session,
            account_id=test_user.account_id,
            start=start,
            end=end,
            only_unpriced=False,
        )
        assert second.rows_updated == 0
        assert second.rows_skipped == 2
        assert second.provider_lookup == {"attempted": 1, "recovered": 0}


def test_reprice_generation_lookup_uses_stored_request_id(
    db_session, test_user, monkeypatch
) -> None:
    """The real recovery helper reads the persisted upstream generation ID."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from preloop.services import openrouter_generation_cost

    model = _create_model(db_session, test_user)
    model.provider_name = "openai-compatible"
    model.model_identifier = "openrouter/auto-beta"
    model.api_endpoint = "https://openrouter.ai/api/v1"
    model.meta_data = {}
    row = _log_unpriced_row(db_session, test_user, model)
    row.model_alias = "openrouter/auto-beta"
    row.upstream_request_id = "gen-synthetic-recovery"
    db_session.commit()
    monkeypatch.setattr(usage_repricing, "lookup_model_price_now", lambda _: False)
    monkeypatch.setattr(
        usage_repricing,
        "estimate_ai_model_usage_cost_detailed",
        lambda *a, **k: CostEstimate(cost=None, source="unpriced"),
    )
    secret_service = SimpleNamespace(
        resolve_ai_model_credentials=Mock(
            return_value=SimpleNamespace(
                credential_type="api_key", value="synthetic-key"
            )
        )
    )
    monkeypatch.setattr(
        openrouter_generation_cost, "get_secret_service", lambda: secret_service
    )
    response = Mock(status_code=200)
    response.json.return_value = {
        "data": {"id": row.upstream_request_id, "total_cost": 0.02, "is_byok": False}
    }
    get = Mock(return_value=response)
    monkeypatch.setattr(openrouter_generation_cost.requests, "get", get)
    start, end = _window()
    result = reprice_gateway_usage(
        db_session, account_id=test_user.account_id, start=start, end=end
    )
    assert result.rows_updated == 1
    assert result.provider_lookup["attempted"] == 1
    assert result.provider_lookup["recovered"] == 1
    db_session.refresh(row)
    assert row.estimated_cost == 0.02
    assert row.cost_source == "provider"
    assert (
        row.meta_data["provider_cost_lookup"]["generation_id"]
        == "gen-synthetic-recovery"
    )
    assert get.call_args.kwargs["params"] == {"id": "gen-synthetic-recovery"}
    assert get.call_args.kwargs["allow_redirects"] is False
    assert (
        secret_service.resolve_ai_model_credentials.call_args.kwargs["allow_refresh"]
        is False
    )


def test_reprice_does_not_scan_another_accounts_usage(db_session, test_user) -> None:
    """The account window excludes another tenant even with a shared model ID."""
    from types import SimpleNamespace
    from preloop.models.crud import crud_account

    model = _create_model(db_session, test_user, pricing={"input_price_per_1k": 0.01})
    own = _log_unpriced_row(db_session, test_user, model)
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    foreign = _log_unpriced_row(
        db_session, SimpleNamespace(id=test_user.id, account_id=other.id), model
    )
    start, end = _window()
    result = reprice_gateway_usage(
        db_session, account_id=test_user.account_id, start=start, end=end
    )
    assert result.rows_examined == result.rows_updated == 1
    db_session.refresh(own)
    db_session.refresh(foreign)
    assert own.estimated_cost is not None
    assert foreign.estimated_cost is None
    assert foreign.cost_source == "unpriced"
