"""DeepSeek's documented ``reasoning_effort`` reaches the wire, and only there.

DeepSeek documents a top-level ``reasoning_effort`` of ``low``, ``high`` or
``max`` on its OpenAI-format chat API for ``deepseek-flash`` and
``deepseek-v4-pro`` (https://api-docs.deepseek.com/guides/thinking_mode).
"""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import litellm
import pytest
from litellm.llms.custom_httpx.http_handler import HTTPHandler

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import ResolvedModelCredentials


def _service() -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context)


def _model(
    identifier: str = "deepseek-flash", endpoint: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        provider_name="deepseek",
        model_identifier=identifier,
        api_endpoint=endpoint,
    )


def _build(
    model: SimpleNamespace, payload: dict[str, Any]
) -> tuple[OpenAIGatewayService, dict[str, Any]]:
    service = _service()
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            ResolvedModelCredentials(
                credential_type="api_key", backend_type="local", value="sk-test"
            )
        )
        kwargs = service._build_completion_kwargs(
            model,
            messages=[{"role": "user", "content": "hi"}],
            payload=payload,
            stream=False,
            provider="openai",
        )
    return service, kwargs


def _wire_body(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Run LiteLLM against a mocked transport and return the posted body."""
    bodies: list[dict[str, Any]] = []

    def respond(*, url: str, data: str, **_kwargs: object) -> httpx.Response:
        bodies.append(json.loads(data))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    with (
        patch.object(HTTPHandler, "post", side_effect=respond),
        patch.object(
            httpx.Client, "send", side_effect=AssertionError("Unexpected HTTP request")
        ),
    ):
        litellm.completion(**kwargs)
    assert len(bodies) == 1
    return bodies[0]


# What DeepSeek is sent for each Preloop effort. ``medium`` is documented to
# run as ``high``, so the gateway sends the value that actually applies.
EXPECTED = {"low": "low", "medium": "high", "high": "high"}


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
@pytest.mark.parametrize(
    "payload_shape",
    ["responses", "chat"],
)
def test_deepseek_effort_reaches_wire_body(effort: str, payload_shape: str) -> None:
    payload = (
        {"reasoning": {"effort": effort, "summary": "auto"}}
        if payload_shape == "responses"
        else {"reasoning_effort": effort}
    )
    service, kwargs = _build(_model(), payload)
    body = _wire_body(kwargs)
    assert body["reasoning_effort"] == EXPECTED[effort]
    assert "reasoning" not in body
    assert "thinking" not in body
    assert service._last_upstream_parameters["reasoning_effort"] == EXPECTED[effort]


def test_deepseek_without_effort_sends_todays_body() -> None:
    _, without = _build(_model(), {})
    _, baseline = _build(_model(), {"reasoning": {"summary": "auto"}})
    body = _wire_body(without)
    assert "reasoning_effort" not in body
    assert "extra_body" not in without
    assert _wire_body(baseline) == body
    assert set(body) == {"model", "messages"}


@pytest.mark.parametrize(
    "identifier,endpoint",
    [
        # A model the documentation does not cover.
        ("deepseek-chat", None),
        ("deepseek-coder-legacy", None),
        # A documented model behind an endpoint the documentation does not cover.
        ("deepseek-flash", "https://proxy.example.com/v1"),
    ],
)
def test_deepseek_effort_not_sent_outside_documented_scope(
    identifier: str, endpoint: str | None
) -> None:
    service, kwargs = _build(_model(identifier, endpoint), {"reasoning_effort": "low"})
    assert "reasoning_effort" not in kwargs.get("extra_body", {})
    assert "reasoning_effort" not in kwargs
    assert "reasoning_effort" not in service._last_upstream_parameters


def test_deepseek_documented_endpoint_accepted() -> None:
    _, kwargs = _build(
        _model("deepseek-v4-pro", "https://api.deepseek.com"),
        {"reasoning_effort": "low"},
    )
    assert kwargs["extra_body"]["reasoning_effort"] == "low"


def test_deepseek_unknown_effort_is_not_forwarded() -> None:
    _, kwargs = _build(_model(), {"reasoning_effort": "ultra-turbo"})
    assert "reasoning_effort" not in kwargs.get("extra_body", {})


def test_other_providers_untouched() -> None:
    model = SimpleNamespace(
        provider_name="openai", model_identifier="gpt-5", api_endpoint=None
    )
    _, kwargs = _build(model, {"reasoning_effort": "low"})
    assert "reasoning_effort" not in kwargs.get("extra_body", {})
