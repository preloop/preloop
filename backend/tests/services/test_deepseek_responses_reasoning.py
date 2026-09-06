"""Two-turn DeepSeek Responses regressions with entirely synthetic reasoning."""

import copy
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.utils.encryption import reset_encryption_cache

REASONING = "Synthetic provider continuity value."
FIXTURE = (
    Path(__file__).parents[1] / "fixtures/openai_gateway/codex_deepseek_tool_turn.json"
)


@contextmanager
def gateway(
    *, account: str = "account-1", model_id: str = "model-1", provider: str = "deepseek"
) -> Iterator[tuple[OpenAIGatewayService, Any]]:
    """Keep request conversion real, mocking only upstream and accounting I/O."""
    model = SimpleNamespace(
        id=model_id,
        provider_name=provider,
        model_identifier="deepseek-v4-flash",
        api_endpoint=None,
    )
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="synthetic", user=SimpleNamespace(id="user-1", account_id=account)
        ),
    )
    service._codex_namespace_tool_aliases = {
        "mcp__preloop__read_issue": ("mcp__preloop", "read_issue")
    }
    with ExitStack() as stack:
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
        yield service, model


def upstream_turn(*, include_reasoning: bool = True) -> dict[str, Any]:
    """Recreate the captured namespace/plain-call shape with synthetic fields."""
    items = json.loads(FIXTURE.read_text())["output"]
    calls = []
    for item in items[1:]:
        name = (
            f"{item['namespace']}__{item['name']}"
            if item.get("namespace")
            else item["name"]
        )
        calls.append(
            {
                "id": item["call_id"],
                "type": "function",
                "function": {"name": name, "arguments": item["arguments"]},
            }
        )
    message = {
        "role": "assistant",
        "content": "Inspecting the fixture.",
        "tool_calls": calls,
    }
    if include_reasoning:
        message["reasoning_content"] = REASONING
    return {
        "id": "synthetic-response",
        "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def stream_chunks(turn: dict[str, Any]) -> list[dict[str, Any]]:
    message = turn["choices"][0]["message"]
    chunks = []
    if "reasoning_content" in message:
        chunks += [
            {"choices": [{"delta": {"reasoning_content": part}}]}
            for part in (REASONING[:12], REASONING[12:])
        ]
    chunks.append({"choices": [{"delta": {"content": message["content"]}}]})
    for index, call in enumerate(message["tool_calls"]):
        chunks.append(
            {"choices": [{"delta": {"tool_calls": [{**call, "index": index}]}}]}
        )
    chunks.append(
        {
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": turn["usage"],
        }
    )
    return chunks


def request_with_history(output: list[dict[str, Any]]) -> dict[str, Any]:
    results = [
        {
            "type": "function_call_output",
            "call_id": item["call_id"],
            "output": "synthetic result",
        }
        for item in output
        if item["type"] == "function_call"
    ]
    return {
        "model": "deepseek/deepseek-v4-flash",
        "input": [
            {"role": "user", "content": "Inspect the fixture."},
            *output,
            *results,
        ],
        "include": ["reasoning.encrypted_content"],
    }


def response_from_stream(events: list[str]) -> dict[str, Any]:
    decoded = [
        json.loads(event.split("data: ", 1)[1])
        for event in events
        if "data: [DONE]" not in event
    ]
    return next(
        item["response"] for item in decoded if item.get("type") == "response.completed"
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("second_stream", [False, True])
def test_two_turn_roundtrip_preserves_reasoning_and_namespace_calls(
    stream: bool, second_stream: bool
) -> None:
    with gateway() as (service, _):
        turn = upstream_turn()
        first_payload = {
            "model": "deepseek/deepseek-v4-flash",
            "input": "Inspect fixture",
            "include": ["reasoning.encrypted_content"],
        }
        if stream:
            with patch.object(
                service, "_open_upstream_stream", return_value=iter(stream_chunks(turn))
            ):
                events = list(service.stream_response(first_payload))
            assert REASONING not in "".join(events)
            first = response_from_stream(events)
            done_items = [
                json.loads(event.split("data: ", 1)[1])["item"]
                for event in events
                if '"type": "response.output_item.done"' in event
            ]
            assert any(
                item.get("encrypted_content")
                for item in done_items
                if item["type"] == "reasoning"
            )
        else:
            with patch.object(service, "_call_litellm", return_value=turn):
                first = service.create_response(first_payload)
        reason = next(item for item in first["output"] if item["type"] == "reasoning")
        assert reason["summary"] == [] and reason["encrypted_content"]
        assert REASONING not in json.dumps(first)
        assert any(item.get("namespace") == "mcp__preloop" for item in first["output"])
        reset_encryption_cache()  # A different gateway replica can restore using the same configured key.
        if second_stream:
            with patch.object(
                service,
                "_open_upstream_stream",
                return_value=iter([{"choices": [{"delta": {"content": "Done"}}]}]),
            ) as complete:
                response_from_stream(
                    list(service.stream_response(request_with_history(first["output"])))
                )
        else:
            with patch.object(
                service,
                "_call_litellm",
                return_value={"choices": [{"message": {"content": "Done"}}]},
            ) as complete:
                service.create_response(request_with_history(first["output"]))
        messages = complete.call_args.kwargs["messages"]
        assistants = [item for item in messages if item["role"] == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["reasoning_content"] == REASONING
        assert assistants[0]["content"] == "Inspecting the fixture."
        assert (
            assistants[0]["tool_calls"] == turn["choices"][0]["message"]["tool_calls"]
        )
        assert [
            item["tool_call_id"] for item in messages if item["role"] == "tool"
        ] == [call["id"] for call in assistants[0]["tool_calls"]]


@pytest.mark.parametrize(
    "mutation", ["tamper", "account", "model", "call", "missing_token"]
)
def test_invalid_opaque_reasoning_fails_before_upstream(mutation: str) -> None:
    with gateway() as (service, _):
        with patch.object(service, "_call_litellm", return_value=upstream_turn()):
            first = service.create_response(
                {"model": "deepseek/deepseek-v4-flash", "input": "Inspect"}
            )
    history = request_with_history(copy.deepcopy(first["output"]))
    reasoning = next(
        item for item in history["input"] if item.get("type") == "reasoning"
    )
    if mutation == "tamper":
        reasoning["encrypted_content"] = "invalid-ciphertext"
    elif mutation == "missing_token":
        reasoning.pop("encrypted_content")
    elif mutation == "call":
        for item in history["input"]:
            if item.get("call_id") == "call_fixture_1":
                item["call_id"] = "different-call"
    with gateway(
        account="other" if mutation == "account" else "account-1",
        model_id="other" if mutation == "model" else "model-1",
    ) as (service, _):
        with patch.object(service, "_call_litellm") as complete:
            with pytest.raises(ModelGatewayAPIError, match="Reasoning item"):
                service.create_response(history)
        complete.assert_not_called()


def test_legacy_fallback_is_deepseek_only_and_preserves_supplied_content() -> None:
    output = json.loads(FIXTURE.read_text())["output"]
    output[0]["reasoning_content"] = "Already supplied synthetic content"
    with gateway() as (service, model):
        messages = service._normalize_responses_input(
            request_with_history(output), ai_model=model
        )
        assistants = [item for item in messages if item["role"] == "assistant"]
        assert (
            assistants[0]["reasoning_content"] == "Already supplied synthetic content"
        )
        assert assistants[1]["reasoning_content"] == ""
    with gateway(provider="openai") as (service, model):
        messages = service._normalize_responses_input(
            request_with_history(output), ai_model=model
        )
        assert all("reasoning_content" not in item for item in messages)


def test_missing_provider_reasoning_does_not_invent_an_opaque_item() -> None:
    with gateway() as (service, _):
        with patch.object(
            service,
            "_call_litellm",
            return_value=upstream_turn(include_reasoning=False),
        ):
            result = service.create_response(
                {"model": "deepseek/deepseek-v4-flash", "input": "Inspect"}
            )
        assert all(item["type"] != "reasoning" for item in result["output"])


def test_three_turn_history_preserves_tool_only_and_text_only_reasoning() -> None:
    with gateway() as (service, _):
        first_turn = upstream_turn()
        first_turn["choices"][0]["message"]["content"] = ""
        with patch.object(service, "_call_litellm", return_value=first_turn):
            first = service.create_response(
                {"model": "deepseek/deepseek-v4-flash", "input": "Inspect"}
            )
        second_payload = request_with_history(first["output"])
        second_turn = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Fixture inspected.",
                        "reasoning_content": "Second synthetic continuity value.",
                    }
                }
            ]
        }
        with patch.object(
            service, "_call_litellm", return_value=second_turn
        ) as complete:
            second = service.create_response(second_payload)
        assert (
            next(
                item
                for item in complete.call_args.kwargs["messages"]
                if item.get("tool_calls")
            )["reasoning_content"]
            == REASONING
        )
        third_payload = {
            **second_payload,
            "input": [
                *second_payload["input"],
                *second["output"],
                {"role": "user", "content": "Continue"},
            ],
        }
        with patch.object(
            service,
            "_call_litellm",
            return_value={"choices": [{"message": {"content": "Done"}}]},
        ) as complete:
            service.create_response(third_payload)
        assistants = [
            item
            for item in complete.call_args.kwargs["messages"]
            if item["role"] == "assistant"
        ]
        assert [item["reasoning_content"] for item in assistants] == [
            REASONING,
            "Second synthetic continuity value.",
        ]
        assert [item["content"] for item in assistants] == ["", "Fixture inspected."]


