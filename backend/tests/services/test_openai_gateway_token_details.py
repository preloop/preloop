"""Cache/reasoning token normalization across usage shapes (#1401)."""

from __future__ import annotations

import logging

import pytest

from preloop.models import models
from preloop.services.model_pricing import _estimate_cost_from_pricing
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.usage_token_details import (
    coerce_token_count,
    extract_token_details,
)

extract = OpenAIGatewayService._extract_token_details


def test_responses_shape_from_issue_is_normalized() -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
        "input_tokens_details": {"cached_tokens": 80},
        "output_tokens_details": {"reasoning_tokens": 5},
    }
    assert extract(usage) == {
        "cache_read_tokens": 80,
        "cache_creation_tokens": None,
        "reasoning_tokens": 5,
    }


def test_responses_cache_creation_tokens_when_emitted() -> None:
    usage = {"input_tokens_details": {"cache_creation_tokens": 7}}
    assert extract(usage)["cache_creation_tokens"] == 7


def test_chat_completions_shape_unchanged() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 40, "cache_creation_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 6},
    }
    assert extract(usage) == {
        "cache_read_tokens": 40,
        "cache_creation_tokens": 3,
        "reasoning_tokens": 6,
    }


def test_anthropic_shape_unchanged() -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": 60,
        "cache_creation_input_tokens": 20,
    }
    assert extract(usage) == {
        "cache_read_tokens": 60,
        "cache_creation_tokens": 20,
        "reasoning_tokens": None,
    }


def test_explicit_zero_is_a_value_not_unknown() -> None:
    usage = {
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
        # A later candidate must not replace the explicit zero.
        "cache_read_input_tokens": 50,
    }
    details = extract(usage)
    assert details["cache_read_tokens"] == 0
    assert details["reasoning_tokens"] == 0


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": 5},
        {"input_tokens_details": None},
        {"input_tokens_details": []},
    ],
)
def test_unknown_stays_none(usage) -> None:
    assert extract(usage) == {
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "reasoning_tokens": None,
    }


@pytest.mark.parametrize(
    "bad",
    [
        -1,
        True,
        False,
        1.5,
        float("nan"),
        float("inf"),
        "abc",
        "-3",
        "1.5",
        "",
        [1],
        {"n": 1},
    ],
)
def test_malformed_values_are_rejected(bad) -> None:
    assert coerce_token_count(bad) is None
    usage = {
        "input_tokens_details": {"cached_tokens": bad},
        "output_tokens_details": {"reasoning_tokens": bad},
    }
    details = extract(usage)
    assert details["cache_read_tokens"] is None
    assert details["reasoning_tokens"] is None


@pytest.mark.parametrize(
    "good,expected", [(0, 0), (80, 80), (80.0, 80), ("80", 80), (" 9 ", 9)]
)
def test_valid_values_are_accepted(good, expected) -> None:
    assert coerce_token_count(good) == expected


def test_rejected_value_falls_through_to_next_candidate() -> None:
    usage = {
        "prompt_tokens_details": {"cached_tokens": -4},
        "input_tokens_details": {"cached_tokens": True},
        "cache_read_input_tokens": 12,
        "completion_tokens_details": {"reasoning_tokens": "many"},
        "output_tokens_details": {"reasoning_tokens": 3},
    }
    details = extract(usage)
    assert details["cache_read_tokens"] == 12
    assert details["reasoning_tokens"] == 3


def test_mixed_payload_precedence_chat_then_responses_then_anthropic() -> None:
    usage = {
        "prompt_tokens_details": {"cached_tokens": 1},
        "input_tokens_details": {"cached_tokens": 2, "cache_creation_tokens": 8},
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 9,
        "completion_tokens_details": {"reasoning_tokens": 4},
        "output_tokens_details": {"reasoning_tokens": 5},
    }
    assert extract(usage) == {
        "cache_read_tokens": 1,
        "cache_creation_tokens": 8,
        "reasoning_tokens": 4,
    }


def test_rejection_logs_once_at_debug_without_payload(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="preloop.services.usage_token_details")
    extract_token_details(
        {
            "input_tokens_details": {"cached_tokens": -987654},
            "output_tokens_details": {"reasoning_tokens": "secret-ish"},
        }
    )
    records = [
        r for r in caplog.records if r.name == "preloop.services.usage_token_details"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    message = records[0].getMessage()
    assert "-987654" not in message and "secret-ish" not in message
    assert "input_tokens_details.cached_tokens" in message


def test_valid_payload_does_not_log(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="preloop.services.usage_token_details")
    extract_token_details({"input_tokens_details": {"cached_tokens": 1}})
    assert not [
        r for r in caplog.records if r.name == "preloop.services.usage_token_details"
    ]


def test_configured_pricing_bills_responses_cache_reads_at_cache_rate() -> None:
    pricing = {
        "input_price_per_1k": 1.0,
        "cache_read_input_price_per_1k": 0.1,
        "output_price_per_1k": 0.0,
    }
    responses = _estimate_cost_from_pricing(
        pricing,
        prompt_tokens=1000,
        completion_tokens=0,
        total_tokens=1000,
        usage_details={
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 800},
        },
    )
    chat = _estimate_cost_from_pricing(
        pricing,
        prompt_tokens=1000,
        completion_tokens=0,
        total_tokens=1000,
        usage_details={
            "prompt_tokens": 1000,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
    )
    # 200 uncached * 1.0/1k + 800 cached * 0.1/1k = 0.28
    assert responses == pytest.approx(0.28)
    assert chat == pytest.approx(0.28)


def test_responses_usage_reaches_usage_row_through_gateway(
    db_session, test_user
) -> None:
    """The Responses details survive the call site into the persisted row."""
    from preloop.models.crud import crud_ai_model
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext

    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Responses detail test",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "test-provider-key",
        },
        account_id=test_user.account_id,
    )
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="test", user=test_user)
    )
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
        "input_tokens_details": {"cached_tokens": 80},
        "output_tokens_details": {"reasoning_tokens": 5},
    }
    service._record_gateway_request_inner(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.5,
        ai_model=ai_model,
        requested_model="gpt-5",
        response_payload={"usage": usage},
        upstream_response={"usage": usage},
        endpoint_kind="responses",
    )
    row = db_session.query(models.ApiUsage).filter_by(ai_model_id=ai_model.id).one()
    assert row.prompt_tokens == 100
    assert row.completion_tokens == 10
    assert row.cache_read_tokens == 80
    assert row.reasoning_tokens == 5
    assert row.cache_creation_tokens is None


@pytest.mark.parametrize(
    "payload",
    [
        {"usage": {"input_tokens_details": {"cached_tokens": 80}}},
        {"input_tokens_details": {"cached_tokens": 80}},
        {"usage_details": {"input_tokens_details": {"cached_tokens": 80}}},
    ],
)
def test_context_analysis_reads_responses_cache_shape(payload) -> None:
    from preloop.services.context_analysis import (
        _payload_cache_creation_tokens,
        _payload_cache_read_tokens,
    )

    assert _payload_cache_read_tokens(payload) == 80
    assert _payload_cache_creation_tokens(payload) == 0
