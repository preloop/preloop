"""Native Model Studio catalog parsing is token-only and skips time bands."""

from preloop.models import models
from preloop.services.alibaba_price_catalog import (
    native_catalog_target,
    parse_native_model,
    reset_live_state_for_tests,
)


def setup_function() -> None:
    reset_live_state_for_tests()


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
