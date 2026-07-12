"""Tests for model pricing estimation."""

from preloop.models.models.ai_model import AIModel
from preloop.models.models.model_price_override import ModelPriceOverride
from preloop.services.model_pricing import (
    _iter_litellm_model_candidates,
    estimate_ai_model_usage_cost,
    estimate_ai_model_usage_cost_detailed,
)


def test_candidates_normalize_bedrock_region_prefix() -> None:
    """us./eu. Bedrock inference-profile prefixes yield price-map candidates."""
    ai_model = AIModel(
        provider_name="aws",
        model_identifier="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    )
    candidates = list(_iter_litellm_model_candidates(ai_model))
    assert "anthropic.claude-sonnet-4-5-20250929-v1:0" in candidates
    assert "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0" in candidates
    # Raw id is tried first (most specific wins when present in the map).
    assert candidates[0] == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_candidates_strip_date_suffixes() -> None:
    """Date-stamped model ids fall back to their undated price-map alias."""
    ai_model = AIModel(
        provider_name="anthropic",
        model_identifier="claude-3-5-sonnet-20241022",
    )
    candidates = list(_iter_litellm_model_candidates(ai_model))
    assert "claude-3-5-sonnet" in candidates
    assert "anthropic/claude-3-5-sonnet" in candidates

    ai_model = AIModel(provider_name="openai", model_identifier="gpt-4o-2024-08-06")
    assert "gpt-4o" in list(_iter_litellm_model_candidates(ai_model))


def test_override_bills_anthropic_top_level_cache_read_at_cache_price() -> None:
    """Regression: Anthropic's top-level cache_read_input_tokens must use the
    cache-read price, not the full input price."""
    ai_model = AIModel(provider_name="anthropic", model_identifier="claude-sonnet-4-5")
    pricing_override = {
        "input_price_per_1k": 3.0,
        "output_price_per_1k": 15.0,
        "cache_read_input_price_per_1k": 0.3,
    }
    # 1000 prompt tokens of which 900 were cache reads.
    cost = estimate_ai_model_usage_cost(
        ai_model,
        prompt_tokens=1000,
        completion_tokens=0,
        total_tokens=1000,
        usage_details={"cache_read_input_tokens": 900},
        pricing_override=pricing_override,
    )
    # 100 uncached * 3/1k + 900 cached * 0.3/1k = 0.3 + 0.27 = 0.57
    assert cost == 0.57


def test_fx_converted_override_produces_usd_cost() -> None:
    """Non-USD overrides convert to USD via fx_rate_to_usd in to_pricing_dict."""
    override = ModelPriceOverride(
        model_alias="openai/gpt-5",
        currency="EUR",
        fx_rate_to_usd=1.10,
        input_price_per_1k=1.0,
        output_price_per_1k=2.0,
    )
    pricing = override.to_pricing_dict()
    assert pricing["currency"] == "USD"
    assert pricing["input_price_per_1k"] == 1.10
    assert pricing["output_price_per_1k"] == 2.20
    assert pricing["original_currency"] == "EUR"
    assert pricing["original_prices"] == {
        "input_price_per_1k": 1.0,
        "output_price_per_1k": 2.0,
    }

    ai_model = AIModel(provider_name="openai", model_identifier="gpt-5")
    cost = estimate_ai_model_usage_cost(
        ai_model,
        prompt_tokens=1000,
        completion_tokens=1000,
        total_tokens=2000,
        pricing_override=pricing,
    )
    assert cost == 3.30


def test_non_usd_override_without_fx_rate_keeps_original_currency() -> None:
    """Without an FX rate the dict is NOT silently converted (validation
    upstream rejects this configuration on create/update)."""
    override = ModelPriceOverride(
        model_alias="openai/gpt-5",
        currency="EUR",
        input_price_per_1k=1.0,
    )
    pricing = override.to_pricing_dict()
    assert pricing["currency"] == "EUR"
    assert pricing["input_price_per_1k"] == 1.0


def test_unknown_model_is_unpriced() -> None:
    """Unknown models yield cost None with an explicit 'unpriced' source."""
    ai_model = AIModel(
        provider_name="openai", model_identifier="totally-unknown-model-x"
    )
    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
    )
    assert estimate.cost is None
    assert estimate.source == "unpriced"


