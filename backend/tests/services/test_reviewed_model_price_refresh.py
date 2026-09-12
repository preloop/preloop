"""Reviewed feeds must fail without partially changing current estimates."""

import asyncio
import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import litellm
import pytest

from preloop.services.reviewed_model_price_refresh import (
    ReviewedPriceRefresher,
    start_reviewed_price_refresh,
    validate_feed,
)


@pytest.fixture
def payload() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "currency": "USD",
        "revision": "review-1",
        "published_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
        "models": {
            "example/model": {
                "policy": "flat_per_token",
                "source_url": "https://example.com/pricing",
                "verified_at": (now - timedelta(minutes=2)).isoformat(),
                "effective_from": (now - timedelta(days=1)).isoformat(),
                "prices": {
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000002,
                },
            }
        },
    }


@pytest.fixture
def refresher(monkeypatch: pytest.MonkeyPatch) -> ReviewedPriceRefresher:
    monkeypatch.setattr(
        litellm,
        "model_cost",
        {
            "example/model": {
                "litellm_provider": "openai",
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000004,
                "cache_read_input_token_cost": 0.000001,
            },
            "untouched": {"input_cost_per_token": 7},
        },
    )
    return ReviewedPriceRefresher(
        url="https://example.com/feed.json",
        allowed_models=["example/model"],
        interval_seconds=60,
    )


def test_applies_complete_snapshot_without_mutating_previous_map(
    refresher: ReviewedPriceRefresher, payload: dict
) -> None:
    previous = litellm.model_cost
    assert refresher.apply(payload) == 1
    assert previous["example/model"]["input_cost_per_token"] == 0.000003
    assert litellm.model_cost["example/model"]["input_cost_per_token"] == 0.000001
    assert "cache_read_input_token_cost" not in litellm.model_cost["example/model"]
    assert litellm.model_cost["untouched"] == previous["untouched"]
    assert (
        litellm.model_cost["example/model"]["preloop_price_provenance"]["revision"]
        == "review-1"
    )
    assert refresher.apply(payload) == 0


@pytest.mark.parametrize(
    "price", [-1, -(10**400), 10**400, float("nan"), float("inf"), True, "0.2"]
)
def test_bad_price_rejected_before_any_mutation(
    refresher: ReviewedPriceRefresher, payload: dict, price: object
) -> None:
    previous = litellm.model_cost
    payload["models"]["example/model"]["prices"]["input_cost_per_token"] = price
    with pytest.raises(ValueError):
        refresher.apply(payload)
    assert litellm.model_cost is previous


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policy", "time_of_day"),
        ("source_url", "http://example.com/pricing"),
        ("verified_at", "2000-01-01T00:00:00Z"),
        ("effective_from", "2099-01-01T00:00:00Z"),
        ("verified_at", "2026-01-01T00:00:00"),
    ],
)
def test_invalid_evidence_rejected(payload: dict, field: str, value: str) -> None:
    payload["models"]["example/model"][field] = value
    with pytest.raises(ValueError):
        validate_feed(payload)


