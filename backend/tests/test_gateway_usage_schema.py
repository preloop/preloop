"""Tests for gateway usage Pydantic schemas."""

import pytest
from pydantic import ValidationError

from preloop.schemas.gateway_usage import GatewayTokenUsage


def test_gateway_token_usage_mirrors_wire_names_onto_product_names() -> None:
    """A caller that fills only prompt/completion still answers input/output."""
    usage = GatewayTokenUsage(prompt_tokens=100, completion_tokens=40, total_tokens=140)

    assert usage.input_tokens == 100
    assert usage.output_tokens == 40
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 40


def test_gateway_token_usage_mirrors_product_names_onto_wire_names() -> None:
    """A caller that fills only input/output still answers prompt/completion."""
    usage = GatewayTokenUsage(input_tokens=80, output_tokens=20, total_tokens=100)

    assert usage.prompt_tokens == 80
    assert usage.completion_tokens == 20
    assert usage.input_tokens == 80
    assert usage.output_tokens == 20


def test_gateway_token_usage_rejects_disagreeing_direction_pairs() -> None:
    """Both names of a pair already set must match; mismatch is not mirrored."""
    with pytest.raises(
        ValidationError, match="prompt_tokens .* input_tokens .* must agree"
    ):
        GatewayTokenUsage(prompt_tokens=100, input_tokens=200)

    with pytest.raises(
        ValidationError, match="completion_tokens .* output_tokens .* must agree"
    ):
        GatewayTokenUsage(completion_tokens=10, output_tokens=40)