@pytest.mark.parametrize("stream", [False, True])
def test_custom_freeform_call_keeps_reasoning_through_protocol_conversion(
    stream: bool,
) -> None:
    with gateway() as (service, _):
        service._codex_freeform_tool_names = {"apply_patch"}
        turn = upstream_turn()
        turn["choices"][0]["message"].update(
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "patch-call",
                        "type": "function",
                        "function": {
                            "name": "apply_patch",
                            "arguments": '{"input":"synthetic patch"}',
                        },
                    }
                ],
            }
        )
        payload = {
            "model": "deepseek/deepseek-v4-flash",
            "input": "Apply fixture patch",
        }
        if stream:
            with patch.object(
                service, "_open_upstream_stream", return_value=iter(stream_chunks(turn))
            ):
                first = response_from_stream(list(service.stream_response(payload)))
        else:
            with patch.object(service, "_call_litellm", return_value=turn):
                first = service.create_response(payload)
        custom = next(
            item for item in first["output"] if item["type"] == "custom_tool_call"
        )
        assert custom["input"] == "synthetic patch"
        followup = {
            **payload,
            "input": [
                {"role": "user", "content": "Apply fixture patch"},
                *first["output"],
                {
                    "type": "custom_tool_call_output",
                    "call_id": "patch-call",
                    "output": "Applied",
                },
            ],
        }
        with patch.object(
            service,
            "_call_litellm",
            return_value={"choices": [{"message": {"content": "Done"}}]},
        ) as complete:
            service.create_response(followup)
        assistant = next(
            item
            for item in complete.call_args.kwargs["messages"]
            if item.get("tool_calls")
        )
        assert assistant["reasoning_content"] == REASONING
        assert assistant["tool_calls"][0]["function"]["name"] == "apply_patch"
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
            "input": "synthetic patch"
        }


