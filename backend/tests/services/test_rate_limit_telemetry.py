"""Unit tests for upstream rate-limit header parsing and classification (#136)."""

import sys
from types import ModuleType, SimpleNamespace

from preloop.services.rate_limit_telemetry import (
    classify_rate_limit_subtype,
    headers_from_exception,
    headers_from_litellm_response,
    parse_rate_limit_headers,
)


class TestParseRateLimitHeaders:
    def test_no_headers_returns_none(self):
        assert parse_rate_limit_headers(None) is None
        assert parse_rate_limit_headers({}) is None

    def test_unrelated_headers_return_none(self):
        assert parse_rate_limit_headers({"content-type": "application/json"}) is None

    def test_anthropic_headers_normalized(self):
        snapshot = parse_rate_limit_headers(
            {
                "anthropic-ratelimit-requests-limit": "50",
                "anthropic-ratelimit-requests-remaining": "49",
                "anthropic-ratelimit-requests-reset": "2026-08-01T13:00:00Z",
                "anthropic-ratelimit-tokens-limit": "80000",
                "anthropic-ratelimit-tokens-remaining": "12345",
                "anthropic-ratelimit-tokens-reset": "2026-08-01T13:00:00Z",
                "content-type": "application/json",
            }
        )
        assert snapshot is not None
        assert snapshot.requests_limit == 50
        assert snapshot.requests_remaining == 49
        assert snapshot.requests_reset_at == "2026-08-01T13:00:00Z"
        assert snapshot.tokens_limit == 80000
        assert snapshot.tokens_remaining == 12345
        # Raw capture keeps every observed ratelimit header verbatim and
        # excludes unrelated headers.
        assert "anthropic-ratelimit-requests-limit" in snapshot.raw
        assert "content-type" not in snapshot.raw

    def test_openai_headers_normalized_with_durations(self):
        snapshot = parse_rate_limit_headers(
            {
                "x-ratelimit-limit-requests": "500",
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "1m30s",
                "x-ratelimit-limit-tokens": "30000",
                "x-ratelimit-remaining-tokens": "29000",
                "x-ratelimit-reset-tokens": "12ms",
            }
        )
        assert snapshot is not None
        assert snapshot.requests_limit == 500
        assert snapshot.requests_remaining == 0
        assert snapshot.requests_reset_after_ms == 90_000
        assert snapshot.tokens_limit == 30000
        assert snapshot.tokens_remaining == 29000
        assert snapshot.tokens_reset_after_ms == 12

    def test_retry_after_seconds(self):
        snapshot = parse_rate_limit_headers({"retry-after": "30"})
        assert snapshot is not None
        assert snapshot.retry_after_ms == 30_000

    def test_retry_after_unparseable_is_none_but_raw_kept(self):
        snapshot = parse_rate_limit_headers({"retry-after": "not-a-number"})
        assert snapshot is not None
        assert snapshot.retry_after_ms is None
        assert snapshot.raw["retry-after"] == "not-a-number"

    def test_unknown_ratelimit_headers_kept_raw_only(self):
        snapshot = parse_rate_limit_headers(
            {"anthropic-ratelimit-unified-status": "allowed"}
        )
        assert snapshot is not None
        assert snapshot.raw["anthropic-ratelimit-unified-status"] == "allowed"
        assert snapshot.requests_limit is None

    def test_headers_object_with_items(self):
        class _Headers:
            def items(self):
                return [("Retry-After", "5")]

        snapshot = parse_rate_limit_headers(_Headers())
        assert snapshot is not None
        assert snapshot.retry_after_ms == 5_000

    def test_broken_headers_object_returns_none(self):
        class _Broken:
            def items(self):
                raise RuntimeError("boom")

        assert parse_rate_limit_headers(_Broken()) is None

    def test_to_meta_drops_nones_and_keeps_raw(self):
        snapshot = parse_rate_limit_headers({"retry-after": "1"})
        assert snapshot is not None
        meta = snapshot.to_meta()
        assert meta["retry_after_ms"] == 1_000
        assert meta["headers"] == {"retry-after": "1"}
        assert "requests_limit" not in meta


