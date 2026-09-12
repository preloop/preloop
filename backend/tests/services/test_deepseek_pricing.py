"""Native DeepSeek effective dates, time bands and billing-source precedence."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from preloop.models import models
from preloop.services import ai_model_pricing, deepseek_pricing
from preloop.services.model_pricing import estimate_ai_model_usage_cost_detailed


def model(
    identifier: str = "deepseek/deepseek-v4-pro",
    provider: str = "deepseek",
    endpoint: str | None = None,
) -> models.AIModel:
    return models.AIModel(
        id="00000000-0000-0000-0000-000000000001",
        provider_name=provider,
        model_identifier=identifier,
        api_endpoint=endpoint,
    )


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("instant", "band", "input_price"),
    [
        ("2026-09-11T00:59:59", "off_peak", 0.66),
        ("2026-09-11T01:00:00", "peak", 1.32),
        ("2026-09-11T03:59:59", "peak", 1.32),
        ("2026-09-11T04:00:00", "off_peak", 0.66),
        ("2026-09-11T05:59:59", "off_peak", 0.66),
        ("2026-09-11T06:00:00", "peak", 1.32),
        ("2026-09-11T09:59:59", "peak", 1.32),
        ("2026-09-11T10:00:00", "off_peak", 0.66),
        ("2026-09-12T02:00:00", "off_peak", 0.66),
        ("2026-09-13T07:00:00", "off_peak", 0.66),
        # The later weekday restriction is not backdated into August.
        ("2026-08-22T02:00:00", "peak", 1.32),
        # Pro continues at its own prices after the rescinded retirement.
        ("2026-09-14T06:00:00", "peak", 1.32),
    ],
)
def test_native_pro_time_bands(instant: str, band: str, input_price: float) -> None:
    tariff = deepseek_pricing.native_tariff(model(), observed_at=moment(instant))
    assert tariff is not None
    assert tariff.band == band
    assert tariff.input_per_1m == input_price


@pytest.mark.parametrize(
    ("instant", "price", "canonical"),
    [
        ("2026-08-16T15:59:59", None, None),
        ("2026-08-16T16:00:00", 0.22, "deepseek-v4-flash"),
        ("2026-09-10T03:59:59", 0.44, "deepseek-v4-flash"),
        ("2026-09-10T04:00:00", 0.15, "deepseek-flash"),
    ],
)
def test_flash_effective_instants(
    instant: str, price: float | None, canonical: str | None
) -> None:
    tariff = deepseek_pricing.native_tariff(
        model("deepseek-v4-flash"), observed_at=moment(instant)
    )
    if price is None:
        assert tariff is None
    else:
        assert tariff is not None
        assert tariff.input_per_1m == price
        assert tariff.model == canonical


@pytest.mark.parametrize(
    ("provider", "endpoint", "identifier", "native"),
    [
        ("deepseek", None, "deepseek/deepseek-v4-pro", True),
        ("openai-compatible", "https://api.deepseek.com/v1", "deepseek-v4-pro", True),
        ("deepseek", "https://openrouter.ai/api/v1", "deepseek-v4-pro", False),
        ("openrouter", None, "deepseek/deepseek-v4-pro", False),
        ("deepseek", "https://api.deepseek.com.example.com", "deepseek-v4-pro", False),
        ("deepseek", "https://example.com/api.deepseek.com", "deepseek-v4-pro", False),
        ("bedrock", None, "deepseek-v4-pro", False),
        ("deepseek", None, "deepseek-v4-flash-0731", False),
        ("deepseek", None, "deepseek-flash", True),
        ("deepseek", None, "deepseek-v4-flash-vision-exp", True),
    ],
)
def test_serving_endpoint_and_exact_alias(
    provider: str, endpoint: str | None, identifier: str, native: bool
) -> None:
    tariff = deepseek_pricing.native_tariff(
        model(identifier, provider, endpoint),
        observed_at=moment("2026-09-12T06:00:00"),
    )
    assert (tariff is not None) == native


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_cache_hit_tokens": 900_000},
        {"prompt_tokens_details": {"cached_tokens": 900_000}},
        {"cache_read_input_tokens": 900_000},
    ],
)
def test_estimator_uses_cache_split_and_persists_tariff(usage: dict) -> None:
    estimate = estimate_ai_model_usage_cost_detailed(
        model(),
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
        usage_details=usage,
        observed_at=moment("2026-09-12T06:00:00"),
    )
    assert estimate.cost == pytest.approx(0.9 * 0.022 + 0.1 * 0.66 + 1.98)
    assert estimate.source == "catalog"
    assert estimate.pricing_snapshot["rate_band"] == "off_peak"
    assert estimate.pricing_snapshot["estimate_limitations"]


def test_explicit_zero_cached_count_wins_over_fallback() -> None:
    tariff = deepseek_pricing.native_tariff(
        model(), observed_at=moment("2026-09-12T06:00:00")
    )
    assert (
        tariff.estimate(
            prompt_tokens=1_000_000,
            completion_tokens=0,
            usage_details={
                "prompt_tokens_details": {"cached_tokens": 0},
                "prompt_cache_hit_tokens": 1_000_000,
            },
        )
        == 0.66
    )


@pytest.mark.parametrize("kind", ["override", "model_config", "provider"])
def test_explicit_prices_and_actual_cost_precede_tariff(kind: str) -> None:
    ai_model = model()
    configured = {"input_price_per_1k": 0.001}
    if kind == "model_config":
        ai_model.meta_data = {"pricing": configured}
    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        usage_details={"cost": 5.0},
        pricing_override=configured if kind == "override" else None,
        observed_at=moment("2026-09-12T06:00:00"),
    )
    assert estimate.cost == (5.0 if kind == "provider" else 1.0)
    assert estimate.source == kind
    assert estimate.pricing_snapshot is None


def test_price_readback_matches_selected_tariff_and_exposes_provenance() -> None:
    ai_model = model()
    tariff = deepseek_pricing.native_tariff(
        ai_model, observed_at=moment("2026-09-12T06:00:00")
    )
    with patch.object(deepseek_pricing, "native_tariff", return_value=tariff):
        response = ai_model_pricing._pricing_response(ai_model, None)
    assert response.price.input_per_1m == 0.66
    assert response.price.output_per_1m == 1.98
    assert response.price.cached_input_per_1m == 0.022
    assert response.catalog_provenance["rate_band"] == "off_peak"
    assert response.catalog_provenance["estimate_limitations"]
    assert response.effective_from == deepseek_pricing.SEPTEMBER_START


def test_reviewed_policy_activates_at_effective_instant_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    entry = {
        "preloop_price_policy": {
            "kind": "deepseek_utc_bands",
            "effective_from": "2026-09-14T06:00:00+00:00",
            "peak": {
                "input_per_1m": 2.0,
                "output_per_1m": 6.0,
                "cached_input_per_1m": 0.06,
            },
            "off_peak": {
                "input_per_1m": 1.0,
                "output_per_1m": 3.0,
                "cached_input_per_1m": 0.03,
            },
            "peak_hours_utc": [[1, 4], [6, 10]],
            "peak_weekdays": [0, 1, 2, 3, 4],
            "public_holidays": "unspecified",
        },
        "preloop_price_provenance": {
            "revision": "reviewed-example",
            "verified_at": "2026-09-12T00:00:00+00:00",
            "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
        },
    }
    monkeypatch.setitem(litellm.model_cost, "deepseek/deepseek-v4-pro", entry)
    before = deepseek_pricing.native_tariff(
        model(), observed_at=moment("2026-09-14T05:59:59")
    )
    after = deepseek_pricing.native_tariff(
        model(), observed_at=moment("2026-09-14T06:00:00")
    )
    assert before.input_per_1m == 0.66
    assert after.input_per_1m == 2.0
    assert after.metadata()["policy_version"] == "reviewed-example"
    assert (
        deepseek_pricing.native_tariff(
            model(endpoint="https://openrouter.ai/api/v1"),
            observed_at=moment("2026-09-14T06:00:00"),
        )
        is None
    )


def test_gateway_records_request_start_tariff_across_stream_boundary(
    db_session, test_user
) -> None:
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext
    from preloop.models.crud import crud_ai_model
    from preloop.services.openai_gateway import OpenAIGatewayService

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Native tariff test",
            "provider_name": "deepseek",
            "model_identifier": "deepseek-v4-pro",
            "api_key": "test-provider-key",
        },
        account_id=test_user.account_id,
    )
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="test", user=test_user)
    )
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    with patch("preloop.services.openai_gateway.datetime") as clock:
        clock.now.return_value = moment("2026-09-11T04:00:01")
        service._record_gateway_request_inner(
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=2.0,
            ai_model=ai_model,
            requested_model="deepseek-v4-pro",
            response_payload={"usage": usage},
            upstream_response={"usage": usage},
            endpoint_kind="chat.completions",
        )
    row = db_session.query(models.ApiUsage).filter_by(ai_model_id=ai_model.id).one()
    assert row.estimated_cost == 1.32
    snapshot = row.meta_data["pricing_snapshot"]
    assert snapshot["rate_band"] == "peak"
    assert snapshot["observed_at"] == "2026-09-11T03:59:59+00:00"
    assert snapshot["timestamp_basis"] == "request_start_estimated_from_duration"


@pytest.mark.parametrize("feed_alias", ["deepseek-flash", "deepseek-v4-flash"])
def test_reviewed_future_version_preserves_inflight_previous_tariff(
    monkeypatch: pytest.MonkeyPatch,
    feed_alias: str,
) -> None:
    import litellm

    def policy(effective_from: str, price: float) -> dict:
        return {
            "kind": "deepseek_utc_bands",
            "effective_from": effective_from,
            "peak": {
                "input_per_1m": price,
                "output_per_1m": price,
                "cached_input_per_1m": price,
            },
            "off_peak": {
                "input_per_1m": price / 2,
                "output_per_1m": price / 2,
                "cached_input_per_1m": price / 2,
            },
            "peak_hours_utc": [[1, 4], [6, 10]],
            "peak_weekdays": [0, 1, 2, 3, 4],
            "public_holidays": "unspecified",
        }

    monkeypatch.setitem(
        litellm.model_cost,
        f"deepseek/{feed_alias}",
        {
            "preloop_price_policy": policy("2026-09-14T06:00:00+00:00", 4),
            "preloop_price_provenance": {"revision": "v2"},
            "preloop_price_policy_history": [
                {
                    "policy": policy("2026-09-11T00:00:00+00:00", 2),
                    "provenance": {"revision": "v1"},
                }
            ],
        },
    )
    # A current canonical feed policy also prices retired native aliases.
    earlier = deepseek_pricing.native_tariff(
        model("deepseek-v4-flash"), observed_at=moment("2026-09-11T06:00:00")
    )
    newer = deepseek_pricing.native_tariff(
        model("deepseek-flash"), observed_at=moment("2026-09-14T06:00:00")
    )
    assert earlier.input_per_1m == 2
    assert earlier.metadata()["policy_version"] == "v1"
    assert newer.input_per_1m == 4
    assert newer.metadata()["policy_version"] == "v2"


@pytest.mark.parametrize(
    ("recorded_at", "snapshot"),
    [
        ("2026-09-11T03:59:59", None),
        ("2026-09-11T04:00:01", {"observed_at": "2026-09-11T03:59:59+00:00"}),
        ("2026-09-11T03:59:59", {"observed_at": "invalid"}),
        ("2026-09-11T03:59:59", {"observed_at": "2026-09-11T04:00:01"}),
    ],
)
def test_manual_reprice_uses_usage_timestamp_and_records_tariff(
    db_session, test_user, recorded_at: str, snapshot: dict | None
) -> None:
    from preloop.models.crud import crud_ai_model, crud_api_usage
    from preloop.services.usage_repricing import reprice_single_row

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Historical tariff test",
            "provider_name": "deepseek",
            "model_identifier": "deepseek-v4-pro",
            "api_key": "test-provider-key",
        },
        account_id=test_user.account_id,
    )
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=1.0,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        provider_name="deepseek",
        model_alias="deepseek-v4-pro",
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        estimated_cost=None,
        cost_source="unpriced",
        meta_data={"pricing_snapshot": snapshot},
    )
    row.timestamp = moment(recorded_at)
    db_session.flush()
    assert reprice_single_row(db_session, api_usage_id=row.id)
    db_session.refresh(row)
    assert row.estimated_cost == 1.32
    assert row.meta_data["pricing_snapshot"]["rate_band"] == "peak"
