"""Tests for model pricing estimation."""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from preloop.models.models.ai_model import AIModel
from preloop.models.models.model_price_override import ModelPriceOverride
from preloop.services.model_price_catalog import CATALOG_PATH, load_catalog
from preloop.services.model_pricing import (
    _iter_litellm_model_candidates,
    estimate_ai_model_usage_cost,
    estimate_ai_model_usage_cost_detailed,
)

_ZAI_PRICING_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "zai_text_models_pricing.md"
)


def _load_update_model_prices() -> Any:
    """Load scripts/update_model_prices.py by path (not the shared install)."""
    script = Path(__file__).resolve().parents[3] / "scripts" / "update_model_prices.py"
    spec = importlib.util.spec_from_file_location("update_model_prices", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    family). glm-5.3 is likewise bundled from docs.z.ai pricing (litellm
    has no zai/glm-5.3 key). Other zai and mistral ids are priced from
    the pinned litellm map.
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

    def test_candidates_include_zai_glm_53(self) -> None:
        ai_model = AIModel(provider_name="zai", model_identifier="glm-5.3")
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert "zai/glm-5.3" in candidates

    def test_glm_53_official_prices_in_bundled_table(self) -> None:
        """Official prices from docs.z.ai/guides/overview/pricing.md
        (fetched 2026-08-24): input $1.4, cached input $0.26, output $4.4
        per 1M. Context/output limits from docs.z.ai/guides/llm/glm-5.3
        (1M context, 128K max output)."""
        prices = json.loads(CATALOG_PATH.read_text())
        entry = prices["zai/glm-5.3"]
        assert entry["litellm_provider"] == "zai"
        assert entry["mode"] == "chat"
        assert entry["input_cost_per_token"] == 1.4e-06
        assert entry["output_cost_per_token"] == 4.4e-06
        assert entry["cache_read_input_token_cost"] == 2.6e-07
        assert entry["max_input_tokens"] == 1_000_000
        assert entry["max_output_tokens"] == 128_000
        assert "cache_creation_input_token_cost" not in entry

    def test_glm_53_cost_resolves_through_estimator(self) -> None:
        """An AIModel row for glm-5.3 produces a catalog price."""
        load_catalog(force=True)
        ai_model = AIModel(provider_name="zai", model_identifier="glm-5.3")
        estimate = estimate_ai_model_usage_cost_detailed(
            ai_model,
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        assert estimate.source == "catalog"
        # 1M input at $1.4/M plus 1M output at $4.4/M.
        assert estimate.cost == pytest.approx(5.8, rel=1e-6)

    def test_candidates_include_mistral_prefix(self) -> None:
        ai_model = AIModel(
            provider_name="mistral", model_identifier="mistral-large-latest"
        )
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert "mistral/mistral-large-latest" in candidates

    def test_bundled_table_prices_every_moonshot_fallback_id(self) -> None:
        prices = json.loads(CATALOG_PATH.read_text())
        for model_id in (
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        ):
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
        # glm-5.3 is in the vendored snapshot, not litellm's published map.
        load_catalog(force=True)
        for model_id in (
            "glm-5.3",
            "glm-5.1",
            "glm-5",
            "glm-4.7",
            "glm-4.7-flash",
        ):
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
        for model_id in (
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "codestral-latest",
            "devstral-latest",
            "ministral-8b-latest",
        ):
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

    def test_default_china_does_not_borrow_international_prices(self) -> None:
        ai_model = AIModel(provider_name="qwen", model_identifier="qwen3.8-max")
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert candidates == []

    def test_dated_qwen_id_never_borrows_undated_prices(self) -> None:
        """Snapshots can carry their own tariffs or peak/off-peak prices."""
        ai_model = AIModel(provider_name="qwen", model_identifier="qwen-plus-20250101")
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert candidates == []

    def test_intl_endpoint_uses_scoped_tariffs_not_generic_candidates(self) -> None:
        ai_model = AIModel(
            provider_name="qwen",
            model_identifier="qwen3.8-max",
            api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        candidates = list(_iter_litellm_model_candidates(ai_model))
        assert candidates == []

    def test_bundled_table_prices_every_qwen_fallback_id(self) -> None:
        prices = json.loads(CATALOG_PATH.read_text())
        for model_id in (
            "qwen3.8-max",
            "qwen3.7-max",
            "qwen3.7-plus",
            # qwen3.6-flash left the list with the 2026-09-21 refresh:
            # upstream litellm no longer publishes a dashscope row for it.
            # Qwen traffic prices through alibaba_international_prices.json
            # (which still carries qwen3.6-flash), not this fallback table.
            "qwen3.5-plus",
            "qwen3-max",
            "qwen3-coder-plus",
            "qwen-plus",
            "qwen-flash",
        ):
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
        ai_model = AIModel(
            provider_name="qwen",
            model_identifier="qwen3.8-max",
            api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        estimate = estimate_ai_model_usage_cost_detailed(
            ai_model,
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        assert estimate.source == "catalog"
        # 1M input at $2/M plus 1M output at $6/M.
        assert estimate.cost == pytest.approx(8.0, rel=1e-6)


@pytest.mark.parametrize("provider_name", ["google", "gemini"])
def test_gemini_38_flash_bills_cached_input_at_the_cache_rate(
    provider_name: str,
) -> None:
    """Gemini 3.8 Flash rows price from the catalog, cache reads included.

    Both provider spellings Preloop stores for Gemini map onto the same
    ``gemini/`` catalog namespace. Production billed 94% of a day's Gemini
    tokens at the full input rate because the only prices available were
    interim overrides without a cache-read rate (issue #850); the catalog
    entry carries one, so the estimator has to use it.
    """
    load_catalog(force=True)
    ai_model = AIModel(provider_name=provider_name, model_identifier="gemini-3.8-flash")
    candidates = list(_iter_litellm_model_candidates(ai_model))
    assert "gemini/gemini-3.8-flash" in candidates

    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=1_000_000,
        completion_tokens=0,
        total_tokens=1_000_000,
        usage_details={
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 900_000},
        },
    )
    assert estimate.source == "catalog"
    # 100k uncached at $0.75/M plus 900k cached at $0.075/M. Billing the
    # cache reads as ordinary input would cost $0.75, 5.3x as much.
    assert estimate.cost == pytest.approx(0.1425, rel=1e-6)

    uncached = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
    )
    assert uncached.source == "catalog"
    # 1M input at $0.75/M plus 1M output at $3.75/M.
    assert uncached.cost == pytest.approx(4.5, rel=1e-6)


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


class TestUpdateModelPriceOverlays:
    """update_model_prices.py overlay parser and merge (no network)."""

    def test_update_model_parser_reads_text_models_fixture(self) -> None:
        script = _load_update_model_prices()
        rows = script.parse_zai_text_models(_ZAI_PRICING_FIXTURE.read_text())
        assert "glm-5.3" in rows
        assert rows["glm-5.3"]["input_cost_per_token"] == 1.4e-06
        assert rows["glm-5.3"]["output_cost_per_token"] == 4.4e-06
        assert rows["glm-5.3"]["cache_read_input_token_cost"] == 2.6e-07
        assert "glm-5-turbo" in rows
        assert rows["glm-5-turbo"]["input_cost_per_token"] == 1.2e-06
        assert rows["glm-4.7-flash"]["input_cost_per_token"] == 0.0
        assert rows["glm-4.7-flash"]["output_cost_per_token"] == 0.0
        # Cached input was "-": field omitted, row kept.
        assert "glm-4-32b-0414-128k" in rows
        assert "cache_read_input_token_cost" not in rows["glm-4-32b-0414-128k"]
        # Dash input/output: skip the row.
        assert "glm-skip-me" not in rows
        # Vision table must not leak into the overlay.
        assert "glm-5v-turbo" not in rows

    def test_update_model_filter_keeps_token_priced_embedding_models(self) -> None:
        """A refresh keeps embeddings (the gateway serves them) but not $0 rows.

        Token-priced embedding entries must survive the filter or every
        gateway vector call would record as unpriced. Multimodal embedding
        rows priced per query/second have no per-token input price, so the
        token ledger could only bill them as $0: they stay out.
        """
        script = _load_update_model_prices()
        upstream = {
            "text-embedding-fixture": {
                "litellm_provider": "openai",
                "mode": "embedding",
                "input_cost_per_token": 2e-08,
                "output_cost_per_token": 0.0,
                "supports_vision": False,
            },
            "video-embedding-fixture": {
                "litellm_provider": "bedrock",
                "mode": "embedding",
                "input_cost_per_query": 7e-05,
                "input_cost_per_video_per_second": 0.0007,
                "output_cost_per_token": 0.0,
            },
            "image-fixture": {
                "litellm_provider": "openai",
                "mode": "image_generation",
                "input_cost_per_token": 1e-06,
            },
            "chat-fixture": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 1e-06,
                "output_cost_per_token": 4e-06,
            },
        }

        filtered = script.filter_catalog(upstream)

        assert set(filtered) == {"text-embedding-fixture", "chat-fixture"}
        assert filtered["text-embedding-fixture"]["mode"] == "embedding"
        # Capability flags are still stripped from the kept embedding row.
        assert "supports_vision" not in filtered["text-embedding-fixture"]

    def test_update_model_flattens_tier_only_upstream_prices(self) -> None:
        """A row priced only by ``tiered_pricing`` keeps its lowest tier.

        Upstream moved dashscope rows (qwen-flash, qwen3-max, qwen3.7-plus)
        to a tier list with no top-level price. The field filter drops that
        list, so before this flattening a refresh vendored those models with
        limits and no price and their usage recorded as unpriced.
        """
        script = _load_update_model_prices()
        upstream = {
            "dashscope/tiered-fixture": {
                "litellm_provider": "dashscope",
                "mode": "chat",
                "max_input_tokens": 997952,
                "tiered_pricing": [
                    {
                        "input_cost_per_token": 2.5e-07,
                        "output_cost_per_token": 2e-06,
                        "range": [256000.0, 1000000.0],
                    },
                    {
                        "input_cost_per_token": 5e-08,
                        "output_cost_per_token": 4e-07,
                        "cache_read_input_token_cost": 1e-08,
                        "range": [0, 256000.0],
                    },
                ],
            },
            "dashscope/flat-fixture": {
                "litellm_provider": "dashscope",
                "mode": "chat",
                "input_cost_per_token": 2e-06,
                "output_cost_per_token": 6e-06,
                "tiered_pricing": [
                    {
                        "input_cost_per_token": 9e-06,
                        "output_cost_per_token": 9e-06,
                        "range": [0, 256000.0],
                    }
                ],
            },
        }

        filtered = script.filter_catalog(upstream)

        tiered = filtered["dashscope/tiered-fixture"]
        # Lowest published tier, not the first list element.
        assert tiered["input_cost_per_token"] == 5e-08
        assert tiered["output_cost_per_token"] == 4e-07
        assert tiered["cache_read_input_token_cost"] == 1e-08
        # The tier list itself is not vendored: the snapshot stays flat.
        assert "tiered_pricing" not in tiered
        assert tiered["max_input_tokens"] == 997952
        # A row that publishes a flat price keeps it; tiers do not override.
        assert filtered["dashscope/flat-fixture"]["input_cost_per_token"] == 2e-06
        assert filtered["dashscope/flat-fixture"]["output_cost_per_token"] == 6e-06

    def test_update_model_moonshot_keys_survive_stub_litellm_merge(self) -> None:
        script = _load_update_model_prices()
        current = {
            "gpt-4o": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 2.5e-06,
                "output_cost_per_token": 1.0e-05,
            },
            "moonshot/kimi-k3": {
                "litellm_provider": "moonshot",
                "mode": "chat",
                "input_cost_per_token": 3e-06,
                "output_cost_per_token": 1.5e-05,
                "cache_read_input_token_cost": 3e-07,
            },
            "zai/glm-5-turbo": {
                "litellm_provider": "zai",
                "mode": "chat",
                "input_cost_per_token": 9.9e-06,
                "output_cost_per_token": 9.9e-06,
                "max_input_tokens": 200000,
            },
        }
        stub_litellm = {
            "gpt-4o": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 1.0e-06,
                "output_cost_per_token": 4.0e-06,
            }
        }
        zai_rows = script.parse_zai_text_models(_ZAI_PRICING_FIXTURE.read_text())
        merged = script.apply_overlays(stub_litellm, current, zai_rows)
        assert merged["moonshot/kimi-k3"]["input_cost_per_token"] == 3e-06
        assert merged["zai/glm-5.3"]["input_cost_per_token"] == 1.4e-06
        assert merged["zai/glm-5.3"]["max_input_tokens"] == 1_000_000
        assert merged["zai/glm-5.3"]["max_output_tokens"] == 128_000
        # Other z.ai rows keep existing max_* and do not invent new ones.
        assert merged["zai/glm-5-turbo"]["input_cost_per_token"] == 1.2e-06
        assert merged["zai/glm-5-turbo"]["max_input_tokens"] == 200000
        assert "max_output_tokens" not in merged["zai/glm-5.2"]
        assert "zai/glm-5v-turbo" not in merged

    def test_update_model_compare_remote_excludes_overlay_prefixes(self) -> None:
        script = _load_update_model_prices()
        current = {
            "_preloop_meta": {"source_url": "https://example.com/litellm.json"},
            "gpt-4o": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 1.0e-06,
            },
            "moonshot/kimi-k3": {
                "litellm_provider": "moonshot",
                "mode": "chat",
                "input_cost_per_token": 3e-06,
            },
            "zai/glm-5.3": {
                "litellm_provider": "zai",
                "mode": "chat",
                "input_cost_per_token": 1.4e-06,
            },
        }
        upstream = {
            "gpt-4o": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 1.0e-06,
            }
        }
        sourced = script.litellm_sourced_catalog(current)
        assert "moonshot/kimi-k3" not in sourced
        assert "zai/glm-5.3" not in sourced
        assert "gpt-4o" in sourced
        assert script.diff_catalogs(sourced, upstream) == []

    def test_update_model_zai_fetch_failure_still_merges_litellm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs.z.ai 404 or parse failure must not abort the litellm write."""
        script = _load_update_model_prices()
        previous_overlay = {
            "url": script.ZAI_PRICING_URL,
            "fetched_at": "2026-01-15T00:00:00+00:00",
            "section": "Text Models",
        }
        current = {
            "_preloop_meta": {
                "source_url": "https://example.com/litellm.json",
                "fetched_at": "2026-01-15T00:00:00+00:00",
                "overlay_sources": [previous_overlay],
            },
            "gpt-4o": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 2.5e-06,
                "output_cost_per_token": 1.0e-05,
            },
            "moonshot/kimi-k3": {
                "litellm_provider": "moonshot",
                "mode": "chat",
                "input_cost_per_token": 3e-06,
                "output_cost_per_token": 1.5e-05,
            },
            "zai/glm-5.3": {
                "litellm_provider": "zai",
                "mode": "chat",
                "input_cost_per_token": 1.4e-06,
                "output_cost_per_token": 4.4e-06,
            },
        }
        written: dict[str, Any] = {}

        def fake_fetch_remote(url: str = script.SOURCE_URL) -> dict[str, Any]:
            return {
                "gpt-4o": {
                    "litellm_provider": "openai",
                    "mode": "chat",
                    "input_cost_per_token": 1.0e-06,
                    "output_cost_per_token": 4.0e-06,
                }
            }

        def fake_fetch_text(url: str) -> str:
            raise OSError("HTTP Error 404: Not Found")

        def fake_write_catalog(
            filtered: dict[str, Any],
            source_url: str,
            overlay_sources: list[dict[str, Any]] | None = None,
        ) -> None:
            written["filtered"] = filtered
            written["source_url"] = source_url
            written["overlay_sources"] = overlay_sources

        monkeypatch.setattr(script, "fetch_remote", fake_fetch_remote)
        monkeypatch.setattr(script, "fetch_text", fake_fetch_text)
        monkeypatch.setattr(script, "load_current", lambda: current)
        monkeypatch.setattr(script, "write_catalog", fake_write_catalog)
        monkeypatch.setattr(sys, "argv", ["update_model_prices.py"])

        assert script.main() == 0
        merged = written["filtered"]
        assert merged["gpt-4o"]["input_cost_per_token"] == 1.0e-06
        assert merged["moonshot/kimi-k3"]["input_cost_per_token"] == 3e-06
        assert merged["zai/glm-5.3"]["input_cost_per_token"] == 1.4e-06
        assert written["overlay_sources"] == [previous_overlay]

    def test_check_overlay_ages_uses_fixture_meta(self) -> None:
        """--check ages overlay_sources from meta without fetching docs.z.ai."""
        script = _load_update_model_prices()
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)
        fresh_meta = {
            "overlay_sources": [
                {
                    "url": "https://docs.z.ai/guides/overview/pricing.md",
                    "fetched_at": "2026-08-20T00:00:00+00:00",
                    "section": "Text Models",
                }
            ]
        }
        stale_meta = {
            "overlay_sources": [
                {
                    "url": "https://docs.z.ai/guides/overview/pricing.md",
                    "fetched_at": "2026-01-01T00:00:00+00:00",
                    "section": "Text Models",
                }
            ]
        }
        assert script.overlay_sources_over_max_age(fresh_meta, 30, now=now) == []
        stale = script.overlay_sources_over_max_age(stale_meta, 30, now=now)
        assert len(stale) == 1
        assert "older than 30 days" in stale[0]
        assert script.overlay_sources_over_max_age({}, 30, now=now) == []


# ---------------------------------------------------------------------------
# Harness-reported model names (usage ingest pricing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("claude-4.5-sonnet", "claude-sonnet-4-5"),
        ("claude-4-sonnet", "claude-sonnet-4"),
        ("claude-4-opus", "claude-opus-4"),
        ("claude-4.5-opus", "claude-opus-4-5"),
        ("Claude-4.5-Sonnet", "claude-sonnet-4-5"),
        ("claude-opus-4.1", "claude-opus-4-1"),
        ("gpt-5", "gpt-5"),
        ("  composer ", "composer"),
        ("", ""),
    ],
)
def test_normalize_external_model_name(reported: str, expected: str) -> None:
    """Cursor's version-first Claude spellings map onto catalog keys."""
    from preloop.services.model_pricing import normalize_external_model_name

    assert normalize_external_model_name(reported) == expected


