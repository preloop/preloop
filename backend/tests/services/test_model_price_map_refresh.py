"""Runtime refresh of the upstream model price map (issue #801).

The vendored snapshot only changes on deploy. These tests pin the runtime
half: a gateway refreshes the upstream map on startup and every TTL, merges
it over the snapshot, logs each fetch and each negative-cache insertion, and
reports the last fetch on the health endpoint. The upstream is a real local
HTTP server so the download path (status codes, retries, body parsing) runs
exactly as it does against GitHub.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator
from unittest.mock import patch

import litellm
import pytest

from preloop.models.models.ai_model import AIModel
from preloop.services import model_price_catalog
from preloop.services.model_pricing import estimate_ai_model_usage_cost_detailed

CATALOG_LOGGER = "preloop.services.model_price_catalog"


class _Upstream:
    """A local stand-in for the published litellm price map."""

    def __init__(self) -> None:
        self.status = 200
        self.price_map: Dict[str, Any] = {}
        self.hits = 0
        self._lock = threading.Lock()
        upstream = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                with upstream._lock:
                    upstream.hits += 1
                    status, body = upstream.status, json.dumps(upstream.price_map)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if status == 200:
                    self.wfile.write(body.encode("utf-8"))

            def log_message(self, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}/model_prices_and_context_window.json"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _FakeClock:
    """Monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Upstream]:
    server = _Upstream()
    monkeypatch.setattr(model_price_catalog, "REMOTE_PRICE_MAP_URL", server.url)
    monkeypatch.setattr(model_price_catalog, "REMOTE_PRICE_MAP_SHA256", "")
    model_price_catalog.reset_lookup_state_for_tests()
    try:
        yield server
    finally:
        server.close()
        model_price_catalog.reset_lookup_state_for_tests()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(model_price_catalog, "_monotonic", fake)
    return fake


def _price_entry(input_per_m: float, output_per_m: float) -> Dict[str, Any]:
    return {
        "litellm_provider": "gemini",
        "mode": "chat",
        "input_cost_per_token": input_per_m / 1_000_000,
        "output_cost_per_token": output_per_m / 1_000_000,
    }


def _catalog_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == CATALOG_LOGGER and record.levelno == logging.WARNING
    ]


