"""Hosted API selection uses upstream capabilities, never gateway alias guesses."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from preloop.services.model_api_protocol import model_api_protocol
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime


def model(**changes: Any) -> SimpleNamespace:
    values = dict(
        provider_name="openai-compatible",
        api_endpoint="https://opencode.ai/zen/v1",
        model_identifier="responses-fixture",
        meta_data={"gateway": {"enabled": True, "model_alias": "custom-alias"}},
        model_parameters=None,
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "responses"),
        ({"api_endpoint": "https://opencode.ai/zen/v1/"}, "responses"),
        ({"api_endpoint": "https://other.example/v1"}, "chat_completions"),
        ({"model_identifier": "chat-fixture"}, "chat_completions"),
        ({"provider_name": "openrouter"}, "chat_completions"),
        (
            {"meta_data": {"gateway": {"responses_api": "transcode"}}},
            "chat_completions",
        ),
        ({"meta_data": {"responses_api": "transcode"}}, "chat_completions"),
        ({"meta_data": {"responses_api": "typo"}}, "responses"),
        (
            {
                "api_endpoint": "https://other.example/v1",
                "meta_data": {"gateway": {"responses_api": "native"}},
            },
            "responses",
        ),
        (
            {"provider_name": "anthropic", "meta_data": {"responses_api": "native"}},
            "chat_completions",
        ),
    ],
)
def test_protocol_is_scoped_to_upstream(changes: dict[str, Any], expected: str) -> None:
    with patch(
        "preloop.services.model_api_protocol.OPENCODE_ZEN_RESPONSES_MODELS",
        {"responses-fixture"},
    ):
        assert model_api_protocol(model(**changes)) == expected


def test_runtime_preserves_protocol_before_replacing_upstream_with_gateway() -> None:
    with patch(
        "preloop.services.model_api_protocol.OPENCODE_ZEN_RESPONSES_MODELS",
        {"responses-fixture"},
    ):
        runtime = resolve_ai_model_runtime(model())
    context = runtime.to_execution_context(gateway_token="fixture-token")
    assert context["model_identifier"] == "custom-alias"
    assert context["model_api_protocol"] == "responses"
    assert "opencode.ai" not in context["model_endpoint"]
