"""A gateway usage row's token columns agree with each other.

Found in production: a Chat Completions provider that reports a cache
inclusive ``prompt_tokens`` together with a top-level
``cache_read_input_tokens`` (litellm adds it for Gemini and Anthropic),
answering a Responses request. The client-facing usage is renamed to the
Responses shape (``input_tokens``, no ``prompt_tokens``) and keeps the
top-level cache key, so the ledger writer read it as a raw Anthropic payload
and added the cache read to the input a second time. The stored input was too
large and the stored total, taken from the provider, was smaller than input
plus output.

These tests pin, per provider shape, that:

* input is stored cache inclusive exactly once;
* the stored total is never below input plus output;
* the cache and reasoning breakdown is kept;
* a recorded cost that reads the total (per-1k, prepaid token balance) is
  charged on a total no smaller than its components, while a row stored
  before the fix is repriced on its stored total.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from preloop.models import models
from preloop.models.crud import crud_ai_model, crud_api_usage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_pricing import estimate_ai_model_usage_cost_detailed
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.usage_token_details import anthropic_cache_input_tokens

pytestmark = pytest.mark.usefixtures("isolate_system_ai_models")


def _bridged_responses_usage(chat_usage: Dict[str, Any]) -> Dict[str, Any]:
    """Client-facing Responses usage for a chat upstream, as the stream builds it."""
    normalized = OpenAIGatewayService._normalize_usage(
        chat_usage,
        prompt_key="prompt_tokens",
        completion_key="completion_tokens",
        output_names=("completion_tokens", "output_tokens"),
    )
    return OpenAIGatewayService._responses_api_usage(chat_usage, normalized)


def _anthropic_client_usage(raw_usage: Dict[str, Any]) -> Dict[str, Any]:
    """Client-facing Messages usage for a raw Anthropic upstream."""
    normalized = OpenAIGatewayService._normalize_usage(
        raw_usage,
        prompt_key="input_tokens",
        completion_key="output_tokens",
        output_names=("output_tokens", "completion_tokens"),
    )
    return {
        "input_tokens": normalized["prompt_tokens"],
        "output_tokens": normalized["completion_tokens"],
    }


#: litellm Chat Completions usage for Gemini: ``prompt_tokens`` already counts
#: the cached input, and the same count is repeated at the top level.
GEMINI_CHAT = {
    "prompt_tokens": 9000,
    "completion_tokens": 600,
    "total_tokens": 9600,
    "prompt_tokens_details": {"text_tokens": 9000, "cached_tokens": 6000},
    "completion_tokens_details": {"text_tokens": 20, "reasoning_tokens": 580},
    "cache_read_input_tokens": 6000,
}

#: litellm Chat Completions usage for Anthropic: cache read and write are
#: folded into ``prompt_tokens`` and repeated at the top level.
ANTHROPIC_CHAT = {
    "prompt_tokens": 1000,
    "completion_tokens": 50,
    "total_tokens": 1050,
    "prompt_tokens_details": {"cached_tokens": 700},
    "cache_read_input_tokens": 700,
    "cache_creation_input_tokens": 100,
}

#: Raw Anthropic Messages usage: ``input_tokens`` leaves the cache out.
ANTHROPIC_RAW = {
    "input_tokens": 200,
    "output_tokens": 50,
    "cache_read_input_tokens": 700,
    "cache_creation_input_tokens": 100,
}

OPENAI_CHAT = {
    "prompt_tokens": 100,
    "completion_tokens": 7,
    "total_tokens": 107,
    "prompt_tokens_details": {"cached_tokens": 64},
    "completion_tokens_details": {"reasoning_tokens": 3},
}

RESPONSES_NATIVE = {
    "input_tokens": 5859,
    "input_tokens_details": {"cached_tokens": 4864},
    "output_tokens": 61,
    "output_tokens_details": {"reasoning_tokens": 22},
    "total_tokens": 5920,
}

#: A provider whose own total leaves the cached input out.
TOTAL_OMITS_CACHE = {
    "prompt_tokens": 100,
    "completion_tokens": 7,
    "total_tokens": 43,
    "prompt_tokens_details": {"cached_tokens": 64},
}

#: A provider whose total counts something beyond input and output.
TOTAL_ABOVE_PARTS = {
    "prompt_tokens": 100,
    "completion_tokens": 7,
    "total_tokens": 150,
}

NO_TOTAL = {"prompt_tokens": 100, "completion_tokens": 7}

# name -> (client usage, upstream usage, prompt, completion, total, cache read,
#          cache creation, reasoning)
SHAPES: Dict[str, tuple] = {
    "gemini_chat_bridged_to_responses": (
        _bridged_responses_usage(GEMINI_CHAT),
        GEMINI_CHAT,
        *(9000, 600, 9600, 6000, None, 580),
    ),
    "gemini_chat_completions": (GEMINI_CHAT, GEMINI_CHAT, 9000, 600, 9600, 6000)
    + (None, 580),
    "anthropic_chat_bridged_to_responses": (
        _bridged_responses_usage(ANTHROPIC_CHAT),
        ANTHROPIC_CHAT,
        *(1000, 50, 1050, 700, 100, None),
    ),
    "anthropic_messages_raw": (
        _anthropic_client_usage(ANTHROPIC_RAW),
        ANTHROPIC_RAW,
        *(1000, 50, 1050, 700, 100, None),
    ),
    "anthropic_messages_raw_without_client_payload": (
        None,
        ANTHROPIC_RAW,
        *(1000, 50, 1050, 700, 100, None),
    ),
    "openai_chat_cached": (OPENAI_CHAT, OPENAI_CHAT, 100, 7, 107, 64, None, 3),
    "responses_native_cached": (
        RESPONSES_NATIVE,
        RESPONSES_NATIVE,
        *(5859, 61, 5920, 4864, None, 22),
    ),
    "provider_total_omits_cached_input": (
        TOTAL_OMITS_CACHE,
        TOTAL_OMITS_CACHE,
        *(100, 7, 107, 64, None, None),
    ),
    "provider_total_above_parts": (
        TOTAL_ABOVE_PARTS,
        TOTAL_ABOVE_PARTS,
        *(100, 7, 150, None, None, None),
    ),
    "provider_sends_no_total": (NO_TOTAL, NO_TOTAL, 100, 7, 107, None, None, None),
}


def _model(db_session, test_user, pricing: Optional[Dict[str, Any]] = None):
    meta_data: Dict[str, Any] = {
        "gateway": {"enabled": True, "model_alias": "example/usage-shapes"}
    }
    if pricing is not None:
        meta_data["pricing"] = pricing
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Usage Shapes Model",
            "provider_name": "openai-compatible",
            "model_identifier": "usage-shapes",
            "api_endpoint": "https://llm.example.com/v1",
            "api_key": "provider-secret",
            "meta_data": meta_data,
        },
        account_id=test_user.account_id,
    )


def _record(
    db_session,
    test_user,
    ai_model,
    *,
    client_usage: Optional[Dict[str, Any]],
    upstream_usage: Optional[Dict[str, Any]],
) -> models.ApiUsage:
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="token", user=test_user)
    )
    service._record_gateway_request(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.2,
        ai_model=ai_model,
        requested_model="example/usage-shapes",
        response_payload=(
            {"usage": client_usage} if client_usage is not None else None
        ),
        upstream_response={"id": "resp_1", "usage": upstream_usage},
        endpoint_kind="responses_stream",
        request_payload={"model": "example/usage-shapes"},
    )
    return (
        db_session.query(models.ApiUsage)
        .filter(models.ApiUsage.id == service.last_usage_id)
        .one()
    )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_recorded_row_is_consistent_for_every_provider_shape(
    db_session, test_user, shape: str
) -> None:
    client, upstream, prompt, completion, total, read, creation, reasoning = SHAPES[
        shape
    ]
    row = _record(
        db_session,
        test_user,
        _model(db_session, test_user),
        client_usage=client,
        upstream_usage=upstream,
    )

    assert row.prompt_tokens == prompt
    assert row.completion_tokens == completion
    assert row.total_tokens == total
    assert row.total_tokens >= row.prompt_tokens + row.completion_tokens
    assert row.cache_read_tokens == read
    assert row.cache_creation_tokens == creation
    assert row.reasoning_tokens == reasoning
    # The provider's own figures stay available for audit.
    assert row.meta_data["usage_details"] == upstream


def test_short_provider_total_is_flagged_not_silently_replaced(
    db_session, test_user
) -> None:
    row = _record(
        db_session,
        test_user,
        _model(db_session, test_user),
        client_usage=TOTAL_OMITS_CACHE,
        upstream_usage=TOTAL_OMITS_CACHE,
    )
    assert row.total_tokens == 107
    assert row.meta_data["usage_details"]["total_tokens"] == 43
    assert "total_tokens_below_components" in row.meta_data["usage_warnings"]


def test_consistent_row_carries_no_total_warning(db_session, test_user) -> None:
    row = _record(
        db_session,
        test_user,
        _model(db_session, test_user),
        client_usage=_bridged_responses_usage(GEMINI_CHAT),
        upstream_usage=GEMINI_CHAT,
    )
    assert not row.meta_data.get("usage_warnings")


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (ANTHROPIC_RAW, 800),
        ({"input_tokens": 10, "output_tokens": 2}, 0),
        (ANTHROPIC_CHAT, 0),
        (GEMINI_CHAT, 0),
        (_bridged_responses_usage(GEMINI_CHAT), 0),
        (_bridged_responses_usage(ANTHROPIC_CHAT), 0),
        (RESPONSES_NATIVE, 0),
        ({**RESPONSES_NATIVE, "cache_read_input_tokens": 4864}, 0),
        (None, 0),
    ],
)
def test_cache_is_added_only_to_a_raw_anthropic_input(
    usage: Optional[Dict[str, Any]], expected: int
) -> None:
    assert anthropic_cache_input_tokens(usage) == expected


@pytest.mark.parametrize(
    ("prompt", "completion", "total", "stored"),
    [
        (100, 7, 43, 107),
        (100, 7, None, 107),
        (100, 7, 0, 107),
        (100, None, 43, 100),
        (None, 7, None, 7),
        (100, 7, 150, 150),
        (100, 7, 107, 107),
        (0, 0, 0, 0),
        (None, None, None, None),
    ],
)
def test_gateway_usage_write_never_stores_a_total_below_its_parts(
    db_session, test_user, prompt, completion, total, stored
) -> None:
    """The write itself refuses an inconsistent row, whoever the caller is."""
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )
    assert row.total_tokens == stored
    assert row.prompt_tokens == prompt
    assert row.completion_tokens == completion


@pytest.mark.parametrize("reported_total", [43, 0])
def test_recorded_per_1k_cost_uses_input_plus_output_when_total_is_short(
    db_session, test_user, reported_total: int
) -> None:
    ai_model = _model(db_session, test_user, pricing={"price_per_1k": 1.0})
    usage = {**TOTAL_OMITS_CACHE, "total_tokens": reported_total}
    row = _record(
        db_session, test_user, ai_model, client_usage=usage, upstream_usage=usage
    )
    assert row.total_tokens == 107
    assert row.estimated_cost == pytest.approx(0.107)
    assert "total_tokens_below_components" in row.meta_data["usage_warnings"]


def test_recorded_prepaid_token_balance_covers_input_plus_output(
    db_session, test_user
) -> None:
    ai_model = _model(
        db_session,
        test_user,
        pricing={
            "input_price_per_1k": 1.0,
            "output_price_per_1k": 1.0,
            "prepaid_token_balance": 43,
        },
    )
    # 107 tokens used, 43 prepaid: 64 are charged. The provider total of 43
    # would have read as fully covered and charged nothing.
    row = _record(
        db_session,
        test_user,
        ai_model,
        client_usage=TOTAL_OMITS_CACHE,
        upstream_usage=TOTAL_OMITS_CACHE,
    )
    assert row.estimated_cost == pytest.approx(0.107 * 64 / 107)


def test_repricing_a_stored_row_charges_on_its_stored_total(
    db_session, test_user
) -> None:
    """A row written before the fix: input too large, total correct.

    Repricing it with a per-1k price must stay on the stored total. Raising
    the total to the inflated parts would over-charge.
    """
    ai_model = _model(db_session, test_user, pricing={"price_per_1k": 1.0})
    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model, prompt_tokens=15000, completion_tokens=600, total_tokens=9600
    )
    assert estimate.cost == pytest.approx(9.6)


def test_missing_provider_total_is_not_flagged(db_session, test_user) -> None:
    row = _record(
        db_session,
        test_user,
        _model(db_session, test_user),
        client_usage=NO_TOTAL,
        upstream_usage=NO_TOTAL,
    )
    assert row.total_tokens == 107
    assert not row.meta_data.get("usage_warnings")


def test_recorded_cost_with_a_configured_input_price_does_not_bill_cache_twice(
    db_session, test_user
) -> None:
    """Inflated input priced the cached tokens at the full rate as well."""
    ai_model = _model(
        db_session,
        test_user,
        pricing={
            "input_price_per_1k": 1.0,
            "cache_read_input_price_per_1k": 0.1,
            "output_price_per_1k": 2.0,
        },
    )
    row = _record(
        db_session,
        test_user,
        ai_model,
        client_usage=_bridged_responses_usage(GEMINI_CHAT),
        upstream_usage=GEMINI_CHAT,
    )
    # 3,000 uncached at 1.0, 6,000 cached at 0.1, 600 output at 2.0.
    assert row.estimated_cost == pytest.approx(3.0 + 0.6 + 1.2)
