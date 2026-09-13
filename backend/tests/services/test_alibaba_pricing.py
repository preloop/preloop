"""Alibaba tariffs must match the serving region and reported token classes."""

from unittest.mock import patch

import pytest

from preloop.models import models
from preloop.services.alibaba_price_catalog import (
    ingest_native_models,
    reset_live_state_for_tests,
)
from preloop.services.ai_model_pricing import _catalog_entry
from preloop.services.model_pricing import (
    CostEstimate,
    estimate_ai_model_usage_cost_detailed,
)


@pytest.fixture(autouse=True)
def _reset_alibaba_overlay() -> None:
    reset_live_state_for_tests()
    yield
    reset_live_state_for_tests()


def _model(model: str = "qwen3.8-max", **kwargs: str) -> models.AIModel:
    return models.AIModel(
        provider_name=kwargs.get("provider", "qwen"),
        model_identifier=model,
        api_endpoint=kwargs.get(
            "endpoint", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        ),
    )


def _estimate(
    model: models.AIModel, usage: dict | None = None, **kwargs
) -> CostEstimate:
    return estimate_ai_model_usage_cost_detailed(
        model,
        prompt_tokens=10000,
        completion_tokens=1000,
        total_tokens=11000,
        usage_details=usage,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("qwen3.8-max", 0.026),
        ("deepseek-v4-pro", 0.0288),
        ("deepseek-v4-flash", 0.0024),
        ("glm-5.2", 0.0184),
        ("kimi-k2.7-code", 0.0135),
        ("kimi-k3", 0.045),
        ("qwen3.8-flash", 0.00197),
    ],
)
def test_singapore_headline_list_costs(model: str, expected: float) -> None:
    result = _estimate(_model(model))
    assert result.source == "catalog"
    assert result.cost == pytest.approx(expected)


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        "https://tenant.us-east-1.maas.aliyuncs.com/compatible-mode/v1",
    ],
)
def test_unverified_region_never_uses_international_or_native_rate(
    endpoint: str,
) -> None:
    model = _model("deepseek-v4-pro", endpoint=endpoint)
    assert _estimate(model).cost is None
    assert _estimate(model).source == "unpriced"
    assert _catalog_entry(model) is None


