"""Native Model Studio catalog parsing is token-only and skips time bands."""

from typing import Any, Callable
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


def _sg_classic(model_id: str = "qwen3.8-flash") -> models.AIModel:
    return models.AIModel(
        provider_name="qwen",
        model_identifier=model_id,
        api_endpoint="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="sk-classic",
    )


def _sg_workspace(model_id: str = "qwen3.8-flash") -> models.AIModel:
    return models.AIModel(
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


def test_parse_skips_image_and_time_banded_rows() -> None:
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
    assert image is None
    assert banded is None


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

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(int(request.url.params["page_no"]))
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
