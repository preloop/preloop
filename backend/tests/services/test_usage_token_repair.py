"""Historical cache/reasoning token repair from retained usage (#1401)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from preloop.models.crud import crud_ai_model, crud_api_usage
from preloop.services.usage_token_repair import repair_token_details

RESPONSES_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 10,
    "total_tokens": 1010,
    "input_tokens_details": {"cached_tokens": 800},
    "output_tokens_details": {"reasoning_tokens": 5},
}


def _model(db_session, test_user, pricing=None):
    meta = {"gateway": {"enabled": True, "model_alias": "openai/gpt-5"}}
    if pricing:
        meta["pricing"] = pricing
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Repair Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": meta,
        },
        account_id=test_user.account_id,
    )


def _row(
    db_session,
    test_user,
    ai_model,
    usage=RESPONSES_USAGE,
    cost=1.0,
    cost_source="model_config",
    **columns,
):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=1000,
        completion_tokens=10,
        total_tokens=1010,
        estimated_cost=cost,
        cost_source=cost_source,
        meta_data={"usage_details": usage},
        **columns,
    )


def test_dry_run_reports_and_writes_nothing(db_session, test_user):
    ai_model = _model(db_session, test_user)
    row = _row(db_session, test_user, ai_model)

    result = repair_token_details(db_session, account_id=test_user.account_id)

    assert result.dry_run is True
    assert result.rows_examined == 1
    assert result.rows_repairable == 1
    assert result.rows_updated == 0
    assert result.columns_filled == {
        "cache_read_tokens": 1,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 1,
    }
    db_session.refresh(row)
    assert row.cache_read_tokens is None
    assert row.reasoning_tokens is None


def test_apply_fills_nulls_and_is_idempotent(db_session, test_user):
    ai_model = _model(db_session, test_user)
    row = _row(db_session, test_user, ai_model)

    first = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True
    )
    db_session.refresh(row)
    assert first.rows_updated == 1
    assert row.cache_read_tokens == 800
    assert row.reasoning_tokens == 5
    assert row.cache_creation_tokens is None
    assert row.estimated_cost == 1.0  # cost untouched without --reprice

    second = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True
    )
    assert second.rows_examined == 0
    assert second.rows_updated == 0


def test_existing_values_are_never_overwritten(db_session, test_user):
    ai_model = _model(db_session, test_user)
    row = _row(db_session, test_user, ai_model, cache_read_tokens=11)

    result = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True
    )

    db_session.refresh(row)
    assert result.columns_filled["cache_read_tokens"] == 0
    assert row.cache_read_tokens == 11
    assert row.reasoning_tokens == 5


def test_chat_and_malformed_rows_are_not_candidates_or_not_repaired(
    db_session, test_user
):
    ai_model = _model(db_session, test_user)
    chat = _row(
        db_session,
        test_user,
        ai_model,
        usage={"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 3}},
    )
    bad = _row(
        db_session,
        test_user,
        ai_model,
        usage={"input_tokens_details": {"cached_tokens": -5}},
    )

    result = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True
    )

    # Neither row is a candidate: chat rows were never missed, and the
    # malformed value can never be repaired.
    assert result.rows_examined == 0
    assert result.rows_repairable == 0
    db_session.refresh(chat)
    db_session.refresh(bad)
    assert chat.cache_read_tokens is None
    assert bad.cache_read_tokens is None


def test_window_account_and_limit_bound_the_run(db_session, test_user):
    ai_model = _model(db_session, test_user)
    rows = [_row(db_session, test_user, ai_model) for _ in range(3)]
    now = datetime.now(timezone.utc)

    assert (
        repair_token_details(
            db_session, account_id=test_user.account_id, since=now + timedelta(hours=1)
        ).rows_examined
        == 0
    )
    assert (
        repair_token_details(
            db_session, account_id=test_user.account_id, until=now - timedelta(days=1)
        ).rows_examined
        == 0
    )
    other_account = "00000000-0000-0000-0000-000000000001"
    assert repair_token_details(db_session, account_id=other_account).rows_examined == 0

    limited = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True, limit=2, batch_size=1
    )
    assert limited.rows_updated == 2
    assert limited.limit_reached is True
    rest = repair_token_details(db_session, account_id=test_user.account_id, apply=True)
    assert rest.rows_updated == 1
    for row in rows:
        db_session.refresh(row)
        assert row.cache_read_tokens == 800


def test_reprice_requires_apply(db_session):
    with pytest.raises(ValueError):
        repair_token_details(db_session, reprice=True)


def test_apply_reprice_fixes_configured_pricing_and_spares_subscription(
    db_session, test_user
):
    ai_model = _model(
        db_session,
        test_user,
        pricing={
            "input_price_per_1k": 1.0,
            "cache_read_input_price_per_1k": 0.1,
            "output_price_per_1k": 0.0,
        },
    )
    # Recorded before the fix: cache reads billed at the full input price.
    priced = _row(db_session, test_user, ai_model, cost=1.0)
    subscription = _row(
        db_session, test_user, ai_model, cost=0.0, cost_source="subscription"
    )

    result = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True, reprice=True
    )

    assert result.rows_updated == 2
    assert result.rows_repriced == 1
    db_session.refresh(priced)
    db_session.refresh(subscription)
    # 200 * 1.0/1k + 800 * 0.1/1k = 0.28
    assert priced.estimated_cost == pytest.approx(0.28)
    assert priced.meta_data["repriced_by"] == "token_detail_repair"
    assert subscription.estimated_cost == 0.0
    assert subscription.cost_source == "subscription"
    assert subscription.cache_read_tokens == 800


def test_cli_defaults_to_dry_run(db_session, test_user, monkeypatch):
    import importlib.util
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "repair_usage_token_details.py"
    )
    spec = importlib.util.spec_from_file_location("repair_usage_token_details", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_db_session", lambda: iter([db_session]))
    monkeypatch.setattr(db_session, "close", lambda: None)
    ai_model = _model(db_session, test_user)
    row = _row(db_session, test_user, ai_model)

    out = CliRunner().invoke(module.main, ["--account-id", str(test_user.account_id)])
    assert out.exit_code == 0, out.output
    assert "[DRY RUN] rows repairable: 1" in out.output
    db_session.refresh(row)
    assert row.cache_read_tokens is None

    bad = CliRunner().invoke(module.main, ["--reprice"])
    assert bad.exit_code != 0
    assert "--reprice requires --apply" in bad.output


def test_unrepairable_rows_never_stall_a_bounded_run(db_session, test_user):
    """Malformed values are not candidates, so --limit always makes progress."""
    ai_model = _model(db_session, test_user)
    for bad in (-5, True, 1.5, "abc", None):
        _row(
            db_session,
            test_user,
            ai_model,
            usage={
                "input_tokens_details": {"cached_tokens": bad},
                "output_tokens_details": {"reasoning_tokens": bad},
            },
        )
    good = _row(
        db_session,
        test_user,
        ai_model,
        usage={"input_tokens_details": {"cached_tokens": "12", "x": 1}},
    )
    integral = _row(
        db_session,
        test_user,
        ai_model,
        usage={"output_tokens_details": {"reasoning_tokens": 7.0}},
    )

    first = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True, limit=1
    )
    second = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True, limit=1
    )
    third = repair_token_details(
        db_session, account_id=test_user.account_id, apply=True, limit=1
    )

    assert (first.rows_updated, second.rows_updated, third.rows_examined) == (1, 1, 0)
    db_session.refresh(good)
    db_session.refresh(integral)
    assert good.cache_read_tokens == 12
    assert integral.reasoning_tokens == 7
