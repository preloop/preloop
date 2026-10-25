"""Native Model Studio catalog parsing keeps token pairs, unit rates, and both time bands."""

from tests.credential_fixtures import ai_model_with_secret

from typing import Any, Callable
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from preloop.models import models
from preloop.services import alibaba_price_catalog as catalog_mod
from preloop.services.alibaba_price_catalog import (
    CatalogRefreshStatus,
    _download_catalog,
    ingest_native_models,
    live_tariff,
    native_catalog_target,
    parse_native_model,
    refresh_from_model,
    reset_live_state_for_tests,
)
from preloop.services.alibaba_pricing import Tariff


def setup_function() -> None:
    reset_live_state_for_tests()


def test_reviewed_workspace_tariff_wins_until_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(catalog_mod, "_utcnow", lambda: now)
    reviewed = Tariff(input=1, output=2, implicit_read=0.05)
    catalog_mod.install_reviewed_catalogs(
        {"singapore-international": {"qwen3.8-flash": reviewed}},
        verified_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
        revision="review-1",
    )
    assert live_tariff(_sg_workspace()) == reviewed
    ingest_native_models([_token_model("qwen3.8-flash", "3", "4")])
    assert live_tariff(_sg_workspace()) == reviewed
    assert catalog_mod.native_tariff(_sg_workspace()).input == 3
    monkeypatch.setattr(catalog_mod, "_utcnow", lambda: now + timedelta(days=2))
    assert live_tariff(_sg_workspace()) == reviewed
    assert catalog_mod.pricing_snapshot(_sg_workspace())["stale"] is True


def test_reviewed_catalog_validation_is_atomic() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        catalog_mod.install_reviewed_catalogs(
            {
                "singapore-international": {"x": Tariff(input=1, output=2)},
                "beijing": {"y": Tariff(input=1, output=2)},
            },
            verified_at=now,
            expires_at=now + timedelta(days=1),
            revision="bad",
        )
    assert live_tariff(_sg_classic("x")) is None


def test_newer_reviewed_tariff_wins_then_falls_back_to_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(catalog_mod, "_utcnow", lambda: now - timedelta(hours=1))
    ingest_native_models([_token_model("qwen3.8-flash", "3", "4")])
    monkeypatch.setattr(catalog_mod, "_utcnow", lambda: now)
    catalog_mod.install_reviewed_catalogs(
        {"singapore-international": {"qwen3.8-flash": Tariff(input=5, output=6)}},
        verified_at=now,
        expires_at=now + timedelta(minutes=30),
        revision="newer",
    )
    assert live_tariff(_sg_workspace()).input == 5
    assert catalog_mod.tariff_source(_sg_workspace()) == "reviewed-catalog"
    monkeypatch.setattr(catalog_mod, "_utcnow", lambda: now + timedelta(hours=1))
    assert live_tariff(_sg_workspace()).input == 3
    assert catalog_mod.tariff_source(_sg_workspace()) == "native-catalog"


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -1])
def test_reviewed_rejects_nonfinite_and_negative_rates(rate: float) -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        catalog_mod.validate_reviewed_catalogs(
            {
                "singapore-international": {
                    "x": Tariff(input=1, output=2, creation=rate)
                }
            },
            verified_at=now,
            expires_at=now + timedelta(days=1),
            revision="bad",
        )


def test_native_refresh_preparation_does_not_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_mod, "_api_key", lambda model: "private-key")
    download = MagicMock(return_value=([_token_model("qwen3.8-flash")], True))
    monkeypatch.setattr(catalog_mod, "_download_catalog", download)
    prepared = catalog_mod.prepare_refresh(_sg_classic())
    download.assert_not_called()
    assert "private-key" not in repr(prepared)
    assert catalog_mod.refresh_prepared(prepared) is CatalogRefreshStatus.ingested
    download.assert_called_once()


def _sg_classic(model_id: str = "qwen3.8-flash") -> models.AIModel:
    return ai_model_with_secret(
        provider_name="qwen",
        model_identifier=model_id,
        api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="sk-classic",
    )


