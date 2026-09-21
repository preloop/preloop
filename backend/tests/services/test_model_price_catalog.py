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


def test_vendored_catalog_prices_embedding_models() -> None:
    """The gateway serves embeddings, so the snapshot has to price them.

    Without embedding rows every vector call the gateway meters would land as
    ``cost_source='unpriced'``. The named model is the one the OpenAI-
    compatible route is most often pointed at; the mode assertion keeps a
    future refresh from silently filtering embeddings back out.
    """
    raw = json.loads(CATALOG_PATH.read_text())
    entry = raw.get("text-embedding-3-small")
    assert entry is not None
    assert entry["mode"] == "embedding"
    assert entry["input_cost_per_token"] > 0
    embedding_entries = [
        value
        for key, value in raw.items()
        if key != "_preloop_meta" and (value or {}).get("mode") == "embedding"
    ]
    assert len(embedding_entries) > 10
    # Usage is metered in tokens, so an embedding row without a per-token
    # input price could only ever be billed as $0.
    for value in embedding_entries:
        assert isinstance(value.get("input_cost_per_token"), (int, float))


def test_vendored_catalog_prices_gemini_38_flash_with_cache_rate() -> None:
    """Gemini 3.8 Flash ships priced, cache-read rate included.

    The snapshot went 71 days without a refresh and stopped at Gemini 3.5,
    so 3.8 Flash rows were priced only by interim account overrides that had
    no cache-read rate: cache hits were billed at the full input rate
    (issue #850). Upstream litellm publishes the rate, so the vendored
    snapshot has to carry it, for the Gemini API key and the Vertex key
    alike.
    """
    raw = json.loads(CATALOG_PATH.read_text())
    for key in ("gemini/gemini-3.8-flash", "vertex_ai/gemini-3.8-flash"):
        entry = raw.get(key)
        assert entry is not None, f"{key} is missing from the vendored snapshot"
        assert entry["mode"] == "chat"
        assert entry["input_cost_per_token"] == 7.5e-07
        assert entry["output_cost_per_token"] == 3.75e-06
        # A cached input token costs a tenth of an uncached one; without this
        # field the estimator falls back to the full input price.
        assert entry["cache_read_input_token_cost"] == 7.5e-08


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
    assert model_price_catalog.schedule_price_lookup(ai_model_id="unused") is False


def test_model_log_token_hides_raw_name() -> None:
    """Log tokens are stable hashes and never embed the raw model name."""
    raw = "secret-customer-model/v1"
    token = model_price_catalog._model_log_token(raw)
    assert token.startswith("model#")
    assert raw not in token
    assert model_price_catalog._model_log_token(raw) == token


def _yield_lookup_model(ai_model: object):
    from contextlib import contextmanager

    @contextmanager
    def _session(_ai_model_id: object):
        yield ai_model

    return _session


def _yield_lookup_models(models_by_id: dict):
    from contextlib import contextmanager

    @contextmanager
    def _session(ai_model_id: object):
        yield models_by_id[ai_model_id]

    return _session


def test_schedule_price_lookup_uses_bounded_executor(monkeypatch) -> None:
    """Live lookups submit to the shared pool instead of spawning raw threads."""
    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")

    from types import SimpleNamespace
    from uuid import uuid4

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
    model_id = uuid4()
    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _yield_lookup_model(ai_model),
    )
    assert model_price_catalog.schedule_price_lookup(ai_model_id=model_id) is True
    assert len(submitted) == 1
    assert "executor-test-model" in model_price_catalog._pending_lookups


def test_alibaba_negative_cache_is_scoped_to_usd_region(monkeypatch) -> None:
    """A US-East miss must not suppress Singapore self-heal for the same SKU."""
    from types import SimpleNamespace
    from uuid import uuid4

    from preloop.services.alibaba_price_catalog import CatalogRefreshStatus

    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")

    class _Settings:
        model_price_live_lookup_enabled = True

    import preloop.config as config_mod

    monkeypatch.setattr(config_mod, "settings", _Settings())
    monkeypatch.setattr(
        "preloop.services.alibaba_price_catalog.prepare_refresh",
        lambda ai_model: CatalogRefreshStatus.unreachable,
    )
    submitted: list[object] = []

    def _fake_submit(fn):
        submitted.append(fn)
        fn()
        return object()

    monkeypatch.setattr(model_price_catalog._LOOKUP_EXECUTOR, "submit", _fake_submit)

    us_id = uuid4()
    sg_id = uuid4()
    us_model = SimpleNamespace(
        provider_name="qwen",
        model_identifier="qwen3.8-flash",
        api_endpoint="https://ws.us-east-1.maas.aliyuncs.com/compatible-mode/v1",
        meta_data=None,
        model_parameters=None,
    )
    sg_model = SimpleNamespace(
        provider_name="qwen",
        model_identifier="qwen3.8-flash",
        api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        meta_data=None,
        model_parameters=None,
    )
    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _yield_lookup_models({us_id: us_model, sg_id: sg_model}),
    )
    assert model_price_catalog.schedule_price_lookup(ai_model_id=us_id) is True
    assert (
        "alibaba:united-states:ws.us-east-1.maas.aliyuncs.com:qwen3.8-flash"
        in model_price_catalog._negative_cache
    )
    submitted.clear()
    assert model_price_catalog.schedule_price_lookup(ai_model_id=us_id) is False
    assert model_price_catalog.schedule_price_lookup(ai_model_id=sg_id) is True
    assert submitted  # Singapore still scheduled


