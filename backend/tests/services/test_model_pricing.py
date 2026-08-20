"""Tests for model pricing estimation."""

import json

import pytest

from preloop.models.models.ai_model import AIModel
from preloop.models.models.model_price_override import ModelPriceOverride
from preloop.services.model_price_catalog import CATALOG_PATH, load_catalog
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


class TestNewProviderPricing:
    """moonshot, zai and mistral fallback ids must resolve to a price.

    moonshot ids are priced from Preloop's bundled snapshot (official
    Moonshot prices; the pinned litellm map lacks kimi-k3 and the k2.7-code
    family). zai and mistral ids are priced from the pinned litellm map.
    """

    def test_candidates_include_moonshot_prefix(self) -> None:
        ai_model = AIModel(provider_name="moonshot", model_identifier="kimi-k3")
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert "kimi-k3" in candidates
        assert "moonshot/kimi-k3" in candidates

    def test_candidates_include_zai_prefix(self) -> None:
        ai_model = AIModel(provider_name="zai", model_identifier="glm-5")
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert "zai/glm-5" in candidates

    def test_candidates_include_mistral_prefix(self) -> None:
        ai_model = AIModel(
            provider_name="mistral", model_identifier="mistral-large-latest"
        )
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert "mistral/mistral-large-latest" in candidates

    def test_bundled_table_prices_every_moonshot_fallback_id(self) -> None:
        from preloop.services.ai_model_provider import MOONSHOT_KNOWN_MODELS

        prices = json.loads(CATALOG_PATH.read_text())
        for model_id in MOONSHOT_KNOWN_MODELS:
            key = f"moonshot/{model_id}"
            assert key in prices, f"{key} missing from model_prices.json"
            entry = prices[key]
            assert entry["litellm_provider"] == "moonshot"
            assert entry["mode"] == "chat"
            assert entry["input_cost_per_token"] > 0
            assert entry["output_cost_per_token"] > 0
            # No official max_output_tokens is published; do not invent one.
            assert "max_output_tokens" not in entry
            assert "max_tokens" not in entry

    def test_kimi_k3_official_prices_in_bundled_table(self) -> None:
        """Official prices from platform.moonshot.ai/docs/pricing/chat-k3.md:
        cache-hit input 0.30, cache-miss input 3.00, output 15.00 per 1M."""
        prices = json.loads(CATALOG_PATH.read_text())
        entry = prices["moonshot/kimi-k3"]
        assert entry["input_cost_per_token"] == 3e-06
        assert entry["output_cost_per_token"] == 1.5e-05
        assert entry["cache_read_input_token_cost"] == 3e-07
        assert entry["max_input_tokens"] == 1048576

    def test_kimi_k3_cost_resolves_through_estimator(self) -> None:
        """End to end: an AIModel row for kimi-k3 produces a catalog price."""
        load_catalog(force=True)
        ai_model = AIModel(provider_name="moonshot", model_identifier="kimi-k3")
        estimate = estimate_ai_model_usage_cost_detailed(
            ai_model,
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        assert estimate.source == "catalog"
        # 1M input at $3/M plus 1M output at $15/M.
        assert estimate.cost == pytest.approx(18.0, rel=1e-6)

    @pytest.mark.parametrize(
        "model_id", ["kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"]
    )
    def test_other_moonshot_fallback_ids_resolve(self, model_id: str) -> None:
        load_catalog(force=True)
        ai_model = AIModel(provider_name="moonshot", model_identifier=model_id)
        estimate = estimate_ai_model_usage_cost_detailed(
            ai_model, prompt_tokens=1000, completion_tokens=1000, total_tokens=2000
        )
        assert estimate.source == "catalog"
        assert estimate.cost is not None and estimate.cost > 0

    def test_zai_fallback_ids_resolve_via_litellm_map(self) -> None:
        from preloop.services.ai_model_provider import ZAI_KNOWN_MODELS

        for model_id in ZAI_KNOWN_MODELS:
            ai_model = AIModel(provider_name="zai", model_identifier=model_id)
            estimate = estimate_ai_model_usage_cost_detailed(
                ai_model,
                prompt_tokens=1000,
                completion_tokens=1000,
                total_tokens=2000,
            )
            assert estimate.source == "catalog", f"{model_id} is unpriced"
            # glm-4.7-flash is free in litellm's map, so cost can be 0.0;
            # what matters is that a price RESOLVED (source above).
            assert estimate.cost is not None, f"{model_id} has no cost"

    def test_mistral_fallback_ids_resolve_via_litellm_map(self) -> None:
        from preloop.services.ai_model_provider import MISTRAL_KNOWN_MODELS

        for model_id in MISTRAL_KNOWN_MODELS:
            ai_model = AIModel(provider_name="mistral", model_identifier=model_id)
            estimate = estimate_ai_model_usage_cost_detailed(
                ai_model,
                prompt_tokens=1000,
                completion_tokens=1000,
                total_tokens=2000,
            )
            assert estimate.source == "catalog", f"{model_id} is unpriced"
            assert estimate.cost is not None and estimate.cost > 0, (
                f"{model_id} has no cost"
            )


class TestQwenProviderPricing:
    """Qwen / Model Studio fallback ids must resolve to dashscope catalog prices.

    International USD list prices from Model Studio docs (fetched 2026-08-17).
    Plus/flash families are tiered; we store the lower published tier.
    """

    def test_candidates_include_dashscope_prefix(self) -> None:
        ai_model = AIModel(provider_name="qwen", model_identifier="qwen3.8-max")
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert "qwen3.8-max" in candidates
        assert "dashscope/qwen3.8-max" in candidates

    def test_dated_qwen_id_expands_to_dashscope_undated(self) -> None:
        """Date-stamped Qwen ids must undate under dashscope, not openai."""
        ai_model = AIModel(provider_name="qwen", model_identifier="qwen-plus-20250101")
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert "dashscope/qwen-plus" in candidates
        assert "openai/qwen-plus" not in candidates

    def test_intl_endpoint_still_prices_as_dashscope(self) -> None:
        ai_model = AIModel(
            provider_name="qwen",
            model_identifier="qwen3.8-max",
            api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert "dashscope/qwen3.8-max" in candidates

    def test_bundled_table_prices_every_qwen_fallback_id(self) -> None:
        from preloop.services.ai_model_provider import QWEN_KNOWN_MODELS

        prices = json.loads(CATALOG_PATH.read_text())
        for model_id in QWEN_KNOWN_MODELS:
            key = f"dashscope/{model_id}"
            assert key in prices, f"{key} missing from model_prices.json"
            entry = prices[key]
            assert entry["litellm_provider"] == "dashscope"
            assert entry["mode"] == "chat"
            assert entry["input_cost_per_token"] > 0
            assert entry["output_cost_per_token"] > 0

    def test_qwen38_max_list_prices_in_bundled_table(self) -> None:
        """modelstudio.alibabacloud.com launch card (2026-08-03): $2 / $6 per 1M."""
        prices = json.loads(CATALOG_PATH.read_text())
        entry = prices["dashscope/qwen3.8-max"]
        assert entry["input_cost_per_token"] == 2e-06
        assert entry["output_cost_per_token"] == 6e-06
        assert entry["max_input_tokens"] == 991808
        assert entry["max_output_tokens"] == 131072

    def test_qwen38_max_cost_resolves_through_estimator(self) -> None:
        load_catalog(force=True)
        ai_model = AIModel(provider_name="qwen", model_identifier="qwen3.8-max")
        estimate = estimate_ai_model_usage_cost_detailed(
            ai_model,
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        assert estimate.source == "catalog"
        # 1M input at $2/M plus 1M output at $6/M.
        assert estimate.cost == pytest.approx(8.0, rel=1e-6)


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


# ---------------------------------------------------------------------------
# OpenRouter / openai-compatible routed models (customer-reported $0.00 bug)
# ---------------------------------------------------------------------------


def test_candidates_strip_openai_compatible_provider_prefix() -> None:
    """The synthetic ``openai-compatible/`` prefix is not a price-map namespace.

    The affected models are recorded as
    ``openai-compatible/deepseek/deepseek-v4-flash-0731``. ``openai-compatible``
    is a Preloop routing label, not a litellm provider, so the prefixed form can
    never match the catalog and must be reduced to the bare vendor/model id.
    """
    ai_model = AIModel(
        provider_name="openai-compatible",
        model_identifier="deepseek/deepseek-v4-flash-0731",
        api_endpoint="https://openrouter.ai/api/v1",
        meta_data={
            "gateway": {
                "model_alias": "openai-compatible/deepseek/deepseek-v4-flash-0731"
            }
        },
    )
    candidates = list(_iter_litellm_model_candidates(ai_model))
    assert "deepseek/deepseek-v4-flash-0731" in candidates
    assert "openai-compatible/deepseek/deepseek-v4-flash-0731" not in candidates


def test_moonshotai_slug_maps_to_moonshot_catalog_key() -> None:
    """OpenRouter's moonshotai/ org slug must hit the vendored moonshot/ prices.

    The unpriced-model alert fired for provider=openai, alias=moonshotai/kimi-k3
    because candidates never included moonshot/kimi-k3, which is the bundled
    key. Same SKU, same $3/$15 per million.
    """
    ai_model = AIModel(
        provider_name="openai",
        model_identifier="moonshotai/kimi-k3",
        meta_data={"gateway": {"model_alias": "moonshotai/kimi-k3"}},
    )
    candidates = list(_iter_litellm_model_candidates(ai_model))
    assert "moonshotai/kimi-k3" in candidates
    assert "moonshot/kimi-k3" in candidates

    load_catalog(force=True)
    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
    )
    assert estimate.source == "catalog"
    assert estimate.cost == pytest.approx(18.0, rel=1e-6)


def test_candidates_add_openrouter_prefix_for_openrouter_endpoint() -> None:
    """A model served via openrouter.ai gains ``openrouter/`` catalog keys.

    litellm prices OpenRouter-routed models under ``openrouter/vendor/model``,
    so the endpoint must contribute that candidate form.
    """
    ai_model = AIModel(
        provider_name="openai-compatible",
        model_identifier="deepseek/deepseek-chat",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    candidates = list(_iter_litellm_model_candidates(ai_model))
    assert "openrouter/deepseek/deepseek-chat" in candidates


def test_dated_openrouter_variant_never_falls_back_to_undated_price() -> None:
    """A dated OpenRouter snapshot must not inherit the undated model's price.

    ``deepseek-v4-flash-0731`` really costs $0.09/$0.18 per million tokens while
    the undated ``deepseek-v4-flash`` costs $0.14/$0.28. Silently stripping the
    ``-0731`` suffix would overstate the bill by ~55%, which is a worse
    failure than reporting the usage as unpriced.
    """
    ai_model = AIModel(
        provider_name="openai-compatible",
        model_identifier="deepseek/deepseek-v4-flash-0731",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    candidates = list(_iter_litellm_model_candidates(ai_model))
    assert "deepseek/deepseek-v4-flash" not in candidates
    assert "openrouter/deepseek/deepseek-v4-flash" not in candidates


# ---------------------------------------------------------------------------
# Provider-reported cost (OpenRouter usage accounting; Auto Router has no
# catalog price by design, so the provider's own ledger figure is the only
# accurate source)
# ---------------------------------------------------------------------------


def _openrouter_auto_model() -> AIModel:
    return AIModel(
        provider_name="openrouter",
        model_identifier="openrouter/auto-beta",
        api_endpoint="https://openrouter.ai/api/v1",
        meta_data={"gateway": {"enabled": True, "model_alias": "openrouter/auto-beta"}},
    )


def test_provider_reported_cost_wins_over_catalog() -> None:
    """usage.cost_details.upstream_inference_cost is authoritative over catalog."""
    ai_model = AIModel(provider_name="openai", model_identifier="gpt-4o")
    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        usage_details={
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cost_details": {"upstream_inference_cost": 0.00001946},
        },
    )
    assert estimate.source == "provider"
    assert estimate.cost == pytest.approx(0.00001946)


def test_provider_reported_cost_from_top_level_usage_cost() -> None:
    """OpenRouter's usage.cost (credits charged) alone is authoritative."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0000205},
    )
    assert estimate.source == "provider"
    assert estimate.cost == pytest.approx(0.0000205)


def test_provider_reported_cost_sums_byok_fee_and_upstream_charge() -> None:
    """BYOK: usage.cost is OpenRouter's fee, upstream_inference_cost the vendor
    charge; the customer pays both, so the authoritative total is their sum."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={
            "cost": 0.000001,
            "cost_details": {"upstream_inference_cost": 0.00002},
        },
    )
    assert estimate.source == "provider"
    assert estimate.cost == pytest.approx(0.000021)


def test_byok_shape_small_fee_plus_upstream_is_summed() -> None:
    """cost < upstream_inference_cost is the BYOK shape (fee + vendor charge):
    the customer pays both, so the total is their sum (#224)."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={
            "cost": 0.00000099,
            "cost_details": {"upstream_inference_cost": 0.0000198},
        },
    )
    assert estimate.source == "provider"
    assert estimate.cost == pytest.approx(0.00000099 + 0.0000198)


def test_explicit_is_byok_true_wins_over_magnitude_heuristic() -> None:
    """A BYOK request whose OpenRouter fee meets/exceeds the vendor charge
    would be mis-read as credits by the magnitude heuristic; an explicit
    is_byok flag from the provider is authoritative (#225 review)."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={
            "cost": 0.00003,
            "is_byok": True,
            "cost_details": {"upstream_inference_cost": 0.00002},
        },
    )
    assert estimate.source == "provider"
    assert estimate.cost == pytest.approx(0.00003 + 0.00002)


