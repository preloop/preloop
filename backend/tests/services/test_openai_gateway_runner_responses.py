"""Responses translation through real runner frames, without provider HTTP."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.services import runner_model_proxy
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.openai_responses_passthrough import (
    should_use_responses_passthrough,
)


@pytest.fixture
def runner_gateway(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    account_id = uuid4()
    model = SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        runner_id=uuid4(),
        endpoint_type="runner",
        provider_name="openai",
        model_identifier="example-model",
        api_endpoint="http://localhost:11434/v1",
        model_parameters={"temperature": 0.2, "max_tokens": 512},
        meta_data={},
    )
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="synthetic-token",
            user=SimpleNamespace(id=uuid4(), account_id=account_id),
        ),
        upstream_backend=MagicMock(),
    )
    for name in (
        "_check_budget",
        "_reject_if_gateway_halted",
        "_emit_gateway_request_started",
        "_deliver_operator_notes",
    ):
        monkeypatch.setattr(service, name, MagicMock(return_value=None))
    monkeypatch.setattr(service, "_resolve_requested_model", lambda *a, **kw: model)
    monkeypatch.setattr(
        service,
        "_optimize_request_context",
        lambda **kw: (kw["messages"], kw["payload"]),
    )
    record = MagicMock()
    monkeypatch.setattr(service, "_record_gateway_request", record)
    for name in (
        "_create_openai_responses_passthrough",
        "_open_openai_responses_passthrough_stream",
        "_build_completion_kwargs",
    ):
        monkeypatch.setattr(
            service,
            name,
            MagicMock(
                side_effect=AssertionError("Runner must not use direct provider I/O")
            ),
        )
    nc = SimpleNamespace(
        request=AsyncMock(), publish=AsyncMock(), flush=AsyncMock(), close=AsyncMock()
    )
    monkeypatch.setattr(runner_model_proxy.nats, "connect", AsyncMock(return_value=nc))
    return SimpleNamespace(service=service, model=model, nc=nc, record=record)


def responses_payload() -> dict[str, Any]:
    return {
        "model": "runner/example",
        "instructions": "Review the change.",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Review PR 7"}],
            },
            {
                "type": "function_call",
                "call_id": "call_previous",
                "namespace": "mcp__preloop",
                "name": "get_pull_request",
                "arguments": '{"number":7}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_previous",
                "output": "Example change",
            },
        ],
        "tools": [
            {
                "type": "namespace",
                "name": "mcp__preloop",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_pull_request",
                        "description": "Read a pull request",
                        "parameters": {
                            "type": "object",
                            "properties": {"number": {"type": "integer"}},
                        },
                    }
                ],
            }
        ],
        "tool_choice": {"type": "function", "name": "mcp__preloop__get_pull_request"},
        "max_output_tokens": 128,
        "text": {"format": {"type": "json_object"}},
        "api_key": "must-not-cross",
        "extra_body": {"model": "must-not-override"},
    }


def reply(data: dict[str, Any] | str, *, stream: bool) -> SimpleNamespace:
    encoded = data if isinstance(data, str) else json.dumps(data)
    return SimpleNamespace(
        data=json.dumps(
            {
                "status": 200,
                "data": "data: " + encoded if stream else encoded,
                "done": encoded == "[DONE]" or not stream,
            }
        ).encode()
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["auto", "native", "transcode"])
@pytest.mark.parametrize(
    "tool_name", ["mcp__preloop__get_pull_request", "get_pull_request"]
)
def test_runner_responses_translate_tools_and_usage(
    runner_gateway: SimpleNamespace,
    stream: bool,
    mode: str,
    tool_name: str,
) -> None:
    rig = runner_gateway
    rig.model.meta_data = {"gateway": {"responses_api": mode}}
    usage = {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    function = {"name": tool_name, "arguments": '{"number":7}'}
    if stream:
        rig.nc.request.side_effect = [
            reply(
                {
                    "id": "chatcmpl_example",
                    "choices": [{"index": 0, "delta": {"content": "Checking."}}],
                },
                stream=True,
            ),
            reply(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_next",
                                        "type": "function",
                                        "function": {
                                            "name": tool_name,
                                            "arguments": '{"number":',
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
                stream=True,
            ),
            reply(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": "7}"}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                stream=True,
            ),
            reply({"choices": [], "usage": usage}, stream=True),
            reply("[DONE]", stream=True),
        ]
        events = list(
            rig.service.stream_response({**responses_payload(), "stream": True})
        )
        assert events[-1] == "data: [DONE]\n\n"
        parsed = [json.loads(event.removeprefix("data: ")) for event in events[:-1]]
        assert parsed[0]["type"] == "response.created"
        assert parsed[-1]["type"] == "response.completed"
        response = parsed[-1]["response"]
        added = next(
            event["item"]
            for event in parsed
            if event["type"] == "response.output_item.added"
            and event["item"]["type"] == "function_call"
        )
        assert added["namespace"] == "mcp__preloop"
        assert added["name"] == "get_pull_request"
        deltas = [
            event["delta"]
            for event in parsed
            if event["type"] == "response.function_call_arguments.delta"
        ]
        assert "".join(deltas) == function["arguments"]
        assert response["output_text"] == "Checking."
        assert [
            json.loads(call.args[1])["operation"]
            for call in rig.nc.request.call_args_list
        ] == ["start", "next", "next", "next", "next"]
    else:
        rig.nc.request.return_value = reply(
            {
                "id": "chatcmpl_example",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Checking.",
                            "tool_calls": [
                                {
                                    "id": "call_next",
                                    "type": "function",
                                    "function": function,
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": usage,
            },
            stream=False,
        )
        response = rig.service.create_response(responses_payload())
    call = next(item for item in response["output"] if item["type"] == "function_call")
    assert (call["namespace"], call["name"], call["call_id"], call["arguments"]) == (
        "mcp__preloop",
        "get_pull_request",
        "call_next",
        '{"number":7}',
    )
    assert response["usage"] == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "input_tokens_details": {"cached_tokens": 3},
        "output_tokens_details": {"reasoning_tokens": 2},
    }
    frame = json.loads(rig.nc.request.call_args_list[0].args[1])
    assert frame["type"] == "model_request" and frame["operation"] == "start"
    assert frame["model_id"] == str(rig.model.id)
    body = frame["body"]
    assert body["model"] == "example-model" and body["stream"] is stream
    assert body["messages"][0] == {"role": "system", "content": "Review the change."}
    assert (
        body["messages"][-2]["tool_calls"][0]["function"]["name"]
        == "mcp__preloop__get_pull_request"
    )
    assert body["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_previous",
        "content": "Example change",
    }
    assert body["tools"][0]["function"]["name"] == "mcp__preloop__get_pull_request"
    assert body["tool_choice"] == {
        "type": "function",
        "function": {"name": "mcp__preloop__get_pull_request"},
    }
    assert body["max_tokens"] == 128 and body["temperature"] == 0.2
    assert body["response_format"] == {"type": "json_object"}
    assert (
        not {
            "input",
            "instructions",
            "text",
            "max_output_tokens",
            "api_key",
            "extra_body",
        }
        & body.keys()
    )
    if stream:
        assert body["stream_options"] == {"include_usage": True}
    rig.service.flush_deferred_stream_record()
    recorded = rig.record.call_args.kwargs
    assert recorded["endpoint"] == "/openai/v1/responses"
    assert recorded["status_code"] == 200
    assert recorded["upstream_response"]["usage"]["prompt_tokens"] == 12
    rig.service.upstream_backend.completion.assert_not_called()
    rig.nc.close.assert_awaited_once()
    assert json.loads(rig.nc.publish.call_args.args[1])["operation"] == "cancel"


@pytest.mark.parametrize("mode", ["auto", "native", "transcode"])
def test_runner_never_probes_native_responses(
    runner_gateway: SimpleNamespace, mode: str
) -> None:
    runner_gateway.model.meta_data = {"gateway": {"responses_api": mode}}
    assert not should_use_responses_passthrough(runner_gateway.model)


def test_runner_embeddings_keep_current_refusal(
    runner_gateway: SimpleNamespace,
) -> None:
    with pytest.raises(
        ModelGatewayAPIError, match="Runner models support chat completions only"
    ) as error:
        runner_gateway.service.create_embedding(
            {"model": "runner/example", "input": "Hello"}
        )
    assert error.value.status_code == 400
    runner_gateway.nc.request.assert_not_called()
    runner_gateway.service.upstream_backend.embedding.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_runner_responses_request_limit_precedes_transport(
    runner_gateway: SimpleNamespace, stream: bool
) -> None:
    payload = {
        "model": "runner/example",
        "input": "x" * runner_model_proxy.MAX_REQUEST_BYTES,
    }
    method = (
        runner_gateway.service.stream_response
        if stream
        else runner_gateway.service.create_response
    )
    with pytest.raises(ModelGatewayAPIError) as error:
        method(payload)
    assert error.value.status_code == 413
    runner_gateway.nc.request.assert_not_called()
    runner_gateway.service.upstream_backend.completion.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_runner_responses_offline_fails_closed(
    runner_gateway: SimpleNamespace, stream: bool
) -> None:
    runner_gateway.nc.request.return_value = SimpleNamespace(
        data=b'{"status":503,"error":"runner_offline","done":true}'
    )
    method = (
        runner_gateway.service.stream_response
        if stream
        else runner_gateway.service.create_response
    )
    with pytest.raises(ModelGatewayAPIError) as error:
        method(responses_payload())
    assert error.value.status_code == 503
    assert error.value.code == "runner_offline"
    runner_gateway.service.upstream_backend.completion.assert_not_called()
    runner_gateway.nc.close.assert_awaited()


@pytest.mark.parametrize("failure", ["disconnect", "malformed", "response_limit"])
def test_runner_partial_responses_fail_without_restart(
    runner_gateway: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    rig = runner_gateway
    frames = [
        reply({"choices": [{"index": 0, "delta": {"content": "Partial"}}]}, stream=True)
    ]
    if failure == "malformed":
        frames.append(reply("invalid JSON", stream=True))
    elif failure == "response_limit":
        frames.append(
            reply(
                {"choices": [{"index": 0, "delta": {"content": "x" * 512}}]},
                stream=True,
            )
        )
        monkeypatch.setattr(runner_model_proxy, "MAX_RESPONSE_BYTES", 400)
    else:
        frames.append(
            SimpleNamespace(data=b'{"status":503,"error":"runner_offline","done":true}')
        )
    rig.nc.request.side_effect = frames
    events = list(rig.service.stream_response(responses_payload()))
    assert any('"type": "response.output_text.delta"' in event for event in events)
    assert any('"type": "error"' in event for event in events)
    assert not any('"type": "response.completed"' in event for event in events)
    assert (
        sum(
            json.loads(call.args[1])["operation"] == "start"
            for call in rig.nc.request.call_args_list
        )
        == 1
    )
    rig.service.upstream_backend.completion.assert_not_called()
    rig.nc.close.assert_awaited_once()


def test_runner_responses_client_close_cancels_upstream(
    runner_gateway: SimpleNamespace,
) -> None:
    rig = runner_gateway
    rig.nc.request.return_value = reply(
        {"choices": [{"index": 0, "delta": {"content": "Hello"}}]}, stream=True
    )
    stream = rig.service.stream_response(responses_payload())
    assert "response.created" in next(stream)
    stream.close()
    rig.nc.close.assert_awaited_once()
    assert json.loads(rig.nc.publish.call_args.args[1])["operation"] == "cancel"
    assert rig.nc.request.call_count == 1


@pytest.mark.parametrize("stream", [False, True])
def test_runner_responses_restore_codex_freeform_tools(
    runner_gateway: SimpleNamespace, stream: bool
) -> None:
    rig = runner_gateway
    arguments = json.dumps({"input": "example patch"})
    tool_call = {
        "id": "call_patch",
        "type": "function",
        "function": {"name": "apply_patch", "arguments": arguments},
    }
    if stream:
        rig.nc.request.side_effect = [
            reply(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": [{"index": 0, **tool_call}]},
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                stream=True,
            ),
            reply("[DONE]", stream=True),
        ]
    else:
        rig.nc.request.return_value = reply(
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "tool_calls": [tool_call]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            stream=False,
        )
    payload = {
        "model": "runner/example",
        "input": "Apply an example patch",
        "tools": [
            {"type": "custom", "name": "apply_patch", "format": {"type": "text"}}
        ],
    }
    if stream:
        events = list(rig.service.stream_response(payload))
        response = json.loads(events[-2].removeprefix("data: "))["response"]
    else:
        response = rig.service.create_response(payload)
    item = response["output"][0]
    assert (item["type"], item["name"], item["input"]) == (
        "custom_tool_call",
        "apply_patch",
        "example patch",
    )
    body = json.loads(rig.nc.request.call_args_list[0].args[1])["body"]
    assert body["tools"][0]["type"] == "function"
