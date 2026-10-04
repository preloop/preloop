"""Synthetic gateway capture regressions for the shared live session timeline."""

import json
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_gateway_events import ModelGatewayEventEmitter


def test_gateway_only_parallel_tools_and_results_keep_provider_identity() -> None:
    emitter = ModelGatewayEventEmitter(MagicMock())
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-example-1",
                            "function": {
                                "name": "terminal",
                                "arguments": '{"command":"pwd"}',
                            },
                        },
                        {
                            "id": "call-example-2",
                            "function": {
                                "name": "terminal",
                                "arguments": '{"command":"pwd"}',
                            },
                        },
                    ],
                }
            }
        ]
    }
    request = {
        "messages": [
            {
                "role": "tool",
                "tool_call_id": "call-example-1",
                "content": "/workspace/example",
            }
        ]
    }
    tools = emitter._extract_structured_tools(request, response)
    assert [(t["kind"], t["call_id"]) for t in tools] == [
        ("call", "call-example-1"),
        ("call", "call-example-2"),
        ("result", "call-example-1"),
    ]
    assert tools[0]["name"] == "terminal"


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
def test_gateway_tool_formats(protocol: str) -> None:
    emitter = ModelGatewayEventEmitter(MagicMock())
    if protocol == "responses":
        request = {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call-example",
                    "output": "done",
                }
            ]
        }
        response = {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-example",
                    "name": "terminal",
                    "arguments": "{}",
                }
            ]
        }
    else:
        request = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-example",
                            "content": "failed",
                            "is_error": True,
                        }
                    ],
                }
            ]
        }
        response = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-example",
                    "name": "terminal",
                    "input": {"command": "pwd"},
                }
            ]
        }
    tools = emitter._extract_structured_tools(request, response)
    assert len(tools) == 2
    assert all(tool["call_id"] == "call-example" for tool in tools)


def test_structured_arguments_use_key_redaction_and_capture_policy() -> None:
    emitter = ModelGatewayEventEmitter(MagicMock())
    response = {
        "output": [
            {
                "type": "function_call",
                "call_id": "call-example",
                "name": "terminal",
                "arguments": '{"api_key":"synthetic-secret","command":"pwd"}',
            }
        ]
    }
    with patch(
        "preloop.services.model_gateway_events.settings.model_gateway_capture_content",
        True,
    ):
        tools = emitter._extract_structured_tools(None, response)
    assert "synthetic-secret" not in json.dumps(tools)
    assert "pwd" in tools[0]["text"]
    with patch(
        "preloop.services.model_gateway_events.settings.model_gateway_capture_content",
        False,
    ):
        tools = emitter._extract_structured_tools(None, response)
    assert tools[0]["redacted"]
    assert "pwd" not in json.dumps(tools)


def test_bounded_history_cannot_displace_current_response() -> None:
    emitter = ModelGatewayEventEmitter(MagicMock())
    history = [
        {
            "type": "function_call",
            "call_id": f"history-{i}",
            "name": "terminal",
            "arguments": "{}",
        }
        for i in range(300)
    ]
    current = {
        "output": [
            {
                "type": "function_call",
                "call_id": "current-example",
                "name": "terminal",
                "arguments": "{}",
            }
        ]
    }
    tools = emitter._extract_structured_tools({"input": history}, current)
    assert len(tools) == 256
    assert tools[0]["call_id"] == "current-example"


def test_global_capture_budget_handles_large_results_and_malformed_shapes() -> None:
    emitter = ModelGatewayEventEmitter(MagicMock())
    malformed = {
        "choices": "unexpected",
        "output": [
            {"type": "function_call", "function": "unexpected", "tool_calls": 1}
        ],
    }
    assert len(emitter._extract_structured_tools(None, malformed)) == 1
    response = {
        "output": [
            {
                "type": "function_call",
                "call_id": "current-example",
                "name": "terminal",
                "arguments": "{}",
            }
        ]
    }
    request = {
        "input": [
            {
                "type": "function_call_output",
                "call_id": f"history-{i}",
                "output": "x" * 40000,
            }
            for i in range(100)
        ]
    }
    tools = emitter._extract_structured_tools(request, response)
    assert len(json.dumps(tools).encode("utf-8")) <= 65536
    assert tools[0]["call_id"] == "current-example"
    assert emitter._tool_metadata_truncated
    assert any(tool["truncated"] for tool in tools)


def test_real_gateway_started_and_completed_share_request_identity(
    db_session, test_user
) -> None:
    """Exercise actual accounting instead of relying only on synthetic event ids."""
    from types import SimpleNamespace
    from preloop.models.crud import crud_ai_model, crud_api_usage
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext
    from preloop.services.openai_gateway import OpenAIGatewayService

    crud_ai_model.create_with_account(
        db=db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Example model",
            "provider_name": "openai",
            "model_identifier": "example-model",
            "api_endpoint": "https://api.example.com/v1",
            "api_key": "synthetic-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "example-model",
                    "provider_adapter": "preloop",
                }
            },
            "is_default": True,
        },
    )
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="synthetic-token", user=test_user)
    )
    response = {
        "id": "response-example",
        "choices": [
            {
                "message": {"role": "assistant", "content": "Example response"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    credentials = SimpleNamespace(
        credential_type="api_key",
        value="synthetic-key",
        backend_type="local_encrypted",
        payload=None,
    )
    secrets = SimpleNamespace(
        resolve_ai_model_credentials=lambda *args, **kwargs: credentials
    )
    with (
        patch(
            "preloop.services.model_runtime_resolver.get_secret_service",
            return_value=secrets,
        ),
        patch(
            "preloop.services.openai_gateway.get_secret_service", return_value=secrets
        ),
        patch.object(service, "_call_litellm", return_value=response),
        patch.object(service, "_check_budget", return_value=None),
        patch(
            "preloop.services.openai_gateway._emit_account_event_nonblocking"
        ) as started,
        patch(
            "preloop.services.model_gateway_events.ModelGatewayEventEmitter.emit_for_usage"
        ),
    ):
        service.create_chat_completion(
            {
                "model": "example-model",
                "messages": [{"role": "user", "content": "Example request"}],
            }
        )
    start = next(
        call.args[0]
        for call in started.call_args_list
        if call.args[0]["type"] == "model_gateway_request_started"
    )
    assert "request" not in start["payload"]
    usage = crud_api_usage.get(db_session, id=service.last_usage_id)
    completion = ModelGatewayEventEmitter(db_session)._build_event(
        usage=usage, request_payload=None, response_payload=response
    )
    assert start["payload"]["request_id"] == completion["payload"]["request_id"]
    assert usage.meta_data["request_id"] == start["payload"]["request_id"]
