"""Structured tool activity recovered from captured gateway bodies.

A live Hermes session recorded through the compatible gateway leaves nothing
but gateway events: model interactions, gateway calls, a session-start row, and
zero native ``tool_call`` activity rows. Everything a supervisor needs about
the tools ("which one, with what arguments, did it succeed") is inside the raw
request/response bodies as structured wire fields.

These fixtures are synthetic and generic on purpose — placeholder tool names,
``example.com`` targets, invented call ids. Nothing here comes from a real
deployment.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from preloop.config import settings
from preloop.services.gateway_tool_activity import (
    MAX_TOOL_ACTIVITY_ENTRIES,
    normalize_tool_activity,
)
from preloop.services.model_gateway_events import ModelGatewayEventEmitter
from preloop.models.models.api_usage import ApiUsage

from uuid import uuid4


def _by_id(activity, entry_id):
    return [e for e in activity["entries"] if e["id"] == entry_id]


class TestAnthropicDialect:
    """The Hermes-through-gateway reproduction: tool_use + tool_result blocks."""

    def test_call_and_result_are_separate_addressable_entries(self):
        activity = normalize_tool_activity(
            request_payload={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": "README.md\npackage.json",
                            }
                        ],
                    }
                ]
            },
            response_payload={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "list_dir",
                        "input": {"path": "."},
                    }
                ]
            },
            capture_content=True,
        )

        call, result = activity["entries"]
        assert call["direction"] == "call"
        assert call["id"] == "toolu_01"
        assert call["name"] == "list_dir"
        assert '"path": "."' in call["arguments"]
        assert result["direction"] == "result"
        assert result["id"] == "toolu_01"
        assert result["name"] == "list_dir"
        assert "README.md" in result["result"]
        assert activity["dialect"] == "anthropic"

    def test_history_replay_recovers_the_named_call(self):
        """The turn that called the tool left no rows of its own.

        Its whole trace is the accumulated request history of the NEXT model
        call, so both halves have to be recoverable from there.
        """
        activity = normalize_tool_activity(
            request_payload={
                "messages": [
                    {"role": "user", "content": "list the repo"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_42",
                                "name": "list_dir",
                                "input": {"path": "."},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_42",
                                "content": "README.md",
                            }
                        ],
                    },
                ]
            },
            response_payload={"content": [{"type": "text", "text": "Two files."}]},
            capture_content=True,
        )

        assert [e["direction"] for e in activity["entries"]] == ["call", "result"]
        assert all(e["name"] == "list_dir" for e in activity["entries"])

    def test_result_with_no_tool_use_anywhere_stays_unnamed(self):
        """An honest fallback beats inventing the name from prose."""
        activity = normalize_tool_activity(
            request_payload={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_9",
                                "content": "x",
                            }
                        ],
                    }
                ]
            },
            response_payload=None,
            capture_content=True,
        )

        assert activity["entries"][0]["name"] is None
        assert activity["entries"][0]["id"] == "toolu_9"

    def test_error_flag_is_carried_through(self):
        activity = normalize_tool_activity(
            request_payload=None,
            response_payload={
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_err",
                        "content": "command not found",
                        "is_error": True,
                    }
                ]
            },
            capture_content=True,
        )

        assert activity["entries"][0]["is_error"] is True


class TestOpenAIChatDialect:
    def test_parallel_calls_with_identical_arguments_stay_distinct(self):
        """Two `ls` calls in one turn are two calls, not one deduplicated row."""
        activity = normalize_tool_activity(
            request_payload=None,
            response_payload={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {
                                        "name": "terminal",
                                        "arguments": '{"command": "ls"}',
                                    },
                                },
                                {
                                    "id": "call_b",
                                    "type": "function",
                                    "function": {
                                        "name": "terminal",
                                        "arguments": '{"command": "ls"}',
                                    },
                                },
                            ],
                        }
                    }
                ]
            },
            capture_content=True,
        )

        assert len(activity["entries"]) == 2
        assert {e["id"] for e in activity["entries"]} == {"call_a", "call_b"}

    def test_result_is_joined_to_its_call_by_tool_call_id(self):
        activity = normalize_tool_activity(
            request_payload={
                "messages": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_a",
                        "name": "terminal",
                        "content": "README.md",
                    }
                ]
            },
            response_payload={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {
                                        "name": "terminal",
                                        "arguments": '{"command": "ls"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            capture_content=True,
        )

        assert len(_by_id(activity, "call_a")) == 2

    def test_json_string_arguments_are_pretty_printed(self):
        activity = normalize_tool_activity(
            request_payload=None,
            response_payload={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "fetch",
                                        "arguments": '{"url": "https://example.com"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            capture_content=True,
        )

        assert activity["entries"][0]["arguments"] == (
            '{\n  "url": "https://example.com"\n}'
        )

    def test_truncated_json_arguments_fall_back_to_text(self):
        """A cut-off argument string is not valid JSON and must not raise."""
        activity = normalize_tool_activity(
            request_payload=None,
            response_payload={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "write",
                                        "arguments": '{"path": "a.txt", "cont',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            capture_content=True,
        )

        assert activity["entries"][0]["arguments"] == '{"path": "a.txt", "cont'


class TestResponsesDialect:
    def test_function_call_and_output_are_matched_by_call_id(self):
        activity = normalize_tool_activity(
            request_payload={
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "fc_1",
                        "name": "search",
                        "arguments": '{"q": "x"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "fc_1",
                        "output": "3 hits",
                    },
                ]
            },
            response_payload={"output": []},
            capture_content=True,
        )

        assert [e["direction"] for e in activity["entries"]] == ["call", "result"]
        assert activity["entries"][0]["name"] == "search"
        assert activity["entries"][1]["result"] == "3 hits"

    def test_nested_message_output_items_are_scanned(self):
        activity = normalize_tool_activity(
            request_payload=None,
            response_payload={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "hi"},
                            {
                                "type": "function_call",
                                "call_id": "fc_9",
                                "name": "ping",
                                "arguments": "{}",
                            },
                        ],
                    }
                ]
            },
            capture_content=True,
        )

        assert activity["entries"][0]["id"] == "fc_9"


class TestIdentityAndBounds:
    def test_repeated_identical_ids_in_one_exchange_collapse(self):
        """The request half is accumulated history: the same id is not a new call."""
        activity = normalize_tool_activity(
            request_payload={
                "messages": [
                    {"role": "tool", "tool_call_id": "call_a", "content": "one"},
                    {"role": "tool", "tool_call_id": "call_a", "content": "one"},
                ]
            },
            response_payload=None,
            capture_content=True,
        )

        assert len(activity["entries"]) == 1

    def test_missing_provider_id_is_flagged_not_invented(self):
        activity = normalize_tool_activity(
            request_payload=None,
            response_payload={
                "content": [{"type": "tool_use", "name": "ls", "input": {}}]
            },
            capture_content=True,
        )

        entry = activity["entries"][0]
        assert entry["id"] is None
        assert entry["stable_id"] is False

    def test_long_history_is_bounded_and_says_so(self):
        messages = [
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": "terminal",
                "content": "ok",
            }
            for index in range(MAX_TOOL_ACTIVITY_ENTRIES + 25)
        ]
        activity = normalize_tool_activity(
            request_payload={"messages": messages},
            response_payload=None,
            capture_content=True,
        )

        assert len(activity["entries"]) == MAX_TOOL_ACTIVITY_ENTRIES
        assert activity["truncated"] is True

    def test_no_tool_structure_yields_nothing_at_all(self):
        """Most gateway calls are plain completions; they must not grow a field."""
        assert (
            normalize_tool_activity(
                request_payload={"messages": [{"role": "user", "content": "hi"}]},
                response_payload={"choices": [{"message": {"content": "hello"}}]},
                capture_content=True,
            )
            is None
        )

    def test_malformed_bodies_do_not_raise(self):
        activity = normalize_tool_activity(
            request_payload={"messages": [None, 7, {"role": "tool"}, "text"]},
            response_payload={"choices": [{"message": {"tool_calls": "nope"}}]},
            capture_content=True,
        )

        assert activity is None or isinstance(activity["entries"], list)


class TestCapturePolicy:
    def test_capture_disabled_withholds_arguments_and_results(self):
        """Capture policy is a promise about content; structure survives it."""
        activity = normalize_tool_activity(
            request_payload=None,
            response_payload={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "terminal",
                        "input": {"command": "rm -rf /"},
                    }
                ]
            },
            capture_content=False,
        )

        entry = activity["entries"][0]
        assert entry["name"] == "terminal"
        assert entry["id"] == "toolu_1"
        assert entry["arguments"] is None
        assert entry["redacted"] is True

    def test_sanitizer_marker_is_honoured_as_redaction(self):
        """The sanitizer runs first; a marker in the body means content is gone."""
        activity = normalize_tool_activity(
            request_payload={
                "messages": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_a",
                        "name": "terminal",
                        "content": "***REDACTED***",
                    }
                ]
            },
            response_payload=None,
            capture_content=True,
        )

        entry = activity["entries"][0]
        assert entry["result"] is None
        assert entry["redacted"] is True

    def test_secrets_inside_arguments_are_redacted(self):
        """Tool arguments are content too, so the emitter's patterns apply."""
        activity = normalize_tool_activity(
            request_payload=None,
            response_payload={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "terminal",
                        "input": {
                            "command": "curl -H 'Authorization: Bearer sk-secret1234567890'"
                        },
                    }
                ]
            },
            capture_content=True,
            redact_text=ModelGatewayEventEmitter._redact_for_tool_activity,
        )

        arguments = activity["entries"][0]["arguments"]
        assert "sk-secret1234567890" not in arguments
        assert "***REDACTED***" in arguments

    def test_oversized_result_is_truncated_with_a_marker(self):
        activity = normalize_tool_activity(
            request_payload={
                "messages": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_a",
                        "name": "terminal",
                        "content": "x" * 5000,
                    }
                ]
            },
            response_payload=None,
            capture_content=True,
        )

        entry = activity["entries"][0]
        assert entry["truncated"] is True
        assert len(entry["result"]) < 5000
        assert "truncated" in entry["result"]