def test_schedule_price_lookup_resolves_credentials_from_persisted_model_id(
    db_session, test_user, monkeypatch
) -> None:
    """HTTP snapshots are credential-free; lookup re-fetches the live row."""
    from contextlib import contextmanager

    from preloop.models.crud import crud_ai_model
    from preloop.services.alibaba_price_catalog import CatalogRefreshStatus, _api_key
    from preloop.services.gateway_execution import GatewayModelSnapshot

    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr("preloop.config.settings.model_price_live_lookup_enabled", True)

    model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Alibaba lookup model",
            "provider_name": "qwen",
            "model_identifier": "qwen3.8-flash",
            "api_endpoint": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-persisted-alibaba",
        },
        account_id=test_user.account_id,
    )
    snapshot = GatewayModelSnapshot.from_model(model)
    assert snapshot.api_key is None
    assert snapshot.credentials_secret is None
    assert _api_key(snapshot) is None

    seen_keys: list[str | None] = []
    refreshed: list[object] = []

    def _refresh(prepared):
        refreshed.append(prepared)
        seen_keys.append(prepared.api_key)
        return CatalogRefreshStatus.ingested

    monkeypatch.setattr(
        "preloop.services.alibaba_price_catalog.refresh_prepared",
        _refresh,
    )

    @contextmanager
    def _lookup_from_test_db(ai_model_id):
        yield crud_ai_model.get(db_session, id=ai_model_id)

    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _lookup_from_test_db,
    )

    def _fake_submit(fn):
        fn()
        return object()

    monkeypatch.setattr(model_price_catalog._LOOKUP_EXECUTOR, "submit", _fake_submit)

    assert model_price_catalog.schedule_price_lookup(ai_model_id=snapshot.id) is True
    assert seen_keys == ["sk-persisted-alibaba"]
    assert refreshed
    assert not isinstance(refreshed[0], GatewayModelSnapshot)
    assert refreshed[0].url == "https://dashscope-intl.aliyuncs.com/api/v1/models"
    assert "sk-persisted-alibaba" not in repr(refreshed[0])


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


def test_price_map_cache_serves_within_ttl_and_refetches_after_expiry() -> None:
    """The shared cache hits once within the TTL and refetches once stale."""
    cache = model_price_catalog._PriceMapCache("test")
    calls = {"count": 0}

    def _fetch():
        calls["count"] += 1
        return {"model": {"input_cost_per_token": 1e-06}}

    assert cache.get(_fetch) is not None
    assert cache.get(_fetch) is not None
    assert calls["count"] == 1  # second call served from cache

    # Age the cache past its TTL and confirm exactly one more download.
    cache._fetched_at -= model_price_catalog._REMOTE_TTL_SECONDS + 1
    assert cache.get(_fetch) is not None
    assert calls["count"] == 2


def test_price_map_cache_backs_off_after_failure() -> None:
    """A failed fetch suppresses the next attempt until the backoff expires."""
    cache = model_price_catalog._PriceMapCache("test")
    calls = {"count": 0}

    def _failing_fetch():
        calls["count"] += 1
        return None

    assert cache.get(_failing_fetch) is None
    assert cache.get(_failing_fetch) is None
    assert calls["count"] == 1  # second call sits out the backoff

    cache._failed_at -= model_price_catalog._REMOTE_FAILURE_BACKOFF_SECONDS + 1
    assert cache.get(_failing_fetch) is None
    assert calls["count"] == 2


def test_openrouter_fetch_failure_backs_off(monkeypatch) -> None:
    """OpenRouter downloads honor the same backoff as litellm's map."""
    model_price_catalog.reset_lookup_state_for_tests()
    calls = {"count": 0}

    class _FakeHttpx:
        @staticmethod
        def get(*args, **kwargs):
            calls["count"] += 1
            raise RuntimeError("network down")

    import sys

    monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)

    assert model_price_catalog._fetch_openrouter_price_map() is None
    assert model_price_catalog._fetch_openrouter_price_map() is None
    assert calls["count"] == 1  # second call sits out backoff