@pytest.mark.parametrize(
    ("name", "provider"),
    [
        ("claude-sonnet-4-5", "anthropic"),
        ("gpt-5", "openai"),
        ("o3-mini", "openai"),
        ("gemini-2.5-pro", "gemini"),
        ("grok-4", "xai"),
        ("deepseek-v3", "deepseek"),
        ("openrouter/anthropic/claude-sonnet-4-5", "openrouter"),
        ("composer", "openai"),
    ],
)
def test_infer_provider_for_model_name(name: str, provider: str) -> None:
    """Bare model names get the provider prefix the catalog resolver expects."""
    from preloop.services.model_pricing import infer_provider_for_model_name

    assert infer_provider_for_model_name(name) == provider


def test_estimate_external_model_usage_cost_prices_catalog_models() -> None:
    """A reported model name is priced through the same catalog as gateway rows."""
    from preloop.services.model_pricing import estimate_external_model_usage_cost

    estimate = estimate_external_model_usage_cost(
        "gpt-5", prompt_tokens=1200, completion_tokens=850
    )
    # 1200 * $1.25/Mtok + 850 * $10/Mtok
    assert estimate.cost == pytest.approx(0.01)
    assert estimate.source == "catalog"


def test_estimate_external_model_usage_cost_maps_cursor_alias() -> None:
    """claude-4.5-sonnet (Cursor spelling) prices as claude-sonnet-4-5."""
    from preloop.services.model_pricing import estimate_external_model_usage_cost

    estimate = estimate_external_model_usage_cost(
        "claude-4.5-sonnet", prompt_tokens=1200, completion_tokens=850
    )
    # 1200 * $3/Mtok + 850 * $15/Mtok
    assert estimate.cost == pytest.approx(0.01635)
    assert estimate.source == "catalog"