def test_explicit_is_byok_false_never_sums() -> None:
    """is_byok=False forces the credits interpretation even when the
    magnitude heuristic (cost < upstream) would have summed."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={
            "cost": 0.00000099,
            "is_byok": False,
            "cost_details": {"upstream_inference_cost": 0.0000198},
        },
    )
    assert estimate.source == "provider"
    assert estimate.cost == pytest.approx(0.00000099)


def test_credits_shape_duplicate_cost_details_not_double_counted() -> None:
    """Credits-based OpenRouter usage returns cost AND an IDENTICAL
    cost_details.upstream_inference_cost (live-verified, #224). cost is the
    total charge; summing would record exactly 2x."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={
            "cost": 0.000001979964,
            "cost_details": {"upstream_inference_cost": 0.000001979964},
        },
    )
    assert estimate.source == "provider"
    assert estimate.cost == pytest.approx(0.000001979964)


def test_credits_shape_cost_above_upstream_uses_cost_alone() -> None:
    """When cost >= upstream_inference_cost, cost already includes the
    upstream charge (credits shape); cost_details is informational (#224)."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={
            "cost": 0.0000305,
            "cost_details": {"upstream_inference_cost": 0.00001946},
        },
    )
    assert estimate.source == "provider"
    assert estimate.cost == pytest.approx(0.0000305)


def test_absent_provider_cost_falls_back_to_catalog_unchanged() -> None:
    """No cost fields in usage -> exactly today's catalog behavior."""
    ai_model = AIModel(provider_name="openai", model_identifier="gpt-4o")
    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=1000,
        completion_tokens=100,
        total_tokens=1100,
        usage_details={"prompt_tokens": 1000, "completion_tokens": 100},
    )
    assert estimate.source == "catalog"
    assert estimate.cost is not None and estimate.cost > 0