def test_reset_lookup_state_clears_both_caches() -> None:
    """Test reset clears the litellm and OpenRouter caches together."""
    model_price_catalog._remote_cache.set({"a": {}})
    model_price_catalog._openrouter_cache.set({"openrouter/b": {}})

    model_price_catalog.reset_lookup_state_for_tests()

    state = model_price_catalog._module_state_for_tests()
    assert state["remote"]["map"] is None
    assert state["remote"]["failed_at"] == 0.0
    assert state["openrouter"]["map"] is None
    assert state["openrouter"]["failed_at"] == 0.0


def test_negative_cache_evicts_oldest_when_full() -> None:
    model_price_catalog.reset_lookup_state_for_tests()
    original_max = model_price_catalog._MAX_NEGATIVE_CACHE_ENTRIES
    model_price_catalog._MAX_NEGATIVE_CACHE_ENTRIES = 2
    try:
        with model_price_catalog._lookup_lock:
            model_price_catalog._remember_negative_lookup("a", stamp=1.0)
            model_price_catalog._remember_negative_lookup("b", stamp=2.0)
            model_price_catalog._remember_negative_lookup("c", stamp=3.0)
            assert "a" not in model_price_catalog._negative_cache
            assert model_price_catalog._negative_cache["c"] == 3.0
            assert len(model_price_catalog._negative_cache) == 2
    finally:
        model_price_catalog._MAX_NEGATIVE_CACHE_ENTRIES = original_max
        model_price_catalog.reset_lookup_state_for_tests()


def test_pending_rows_recover_before_alerts_without_duplicate_fetch(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr("preloop.config.settings.model_price_live_lookup_enabled", True)
    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _yield_lookup_model(
            SimpleNamespace(
                provider_name="openai",
                model_identifier="recovery-example",
                meta_data=None,
                model_parameters=None,
            )
        ),
    )
    workers = []
    monkeypatch.setattr(model_price_catalog._LOOKUP_EXECUTOR, "submit", workers.append)
    events = []
    monkeypatch.setattr(
        model_price_catalog, "lookup_model_price_now", lambda _: "recovery-example"
    )
    monkeypatch.setattr(
        model_price_catalog,
        "_reprice_usage_row",
        lambda row: events.append(("reprice", row)),
    )
    monkeypatch.setattr(
        "preloop.services.unpriced_model_alert.notify_unpriced_usage_row",
        lambda row, **kwargs: events.append((kwargs["refresh_status"], row)),
    )
    for row in ("row-1", "row-2"):
        assert model_price_catalog.schedule_price_lookup(
            ai_model_id="model-1", api_usage_id=row, notify_after_lookup=True
        )
    assert len(workers) == 1
    assert events == []
    workers[0]()
    assert events == [
        ("reprice", "row-1"),
        ("ingested", "row-1"),
        ("reprice", "row-2"),
        ("ingested", "row-2"),
    ]


def test_failed_lookup_keeps_unresolved_notification(monkeypatch) -> None:
    from types import SimpleNamespace

    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr("preloop.config.settings.model_price_live_lookup_enabled", True)
    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _yield_lookup_model(
            SimpleNamespace(
                provider_name="openai",
                model_identifier="recovery-failure",
                meta_data=None,
                model_parameters=None,
            )
        ),
    )
    monkeypatch.setattr(
        model_price_catalog._LOOKUP_EXECUTOR, "submit", lambda worker: worker()
    )
    monkeypatch.setattr(model_price_catalog, "lookup_model_price_now", lambda _: None)
    notifications = []
    monkeypatch.setattr(
        "preloop.services.unpriced_model_alert.notify_unpriced_usage_row",
        lambda row, **kwargs: notifications.append(kwargs["refresh_status"]),
    )
    assert model_price_catalog.schedule_price_lookup(
        ai_model_id="model-1", api_usage_id="row-1", notify_after_lookup=True
    )
    assert notifications == ["unavailable"]


def test_submit_failure_notifies_queued_rows(monkeypatch) -> None:
    """Executor reject after queue ownership still alerts notify=True rows."""
    from types import SimpleNamespace

    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr("preloop.config.settings.model_price_live_lookup_enabled", True)
    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _yield_lookup_model(
            SimpleNamespace(
                provider_name="openai",
                model_identifier="recovery-submit-failure",
                meta_data=None,
                model_parameters=None,
            )
        ),
    )

    def _fail_submit(_worker):
        key = next(iter(model_price_catalog._pending_lookups))
        model_price_catalog._pending_usage[key].append(("row-joined", True))
        model_price_catalog._pending_usage[key].append(("row-silent", False))
        raise RuntimeError("synthetic executor reject")

    monkeypatch.setattr(model_price_catalog._LOOKUP_EXECUTOR, "submit", _fail_submit)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "preloop.services.unpriced_model_alert.notify_unpriced_usage_row",
        lambda row, **kwargs: events.append((kwargs["refresh_status"], row)),
    )

    raised: RuntimeError | None = None
    try:
        model_price_catalog.schedule_price_lookup(
            ai_model_id="model-1",
            api_usage_id="row-current",
            notify_after_lookup=True,
        )
    except RuntimeError as exc:
        raised = exc
    assert raised is not None
    assert "synthetic executor reject" in str(raised)
    assert events == [
        ("submit_failed", "row-current"),
        ("submit_failed", "row-joined"),
    ]
    assert model_price_catalog._pending_usage == {}
    assert model_price_catalog._pending_lookups == set()