def _sg_workspace(model_id: str = "qwen3.8-flash") -> models.AIModel:
    return ai_model_with_secret(
        provider_name="qwen",
        model_identifier=model_id,
        api_endpoint="https://ws.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        api_key="sk-workspace",
    )


def _token_model(
    model_id: str,
    input_price: str = "0.15",
    output_price: str = "0.47",
) -> dict[str, Any]:
    return {
        "model": model_id,
        "prices": [
            {
                "range_name": "Default",
                "prices": [
                    {
                        "type": "input_token",
                        "price": input_price,
                        "price_unit": "Per 1M tokens",
                    },
                    {
                        "type": "output_token",
                        "price": output_price,
                        "price_unit": "Per 1M tokens",
                    },
                ],
            }
        ],
    }


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.Client

    def _client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(catalog_mod.httpx, "Client", _client)


def test_singapore_and_workspace_use_documented_native_host() -> None:
    classic = models.AIModel(
        provider_name="qwen",
        model_identifier="qwen3.8-flash",
        api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )
    workspace = models.AIModel(
        provider_name="qwen",
        model_identifier="qwen3.8-flash",
        api_endpoint="https://ws.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    )
    assert native_catalog_target(classic) == (
        "https://dashscope-intl.aliyuncs.com/api/v1/models",
        "international",
    )
    assert native_catalog_target(workspace) == (
        "https://dashscope-intl.aliyuncs.com/api/v1/models",
        "international",
    )


def test_beijing_and_classic_us_have_no_usd_native_target() -> None:
    beijing = models.AIModel(
        provider_name="qwen",
        model_identifier="qwen3.8-flash",
        api_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    classic_us = models.AIModel(
        provider_name="qwen",
        model_identifier="qwen3.8-flash",
        api_endpoint="https://dashscope-us.aliyuncs.com/compatible-mode/v1",
    )
    assert native_catalog_target(beijing) is None
    assert native_catalog_target(classic_us) is None


def test_parse_skips_nan_token_prices() -> None:
    nan_price = parse_native_model(
        {
            "model": "nan-model",
            "prices": [
                {
                    "range_name": "Default",
                    "prices": [
                        {
                            "type": "input_token",
                            "price": "nan",
                            "price_unit": "Per 1M tokens",
                        },
                        {
                            "type": "output_token",
                            "price": "2",
                            "price_unit": "Per 1M tokens",
                        },
                    ],
                }
            ],
        }
    )
    assert nan_price is None


def test_parse_skips_busy_only_time_banded_rows() -> None:
    banded = parse_native_model(
        {
            "model": "busy-model",
            "prices": [
                {
                    "range_name": "Default",
                    "prices": [
                        {
                            "type": "input_token",
                            "price": "1",
                            "price_unit": "Per 1M tokens",
                            "time_band": "busy",
                        },
                        {
                            "type": "output_token",
                            "price": "2",
                            "price_unit": "Per 1M tokens",
                            "time_band": "busy",
                        },
                    ],
                }
            ],
        }
    )
    assert banded is None


def test_parse_keeps_single_image_list_price() -> None:
    image = parse_native_model(
        {
            "model": "qwen-image-max",
            "prices": [
                {
                    "range_name": "Default",
                    "prices": [
                        {
                            "type": "image_number",
                            "price": "0.075",
                            "price_unit": "per image",
                        }
                    ],
                }
            ],
        }
    )
    assert image is not None
    assert image.per_image == 0.075
    assert image.has_token_rates() is False


def test_parse_thinking_embedding_and_omni_chat_defaults() -> None:
    thinking = parse_native_model(
        {
            "model": "qwen3-next-80b-a3b-thinking",
            "prices": [
                {
                    "range_name": "Default",
                    "prices": [
                        {
                            "type": "thinking_input_token",
                            "price": "0.15",
                            "price_unit": "Per 1M tokens",
                            "time_band": "standard",
                        },
                        {
                            "type": "thinking_output_token",
                            "price": "1.2",
                            "price_unit": "Per 1M tokens",
                            "time_band": "standard",
                        },
                    ],
                }
            ],
        }
    )
    assert thinking is not None
    assert thinking.input == 0.15
    assert thinking.output == 1.2
    embedding = parse_native_model(
        {
            "model": "text-embedding-v4",
            "prices": [
                {
                    "range_name": "Default",
                    "prices": [
                        {
                            "type": "embedding_token",
                            "price": "0.07",
                            "price_unit": "Per 1M tokens",
                        }
                    ],
                }
            ],
        }
    )
    assert embedding is not None
    assert embedding.input == 0.07
    assert embedding.output == 0.0
    omni = parse_native_model(
        {
            "model": "qwen3.5-omni-plus",
            "prices": [
                {
                    "range_name": "Default",
                    "prices": [
                        {
                            "type": "omni_audio_input_token",
                            "price": "11",
                            "price_unit": "Per 1M tokens",
                        },
                        {
                            "type": "omni_audio_output_token",
                            "price": "44",
                            "price_unit": "Per 1M tokens",
                        },
                        {
                            "type": "omni_no_audio_input_token",
                            "price": "1.4",
                            "price_unit": "Per 1M tokens",
                        },
                        {
                            "type": "omni_no_audio_output_token",
                            "price": "8.3",
                            "price_unit": "Per 1M tokens",
                        },
                    ],
                }
            ],
        }
    )
    assert omni is not None
    assert omni.input == 1.4
    assert omni.output == 8.3
    assert ("omni_audio_input_token", "Per 1M tokens", 11.0) in omni.extra_rates


def test_parse_keeps_idle_and_busy_token_bands() -> None:
    tariff = parse_native_model(
        {
            "model": "deepseek-v4.1-flash",
            "prices": [
                {
                    "range_name": "Default",
                    "prices": [
                        {
                            "type": "input_token",
                            "price": "0.3",
                            "price_unit": "Per 1M tokens",
                            "time_band": "busy",
                        },
                        {
                            "type": "output_token",
                            "price": "1.2",
                            "price_unit": "Per 1M tokens",
                            "time_band": "busy",
                        },
                        {
                            "type": "input_token_cache_implicit",
                            "price": "0.03",
                            "price_unit": "Per 1M tokens",
                            "time_band": "busy",
                        },
                        {
                            "type": "input_token",
                            "price": "0.15",
                            "price_unit": "Per 1M tokens",
                            "time_band": "idle",
                        },
                        {
                            "type": "output_token",
                            "price": "0.6",
                            "price_unit": "Per 1M tokens",
                            "time_band": "idle",
                        },
                        {
                            "type": "input_token_cache_implicit",
                            "price": "0.015",
                            "price_unit": "Per 1M tokens",
                            "time_band": "idle",
                        },
                    ],
                }
            ],
        }
    )
    assert tariff is not None
    assert tariff.time_bands is not None
    assert tariff.time_bands.busy.input == 0.3
    assert tariff.time_bands.idle.input == 0.15
    assert tariff.time_bands.idle.implicit_read == 0.015


def test_parse_keeps_unbanded_token_tariff_and_tiers() -> None:
    tariff = parse_native_model(
        {
            "model": "qwen3.7-flash",
            "prices": [
                {
                    "range_name": "0<Token<=32k",
                    "prices": [
                        {
                            "type": "input_token",
                            "price": "0.03",
                            "price_unit": "per million tokens",
                        },
                        {
                            "type": "output_token",
                            "price": "0.13",
                            "price_unit": "per million tokens",
                        },
                    ],
                },
                {
                    "range_name": "32k<Input<=256k",
                    "prices": [
                        {
                            "type": "input_token",
                            "price": "0.1",
                            "price_unit": "per million tokens",
                        },
                        {
                            "type": "output_token",
                            "price": "0.4",
                            "price_unit": "per million tokens",
                        },
                    ],
                },
            ],
        }
    )
    assert tariff is not None
    assert tariff.input == 0.03
    assert tariff.output == 0.13
    assert tariff.max_input == 32_000
    assert len(tariff.tiers) == 2
    assert tariff.tiers[1].max_input == 256_000
    assert tariff.tiers[1].input == 0.1


def test_refresh_does_not_send_singapore_workspace_key_to_classic_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download = MagicMock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(catalog_mod, "_download_catalog", download)
    status = refresh_from_model(_sg_workspace())
    assert status is CatalogRefreshStatus.host_mismatch
    download.assert_not_called()
    assert live_tariff(_sg_workspace()) is None


def test_complete_download_replaces_removed_skus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingest_native_models(
        [_token_model("old-sku", "1", "2")],
        region="singapore-international",
    )
    monkeypatch.setattr(catalog_mod, "_api_key", lambda ai_model: "sk-classic")

    def _complete_download(
        url: str, api_key: str, service_site: str
    ) -> tuple[list[dict[str, Any]], bool]:
        return [_token_model("new-sku", "3", "4")], True

    monkeypatch.setattr(catalog_mod, "_download_catalog", _complete_download)
    status = refresh_from_model(_sg_classic("new-sku"))
    assert status is CatalogRefreshStatus.ingested
    assert live_tariff(_sg_classic("old-sku")) is None
    assert live_tariff(_sg_classic("new-sku")) == Tariff(input=3.0, output=4.0)


def test_partial_discovery_merge_keeps_existing_skus() -> None:
    ingest_native_models(
        [_token_model("keep-sku", "1", "2")],
        region="singapore-international",
    )
    ingest_native_models(
        [_token_model("added-sku", "3", "4")],
        region="singapore-international",
    )
    assert live_tariff(_sg_classic("keep-sku")) is not None
    assert live_tariff(_sg_classic("added-sku")) is not None


def test_incomplete_download_does_not_drop_existing_skus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingest_native_models(
        [_token_model("keep-sku", "1", "2")],
        region="singapore-international",
    )
    monkeypatch.setattr(catalog_mod, "_api_key", lambda ai_model: "sk-classic")

    def _incomplete_download(
        url: str, api_key: str, service_site: str
    ) -> tuple[list[dict[str, Any]], bool]:
        return [], False

    monkeypatch.setattr(catalog_mod, "_download_catalog", _incomplete_download)
    status = refresh_from_model(_sg_classic("keep-sku"))
    assert status is CatalogRefreshStatus.unreachable
    assert live_tariff(_sg_classic("keep-sku")) is not None


def test_download_catalog_single_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: list[int] = []
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page_no"]))
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "output": {"models": [_token_model("qwen3.8-flash")], "total": 1},
            },
        )

    _patch_client(monkeypatch, handler)
    entries, complete = _download_catalog(
        "https://dashscope-intl.aliyuncs.com/api/v1/models",
        "sk-classic",
        "international",
    )
    assert pages == [1]
    assert complete is True
    assert [row["model"] for row in entries] == ["qwen3.8-flash"]
    assert "capabilities" not in captured[0].url.params
    assert captured[0].url.params["service_site"] == "international"