def _usage(**overrides) -> ApiUsage:
    usage = ApiUsage(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.5,
        user_id=uuid4(),
        account_id=uuid4(),
        api_key_id=None,
        auth_subject_type="api_key",
        ai_model_id=uuid4(),
        flow_id=uuid4(),
        flow_execution_id=uuid4(),
        runtime_session_id=uuid4(),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        estimated_cost=0.0,
        runtime_principal_type="flow_execution",
        runtime_principal_id="exec-1",
        runtime_principal_name="Gateway Flow",
        meta_data={"gateway_request_id": "req_abc123"},
    )
    usage.id = uuid4()
    for key, value in overrides.items():
        setattr(usage, key, value)
    return usage


class TestEmitterIntegration:
    def test_event_carries_normalized_tool_activity(self):
        """Gateway-only history must still yield named tools, not raw JSON."""
        emitter = ModelGatewayEventEmitter(MagicMock())
        with patch.object(settings, "model_gateway_capture_content", True):
            event = emitter._build_event(
                usage=_usage(),
                request_payload=None,
                response_payload={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "terminal",
                                            "arguments": '{"command": "pytest -q"}',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        activity = event["payload"]["tool_activity"]
        assert activity["entries"][0]["name"] == "terminal"
        assert "pytest -q" in activity["entries"][0]["arguments"]

    def test_event_without_tools_omits_the_field(self):
        emitter = ModelGatewayEventEmitter(MagicMock())
        with patch.object(settings, "model_gateway_capture_content", True):
            event = emitter._build_event(
                usage=_usage(),
                request_payload={"messages": [{"role": "user", "content": "hi"}]},
                response_payload={"output_text": "hello"},
            )

        assert event["payload"]["tool_activity"] is None

    def test_correlation_id_travels_with_the_usage_row(self):
        """The completion must name the same id the started event published."""
        emitter = ModelGatewayEventEmitter(MagicMock())
        with patch.object(settings, "model_gateway_capture_content", True):
            event = emitter._build_event(
                usage=_usage(),
                request_payload=None,
                response_payload={"output_text": "ok"},
            )

        assert event["payload"]["gateway_request_id"] == "req_abc123"

    def test_correlation_id_is_absent_for_older_rows(self):
        """No id must read as "unknown", never as a match with something else."""
        emitter = ModelGatewayEventEmitter(MagicMock())
        with patch.object(settings, "model_gateway_capture_content", True):
            event = emitter._build_event(
                usage=_usage(meta_data={}),
                request_payload=None,
                response_payload={"output_text": "ok"},
            )

        assert event["payload"]["gateway_request_id"] is None