class TestHeadersFromException:
    def test_direct_headers_attribute(self):
        exc = Exception("boom")
        exc.headers = {"retry-after": "3"}  # type: ignore[attr-defined]
        assert headers_from_exception(exc) == {"retry-after": "3"}

    def test_response_headers_attribute(self):
        exc = Exception("boom")
        exc.response = SimpleNamespace(headers={"retry-after": "4"})  # type: ignore[attr-defined]
        assert headers_from_exception(exc) == {"retry-after": "4"}

    def test_no_headers_returns_none(self):
        assert headers_from_exception(Exception("boom")) is None


class TestHeadersFromLitellmResponse:
    def test_strips_llm_provider_prefix(self):
        response = SimpleNamespace(
            _hidden_params={
                "additional_headers": {
                    "llm_provider-anthropic-ratelimit-requests-remaining": "7",
                    "x-ratelimit-remaining-tokens": "100",
                }
            }
        )
        headers = headers_from_litellm_response(response)
        assert headers == {
            "anthropic-ratelimit-requests-remaining": "7",
            "x-ratelimit-remaining-tokens": "100",
        }

    def test_missing_hidden_params_returns_none(self):
        assert headers_from_litellm_response(SimpleNamespace()) is None
        assert headers_from_litellm_response(SimpleNamespace(_hidden_params={})) is None


class TestClassifyRateLimitSubtype:
    def test_non_429_returns_none(self):
        assert classify_rate_limit_subtype(200, None) == (None, None)
        assert classify_rate_limit_subtype(503, "overloaded") == (None, None)

    def test_quota_markers_classified_as_quota_exhausted(self):
        subtype, source = classify_rate_limit_subtype(
            429, "You exceeded your current quota, please check your plan"
        )
        assert subtype == "quota_exhausted"
        assert source in ("taxonomy", "heuristic")

    def test_plain_429_classified_as_transient(self):
        subtype, source = classify_rate_limit_subtype(
            429, "Number of request tokens has exceeded your rate limit"
        )
        assert subtype == "transient"
        assert source in ("taxonomy", "heuristic")

    def test_source_is_heuristic_until_taxonomy_lands(self):
        """Pin the provenance label for the pre-#141 world.

        When the shared upstream-error taxonomy (PR #141) merges, this test
        should be updated to expect ``taxonomy``.
        """
        try:
            import preloop.services.upstream_errors  # noqa: F401

            expected = "taxonomy"
        except ImportError:
            expected = "heuristic"
        _, source = classify_rate_limit_subtype(429, "slow down")
        assert source == expected

    def test_delegates_to_taxonomy_when_module_present(self, monkeypatch):
        """A working #141 classifier is used and labeled as the source."""
        fake = ModuleType("preloop.services.upstream_errors")
        fake.ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED = "upstream_quota_exhausted"
        fake.classify_recorded_error = (
            lambda status_code, detail: "upstream_quota_exhausted"
        )
        monkeypatch.setitem(sys.modules, "preloop.services.upstream_errors", fake)

        assert classify_rate_limit_subtype(429, "anything") == (
            "quota_exhausted",
            "taxonomy",
        )

    def test_taxonomy_runtime_failure_falls_back_to_heuristic(self, monkeypatch):
        """A buggy #141 classifier must not propagate out of telemetry.

        Runtime failures (not just ImportError) fall back to the local
        heuristic, labeled as such.
        """

        def _boom(status_code, detail):
            raise TypeError("unexpected input shape")

        fake = ModuleType("preloop.services.upstream_errors")
        fake.ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED = "upstream_quota_exhausted"
        fake.classify_recorded_error = _boom
        monkeypatch.setitem(sys.modules, "preloop.services.upstream_errors", fake)

        assert classify_rate_limit_subtype(429, "exceeded your current quota") == (
            "quota_exhausted",
            "heuristic",
        )
        assert classify_rate_limit_subtype(429, "slow down") == (
            "transient",
            "heuristic",
        )