def test_download_catalog_stops_on_repeated_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_mod, "_PAGE_SIZE", 2)
    pages: list[int] = []
    repeated = [_token_model("a"), _token_model("b")]

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page_no"]))
        return httpx.Response(
            200,
            json={
                "success": True,
                "output": {"models": repeated, "total": 10},
            },
        )

    _patch_client(monkeypatch, handler)
    entries, complete = _download_catalog(
        "https://dashscope-intl.aliyuncs.com/api/v1/models",
        "sk-classic",
        "international",
    )
    assert pages == [1, 2]
    assert complete is False
    assert [row["model"] for row in entries] == ["a", "b"]


def test_download_catalog_success_false_yields_no_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "output": {"models": [_token_model("x")]}},
        )

    _patch_client(monkeypatch, handler)
    entries, complete = _download_catalog(
        "https://dashscope-intl.aliyuncs.com/api/v1/models",
        "sk-classic",
        "international",
    )
    assert entries == []
    assert complete is False


def test_native_pagination_has_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(catalog_mod.time, "monotonic", lambda: now[0])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 5.0
        now[0] = 6.0
        return httpx.Response(
            200,
            json={
                "success": True,
                "output": {"models": [_token_model("example")], "total": 200},
            },
        )

    _patch_client(monkeypatch, handler)
    entries, complete = _download_catalog(
        "https://dashscope-intl.aliyuncs.com/api/v1/models",
        "test-key",
        "international",
        max_duration_seconds=5,
    )
    assert len(entries) == 1
    assert complete is False