@pytest.mark.parametrize("name", ["composer", "auto", "claude-4-sonnet", ""])
def test_estimate_external_model_usage_cost_unknown_stays_unpriced(name: str) -> None:
    """Unknown or catalog-less names return unpriced rather than a guess.

    ``claude-4-sonnet`` is the 1.100.x regression: litellm fabricates
    ``(0.0, 0.0)`` for that spelling even though the map has no entry, so
    a naive ``cost_per_token`` sum would bill catalog $0.
    """
    from preloop.services.model_pricing import estimate_external_model_usage_cost

    estimate = estimate_external_model_usage_cost(
        name, prompt_tokens=1200, completion_tokens=850
    )
    assert estimate.cost is None
    assert estimate.source == "unpriced"


def test_estimate_external_model_usage_cost_zero_tokens_unpriced() -> None:
    """Zero tokens never produce a cost."""
    from preloop.services.model_pricing import estimate_external_model_usage_cost

    estimate = estimate_external_model_usage_cost(
        "gpt-5", prompt_tokens=0, completion_tokens=0
    )
    assert estimate.cost is None
    assert estimate.source == "unpriced"


def test_alibaba_detailed_estimate_forwards_historical_instant_and_provenance(
    monkeypatch,
):
    from datetime import datetime, timezone
    from preloop.models import models
    from preloop.services import alibaba_pricing, alibaba_price_catalog
    from preloop.services.model_pricing import estimate_ai_model_usage_cost_detailed

    model = models.AIModel(
        provider_name="qwen",
        model_identifier="qwen-provenance-example",
        api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )
    observed = datetime(2026, 1, 2, tzinfo=timezone.utc)
    seen = []
    snapshot = {
        "provider": "alibaba",
        "source": "reviewed",
        "effective_from": "2026-01-01T00:00:00Z",
    }

    def estimate(ai_model, **kwargs):
        seen.append(kwargs["observed_at"])
        return 0.01

    monkeypatch.setattr(alibaba_pricing, "estimate", estimate)
    monkeypatch.setattr(
        alibaba_price_catalog, "pricing_snapshot", lambda *args, **kwargs: snapshot
    )
    result = estimate_ai_model_usage_cost_detailed(
        model,
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
        observed_at=observed,
    )
    assert seen == [observed]
    assert result.cost == 0.01
    assert result.pricing_snapshot == snapshot


