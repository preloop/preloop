"""Unit tests for per-request and per-session prompt-cache accounting."""

from types import SimpleNamespace

from preloop.services.cache_accounting import (
    CACHE_MISS_SOURCE_DERIVED,
    CACHE_MISS_SOURCE_REPORTED,
    SAVINGS_BASIS_CATALOG_EXACT,
    SAVINGS_BASIS_CATALOG_EXACT_PARTIAL,
    SAVINGS_OMITTED_NO_CACHE_READS,
    SAVINGS_OMITTED_NO_CATALOG_PRICE,
    build_request_cache_accounting,
    reported_cache_miss_tokens,
    summarize_session_cache,
)


def _row(**overrides):
    """Build a minimal ApiUsage-shaped row for accounting."""
    base = {
        "prompt_tokens": 1000,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "model_alias": "gpt-4o",
        "provider_name": "openai",
        "usage_source": "provider",
        "meta_data": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRequestAccounting:
    def test_absent_cache_columns_stay_none_not_zero(self):
        """A provider that reports nothing must not be shown as zero cache."""
        accounting = build_request_cache_accounting(_row())
        assert accounting.cache_read_tokens is None
        assert accounting.cache_creation_tokens is None
        assert accounting.cache_miss_tokens is None
        assert accounting.cache_miss_source is None
        assert accounting.has_cache_data is False

    def test_reported_zero_read_is_kept_as_zero(self):
        """A real provider-reported zero is data, and stays zero."""
        accounting = build_request_cache_accounting(_row(cache_read_tokens=0))
        assert accounting.cache_read_tokens == 0
        assert accounting.has_cache_data is True
        assert accounting.cache_miss_tokens == 1000
        assert accounting.cache_miss_source == CACHE_MISS_SOURCE_DERIVED

    def test_anthropic_exact_read_and_write_derives_miss(self):
        accounting = build_request_cache_accounting(
            _row(
                prompt_tokens=10_000,
                cache_read_tokens=7_000,
                cache_creation_tokens=2_000,
                model_alias="anthropic/claude-sonnet-4",
                provider_name="anthropic",
            )
        )
        assert accounting.cache_read_tokens == 7_000
        assert accounting.cache_creation_tokens == 2_000
        assert accounting.cache_miss_tokens == 1_000
        assert accounting.cache_miss_source == CACHE_MISS_SOURCE_DERIVED

    def test_derived_miss_never_goes_negative(self):
        """Provider rounding must not produce a negative miss count."""
        accounting = build_request_cache_accounting(
            _row(prompt_tokens=100, cache_read_tokens=90, cache_creation_tokens=30)
        )
        assert accounting.cache_miss_tokens == 0

    def test_deepseek_reported_miss_is_promoted_and_labelled(self):
        """DeepSeek's explicit miss count wins over arithmetic derivation."""
        accounting = build_request_cache_accounting(
            _row(
                prompt_tokens=1_500,
                cache_read_tokens=1_280,
                model_alias="deepseek/deepseek-chat",
                provider_name="deepseek",
                meta_data={
                    "usage_details": {
                        "prompt_cache_hit_tokens": 1_280,
                        "prompt_cache_miss_tokens": 220,
                    }
                },
            )
        )
        assert accounting.cache_miss_tokens == 220
        assert accounting.cache_miss_source == CACHE_MISS_SOURCE_REPORTED

    def test_reported_miss_alone_is_cache_data(self):
        """A miss-only report is cache data, even with no read or write."""
        accounting = build_request_cache_accounting(
            _row(
                prompt_tokens=500,
                meta_data={"usage_details": {"prompt_cache_miss_tokens": 500}},
            )
        )
        assert accounting.cache_read_tokens is None
        assert accounting.cache_creation_tokens is None
        assert accounting.cache_miss_tokens == 500
        assert accounting.cache_miss_source == CACHE_MISS_SOURCE_REPORTED
        assert accounting.has_cache_data is True

    def test_legacy_row_falls_back_to_usage_details(self):
        """Rows written before the columns existed still read out correctly."""
        accounting = build_request_cache_accounting(
            _row(
                prompt_tokens=5_000,
                meta_data={
                    "usage_details": {
                        "cache_read_input_tokens": 4_000,
                        "cache_creation_input_tokens": 500,
                    }
                },
            )
        )
        assert accounting.cache_read_tokens == 4_000
        assert accounting.cache_creation_tokens == 500
        assert accounting.cache_miss_tokens == 500

    def test_openai_nested_cached_tokens_fallback(self):
        accounting = build_request_cache_accounting(
            _row(
                prompt_tokens=2_000,
                meta_data={
                    "usage_details": {"prompt_tokens_details": {"cached_tokens": 1_024}}
                },
            )
        )
        assert accounting.cache_read_tokens == 1_024
        assert accounting.cache_creation_tokens is None

    def test_reported_cache_miss_tokens_absent_returns_none(self):
        assert reported_cache_miss_tokens(None) is None
        assert reported_cache_miss_tokens({"usage_details": {}}) is None
        assert reported_cache_miss_tokens({"usage_details": "nope"}) is None

    def test_reported_miss_nested_prompt_tokens_details_path(self):
        """The nested prompt_tokens_details.prompt_cache_miss_tokens is read."""
        meta_data = {
            "usage_details": {
                "prompt_tokens_details": {"prompt_cache_miss_tokens": 640}
            }
        }
        assert reported_cache_miss_tokens(meta_data) == 640

    def test_reported_miss_cache_miss_input_tokens_fallback(self):
        """The third fallback key, cache_miss_input_tokens, is honored."""
        meta_data = {"usage_details": {"cache_miss_input_tokens": 77}}
        assert reported_cache_miss_tokens(meta_data) == 77

    def test_reported_miss_key_precedence(self):
        """Top-level prompt_cache_miss_tokens wins over the fallbacks."""
        meta_data = {
            "usage_details": {
                "prompt_cache_miss_tokens": 1,
                "prompt_tokens_details": {"prompt_cache_miss_tokens": 2},
                "cache_miss_input_tokens": 3,
            }
        }
        assert reported_cache_miss_tokens(meta_data) == 1


class TestSessionSummary:
    def test_ratio_denominator_excludes_uncovered_requests(self):
        """Blind rows must not be averaged in as cache misses."""
        summary = summarize_session_cache(
            [
                _row(
                    prompt_tokens=1_000,
                    cache_read_tokens=800,
                    cache_creation_tokens=0,
                    model_alias="anthropic/claude-sonnet-4",
                    provider_name="anthropic",
                ),
                _row(prompt_tokens=4_000),  # provider reported no cache split
            ]
        )
        assert summary.requests_total == 2
        assert summary.requests_with_cache_data == 1
        assert summary.requests_without_cache_data == 1
        assert summary.covered_prompt_tokens == 1_000
        assert summary.uncovered_prompt_tokens == 4_000
        assert summary.cached_prompt_tokens == 800
        assert summary.uncached_prompt_tokens == 200
        assert summary.cache_hit_ratio == 0.8

    def test_miss_only_report_is_not_without_cache_data(self):
        """A reported miss with no read/write is covered, not a blind row."""
        summary = summarize_session_cache(
            [
                _row(
                    prompt_tokens=500,
                    meta_data={"usage_details": {"prompt_cache_miss_tokens": 500}},
                )
            ]
        )
        assert summary.requests_total == 1
        assert summary.requests_with_cache_data == 1
        assert summary.requests_without_cache_data == 0
        assert summary.covered_prompt_tokens == 500
        assert summary.uncovered_prompt_tokens == 0
        assert summary.uncached_prompt_tokens == 500

    def test_write_tokens_none_when_no_provider_reports_writes(self):
        """No write concept is not the same claim as zero rewritten tokens."""
        summary = summarize_session_cache(
            [_row(prompt_tokens=1_000, cache_read_tokens=500)]
        )
        assert summary.cache_write_tokens is None

    def test_write_tokens_summed_when_reported(self):
        summary = summarize_session_cache(
            [
                _row(
                    prompt_tokens=1_000,
                    cache_read_tokens=500,
                    cache_creation_tokens=200,
                    model_alias="anthropic/claude-sonnet-4",
                    provider_name="anthropic",
                ),
                _row(
                    prompt_tokens=1_000,
                    cache_read_tokens=900,
                    cache_creation_tokens=0,
                    model_alias="anthropic/claude-sonnet-4",
                    provider_name="anthropic",
                ),
            ]
        )
        assert summary.cache_write_tokens == 200

    def test_savings_from_exact_catalog_prices(self):
        """gpt-4o carries explicit input and cache-read costs in the catalog."""
        summary = summarize_session_cache(
            [
                _row(
                    prompt_tokens=10_000,
                    cache_read_tokens=10_000,
                    model_alias="gpt-4o",
                    provider_name="openai",
                )
            ]
        )
        # input 2.5e-06/token, cache read 1.25e-06/token -> 1.25e-06 saved each.
        assert summary.savings_basis == SAVINGS_BASIS_CATALOG_EXACT
        assert summary.savings_omitted_reason is None
        assert summary.estimated_cache_savings_usd == 0.0125

    def test_savings_omitted_when_catalog_lacks_cache_price(self):
        """Honesty rail: no multiplier guesswork behind a dollar figure."""
        summary = summarize_session_cache(
            [
                _row(
                    prompt_tokens=1_000,
                    cache_read_tokens=900,
                    model_alias="totally-unknown-model-xyz",
                    provider_name="custom",
                )
            ]
        )
        assert summary.estimated_cache_savings_usd is None
        assert summary.savings_omitted_reason == SAVINGS_OMITTED_NO_CATALOG_PRICE

    def test_savings_omitted_when_no_cache_reads(self):
        summary = summarize_session_cache(
            [_row(prompt_tokens=1_000, cache_read_tokens=0)]
        )
        assert summary.estimated_cache_savings_usd is None
        assert summary.savings_omitted_reason == SAVINGS_OMITTED_NO_CACHE_READS

    def test_empty_session_has_no_ratio(self):
        summary = summarize_session_cache([])
        assert summary.requests_total == 0
        assert summary.cache_hit_ratio is None
        assert summary.cache_write_tokens is None

    def test_mixed_models_savings_is_partial_lower_bound(self):
        """A priced model's savings survive an unpriced model in the session."""
        summary = summarize_session_cache(
            [
                _row(
                    prompt_tokens=10_000,
                    cache_read_tokens=10_000,
                    model_alias="gpt-4o",
                    provider_name="openai",
                ),
                _row(
                    prompt_tokens=1_000,
                    cache_read_tokens=900,
                    model_alias="totally-unknown-model-xyz",
                    provider_name="custom",
                ),
            ]
        )
        assert summary.estimated_cache_savings_usd == 0.0125
        assert summary.savings_basis == SAVINGS_BASIS_CATALOG_EXACT_PARTIAL
        assert summary.savings_omitted_reason is None

    def test_unknown_prompt_totals_counted_not_zeroed(self):
        """prompt_tokens=None is unknown, not zero, in the rollup."""
        summary = summarize_session_cache(
            [
                _row(prompt_tokens=None, cache_read_tokens=500),
                _row(prompt_tokens=None),
                _row(prompt_tokens=1_000, cache_read_tokens=800),
            ]
        )
        assert summary.requests_with_unknown_prompt_tokens == 2
        # Only the row with a real prompt total contributes to the sums.
        assert summary.covered_prompt_tokens == 1_000
        assert summary.uncovered_prompt_tokens == 0

    def test_unknown_prompt_totals_not_zeroed_in_model_groups(self):
        """A group must disclose unknown prompt totals, not absorb them as 0."""
        summary = summarize_session_cache(
            [
                _row(prompt_tokens=None, cache_read_tokens=500),
                _row(prompt_tokens=1_000, cache_read_tokens=800),
            ]
        )
        assert len(summary.models) == 1
        group = summary.models[0]
        # The None row contributes to the unknown counter, never 0 tokens.
        assert group.prompt_tokens == 1_000
        assert group.requests_with_unknown_prompt_tokens == 1
        assert group.requests == 2
        payload = group.as_dict()
        assert payload["requests_with_unknown_prompt_tokens"] == 1

    def test_summary_as_dict_exposes_models(self):
        """The per-model breakdown is part of the serialized summary."""
        summary = summarize_session_cache(
            [
                _row(
                    prompt_tokens=1_000,
                    cache_read_tokens=800,
                    cache_creation_tokens=100,
                    model_alias="anthropic/claude-sonnet-4",
                    provider_name="anthropic",
                ),
                _row(
                    prompt_tokens=2_000,
                    cache_read_tokens=1_500,
                    model_alias="gpt-4o",
                    provider_name="openai",
                ),
            ]
        )
        payload = summary.as_dict()
        models = {entry["model_alias"]: entry for entry in payload["models"]}
        assert set(models) == {"anthropic/claude-sonnet-4", "gpt-4o"}
        anthropic = models["anthropic/claude-sonnet-4"]
        assert anthropic["cache_read_tokens"] == 800
        assert anthropic["cache_creation_tokens"] == 100
        assert anthropic["write_reported"] is True
        openai = models["gpt-4o"]
        assert openai["cache_read_tokens"] == 1_500
        assert openai["write_reported"] is False