def test_repricing_failure_still_notifies_unresolved_usage(monkeypatch) -> None:
    from types import SimpleNamespace

    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr("preloop.config.settings.model_price_live_lookup_enabled", True)
    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _yield_lookup_model(
            SimpleNamespace(
                provider_name="openai",
                model_identifier="recovery-update-failure",
                meta_data=None,
                model_parameters=None,
            )
        ),
    )
    monkeypatch.setattr(
        model_price_catalog._LOOKUP_EXECUTOR, "submit", lambda worker: worker()
    )
    monkeypatch.setattr(
        model_price_catalog,
        "lookup_model_price_now",
        lambda _: "recovery-update-failure",
    )

    def failed_reprice(row):
        raise RuntimeError("synthetic update failure")

    monkeypatch.setattr(model_price_catalog, "_reprice_usage_row", failed_reprice)
    notifications = []
    monkeypatch.setattr(
        "preloop.services.unpriced_model_alert.notify_unpriced_usage_row",
        lambda row, **kwargs: notifications.append(kwargs["refresh_status"]),
    )
    assert model_price_catalog.schedule_price_lookup(
        ai_model_id="model-1", api_usage_id="row-1", notify_after_lookup=True
    )
    assert notifications == ["ingested_repricing_failed"]


def test_ingested_alibaba_catalog_missing_dimension_is_not_repeated(
    monkeypatch,
) -> None:
    from types import SimpleNamespace
    from preloop.services import alibaba_price_catalog as catalog

    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr("preloop.config.settings.model_price_live_lookup_enabled", True)
    model = SimpleNamespace(
        provider_name="qwen",
        model_identifier="qwen-cache-example",
        api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        meta_data=None,
    )
    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _yield_lookup_model(model),
    )
    monkeypatch.setattr(
        catalog,
        "prepare_refresh",
        lambda _: catalog.PreparedCatalogRefresh(
            catalog.SINGAPORE_NATIVE_URL, "international", "synthetic"
        ),
    )
    downloads = []
    monkeypatch.setattr(
        catalog,
        "refresh_prepared",
        lambda _: downloads.append(1) or catalog.CatalogRefreshStatus.ingested,
    )
    monkeypatch.setattr(
        model_price_catalog._LOOKUP_EXECUTOR, "submit", lambda worker: worker()
    )
    monkeypatch.setattr(model_price_catalog, "_reprice_usage_row", lambda _: None)
    assert model_price_catalog.schedule_price_lookup(
        ai_model_id="model-example", api_usage_id="usage-1"
    )
    assert not model_price_catalog.schedule_price_lookup(
        ai_model_id="model-example", api_usage_id="usage-2"
    )
    assert downloads == [1]


def test_workspace_refresh_failure_does_not_throttle_classic_host(monkeypatch) -> None:
    from types import SimpleNamespace
    from preloop.services import alibaba_price_catalog as catalog

    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr("preloop.config.settings.model_price_live_lookup_enabled", True)
    models = {
        "workspace": SimpleNamespace(
            provider_name="qwen",
            model_identifier="qwen-cache-example",
            api_endpoint="https://example.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
            meta_data=None,
        ),
        "classic": SimpleNamespace(
            provider_name="qwen",
            model_identifier="qwen-cache-example",
            api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            meta_data=None,
        ),
    }
    monkeypatch.setattr(
        model_price_catalog,
        "_ai_model_price_lookup_session",
        _yield_lookup_models(models),
    )
    calls = []

    def prepare(model):
        calls.append(model.api_endpoint)
        return catalog.CatalogRefreshStatus.host_mismatch

    monkeypatch.setattr(catalog, "prepare_refresh", prepare)
    monkeypatch.setattr(
        model_price_catalog._LOOKUP_EXECUTOR, "submit", lambda worker: worker()
    )
    assert model_price_catalog.schedule_price_lookup(ai_model_id="workspace")
    assert model_price_catalog.schedule_price_lookup(ai_model_id="classic")
    assert len(calls) == 2