@pytest.mark.parametrize(
    "native_input,native_read,native_max_input,created,prompt_tokens,expected_cost",
    [
        (0.15, None, None, 0, 60000, 0.003038),
        (0.15, None, 1_000_000, 0, 60000, None),
        (0.2, None, None, 0, 60000, None),
        (0.15, 0.02, None, 1000, 60000, None),
        (0.15, None, None, 0, 1_000_001, 0.144038),
        (0.15, None, 1_000_000, 0, 1_000_001, None),
    ],
)
def test_partial_native_flash_tariff_only_uses_matching_verified_seed(
    native_input: float,
    native_read: float | None,
    native_max_input: int | None,
    created: int,
    prompt_tokens: int,
    expected_cost: float | None,
) -> None:
    """Missing native dimensions cannot erase matching seed evidence or mix prices.

    Native qwen3.8-flash is unbounded. A native 1M cap is a different context
    policy than the seed, so cache rates must not be spliced from seed.
    """
    from preloop.models import models
    from preloop.services import alibaba_price_catalog
    from preloop.services.alibaba_pricing import Tariff
    from preloop.services.model_pricing import estimate_ai_model_usage_cost_detailed

    model = models.AIModel(
        provider_name="qwen",
        model_identifier="qwen3.8-flash",
        api_endpoint="https://example.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    )
    alibaba_price_catalog.reset_live_state_for_tests()
    partial_native = Tariff(
        input=native_input,
        output=0.47,
        max_input=native_max_input,
        implicit_read=native_read,
    )
    alibaba_price_catalog.install_live_tariff(
        "singapore-international", "qwen3.8-flash", partial_native
    )
    try:
        result = estimate_ai_model_usage_cost_detailed(
            model,
            prompt_tokens=prompt_tokens,
            completion_tokens=1000,
            total_tokens=prompt_tokens + 1000,
            usage_details={
                "_preloop_cache_mode": "implicit",
                "prompt_tokens_details": {
                    "cached_tokens": 48000,
                    "cache_creation_input_tokens": created,
                },
            },
        )
        if expected_cost is None:
            assert result.cost is None
            assert result.source == "unpriced"
        else:
            assert result.cost == pytest.approx(expected_cost)
            assert result.source == "catalog"
            assert result.pricing_snapshot is not None
            assert "seed" in result.pricing_snapshot["source"]
        assert alibaba_price_catalog.native_tariff(model) is partial_native
        assert partial_native.implicit_read == native_read
    finally:
        alibaba_price_catalog.reset_live_state_for_tests()