@pytest.mark.parametrize(
    "mutation", ["identifier", "endpoint", "duplicate", "edited_text", "missing_text"]
)
def test_envelope_rejects_changed_model_configuration_or_assistant_turn(
    mutation: str,
) -> None:
    with gateway() as (service, model):
        with patch.object(service, "_call_litellm", return_value=upstream_turn()):
            first = service.create_response(
                {"model": "deepseek/deepseek-v4-flash", "input": "Inspect"}
            )
        payload = request_with_history(first["output"])
        if mutation == "identifier":
            model.model_identifier = "deepseek-v4-pro"
        elif mutation == "endpoint":
            model.api_endpoint = "https://different-provider.example.test/v1"
        elif mutation == "duplicate":
            payload["input"].insert(1, copy.deepcopy(first["output"][0]))
        elif mutation == "edited_text":
            next(item for item in payload["input"] if item.get("type") == "message")[
                "content"
            ][0]["text"] = "Edited history"
        else:
            payload["input"] = [
                item for item in payload["input"] if item.get("type") != "message"
            ]
        with patch.object(service, "_call_litellm") as complete:
            with pytest.raises(ModelGatewayAPIError, match="Reasoning item"):
                service.create_response(payload)
        complete.assert_not_called()


def test_reasoning_envelope_size_is_checked_before_decryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import deepseek_responses_reasoning

    with gateway() as (service, _):
        with patch.object(service, "_call_litellm", return_value=upstream_turn()):
            first = service.create_response(
                {"model": "deepseek/deepseek-v4-flash", "input": "Inspect"}
            )
        monkeypatch.setattr(
            deepseek_responses_reasoning, "MAX_ENCRYPTED_REASONING_CHARS", 10
        )
        with patch.object(deepseek_responses_reasoning, "decrypt_value") as decrypt:
            with pytest.raises(ModelGatewayAPIError, match="Reasoning item"):
                service.create_response(request_with_history(first["output"]))
        decrypt.assert_not_called()


