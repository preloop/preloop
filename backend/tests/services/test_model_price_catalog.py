"""Tests for the vendored model-price catalog loader."""

import json

import litellm

from preloop.services import model_price_catalog
from preloop.services.model_price_catalog import (
    CATALOG_PATH,
    catalog_metadata,
    load_catalog,
)


def test_vendored_catalog_exists_with_provenance() -> None:
    """The snapshot ships in-repo with source/date/count metadata."""
    raw = json.loads(CATALOG_PATH.read_text())
    meta = raw.get("_preloop_meta")
    assert meta is not None
    assert meta["source_url"].startswith("https://")
    assert meta["fetched_at"]
    assert meta["model_count"] > 500


def test_load_catalog_registers_prices_with_litellm(tmp_path) -> None:
    """register_model merges catalog entries into litellm.model_cost."""
    snapshot = {
        "_preloop_meta": {
            "source_url": "https://example.test",
            "fetched_at": "2026-07-12T00:00:00+00:00",
            "model_count": 1,
        },
        "preloop-test-model": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000002,
        },
    }
    path = tmp_path / "model_prices.json"
    path.write_text(json.dumps(snapshot))

    assert load_catalog(path, force=True) is True
    assert "preloop-test-model" in litellm.model_cost
    assert litellm.model_cost["preloop-test-model"]["input_cost_per_token"] == 0.000001
    # Pre-existing models keep their entries (merge, not replace).
    assert len(litellm.model_cost) > 1


def test_load_catalog_missing_file_is_nonfatal(tmp_path) -> None:
    """A missing snapshot logs and falls back to litellm defaults."""
    assert load_catalog(tmp_path / "missing.json", force=True) is False


def test_catalog_metadata_reads_lazily() -> None:
    """Provenance is readable without forcing registration."""
    model_price_catalog._metadata = None
    meta = catalog_metadata()
    assert meta is not None
    assert meta.get("model_count") > 500


def test_live_lookup_registers_matching_model(monkeypatch) -> None:
    """A model present upstream is registered with litellm and returned."""
    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setattr(
        model_price_catalog,
        "_fetch_remote_price_map",
        lambda: {
            "preloop-live-model": {
                "litellm_provider": "openai",
                "mode": "chat",
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000006,
            }
        },
    )

    matched = model_price_catalog.lookup_model_price_now(
        ["preloop-live-model", "openai/preloop-live-model"]
    )

    assert matched == "preloop-live-model"
    assert "preloop-live-model" in litellm.model_cost


def test_live_lookup_negative_caches_unknown_models(monkeypatch) -> None:
    """Unknown models are looked up once, then negative-cached."""
    model_price_catalog.reset_lookup_state_for_tests()
    calls = {"count": 0}

    def _fake_fetch():
        calls["count"] += 1
        return {}

    monkeypatch.setattr(model_price_catalog, "_fetch_remote_price_map", _fake_fetch)

    assert model_price_catalog.lookup_model_price_now(["nope-model"]) is None
    assert model_price_catalog.lookup_model_price_now(["nope-model"]) is None
    assert calls["count"] == 1  # second call short-circuits on the negative cache


def test_remote_fetch_failure_backs_off(monkeypatch) -> None:
    """A failed download sets a backoff so the next lookup skips the network."""
    model_price_catalog.reset_lookup_state_for_tests()
    calls = {"count": 0}

    class _FakeHttpx:
        @staticmethod
        def get(*args, **kwargs):
            calls["count"] += 1
            raise RuntimeError("network down")

    import sys

    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)

    expected_attempts = max(1, model_price_catalog._REMOTE_FETCH_RETRIES + 1)
    assert model_price_catalog._fetch_remote_price_map() is None
    assert model_price_catalog._fetch_remote_price_map() is None
    assert calls["count"] == expected_attempts  # second call sits out backoff


def test_lookup_model_price_now_registers_match(monkeypatch) -> None:
    """Live lookup path registers a matched remote price with litellm."""
    model_price_catalog.reset_lookup_state_for_tests()
    remote = {
        "openai/live-lookup-model": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000002,
        }
    }
    monkeypatch.setattr(model_price_catalog, "_fetch_remote_price_map", lambda: remote)
    matched = model_price_catalog.lookup_model_price_now(["openai/live-lookup-model"])
    assert matched == "openai/live-lookup-model"
    assert "openai/live-lookup-model" in litellm.model_cost