def test_expired_feed_rejected(payload: dict) -> None:
    payload["expires_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        validate_feed(payload)


def test_model_scope_and_unsupported_existing_tiers_fail_atomically(
    refresher: ReviewedPriceRefresher, payload: dict
) -> None:
    previous = litellm.model_cost
    payload["models"]["unknown"] = copy.deepcopy(payload["models"]["example/model"])
    with pytest.raises(ValueError, match="allowlist"):
        refresher.apply(payload)
    assert litellm.model_cost is previous
    refresher.allowed_models = frozenset(payload["models"])
    with pytest.raises(ValueError, match="existing catalog"):
        refresher.apply(payload)
    assert litellm.model_cost is previous
    del payload["models"]["unknown"]
    previous["example/model"]["input_cost_per_token_above_200k_tokens"] = 0.001
    with pytest.raises(ValueError, match="dedicated support"):
        refresher.apply(payload)
    assert litellm.model_cost is previous


def test_same_timestamp_cannot_silently_mutate_revision(
    refresher: ReviewedPriceRefresher, payload: dict
) -> None:
    refresher.apply(payload)
    previous = litellm.model_cost
    payload["models"]["example/model"]["prices"]["input_cost_per_token"] = 0.005
    with pytest.raises(ValueError, match="older or mutated"):
        refresher.apply(payload)
    assert litellm.model_cost is previous


@pytest.mark.asyncio
async def test_download_uses_feed_and_rejects_redirects(
    refresher: ReviewedPriceRefresher, payload: dict
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await refresher.refresh(client) == 1
    previous = litellm.model_cost
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://other.test"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await refresher.refresh(client)
    assert litellm.model_cost is previous


@pytest.mark.asyncio
async def test_poll_failure_retains_prices_and_stop_cancels_task(
    refresher: ReviewedPriceRefresher, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = litellm.model_cost
    poll = AsyncMock(side_effect=ValueError("invalid feed"))
    monkeypatch.setattr(refresher, "refresh", poll)
    refresher.start()
    task = refresher.task
    refresher.start()
    assert refresher.task is task
    for _ in range(10):
        await asyncio.sleep(0)
        if poll.await_count:
            break
    await refresher.stop()
    assert poll.await_count == 1
    assert task is not None and task.cancelled()
    assert refresher.task is None
    assert litellm.model_cost is previous


def test_disabled_setting_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from preloop.config import settings

    monkeypatch.setattr(settings, "model_price_refresh_url", "")
    assert start_reviewed_price_refresh() is None


def test_builder_exports_catalog_prices_and_requires_evidence(payload: dict) -> None:
    script = (
        Path(__file__).resolve().parents[3] / "scripts/build_reviewed_model_prices.py"
    )
    spec = importlib.util.spec_from_file_location("price_feed_builder", script)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    catalog = {"example/model": payload["models"]["example/model"].pop("prices")}
    built = builder.build_feed(catalog, payload)
    assert built["models"]["example/model"]["prices"] == catalog["example/model"]
    assert validate_feed(json.loads(json.dumps(built)))
    payload["models"]["example/model"]["prices"] = catalog["example/model"]
    with pytest.raises(ValueError, match="must come from"):
        builder.build_feed(catalog, payload)


def test_warmed_litellm_prices_change_and_same_feed_repairs_mutation(
    refresher: ReviewedPriceRefresher, payload: dict
) -> None:
    from litellm.utils import _invalidate_model_cost_lowercase_map

    litellm.model_cost["example/model"].update({"mode": "chat", "max_tokens": 4096})
    _invalidate_model_cost_lowercase_map()
    before = litellm.cost_per_token(
        model="example/model",
        prompt_tokens=1000,
        completion_tokens=1000,
        custom_llm_provider="openai",
    )
    refresher.apply(payload)
    after = litellm.cost_per_token(
        model="example/model",
        prompt_tokens=1000,
        completion_tokens=1000,
        custom_llm_provider="openai",
    )
    assert before == pytest.approx((0.003, 0.004))
    assert after == pytest.approx((0.001, 0.002))
    litellm.model_cost["example/model"]["input_cost_per_token"] = 0.4
    litellm.model_cost["newly-discovered"] = {"input_cost_per_token": 0.5}
    assert refresher.apply(payload) == 1
    assert litellm.model_cost["example/model"]["input_cost_per_token"] == 0.000001
    assert "newly-discovered" in litellm.model_cost


def test_live_lookup_cannot_overwrite_reviewed_entry(
    refresher: ReviewedPriceRefresher, payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services import model_price_catalog

    refresher.apply(payload)
    model_price_catalog.reset_lookup_state_for_tests()
    monkeypatch.setattr(
        model_price_catalog,
        "_fetch_remote_price_map",
        lambda: {"example/model": {"input_cost_per_token": 0.8}},
    )
    assert (
        model_price_catalog.lookup_model_price_now(["example/model"]) == "example/model"
    )
    assert litellm.model_cost["example/model"]["input_cost_per_token"] == 0.000001


@pytest.fixture
def native_payload(refresher: ReviewedPriceRefresher, payload: dict) -> dict:
    entry = payload["models"]["example/model"]
    entry.pop("prices")
    entry["policy"] = "deepseek_utc_bands"
    entry["price_policy"] = {
        "kind": "deepseek_utc_bands",
        "effective_from": entry["effective_from"],
        "peak": {
            "input_per_1m": 0.3,
            "output_per_1m": 1.2,
            "cached_input_per_1m": 0.006,
        },
        "off_peak": {
            "input_per_1m": 0.15,
            "output_per_1m": 0.6,
            "cached_input_per_1m": 0.003,
        },
        "peak_hours_utc": [[1, 4], [6, 10]],
        "peak_weekdays": [0, 1, 2, 3, 4],
        "public_holidays": "unspecified",
    }
    litellm.model_cost["example/model"]["litellm_provider"] = "deepseek"
    litellm.model_cost["deepseek/deepseek-flash"] = litellm.model_cost.pop(
        "example/model"
    )
    payload["models"]["deepseek/deepseek-flash"] = payload["models"].pop(
        "example/model"
    )
    refresher.allowed_models = frozenset(["deepseek/deepseek-flash"])
    return payload


def test_native_policy_updates_without_flattening(
    refresher: ReviewedPriceRefresher, native_payload: dict
) -> None:
    assert refresher.apply(native_payload) == 1
    updated = litellm.model_cost["deepseek/deepseek-flash"]
    assert updated["input_cost_per_token"] == 0.000003
    assert updated["preloop_price_policy"]["peak"]["input_per_1m"] == 0.3


def test_initial_catalog_policy_without_runtime_provenance_can_be_verified(
    refresher: ReviewedPriceRefresher, native_payload: dict
) -> None:
    key = "deepseek/deepseek-flash"
    litellm.model_cost[key]["preloop_price_policy"] = copy.deepcopy(
        native_payload["models"][key]["price_policy"]
    )
    assert refresher.apply(native_payload) == 1
    assert litellm.model_cost[key]["preloop_price_provenance"]["revision"] == "review-1"


def test_future_revision_preserves_previous_tariff_and_restart_history(
    refresher: ReviewedPriceRefresher, native_payload: dict
) -> None:
    key = "deepseek/deepseek-flash"
    refresher.apply(native_payload)
    previous = copy.deepcopy(litellm.model_cost[key])
    revised = copy.deepcopy(native_payload)
    now = datetime.now(timezone.utc)
    revised["published_at"] = now.isoformat()
    revised["revision"] = "review-2"
    entry = revised["models"][key]
    entry["effective_from"] = (now + timedelta(days=1)).isoformat()
    entry["price_policy"]["effective_from"] = entry["effective_from"]
    entry["price_policy"]["peak"]["input_per_1m"] = 0.5
    entry["price_policy_history"] = [
        {
            "policy": previous["preloop_price_policy"],
            "provenance": previous["preloop_price_provenance"],
        }
    ]
    assert refresher.apply(revised) == 1
    assert (
        litellm.model_cost[key]["preloop_price_policy_history"]
        == entry["price_policy_history"]
    )
    # A fresh process can reconstruct the same history from the published feed.
    litellm.model_cost[key] = {"litellm_provider": "deepseek"}
    restarted = ReviewedPriceRefresher(
        url=refresher.url, allowed_models=[key], interval_seconds=60
    )
    assert restarted.apply(revised) == 1
    assert (
        litellm.model_cost[key]["preloop_price_policy_history"]
        == entry["price_policy_history"]
    )


def test_equivalent_timezone_cannot_mutate_reviewed_effective_instant(
    refresher: ReviewedPriceRefresher, native_payload: dict
) -> None:
    refresher.apply(native_payload)
    revised = copy.deepcopy(native_payload)
    revised["published_at"] = datetime.now(timezone.utc).isoformat()
    entry = revised["models"]["deepseek/deepseek-flash"]
    equivalent = (
        datetime.fromisoformat(entry["effective_from"])
        .astimezone(timezone(timedelta(hours=2)))
        .isoformat()
    )
    entry["effective_from"] = equivalent
    entry["price_policy"]["effective_from"] = equivalent
    entry["price_policy"]["peak"]["input_per_1m"] = 9.0
    with pytest.raises(ValueError, match="Cannot mutate"):
        refresher.apply(revised)


@pytest.mark.parametrize(
    "field,value",
    [
        ("peak_hours_utc", [[2, 4], [6, 10]]),
        ("peak_weekdays", [0, 1, 2, 3, 4, 5]),
        ("effective_from", "2026-09-01T00:00:00Z"),
    ],
)
def test_unsupported_dynamic_shapes_are_not_accepted(
    native_payload: dict, field: str, value: object
) -> None:
    native_payload["models"]["deepseek/deepseek-flash"]["price_policy"][field] = value
    with pytest.raises(ValueError):
        validate_feed(native_payload)


def test_native_flat_and_marketplace_dynamic_policies_rejected(
    refresher: ReviewedPriceRefresher, native_payload: dict, payload: dict
) -> None:
    key = "deepseek/deepseek-flash"
    native = copy.deepcopy(native_payload)
    native["models"][key] = {
        **native["models"][key],
        "policy": "flat_per_token",
        "prices": {"input_cost_per_token": 0.1, "output_cost_per_token": 0.2},
    }
    native["models"][key].pop("price_policy")
    with pytest.raises(ValueError, match="native policy"):
        refresher.apply(native)
    litellm.model_cost[key]["litellm_provider"] = "openrouter"
    with pytest.raises(ValueError, match="direct model"):
        refresher.apply(native_payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["api", "gateway", "all"])
async def test_actual_app_lifespan_owns_refresh_task(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from preloop.api import app as app_module
    from preloop.services import reviewed_model_price_refresh as service

    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("INIT_DB", "false")
    monkeypatch.setenv("INIT_TEST_DATA", "false")
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", role)
    refresher = MagicMock(stop=AsyncMock())
    start = MagicMock(return_value=refresher)
    monkeypatch.setattr(service, "start_reviewed_price_refresh", start)
    async with app_module.lifespan(FastAPI()):
        start.assert_called_once()
        refresher.stop.assert_not_awaited()
    refresher.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_actual_worker_starts_once_and_stops_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import reviewed_model_price_refresh as service
    from preloop.sync.services.nats_worker import PreloopSyncNatsWorker

    worker = PreloopSyncNatsWorker(nats_url="nats://example.com", queue_name="test")
    worker.nc = MagicMock(is_connected=True, is_closed=True)
    monkeypatch.setattr(worker, "_subjects_to_subscribe", lambda: [])
    monkeypatch.setattr(worker, "_remove_conflicting_wildcard_consumer", AsyncMock())
    monkeypatch.setattr(worker, "begin_drain", AsyncMock())
    refresher = MagicMock(stop=AsyncMock())
    start = MagicMock(return_value=refresher)
    monkeypatch.setattr(service, "start_reviewed_price_refresh", start)
    with pytest.raises(RuntimeError, match="no subjects"):
        await worker.start_listening()
    with pytest.raises(RuntimeError, match="no subjects"):
        await worker.start_listening()
    start.assert_called_once()
    await worker.stop()
    refresher.stop.assert_awaited_once()
