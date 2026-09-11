"""Offline Model Studio routing, controls, and streamed usage regressions."""

import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import httpx
import litellm
from openai import OpenAI
from sqlalchemy import select

from preloop.models import models
from preloop.models.crud import crud_ai_model

from preloop.services.ai_model_provider import QWEN_DEFAULT_BASE_URL
from preloop.services.model_credentials import resolve_model_call_credentials
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService


def _service() -> OpenAIGatewayService:
    return OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="synthetic-token",
            user=SimpleNamespace(id="user-1", account_id="account-1"),
        ),
        upstream_backend=MagicMock(),
    )


def _model(endpoint: str | None = None, provider: str = "qwen") -> SimpleNamespace:
    return SimpleNamespace(
        id="model-1",
        provider_name=provider,
        model_identifier="qwen3.8-max",
        api_endpoint=endpoint,
        meta_data={},
    )


def _kwargs(model: SimpleNamespace, payload: dict) -> dict:
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic-provider-key"
        )
        return _service()._build_completion_kwargs(
            model,
            messages=[{"role": "user", "content": "hello"}],
            payload=payload,
            stream=True,
            provider="openai",
        )


@pytest.mark.parametrize("endpoint", [None, "", "  "])
def test_qwen_chat_defaults_to_same_base_as_discovery(endpoint: str | None) -> None:
    kwargs = _kwargs(_model(endpoint), {})
    assert kwargs["api_base"] == QWEN_DEFAULT_BASE_URL
    assert kwargs["model"] == "openai/qwen3.8-max"


def test_qwen_auxiliary_calls_share_default_base() -> None:
    with patch("preloop.services.model_credentials.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = None
        assert (
            resolve_model_call_credentials(_model())["api_base"]
            == QWEN_DEFAULT_BASE_URL
        )


def test_workspace_url_tools_usage_and_thinking_controls_are_forwarded() -> None:
    endpoint = "https://example.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        }
    ]
    kwargs = _kwargs(
        _model(endpoint),
        {
            "enable_thinking": False,
            "thinking_budget": 128,
            "tools": tools,
            "tool_choice": "auto",
            "extra_body": {"ignored_field": "never-forward-arbitrary-body"},
        },
    )
    assert kwargs["api_base"] == endpoint
    assert kwargs["tools"] == tools
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["stream_options"]["include_usage"] is True
    assert kwargs["extra_body"] == {"enable_thinking": False, "thinking_budget": 128}


def test_sdk_extra_body_thinking_controls_are_supported() -> None:
    assert _kwargs(_model(), {"extra_body": {"enable_thinking": True}})[
        "extra_body"
    ] == {"enable_thinking": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"enable_thinking": "false"},
        {"enable_thinking": 0},
        {"thinking_budget": True},
        {"thinking_budget": -1},
        {"thinking_budget": "128"},
    ],
)
def test_invalid_thinking_controls_are_rejected(payload: dict) -> None:
    with pytest.raises(ModelGatewayAPIError) as error:
        _kwargs(_model(), payload)
    assert error.value.status_code == 400


def test_thinking_controls_are_not_added_to_other_providers() -> None:
    kwargs = _kwargs(
        _model("https://api.openai.com/v1", "openai"), {"enable_thinking": True}
    )
    assert "extra_body" not in kwargs


def test_responses_reasoning_effort_is_forwarded_and_conflicting_budget_rejected() -> (
    None
):
    assert _kwargs(_model(), {"reasoning": {"effort": "high"}})["extra_body"] == {
        "reasoning_effort": "high"
    }
    for payload in (
        {"reasoning_effort": "unsupported"},
        {"reasoning": {"effort": "high"}, "thinking_budget": 128},
    ):
        with pytest.raises(ModelGatewayAPIError) as error:
            _kwargs(_model(), payload)
        assert error.value.status_code == 400