def test_schedule_price_lookup_disabled_under_testing() -> None:
    """The background scheduler is inert in test runs (TESTING=true)."""
    model_price_catalog.reset_lookup_state_for_tests()
    from types import SimpleNamespace

    ai_model = SimpleNamespace(
        provider_name="openai",
        model_identifier="whatever-model",
        meta_data=None,
        model_parameters=None,
    )
    assert model_price_catalog.schedule_price_lookup(ai_model=ai_model) is False


def test_model_log_token_hides_raw_name() -> None:
    """Log tokens are stable hashes and never embed the raw model name."""
    raw = "secret-customer-model/v1"
    token = model_price_catalog._model_log_token(raw)
    assert token.startswith("model#")
    assert raw not in token
    assert model_price_catalog._model_log_token(raw) == token


def test_schedule_price_lookup_uses_bounded_executor(monkeypatch) -> None:
    """Live lookups submit to the shared pool instead of spawning raw threads."""
    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")

    from types import SimpleNamespace

    class _Settings:
        model_price_live_lookup_enabled = True

    import preloop.config as config_mod

    monkeypatch.setattr(config_mod, "settings", _Settings())

    submitted: list[object] = []

    def _fake_submit(fn):
        submitted.append(fn)
        return object()

    monkeypatch.setattr(model_price_catalog._LOOKUP_EXECUTOR, "submit", _fake_submit)

    ai_model = SimpleNamespace(
        provider_name="openai",
        model_identifier="executor-test-model",
        meta_data=None,
        model_parameters=None,
    )
    assert model_price_catalog.schedule_price_lookup(ai_model=ai_model) is True
    assert len(submitted) == 1
    assert "executor-test-model" in model_price_catalog._pending_lookups


# ---------------------------------------------------------------------------
# OpenRouter marketplace pricing (models absent from litellm's map)
# ---------------------------------------------------------------------------


def test_openrouter_map_converts_per_token_pricing() -> None:
    """OpenRouter's per-token strings become litellm-shaped price entries.

    OpenRouter publishes authoritative per-token prices as decimal strings.
    They are converted verbatim (no unit rescaling) so a dashboard number can
    always be traced back to the vendor's published price.
    """
    payload = {
        "data": [
            {
                "id": "deepseek/deepseek-v4-flash-0731",
                "pricing": {
                    "prompt": "0.00000009",
                    "completion": "0.00000018",
                    "input_cache_read": "0.000000018",
                },
            }
        ]
    }

    entries = model_price_catalog._openrouter_entries_from_payload(payload)

    entry = entries["openrouter/deepseek/deepseek-v4-flash-0731"]
    assert entry["input_cost_per_token"] == 9e-08
    assert entry["output_cost_per_token"] == 1.8e-07
    assert entry["cache_read_input_token_cost"] == 1.8e-08
    assert entry["litellm_provider"] == "openrouter"


def test_openrouter_map_skips_models_without_usable_prices() -> None:
    """Zero/missing prices are not registered as a real $0 price.

    A free-tier or price-less listing must stay unpriced rather than assert
    that the model costs nothing.
    """
    payload = {
        "data": [
            {"id": "vendor/no-pricing"},
            {"id": "vendor/zero", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "vendor/bad", "pricing": {"prompt": "abc", "completion": "x"}},
        ]
    }

    entries = model_price_catalog._openrouter_entries_from_payload(payload)

    assert entries == {}


def test_live_lookup_falls_back_to_openrouter_for_marketplace_models(
    monkeypatch,
) -> None:
    """A model missing from litellm's map is priced from OpenRouter.

    This is the exact customer-reported case: OpenRouter-routed DeepSeek usage
    that litellm does not carry, which previously surfaced as $0.00.
    """
    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setattr(model_price_catalog, "_fetch_remote_price_map", lambda: {})
    monkeypatch.setattr(
        model_price_catalog,
        "_fetch_openrouter_price_map",
        lambda: {
            "openrouter/deepseek/deepseek-v4-flash-0731": {
                "litellm_provider": "openrouter",
                "mode": "chat",
                "input_cost_per_token": 9e-08,
                "output_cost_per_token": 1.8e-07,
            }
        },
    )

    matched = model_price_catalog.lookup_model_price_now(
        [
            "openrouter/deepseek/deepseek-v4-flash-0731",
            "deepseek/deepseek-v4-flash-0731",
        ]
    )

    assert matched == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert "openrouter/deepseek/deepseek-v4-flash-0731" in litellm.model_cost
