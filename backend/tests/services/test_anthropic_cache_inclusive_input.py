"""Anthropic input is stored cache inclusive, like every other provider (#1955).

Anthropic reports ``input_tokens`` without the tokens read from or written to
the prompt cache; OpenAI-shaped usage includes them. The ledger stores one
shape (cache inclusive) so the uncached remainder ``input - read - write`` is
right for every row, while the raw provider usage stays in
``meta_data.usage_details`` for audit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from preloop.models.crud import crud_ai_model
from preloop.models.crud.api_usage import cache_split_columns, cache_split_from_row
from preloop.models.models.api_usage import ApiUsage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_pricing import estimate_ai_model_usage_cost_detailed
from preloop.services.openai_gateway import OpenAIGatewayService

RAW_ANTHROPIC_USAGE = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 10_000,
    "cache_creation_input_tokens": 2_000,
}

# Per 1k: input 3.0, cache read 0.3, cache write 3.75, output 15.0.
PRICING = {
    "input_price_per_1k": 3.0,
    "cache_read_input_price_per_1k": 0.3,
    "cache_creation_input_price_per_1k": 3.75,
    "output_price_per_1k": 15.0,
}
EXPECTED_COST = round(
    100 / 1000 * 3.0 + 10_000 / 1000 * 0.3 + 2_000 / 1000 * 3.75 + 50 / 1000 * 15.0,
    6,
)

normalize = OpenAIGatewayService._normalize_usage


def test_normalize_usage_makes_raw_anthropic_input_cache_inclusive() -> None:
    usage = normalize(
        dict(RAW_ANTHROPIC_USAGE),
        prompt_key="input_tokens",
        completion_key="output_tokens",
        output_names=("output_tokens", "completion_tokens"),
    )
    assert usage["prompt_tokens"] == 12_100
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 12_150
    assert usage["cache_read_input_tokens"] == 10_000
    assert usage["cache_creation_input_tokens"] == 2_000


@pytest.mark.parametrize(
    "usage",
    [
        # litellm's OpenAI-shaped usage already counts the cache in
        # prompt_tokens; adding it again would double count.
        {
            "prompt_tokens": 12_100,
            "completion_tokens": 50,
            "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 2_000,
        },
        # Responses-shaped usage is cache inclusive and nests the split.
        {
            "input_tokens": 12_100,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 10_000},
        },
    ],
)
def test_normalize_usage_leaves_cache_inclusive_shapes_alone(usage) -> None:
    normalized = normalize(
        usage,
        prompt_key="prompt_tokens",
        completion_key="completion_tokens",
        output_names=("completion_tokens", "output_tokens"),
    )
    assert normalized["prompt_tokens"] == 12_100


def test_cache_inclusive_input_keeps_cache_prices(db_session, test_user) -> None:
    """Cached input is priced at the cache rate, not the full input rate."""
    model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude cache pricing",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "provider-secret",
            "meta_data": {"pricing": PRICING},
        },
        account_id=test_user.account_id,
    )
    estimate = estimate_ai_model_usage_cost_detailed(
        model,
        prompt_tokens=12_100,
        completion_tokens=50,
        total_tokens=12_150,
        usage_details=dict(RAW_ANTHROPIC_USAGE),
    )
    assert estimate.cost == pytest.approx(EXPECTED_COST)


def test_catalog_price_of_cache_inclusive_input_uses_cache_rates(
    db_session, test_user
) -> None:
    """The catalog path prices from the raw usage, so the cache rate holds."""
    model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude catalog pricing",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "provider-secret",
            "meta_data": {},
        },
        account_id=test_user.account_id,
    )
    estimate = estimate_ai_model_usage_cost_detailed(
        model,
        prompt_tokens=12_100,
        completion_tokens=50,
        total_tokens=12_150,
        usage_details=dict(RAW_ANTHROPIC_USAGE),
    )
    # Catalog: input 3e-6, cache read 3e-7, cache write 3.75e-6, output 1.5e-5.
    assert estimate.source == "catalog"
    assert estimate.cost == pytest.approx(0.01155)


def test_passthrough_records_cache_inclusive_input(db_session, test_user) -> None:
    """A raw Anthropic response lands in the ledger cache inclusive."""
    model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude passthrough",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "anthropic/claude-sonnet-4-5",
                    "provider_adapter": "preloop",
                },
                "pricing": PRICING,
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    upstream = {
        "id": "msg_cache",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "OK"}],
        "stop_reason": "end_turn",
        "usage": dict(RAW_ANTHROPIC_USAGE),
    }
    with (
        patch.object(service, "_resolve_requested_model", return_value=model),
        patch.object(
            service, "_anthropic_oauth_passthrough_token", return_value="oauth"
        ),
        patch.object(
            service, "_anthropic_oauth_passthrough_complete", return_value=upstream
        ),
    ):
        payload = service.create_message(
            {
                "model": "anthropic/claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 64,
            },
            anthropic_version="2023-06-01",
        )

    # The client still gets the provider's own Anthropic usage.
    assert payload["usage"]["input_tokens"] == 100

    row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.account_id == test_user.account_id)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .one()
    )
    assert row.prompt_tokens == 12_100
    assert row.completion_tokens == 50
    assert row.total_tokens == 12_150
    assert row.cache_read_tokens == 10_000
    assert row.cache_creation_tokens == 2_000
    # Raw provider figures kept for audit.
    assert row.meta_data["usage_details"]["input_tokens"] == 100
    priced = row.estimated_cost or row.meta_data.get("api_equivalent_cost")
    assert float(priced) == pytest.approx(EXPECTED_COST)

    # Aggregate: hit never exceeds input, miss is input minus the cache.
    totals = (
        db_session.query(*cache_split_columns(ApiUsage))
        .filter(ApiUsage.account_id == test_user.account_id)
        .one()
    )
    split = cache_split_from_row(totals)
    assert split["cache_read_tokens"] <= int(totals.covered_prompt_tokens)
    assert split["uncached_input_tokens"] == 100