def test_absent_provider_cost_still_unpriced_for_uncatalogued_model() -> None:
    """Auto Router without usage accounting stays unpriced (no invented price)."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={"prompt_tokens": 10, "completion_tokens": 5},
    )
    assert estimate.source == "unpriced"
    assert estimate.cost is None


def test_explicit_zero_provider_cost_is_accounted() -> None:
    """usage.cost=0 (key present) is a real provider $0 charge."""
    for usage_details in (
        {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0},
        {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0},
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost": 0,
            "cost_details": {
                "upstream_inference_cost": 0,
                "upstream_inference_prompt_cost": 0,
                "upstream_inference_completions_cost": 0,
            },
        },
    ):
        estimate = estimate_ai_model_usage_cost_detailed(
            _openrouter_auto_model(),
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            usage_details=usage_details,
        )
        assert estimate.source == "provider", usage_details
        assert estimate.cost == 0.0, usage_details


def test_negative_provider_cost_is_ignored() -> None:
    """cost=-1 is the catalog sentinel, not an accounted charge."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={"prompt_tokens": 10, "completion_tokens": 5, "cost": -1},
    )
    assert estimate.source == "unpriced"
    assert estimate.cost is None


def test_zero_upstream_inference_cost_alone_is_ignored() -> None:
    """A zero upstream_inference_cost without usage.cost is not accounted."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        usage_details={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_details": {"upstream_inference_cost": 0},
        },
    )
    assert estimate.source == "unpriced"
    assert estimate.cost is None


def test_explicit_price_override_still_wins_over_provider_cost() -> None:
    """An operator's explicit override outranks even the provider ledger."""
    estimate = estimate_ai_model_usage_cost_detailed(
        _openrouter_auto_model(),
        prompt_tokens=1000,
        completion_tokens=0,
        total_tokens=1000,
        usage_details={"cost": 0.5},
        pricing_override={"input_price_per_1k": 0.01},
    )
    assert estimate.source == "override"
    assert estimate.cost == pytest.approx(0.01)