def test_non_deepseek_response_does_not_gain_transcoded_reasoning() -> None:
    with gateway(provider="openai") as (service, _):
        with patch.object(service, "_call_litellm", return_value=upstream_turn()):
            first = service.create_response(
                {"model": "openai/example", "input": "Inspect"}
            )
        assert all(item["type"] != "reasoning" for item in first["output"])


def test_real_completion_argument_builder_keeps_restored_provider_reasoning() -> None:
    with gateway() as (service, model):
        service.upstream_backend = MagicMock()
        with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
            secrets.return_value.resolve_ai_model_credentials.return_value = (
                SimpleNamespace(
                    credential_type="api_key", value="synthetic-provider-key"
                )
            )
            service._call_litellm(
                model,
                messages=[
                    {"role": "assistant", "content": "", "reasoning_content": REASONING}
                ],
                payload={},
                provider="openai",
            )
        assert (
            service.upstream_backend.completion.call_args.kwargs["messages"][0][
                "reasoning_content"
            ]
            == REASONING
        )


@pytest.mark.parametrize("stream", [False, True])
def test_empty_provider_reasoning_does_not_emit_an_opaque_item(stream: bool) -> None:
    with gateway() as (service, _):
        payload = {"model": "deepseek/deepseek-v4-flash", "input": "Inspect"}
        if stream:
            chunks = [
                {"choices": [{"delta": {"reasoning_content": "", "content": "Done"}}]}
            ]
            with patch.object(
                service, "_open_upstream_stream", return_value=iter(chunks)
            ):
                result = response_from_stream(list(service.stream_response(payload)))
        else:
            turn = upstream_turn()
            turn["choices"][0]["message"]["reasoning_content"] = ""
            with patch.object(service, "_call_litellm", return_value=turn):
                result = service.create_response(payload)
        assert all(item["type"] != "reasoning" for item in result["output"])


def test_stream_rejects_oversize_reasoning_before_consuming_more_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import openai_gateway

    monkeypatch.setattr(openai_gateway, "MAX_REASONING_BUFFER_BYTES", 7, raising=False)
    consumed = []

    def chunks() -> Iterator[dict[str, Any]]:
        for index in range(3):
            consumed.append(index)
            yield {"choices": [{"delta": {"reasoning_content": "éé"}}]}

    with gateway() as (service, _):
        with patch.object(service, "_open_upstream_stream", return_value=chunks()):
            events = list(
                service.stream_response(
                    {"model": "deepseek/deepseek-v4-flash", "input": "Inspect"}
                )
            )
    assert consumed == [0, 1]
    assert "reasoning_content_too_large" in "".join(events)
    assert '"type": "response.completed"' not in "".join(events)
