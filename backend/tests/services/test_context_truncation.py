"""Prompt truncation uses provider evidence, never an estimated usage fallback."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from preloop.services import context_truncation as truncation
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def test_large_input_deficit_records_both_counts() -> None:
    """A small reported window cannot silently pass as agent misbehavior."""
    signal = truncation.detect_truncation(estimated=26000, reported=8194)
    assert signal["estimated_input_tokens"] == 26000
    assert signal["reported_input_tokens"] == 8194
    assert signal["signal"] == "input_token_deficit"
    message = truncation.truncation_message(signal)
    assert "appears to have truncated" in message
    assert "8194" in message and "26000" in message
    assert "context window" in message


@pytest.mark.parametrize(
    "estimated,reported", [(26000, 18000), (6000, 1000), (26000, 0)]
)
def test_normal_variance_small_requests_and_missing_usage_are_not_truncation(
    estimated: int, reported: int
) -> None:
    """A ratio needs both a floor and meaningful provider-reported usage."""
    assert truncation.detect_truncation(estimated=estimated, reported=reported) is None


def test_identical_count_with_growing_history_is_second_signal() -> None:
    """Two requests can expose a ceiling before the strong ratio trips."""
    previous = {"estimated_input_tokens": 16000, "reported_input_tokens": 10000}
    signal = truncation.detect_truncation(
        estimated=16500, reported=10000, previous=previous
    )
    assert signal["signal"] == "unchanged_input_tokens_with_growing_history"
    assert (
        truncation.detect_truncation(estimated=16000, reported=10000, previous=previous)
        is None
    )
    assert (
        truncation.detect_truncation(estimated=16500, reported=10100, previous=previous)
        is None
    )


def test_estimate_includes_instructions_history_and_tools_but_not_image_data() -> None:
    """Transport options and base64 media are not text tokens."""
    payload = {
        "instructions": "Read the change. " * 1000,
        "input": [{"role": "user", "content": "Review the code. " * 1000}],
        "tools": [
            {"type": "function", "name": "inspect", "description": "Read. " * 1000}
        ],
    }
    estimate = truncation.estimate_request_input(payload)
    assert estimate > 9000
    payload["input"].append(
        {"type": "input_image", "image_url": "data:image/png;base64," + "a" * 100000}
    )
    assert truncation.estimate_request_input(payload) == estimate


def test_opaque_reasoning_is_not_counted_as_prompt_text() -> None:
    """Encrypted reasoning wire bytes do not measure readable context tokens."""
    payload = {"input": [{"type": "reasoning", "summary": []}]}
    estimate = truncation.estimate_request_input(payload)
    payload["input"][0]["encrypted_content"] = "opaque" * 20000
    assert truncation.estimate_request_input(payload) == estimate


def test_tool_schema_can_define_a_property_named_type() -> None:
    """Schema property dictionaries are not content block type tags."""
    payload = {
        "tools": [
            {
                "type": "function",
                "name": "inspect",
                "parameters": {
                    "type": "object",
                    "properties": {"type": {"type": ["string", "null"]}},
                },
            }
        ]
    }
    assert truncation.estimate_request_input(payload) > 0


def test_flow_preflight_refuses_before_upstream() -> None:
    """The registered server window is enforced before the first call."""
    execution_id = str(UUID(int=1))
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="test",
            user=SimpleNamespace(id=UUID(int=2), account_id=UUID(int=3)),
            api_key=SimpleNamespace(
                account_id=UUID(int=3), context_data={"flow_execution_id": execution_id}
            ),
        ),
    )
    model = SimpleNamespace(
        id=UUID(int=4),
        model_identifier="example-coder",
        provider_name="openai-compatible",
        model_parameters={"context_window": 8192},
        model_gateway_model_alias=None,
    )
    with (
        patch.object(truncation, "record_execution_truncation") as record,
        pytest.raises(Exception, match="registered context window") as error,
    ):
        service._check_context_window(
            model,
            {"model": "example-coder", "input": "Review this change. " * 6000},
            gateway_provider="openai",
        )
    assert error.value.code == "context_truncated_suspected"
    assert record.call_args.kwargs["signal"]["estimated_input_tokens"] > 8192