def test_explicit_cache_markers_survive_litellm_wire_without_body_override() -> None:
    service = _service()
    governed_messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "governed text",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic"
        )
        kwargs = service._build_completion_kwargs(
            _model(),
            messages=governed_messages,
            payload={
                "messages": [
                    {"role": "user", "content": "pre-policy text must not win"}
                ],
                "extra_body": {
                    "messages": [{"role": "user", "content": "must not win"}],
                    "api_base": "https://example.com",
                    "model": "must-not-win",
                },
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "parameters": {"type": "object"},
                    }
                ],
                "enable_thinking": False,
            },
            stream=False,
            provider="openai",
        )
    wire = []

    def handle(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "offline",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen3.8-max",
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

    with OpenAI(
        api_key="synthetic",
        base_url=kwargs["api_base"],
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_retries=0,
    ) as client:
        with patch.object(
            socket.socket, "connect", side_effect=AssertionError("Network forbidden")
        ):
            litellm.completion(**kwargs, client=client)
    assert wire[0]["messages"] == governed_messages
    assert wire[0]["tools"][0]["function"]["name"] == "lookup"
    assert wire[0]["enable_thinking"] is False
    assert wire[0]["model"] == "qwen3.8-max"
    assert service._last_alibaba_cache_mode == "explicit"
    assert kwargs["extra_body"]["messages"] is not governed_messages


@pytest.mark.parametrize(
    "marker", [{"type": "other"}, {"type": "ephemeral", "ttl": "1h"}, "ephemeral"]
)
def test_unknown_explicit_cache_marker_is_rejected(marker) -> None:
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic"
        )
        with pytest.raises(ModelGatewayAPIError):
            _service()._build_completion_kwargs(
                _model(),
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "x", "cache_control": marker}
                        ],
                    }
                ],
                payload={},
                stream=False,
                provider="openai",
            )


@pytest.mark.parametrize(
    "extra_blocks",
    [
        [{"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}],
        [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}] * 4,
    ],
)
def test_explicit_cache_rejects_multimodal_or_excess_markers(
    extra_blocks: list,
) -> None:
    content = [
        {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}},
        *extra_blocks,
    ]
    with patch("preloop.services.openai_gateway.get_secret_service") as secret:
        secret.return_value.resolve_ai_model_credentials.return_value = SimpleNamespace(
            credential_type="api_key", value="synthetic"
        )
        with pytest.raises(ModelGatewayAPIError):
            _service()._build_completion_kwargs(
                _model(),
                messages=[{"role": "user", "content": content}],
                payload={},
                stream=False,
                provider="openai",
            )


def test_qwen_label_on_openrouter_does_not_enable_alibaba_controls() -> None:
    kwargs = _kwargs(
        _model("https://openrouter.ai/api/v1"),
        {"enable_thinking": True, "thinking_budget": -1},
    )
    assert kwargs["model"].startswith("openrouter/")
    assert "enable_thinking" not in kwargs.get("extra_body", {})
    assert "thinking_budget" not in kwargs.get("extra_body", {})


def test_stream_reasoning_and_tool_deltas_preserve_usage_without_double_counting() -> (
    None
):
    service = _service()
    usage = {
        "prompt_tokens": 55,
        "completion_tokens": 31,
        "total_tokens": 86,
        "completion_tokens_details": {"reasoning_tokens": 25},
        "prompt_tokens_details": {"cached_tokens": 10},
    }
    chunk = {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "synthetic reasoning",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                }
            }
        ],
        "usage": usage,
    }
    normalized = service._normalize_chat_stream_chunk(
        chunk, model_name="qwen/qwen3.8-max", response_id="response-1", created_at=1
    )
    assert normalized["choices"][0]["delta"] == chunk["choices"][0]["delta"]
    assert normalized["usage"] == usage
    details = service._extract_token_details(usage)
    assert details["reasoning_tokens"] == 25
    assert details["cache_read_tokens"] == 10


@pytest.mark.parametrize(
    "prompt_details",
    [
        {"cache_creation_input_tokens": 12},
        {"cache_creation": {"ephemeral_5m_input_tokens": 12}},
    ],
)
def test_alibaba_cache_creation_details_are_recorded(prompt_details: dict) -> None:
    assert (
        _service()._extract_token_details({"prompt_tokens_details": prompt_details})[
            "cache_creation_tokens"
        ]
        == 12
    )


