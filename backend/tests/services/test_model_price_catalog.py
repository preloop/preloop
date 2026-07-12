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
