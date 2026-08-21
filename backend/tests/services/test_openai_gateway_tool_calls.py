"""Tests for OpenAI-compatible gateway tool call responses."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def test_chat_completion_preserves_tool_calls_in_response_payload():
    """Tool-capable clients need tool_calls whenever finish_reason is tool_calls."""
    account_id = uuid4()
    user = SimpleNamespace(id=uuid4(), account_id=account_id)
    service = OpenAIGatewayService(
        db=MagicMock(),
        auth_context=ModelGatewayAuthContext(token="token", user=user),
    )
    ai_model = SimpleNamespace(id=uuid4(), provider_name="deepseek")
    tool_calls = [
        {
            "id": "call_read",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": '{"command":"ls","description":"List files"}',
            },
        }
    ]

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(service, "_is_openai_codex_model", return_value=False),
        patch.object(
            service,
            "_call_litellm",
            return_value={
                "id": "chatcmpl_tool",
                "created": 1710000000,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": tool_calls,
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                },
            },
        ),
        patch.object(service, "_record_gateway_request") as record_gateway_request,
    ):
        response = service.create_chat_completion(
            {
                "model": "deepseek/deepseek-v4-pro",
                "messages": [{"role": "user", "content": "Inspect the repo"}],
                "tools": [{"type": "function", "function": {"name": "bash"}}],
            }
        )

    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == tool_calls
    assert choice["message"]["content"] == ""
    record_gateway_request.assert_called_once()
    assert (
        record_gateway_request.call_args.kwargs["response_payload"]["choices"][0][
            "message"
        ]["tool_calls"]
        == tool_calls
    )


def test_stream_chat_completion_preserves_tool_calls_in_recorded_response_payload():
    """Streaming chat completions must preserve tool calls for audit logs."""
    account_id = uuid4()
    user = SimpleNamespace(id=uuid4(), account_id=account_id)
    service = OpenAIGatewayService(
        db=MagicMock(),
        auth_context=ModelGatewayAuthContext(token="token", user=user),
    )
    ai_model = SimpleNamespace(id=uuid4(), provider_name="deepseek")

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(service, "_is_openai_codex_model", return_value=False),
        patch.object(
            service,
            "_call_litellm",
            return_value=[
                {
                    "id": "chatcmpl_stream_tool",
                    "created": 1710000000,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_read",
                                        "type": "function",
                                        "function": {
                                            "name": "bash",
                                            "arguments": "",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl_stream_tool",
                    "created": 1710000000,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": '{"command":"ls"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 3,
                        "total_tokens": 13,
                    },
                },
            ],
        ),
        patch.object(service, "_record_gateway_request") as record_gateway_request,
    ):
        chunks = list(
            service.stream_chat_completion(
                {
                    "model": "deepseek/deepseek-v4-pro",
                    "messages": [{"role": "user", "content": "Inspect the repo"}],
                    "stream": True,
                    "tools": [{"type": "function", "function": {"name": "bash"}}],
                }
            )
        )
        service.flush_deferred_stream_record()

    assert chunks[-1] == "data: [DONE]\n\n"
    first_payload = json.loads(chunks[0].removeprefix("data: ").strip())
    assert first_payload["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_read"
    response_payload = record_gateway_request.call_args.kwargs["response_payload"]
    assert response_payload["choices"][0]["finish_reason"] == "tool_calls"
    assert response_payload["choices"][0]["message"]["tool_calls"] == [
        {
            "id": "call_read",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command":"ls"}'},
        }
    ]