def test_gateway_stream_routes_tools_and_records_provider_usage(
    db_session, test_user
) -> None:
    """Exercise gateway -> fake upstream -> SSE and real local usage persistence."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Synthetic Model Studio",
            "provider_name": "qwen",
            "model_identifier": "qwen3.8-max",
            "api_key": "synthetic-provider-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "qwen/test",
                    "provider_adapter": "preloop",
                }
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    chunks = [
        {
            "id": "synthetic-stream",
            "choices": [
                {"index": 0, "delta": {"reasoning_content": "synthetic reasoning"}}
            ],
        },
        {
            "id": "synthetic-stream",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": "synthetic-stream",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "synthetic-stream",
            "choices": [],
            "usage": {
                "prompt_tokens": 55,
                "completion_tokens": 31,
                "total_tokens": 86,
                "completion_tokens_details": {"reasoning_tokens": 25},
                "prompt_tokens_details": {
                    "cached_tokens": 10,
                    "cache_creation_input_tokens": 12,
                },
            },
        },
    ]
    with patch(
        "preloop.services.openai_gateway.litellm.completion", return_value=iter(chunks)
    ) as upstream:
        events = list(
            service.stream_chat_completion(
                {
                    "model": "qwen/test",
                    "messages": [{"role": "user", "content": "Use lookup"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "enable_thinking": True,
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                }
            )
        )
        service.flush_deferred_stream_record()
    kwargs = upstream.call_args.kwargs
    assert kwargs["model"] == "openai/qwen3.8-max"
    assert kwargs["api_base"] == QWEN_DEFAULT_BASE_URL
    assert kwargs["extra_body"] == {"enable_thinking": True}
    assert kwargs["stream_options"]["include_usage"] is True
    assert kwargs["tools"][0]["function"]["name"] == "lookup"
    payloads = [
        json.loads(event.removeprefix("data: ").strip())
        for event in events
        if event.startswith("data: ") and "[DONE]" not in event
    ]
    assert any(
        payload.get("choices") and payload["choices"][0]["delta"].get("tool_calls")
        for payload in payloads
    )
    assert any(
        payload.get("choices")
        and payload["choices"][0]["delta"].get("reasoning_content")
        for payload in payloads
    )
    assert any(
        payload.get("choices")
        and payload["choices"][0].get("finish_reason") == "tool_calls"
        for payload in payloads
    )
    usage = db_session.scalars(
        select(models.ApiUsage)
        .where(models.ApiUsage.endpoint == "/openai/v1/chat/completions")
        .order_by(models.ApiUsage.timestamp.desc())
    ).first()
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (
        55,
        31,
        86,
    )
    assert usage.reasoning_tokens == 25
    assert usage.cache_read_tokens == 10
    assert usage.cache_creation_tokens == 12
    assert usage.meta_data["usage_details"]["_preloop_cache_mode"] == "implicit"
    assert all("_preloop_cache_mode" not in str(payload) for payload in payloads)


@pytest.mark.parametrize(
    "endpoint, expected_finish",
    [(None, "tool_calls"), ("https://openrouter.ai/api/v1", "stop")],
)
def test_nonstream_tool_stop_continues_with_tool_result(
    db_session, test_user, endpoint, expected_finish
) -> None:
    """A Model Studio stop with tool calls remains a two-turn agent interaction."""
    model = crud_ai_model.create_with_account(
        db_session,
        obj_in={
            "name": "Synthetic Model Studio",
            "provider_name": "qwen",
            "model_identifier": "qwen3.8-max",
            "api_key": "synthetic",
            "is_default": True,
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "qwen/test",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )
    # Simulate a legacy row; current CRUD correctly rejects a new qwen row
    # pointed at OpenRouter, but the endpoint still owns existing routing.
    model.api_endpoint = endpoint
    db_session.flush()
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }
    first = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [call],
                    "reasoning_content": "synthetic thinking",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    second = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "pong"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
    }
    messages = [{"role": "user", "content": "Use lookup"}]
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        side_effect=[first, second],
    ) as upstream:
        response = service.create_chat_completion(
            {
                "model": "qwen/test",
                "messages": messages,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            }
        )
        assert response["choices"][0]["finish_reason"] == expected_finish
        if endpoint is None:
            assert (
                response["choices"][0]["message"]["reasoning_content"]
                == "synthetic thinking"
            )
        else:
            assert "reasoning_content" not in response["choices"][0]["message"]
        messages = [
            *messages,
            response["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call_1", "content": "pong"},
        ]
        final = service.create_chat_completion(
            {"model": "qwen/test", "messages": messages}
        )
    assert final["choices"][0]["message"]["content"] == "pong"
    assert final["choices"][0]["finish_reason"] == "stop"
    assert upstream.call_args.kwargs["messages"][-1]["tool_call_id"] == "call_1"