def test_workspace_and_custom_provider_share_the_exact_serving_tariff() -> None:
    model = _model(
        "deepseek-v4-pro",
        provider="custom",
        endpoint="https://tenant.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    )
    with patch("litellm.cost_per_token", side_effect=AssertionError("wrong provider")):
        assert _estimate(model).cost == pytest.approx(0.0288)
    key, entry = _catalog_entry(model)
    assert "singapore" in key
    assert entry["input_cost_per_token"] == pytest.approx(2.4e-6)


def test_unknown_snapshot_and_gateway_alias_do_not_borrow_a_price() -> None:
    model = _model("deepseek-v4-pro-unknown")
    model.meta_data = {"gateway": {"model_alias": "deepseek-v4-pro"}}
    assert _estimate(model).cost is None
    assert _catalog_entry(model) is None


@pytest.mark.parametrize("mode", ["implicit", "explicit"])
def test_native_cache_observations_without_currency_are_not_prices(mode: str) -> None:
    result = _estimate(
        _model(),
        {
            "_preloop_cache_mode": mode,
            "prompt_tokens_details": {"cached_tokens": 5000},
            "completion_tokens_details": {"reasoning_tokens": 900},
        },
    )
    assert result.cost is None
    assert result.source == "unpriced"


def test_old_cached_usage_without_mode_is_unknown() -> None:
    assert (
        _estimate(_model(), {"prompt_tokens_details": {"cached_tokens": 5000}}).cost
        is None
    )


def test_nested_cache_creation_never_uses_an_unverified_tariff() -> None:
    usage = {
        "_preloop_cache_mode": "explicit",
        "prompt_tokens_details": {
            "cached_tokens": 4000,
            "cache_creation_input_tokens": 1000,
        },
        "completion_tokens_details": {"reasoning_tokens": 900},
    }
    assert _estimate(_model(), usage).cost is None


def test_official_glm_cache_ratio_and_reasoning_are_not_double_counted() -> None:
    usage = {
        "_preloop_cache_mode": "implicit",
        "prompt_tokens_details": {"cached_tokens": 5000},
        "completion_tokens_details": {"reasoning_tokens": 900},
    }
    # 5k standard at1.4 +5k cached at25% +1k total output at4.4 per1M.
    assert _estimate(_model("glm-5.2"), usage).cost == pytest.approx(0.01315)


def test_openrouter_route_keeps_its_authoritative_provider_accounting() -> None:
    result = _estimate(
        _model(endpoint="https://openrouter.ai/api/v1"), {"cost": 0.0123}
    )
    assert result.cost == 0.0123
    assert result.source == "provider"


def test_whitespace_on_endpoint_does_not_change_tariff() -> None:
    result = _estimate(
        _model(endpoint=" https://dashscope-intl.aliyuncs.com/compatible-mode/v1 ")
    )
    assert result.cost == pytest.approx(0.026)


def test_unknown_cache_tariff_stays_unpriced() -> None:
    assert (
        _estimate(
            _model("deepseek-v4-pro"),
            {
                "_preloop_cache_mode": "implicit",
                "prompt_tokens_details": {"cached_tokens": 1000},
            },
        ).cost
        is None
    )


def test_operator_override_remains_authoritative() -> None:
    result = _estimate(
        _model("unknown"),
        pricing_override={
            "input_price_per_1k": 0.01,
            "output_price_per_1k": 0.02,
            "cache_creation_input_price_per_1k": 0.02,
        },
        usage={"prompt_tokens_details": {"cache_creation_input_tokens": 1000}},
    )
    assert result.source == "override"
    assert result.cost == pytest.approx(0.13)


@pytest.mark.parametrize("currency", [None, "CNY", "USD"])
def test_undocumented_provider_money_cannot_bypass_tariff(currency: str | None) -> None:
    result = _estimate(_model(), {"cost": 0.0123, "currency": currency})
    assert result.cost == pytest.approx(0.026)
    assert result.source == "catalog"


def test_qwen_context_limit_does_not_fall_back_to_a_lower_tier() -> None:
    result = estimate_ai_model_usage_cost_detailed(
        _model(),
        prompt_tokens=1_000_001,
        completion_tokens=1000,
        total_tokens=1_001_001,
    )
    assert result.cost is None


def test_impossible_cache_counts_do_not_produce_negative_cost() -> None:
    assert (
        _estimate(
            _model(),
            {
                "_preloop_cache_mode": "implicit",
                "prompt_tokens_details": {"cached_tokens": 11000},
            },
        ).cost
        is None
    )


def test_snapshot_with_gateway_alias_prices_observed_uncached_usage() -> None:
    """A dated upstream ID retains its tariff despite the prefixed client alias."""
    model = _model(
        "qwen3.8-max-0902",
        endpoint="https://tenant.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    )
    model.meta_data = {"gateway": {"model_alias": "qwen/qwen3.8-max-0902"}}
    result = estimate_ai_model_usage_cost_detailed(
        model,
        prompt_tokens=72,
        completion_tokens=69,
        total_tokens=141,
        usage_details={
            "prompt_tokens": 72,
            "completion_tokens": 69,
            "total_tokens": 141,
            "prompt_tokens_details": {
                "text_tokens": 72,
                "audio_tokens": None,
                "cached_tokens": 0,
            },
            "completion_tokens_details": {
                "text_tokens": 69,
                "audio_tokens": None,
                "reasoning_tokens": 59,
            },
        },
    )
    # Reasoning is included in the 69 output tokens, not added again.
    assert result.cost == pytest.approx(0.000558)
    assert result.source == "catalog"


def test_seed_covers_current_singapore_chat_skus() -> None:
    from preloop.services.alibaba_pricing import _SEED

    assert "qwen3.8-flash" in _SEED
    assert "qwen3.5-flash" in _SEED
    assert "qwen-plus" in _SEED
    assert len(_SEED) >= 80


def test_live_native_overlay_prices_cache_on_usd_site() -> None:
    ingest_native_models(
        [
            {
                "model": "qwen3.8-max",
                "prices": [
                    {
                        "range_name": "Default",
                        "prices": [
                            {
                                "type": "input_token",
                                "price": "2",
                                "price_unit": "Per 1M tokens",
                            },
                            {
                                "type": "output_token",
                                "price": "6",
                                "price_unit": "Per 1M tokens",
                            },
                            {
                                "type": "input_token_cache",
                                "price": "0.25",
                                "price_unit": "Per 1M tokens",
                            },
                        ],
                    }
                ],
            }
        ],
        region="singapore-international",
    )
    result = _estimate(
        _model(),
        {
            "_preloop_cache_mode": "implicit",
            "prompt_tokens_details": {"cached_tokens": 5000},
        },
    )
    # 5k standard at $2 + 5k cached at $0.25 + 1k output at $6 per 1M.
    assert result.source == "catalog"
    assert result.cost == pytest.approx(0.01725)


def test_whole_request_tier_is_selected_not_the_lowest() -> None:
    ingest_native_models(
        [
            {
                "model": "tiered-chat",
                "prices": [
                    {
                        "range_name": "0<Token<=32k",
                        "prices": [
                            {
                                "type": "input_token",
                                "price": "1",
                                "price_unit": "Per 1M tokens",
                            },
                            {
                                "type": "output_token",
                                "price": "2",
                                "price_unit": "Per 1M tokens",
                            },
                        ],
                    },
                    {
                        "range_name": "32k<Input<=256k",
                        "prices": [
                            {
                                "type": "input_token",
                                "price": "4",
                                "price_unit": "Per 1M tokens",
                            },
                            {
                                "type": "output_token",
                                "price": "8",
                                "price_unit": "Per 1M tokens",
                            },
                        ],
                    },
                ],
            }
        ],
        region="singapore-international",
    )
    model = _model("tiered-chat")
    low = estimate_ai_model_usage_cost_detailed(
        model, prompt_tokens=10_000, completion_tokens=0, total_tokens=10_000
    )
    mid = estimate_ai_model_usage_cost_detailed(
        model, prompt_tokens=100_000, completion_tokens=0, total_tokens=100_000
    )
    over = estimate_ai_model_usage_cost_detailed(
        model, prompt_tokens=300_000, completion_tokens=0, total_tokens=300_000
    )
    assert low.cost == pytest.approx(0.01)
    assert mid.cost == pytest.approx(0.4)
    assert over.cost is None
