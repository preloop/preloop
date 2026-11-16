"""A Codex review request through an OpenRouter chat upstream keeps its tools.

The request is shaped like a native-result Codex review: Responses tools
(``exec_command`` among them), ``tool_choice: auto`` and the result schema as
``text.format``. The upstream is a stub: its first turn is a short assistant
message with ``finish_reason: stop``, its second a streamed tool call.
"""

import json
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService

SCHEMA = {
    "type": "object",
    "properties": {"review": {"type": "string"}, "base_sha": {"type": "string"}},
    "required": ["review", "base_sha"],
    "additionalProperties": False,
}

TOOLS = [
    {
        "type": "function",
        "name": "exec_command",
        "description": "Run a shell command.",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "write_stdin",
        "description": "Write to a running command.",
        "parameters": {"type": "object", "properties": {}},
        "strict": False,
    },
]


def _payload() -> dict[str, Any]:
    return {
        "model": "deepseek-v4-flash",
        "input": [{"role": "user", "content": "Review the checked-out change."}],
        "tools": TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "codex_output_schema",
                "schema": SCHEMA,
                "strict": True,
            }
        },
        "stream": True,
    }


def _service() -> tuple[OpenAIGatewayService, ExitStack]:
    model = SimpleNamespace(
        id="model-1",
        provider_name="openrouter",
        model_identifier="deepseek/deepseek-v4-flash",
        api_endpoint=None,
    )
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="synthetic", user=SimpleNamespace(id="u", account_id="a")
        ),
    )
    stack = ExitStack()
    stack.enter_context(
        patch.object(service, "_resolve_requested_model", return_value=model)
    )
    for method in (
        "_check_budget",
        "_emit_gateway_request_started",
        "_record_gateway_request",
        "_defer_stream_record",
        "_finish_stream_generator",
    ):
        stack.enter_context(patch.object(service, method, return_value=None))
    stack.enter_context(
        patch.object(service, "_is_openai_codex_model", return_value=False)
    )
    stack.enter_context(
        patch(
            "preloop.services.openai_gateway.should_use_responses_passthrough",
            return_value=False,
        )
    )
    return service, stack


def _events(stream: Any) -> list[dict[str, Any]]:
    return [
        json.loads(event.split("data: ", 1)[1])
        for event in stream
        if "data: " in event and "[DONE]" not in event
    ]


def test_tools_tool_choice_and_schema_reach_the_chat_upstream() -> None:
    service, _ = _service()
    kwargs = service._normalize_completion_parameters(_payload(), provider="openai")
    names = [tool["function"]["name"] for tool in kwargs["tools"]]
    assert names == ["exec_command", "write_stdin"]
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["function"]["parameters"]["required"] == ["cmd"]
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["schema"] == SCHEMA


def test_stop_turn_then_tool_call_turn_render_as_responses_items() -> None:
    service, stack = _service()
    stop_turn = [
        {"choices": [{"delta": {"content": "Starting by locating the checkout."}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "exec_command", "arguments": '{"cmd":"git log -1"}'},
    }
    tool_turn = [
        {"choices": [{"delta": {"tool_calls": [{**call, "index": 0}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    with stack:
        with patch.object(
            service, "_open_upstream_stream", side_effect=[iter(stop_turn)]
        ) as upstream:
            first = _events(service.stream_response(_payload()))
        sent = upstream.call_args.kwargs["payload"]
        assert sent["tools"] == TOOLS and sent["tool_choice"] == "auto"
        done = next(e for e in first if e["type"] == "response.completed")
        assert done["response"]["status"] == "completed"
        assert [item["type"] for item in done["response"]["output"]] == ["message"]

        with patch.object(
            service, "_open_upstream_stream", side_effect=[iter(tool_turn)]
        ):
            second = _events(service.stream_response(_payload()))
    done = next(e for e in second if e["type"] == "response.completed")
    calls = [i for i in done["response"]["output"] if i["type"] == "function_call"]
    assert [(c["name"], c["call_id"]) for c in calls] == [("exec_command", "call_1")]
    assert json.loads(calls[0]["arguments"]) == {"cmd": "git log -1"}