def test_fresh_gateway_prices_snapshot_gap_on_first_request(
    upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the 2026-09-18 miss (``google/gemini-3.8-flash``).

    The snapshot lacked the model and upstream priced it as
    ``gemini/gemini-3.8-flash``. A gateway that refreshes on startup must
    price the first request from the upstream map, with no unpriced row, no
    on-miss lookup and no negative-cache entry.
    """
    for key in ("gemini/gemini-3.8-flash", "gemini-3.8-flash"):
        if key in litellm.model_cost:
            monkeypatch.delitem(litellm.model_cost, key)
    upstream.price_map = {"gemini/gemini-3.8-flash": _price_entry(0.75, 3.75)}
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr("preloop.config.settings.model_price_live_lookup_enabled", True)
    ai_model = AIModel(
        provider_name="google", model_identifier="gemini-3.8-flash", meta_data={}
    )

    async def _start_gateway() -> None:
        refresher = model_price_catalog.start_price_map_refresh()
        assert refresher is not None
        try:
            await asyncio.wait_for(refresher.first_cycle_done.wait(), timeout=10)
        finally:
            await refresher.stop()

    asyncio.run(_start_gateway())

    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model, prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000
    )
    assert estimate.cost == pytest.approx(0.75)
    assert estimate.source != "unpriced"
    assert model_price_catalog._negative_cache == {}
    assert upstream.hits == 1


def test_upstream_5xx_serves_snapshot_warns_once_and_retries_after_backoff(
    upstream: _Upstream,
    clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 5xx upstream keeps the snapshot, logs one WARNING, and backs off."""
    monkeypatch.setattr(model_price_catalog, "_REMOTE_FETCH_RETRIES", 2)
    monkeypatch.setattr(model_price_catalog, "_REMOTE_FAILURE_BACKOFF_SECONDS", 900)
    upstream.status = 503
    snapshot_key = "gpt-4o"
    snapshot_price = litellm.model_cost[snapshot_key]["input_cost_per_token"]
    caplog.set_level(logging.DEBUG, logger=CATALOG_LOGGER)

    delay = model_price_catalog.refresh_price_map_once()

    assert delay == pytest.approx(900)
    assert upstream.hits == 3  # one fetch, three attempts
    warnings = _catalog_warnings(caplog)
    assert len(warnings) == 1
    assert "outcome=failed" in warnings[0].getMessage()
    assert litellm.model_cost[snapshot_key]["input_cost_per_token"] == snapshot_price
    assert model_price_catalog.price_map_status()["last_outcome"] == "failed"

    # Inside the backoff window nothing (eager cycle or on-miss) re-downloads.
    clock.advance(899)
    caplog.clear()
    assert model_price_catalog.refresh_price_map_once() == pytest.approx(1)
    assert model_price_catalog.lookup_model_price_now(["gemini/not-there"]) is None
    assert upstream.hits == 3
    assert _catalog_warnings(caplog) == []

    # Once the backoff elapses the next cycle fetches again and succeeds.
    clock.advance(1)
    upstream.status = 200
    upstream.price_map = {"gemini/backoff-recovered-801": _price_entry(1.0, 2.0)}
    delay = model_price_catalog.refresh_price_map_once()
    assert upstream.hits == 4
    assert delay == pytest.approx(model_price_catalog._REMOTE_TTL_SECONDS)
    assert "gemini/backoff-recovered-801" in litellm.model_cost
    assert model_price_catalog.price_map_status()["last_outcome"] == "ok"


def test_successful_fetch_logs_one_info_line_with_count_and_sha(
    upstream: _Upstream,
    clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each fetch logs one INFO line: outcome, entry count, pinned sha256."""
    import hashlib

    upstream.price_map = {"gemini/info-line-801": _price_entry(1.0, 2.0)}
    body = json.dumps(upstream.price_map).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(model_price_catalog, "REMOTE_PRICE_MAP_SHA256", digest)
    caplog.set_level(logging.INFO, logger=CATALOG_LOGGER)

    model_price_catalog.refresh_price_map_once()

    fetch_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == CATALOG_LOGGER
        and "Model price map fetch" in record.getMessage()
    ]
    assert len(fetch_lines) == 1
    line = fetch_lines[0]
    assert "outcome=ok" in line
    assert "entries=1" in line
    assert f"sha256={digest}" in line
    status = model_price_catalog.price_map_status()
    assert status["entry_count"] == 1
    assert status["last_success_at"] is not None


def test_negative_cache_logs_alias_once_per_window(
    clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A negative-cache insertion is logged once per TTL window per alias."""
    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setattr(model_price_catalog, "_fetch_remote_price_map", lambda: {})
    caplog.set_level(logging.WARNING, logger=CATALOG_LOGGER)
    alias = "gemini/unknown-window-801"
    token = model_price_catalog._model_log_token(alias)

    def _alias_warnings() -> int:
        return sum(1 for r in _catalog_warnings(caplog) if token in r.getMessage())

    for _ in range(3):
        assert model_price_catalog.lookup_model_price_now([alias]) is None
        clock.advance(3600)
    assert _alias_warnings() == 1

    # Re-inserting an alias that is still cached (overlapping lookups) is
    # not a new window and must not log again.
    with model_price_catalog._lookup_lock:
        model_price_catalog._remember_negative_lookup(alias, reason="not_in_map")
    assert _alias_warnings() == 1

    clock.advance(model_price_catalog._NEGATIVE_TTL_SECONDS)
    assert model_price_catalog.lookup_model_price_now([alias]) is None
    assert _alias_warnings() == 2
    message = [r for r in _catalog_warnings(caplog) if token in r.getMessage()][0]
    # The namespace is public; the rest of the name stays hashed.
    assert "namespace=gemini" in message.getMessage()
    assert "unknown-window-801" not in message.getMessage()


def test_merge_respects_reviewed_and_first_party_prices(
    upstream: _Upstream, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream wins over the snapshot except where Preloop reviewed a price."""
    reviewed_key = "gemini/reviewed-801"
    overlay_key = "zai/overlay-801"
    plain_key = "gemini/plain-801"
    monkeypatch.setitem(
        litellm.model_cost,
        reviewed_key,
        {
            **_price_entry(5.0, 5.0),
            "preloop_price_provenance": {"revision": "r1"},
        },
    )
    monkeypatch.setitem(
        litellm.model_cost,
        overlay_key,
        {**_price_entry(1.0, 1.0), "litellm_provider": "zai"},
    )
    monkeypatch.setitem(litellm.model_cost, plain_key, _price_entry(1.0, 1.0))
    monkeypatch.setattr(
        model_price_catalog, "_vendored_overlay_keys", frozenset({overlay_key})
    )
    tier_only = {
        "litellm_provider": "gemini",
        "mode": "chat",
        "input_cost_per_token": None,
        "tiered_pricing": [
            {"range": [0, 200000], "input_cost_per_token": 2e-06},
            {"range": [200000, 1000000], "input_cost_per_token": 4e-06},
        ],
        "output_cost_per_token": 8e-06,
    }
    upstream.price_map = {
        reviewed_key: _price_entry(9.0, 9.0),
        overlay_key: {**_price_entry(9.0, 9.0), "litellm_provider": "zai"},
        plain_key: {**_price_entry(2.0, 3.0), "cache_read_input_token_cost": None},
        "gemini/tier-only-801": tier_only,
        "gemini/priceless-801": {"litellm_provider": "gemini", "mode": "chat"},
        "sample_spec": {"input_cost_per_token": 0.0},
    }

    model_price_catalog.refresh_price_map_once()

    assert litellm.model_cost[reviewed_key]["input_cost_per_token"] == 5e-06
    assert litellm.model_cost[overlay_key]["input_cost_per_token"] == 1e-06
    assert litellm.model_cost[plain_key]["input_cost_per_token"] == 2e-06
    assert litellm.model_cost[plain_key]["output_cost_per_token"] == 3e-06
    assert litellm.model_cost["gemini/tier-only-801"]["input_cost_per_token"] == 2e-06
    assert "gemini/priceless-801" not in litellm.model_cost
    status = model_price_catalog.price_map_status()
    assert status["entry_count"] == 6
    assert status["registered_count"] == 2

    # An unchanged map registers nothing on the next cycle.
    clock.advance(model_price_catalog._REMOTE_TTL_SECONDS)
    with patch.object(litellm, "register_model") as register:
        model_price_catalog.refresh_price_map_once()
    register.assert_not_called()
    for key in ("gemini/tier-only-801",):
        litellm.model_cost.pop(key, None)


def test_refresh_disabled_under_testing_and_when_lookups_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No background fetches in tests or when live lookups are switched off."""
    assert model_price_catalog.start_price_map_refresh() is None
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr(
        "preloop.config.settings.model_price_live_lookup_enabled", False
    )
    assert model_price_catalog.start_price_map_refresh() is None


def test_health_reports_last_price_map_fetch(
    upstream: _Upstream, clock: _FakeClock
) -> None:
    """The readiness payload exposes the last fetch time and entry count."""
    from preloop.api.endpoints import health

    upstream.price_map = {"gemini/health-801": _price_entry(1.0, 2.0)}
    model_price_catalog.refresh_price_map_once()

    with (
        patch.object(health, "get_health_engine"),
        patch.dict("os.environ", {"PRELOOP_SERVICE_ROLE": "gateway"}),
    ):
        payload = health.health_check()

    price_map = payload["model_price_map"]
    assert price_map["last_outcome"] == "ok"
    assert price_map["entry_count"] == 1
    assert price_map["last_success_at"] is not None
    assert "url" not in json.dumps(price_map).lower()
    litellm.model_cost.pop("gemini/health-801", None)


def _load_update_model_prices() -> Any:
    """Load scripts/update_model_prices.py by path."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "update_model_prices.py"
    spec = importlib.util.spec_from_file_location("update_model_prices_801", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "entry",
    [
        {
            "input_cost_per_token": None,
            "tiered_pricing": [
                {"range": [200000, 1000000], "input_cost_per_token": 4e-06},
                {
                    "range": [0, 200000],
                    "input_cost_per_token": 2e-06,
                    "output_cost_per_token": 6e-06,
                },
            ],
        },
        {
            "input_cost_per_token": 1e-06,
            "tiered_pricing": [
                {"range": [0, 32000], "input_cost_per_token": 3e-06},
                {"range": [32000, 128000], "output_cost_per_token": 9e-06},
            ],
        },
        {"input_cost_per_token": 1e-06, "tiered_pricing": ["junk", {"range": "x"}]},
        {"input_cost_per_token": 1e-06},
    ],
)
def test_runtime_tier_flattening_matches_the_snapshot_script(
    entry: Dict[str, Any],
) -> None:
    """The runtime merge and the snapshot script price tier rows identically."""
    script = _load_update_model_prices()
    assert model_price_catalog._flatten_tiered_prices(
        entry
    ) == script._flatten_tiered_pricing(entry)