def test_detailed_estimate_reports_source() -> None:
    """Provenance distinguishes override vs model_config pricing."""
    ai_model = AIModel(
        provider_name="openai",
        model_identifier="gpt-5",
        meta_data={"pricing": {"input_price_per_1k": 1.0}},
    )
    configured = estimate_ai_model_usage_cost_detailed(
        ai_model, prompt_tokens=1000, completion_tokens=0, total_tokens=1000
    )
    assert configured.source == "model_config"
    assert configured.cost == 1.0

    overridden = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=1000,
        completion_tokens=0,
        total_tokens=1000,
        pricing_override={"input_price_per_1k": 0.5},
    )
    assert overridden.source == "override"
    assert overridden.cost == 0.5


def test_pricing_override_applies_discount_and_prepaid_credit() -> None:
    """Pricing overrides should support negotiated discounts and credits."""
    ai_model = AIModel(provider_name="openai", model_identifier="gpt-4o")

    cost = estimate_ai_model_usage_cost(
        ai_model,
        prompt_tokens=1000,
        completion_tokens=1000,
        total_tokens=2000,
        pricing_override={
            "input_price_per_1k": 1.0,
            "output_price_per_1k": 3.0,
            "discount_percent": 50.0,
            "prepaid_credit_balance_usd": 1.0,
        },
    )

    assert cost == 1.0


def test_pricing_override_supports_prepaid_token_balance() -> None:
    """Prepaid token balances should reduce request cost proportionally."""
    ai_model = AIModel(provider_name="openai", model_identifier="gpt-4o")

    cost = estimate_ai_model_usage_cost(
        ai_model,
        prompt_tokens=1000,
        completion_tokens=1000,
        total_tokens=2000,
        pricing_override={
            "price_per_1k": 2.0,
            "prepaid_token_balance": 1000,
        },
    )

    assert cost == 2.0


def test_pricing_override_supports_zero_fixed_request_price() -> None:
    """A fixed request price of zero should make a model free for matching calls."""
    ai_model = AIModel(provider_name="openai", model_identifier="gpt-4o")

    cost = estimate_ai_model_usage_cost(
        ai_model,
        prompt_tokens=1000,
        completion_tokens=1000,
        total_tokens=2000,
        pricing_override={"request_price": 0.0},
    )

    assert cost == 0.0


def test_discount_only_override_applies_to_litellm_list_price(monkeypatch) -> None:
    """A discount-only override should resolve list price first, then discount it."""
    ai_model = AIModel(provider_name="openai", model_identifier="gpt-4o")

    monkeypatch.setattr(
        "preloop.services.model_pricing.litellm.cost_per_token",
        lambda **_kwargs: (0.01, 0.03),
    )

    cost = estimate_ai_model_usage_cost(
        ai_model,
        prompt_tokens=1000,
        completion_tokens=1000,
        total_tokens=2000,
        pricing_override={"discount_percent": 25.0},
    )

    assert cost == 0.03


def test_model_price_override_serializes_adjustment_terms() -> None:
    """Persisted overrides should expose all adjustment terms to estimators."""
    override = ModelPriceOverride(
        model_alias="openai/gpt-4o",
        request_price=0.0,
        discount_percent=10.0,
        prepaid_token_balance=2000,
        prepaid_credit_balance_usd=5.0,
    )

    pricing = override.to_pricing_dict()

    assert pricing["request_price"] == 0.0
    assert pricing["discount_percent"] == 10.0
    assert pricing["prepaid_token_balance"] == 2000
    assert pricing["prepaid_credit_balance_usd"] == 5.0
