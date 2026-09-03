"""Tests for model gateway event capture and redaction behavior."""

from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_gateway_events import ModelGatewayEventEmitter
from uuid import uuid4

from preloop.config import settings
from preloop.models.models.api_usage import ApiUsage


def test_capture_enabled_keeps_content_but_redacts_sensitive_text():
    emitter = ModelGatewayEventEmitter(MagicMock())
    text = (
        "Authorization: Bearer super-secret-token\n"
        "OPENAI_API_KEY=sk-testsecret1234567890\n"
        "Normal review content stays visible."
    )

    with patch("preloop.services.model_gateway_events.settings") as mock_settings:
        mock_settings.model_gateway_capture_content = True
        mock_settings.model_gateway_max_preview_chars = 1000

        sanitized_text, metadata = emitter._sanitize_text_with_meta(text)

    assert isinstance(sanitized_text, str)
    assert "Normal review content stays visible." in sanitized_text
    assert "Authorization: Bearer ***REDACTED***" in sanitized_text
    assert "OPENAI_API_KEY=***REDACTED***" in sanitized_text
    assert metadata["redacted"] is True
    assert metadata["truncated"] is False


def test_capture_disabled_redacts_entire_preview_text():
    emitter = ModelGatewayEventEmitter(MagicMock())
    text = "Review this merge request."

    with patch("preloop.services.model_gateway_events.settings") as mock_settings:
        mock_settings.model_gateway_capture_content = False
        mock_settings.model_gateway_max_preview_chars = 1000

        sanitized_text, metadata = emitter._sanitize_text_with_meta(text)

    assert sanitized_text == {"redacted": True, "length": len(text)}
    assert metadata == {"redacted": True, "truncated": False, "length": len(text)}


def _build_usage(**overrides) -> ApiUsage:
    usage = ApiUsage(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.125,
        user_id=uuid4(),
        account_id=uuid4(),
        api_key_id=uuid4(),
        auth_subject_type="api_key",
        ai_model_id=uuid4(),
        flow_id=uuid4(),
        flow_execution_id=uuid4(),
        runtime_session_id=uuid4(),
        model_alias="openai/gpt-5",
        provider_name="openai",
        upstream_request_id="req_123",
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
        estimated_cost=0.00025,
        runtime_principal_type="flow_execution",
        runtime_principal_id="exec-123",
        runtime_principal_name="Gateway Flow",
        meta_data={
            "endpoint_kind": "responses",
            "requested_model": "openai/gpt-5",
            "gateway_provider": "preloop",
            "finish_reason": "stop",
            "budget": {
                "soft_limit_exceeded": False,
                "hard_limit_exceeded": False,
                "estimated_request_cost_usd": 0.00025,
            },
            "error_detail": None,
        },
    )
    usage.id = uuid4()
    for key, value in overrides.items():
        setattr(usage, key, value)
    return usage


def test_build_event_prefers_api_usage_cache_columns_over_stale_meta():
    """Activity payload cache tokens must come from ApiUsage columns first."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage(
        cache_read_tokens=1200,
        cache_creation_tokens=3400,
        meta_data={
            "endpoint_kind": "responses",
            "usage_details": {
                "cache_read_input_tokens": 1,
                "cache_creation_input_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 1},
            },
            "budget": {"soft_limit_exceeded": False},
            "error_detail": None,
        },
    )
    with patch.object(settings, "model_gateway_capture_content", False):
        event = emitter._build_event(
            usage=usage,
            request_payload={"messages": [{"role": "user", "content": "hi"}]},
            response_payload={"output_text": "ok"},
        )
    payload = event["payload"]
    assert payload["cache_read_input_tokens"] == 1200
    assert payload["cache_creation_input_tokens"] == 3400


def test_build_event_includes_budget_runtime_principal_and_redacted_payloads():
    """Captured payload previews should keep structure while redacting secrets."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()

    with (
        patch.object(settings, "model_gateway_capture_content", True),
        patch.object(settings, "model_gateway_max_preview_chars", 10),
    ):
        event = emitter._build_event(
            usage=usage,
            request_payload={
                "model": "openai/gpt-5",
                "instructions": "12345678901",
                "input": "abcdefghijk",
                "messages": [{"role": "user", "content": "hello world"}],
                "api_key": "sk-secret",
                "nested": {
                    "authorization": "Bearer secret",
                    "session_token": "tok-123",
                    "text": "qrstuvwxyz123",
                },
            },
            response_payload={
                "output_text": "ABCDEFGHIJK",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "LMNOPQRSTUV"}],
                    }
                ],
                "tool_token": "secret-tool-token",
            },
        )

    payload = event["payload"]
    assert event["type"] == "model_gateway_call"
    assert payload["outcome"] == "success"
    assert payload["budget"] == usage.meta_data["budget"]
    assert event["runtime_session_id"] == str(usage.runtime_session_id)
    assert payload["runtime_session_id"] == str(usage.runtime_session_id)
    assert payload["runtime_principal"] == {
        "type": "flow_execution",
        "id": "exec-123",
        "name": "Gateway Flow",
    }
    assert payload["request"]["api_key"] == "***REDACTED***"
    assert payload["request"]["nested"]["authorization"] == "***REDACTED***"
    assert payload["request"]["nested"]["session_token"] == "***REDACTED***"
    assert payload["response"]["tool_token"] == "***REDACTED***"
    assert payload["request"]["instructions"] == "12345678901"
    assert payload["request"]["messages"][0]["content"] == "hello world"
    assert payload["request"]["nested"]["text"] == "qrstuvwxyz123"
    assert payload["response"]["output_text"] == "ABCDEFGHIJK"
    assert payload["response"]["output"][0]["content"][0]["text"] == "LMNOPQRSTUV"
    assert payload["capture_policy"] == {
        "content_capture_enabled": True,
        "max_preview_chars": 10,
        "sensitive_fields_redacted": True,
        "content_redacted": False,
        "content_truncated": True,
        "conversation_preview_available": True,
    }
    assert payload["conversation_preview"]["metadata"] == {
        "message_count": 4,
        "request_message_count": 3,
        "response_message_count": 1,
        "has_redacted_content": False,
        "has_truncated_content": True,
    }
    assert payload["conversation_preview"]["messages"] == [
        {
            "source": "request",
            "role": "system",
            "text": "1234567890... [truncated]",
            "redacted": False,
            "truncated": True,
            "original_length": 11,
        },
        {
            "source": "request",
            "role": "user",
            "text": "abcdefghij... [truncated]",
            "redacted": False,
            "truncated": True,
            "original_length": 11,
        },
        {
            "source": "request",
            "role": "user",
            "text": "hello worl... [truncated]",
            "redacted": False,
            "truncated": True,
            "original_length": 11,
        },
        {
            "source": "response",
            "role": "assistant",
            "text": "LMNOPQRSTU... [truncated]",
            "redacted": False,
            "truncated": True,
            "original_length": 11,
        },
    ]


def test_build_event_redacts_content_by_default():
    """Content-bearing fields should be redacted when capture is disabled."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()

    with patch.object(settings, "model_gateway_capture_content", False):
        event = emitter._build_event(
            usage=usage,
            request_payload={
                "input": "prompt",
                "messages": [{"role": "user", "content": "hey"}],
            },
            response_payload={
                "output_text": "answer",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "nested"}],
                    }
                ],
            },
        )

    payload = event["payload"]
    assert payload["request"]["input"] == "***REDACTED***"
    assert payload["request"]["messages"][0]["content"] == "***REDACTED***"
    assert payload["response"]["output_text"] == "***REDACTED***"
    assert payload["response"]["output"][0]["content"][0]["text"] == "***REDACTED***"
    assert payload["capture_policy"] == {
        "content_capture_enabled": False,
        "max_preview_chars": settings.model_gateway_max_preview_chars,
        "sensitive_fields_redacted": True,
        "content_redacted": True,
        "content_truncated": False,
        "conversation_preview_available": True,
    }
    assert payload["conversation_preview"]["metadata"] == {
        "message_count": 3,
        "request_message_count": 2,
        "response_message_count": 1,
        "has_redacted_content": True,
        "has_truncated_content": False,
    }
    assert payload["conversation_preview"]["messages"] == [
        {
            "source": "request",
            "role": "user",
            "text": None,
            "redacted": True,
            "truncated": False,
            "original_length": 6,
        },
        {
            "source": "request",
            "role": "user",
            "text": None,
            "redacted": True,
            "truncated": False,
            "original_length": 3,
        },
        {
            "source": "response",
            "role": "assistant",
            "text": None,
            "redacted": True,
            "truncated": False,
            "original_length": 6,
        },
    ]


def test_build_event_extracts_conversation_preview_for_openai_responses_payloads():
    """Responses-style payloads should normalize into a neutral conversation preview."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()

    with (
        patch.object(settings, "model_gateway_capture_content", True),
        patch.object(settings, "model_gateway_max_preview_chars", 50),
    ):
        event = emitter._build_event(
            usage=usage,
            request_payload={
                "model": "openai/gpt-5",
                "instructions": "Be concise",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Summarize issue #12"}
                        ],
                    }
                ],
            },
            response_payload={
                "id": "resp_123",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Issue #12 is ready to ship.",
                            }
                        ],
                    }
                ],
                "output_text": "Issue #12 is ready to ship.",
            },
        )

    assert event["payload"]["conversation_preview"]["messages"] == [
        {
            "source": "request",
            "role": "system",
            "text": "Be concise",
            "redacted": False,
            "truncated": False,
            "original_length": 10,
        },
        {
            "source": "request",
            "role": "user",
            "text": "Summarize issue #12",
            "redacted": False,
            "truncated": False,
            "original_length": 19,
        },
        {
            "source": "response",
            "role": "assistant",
            "text": "Issue #12 is ready to ship.",
            "redacted": False,
            "truncated": False,
            "original_length": 27,
        },
    ]


def test_build_event_extracts_conversation_preview_for_anthropic_messages():
    """Anthropic messages payloads should preserve roles while honoring redaction policy."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()

    with (
        patch.object(settings, "model_gateway_capture_content", False),
        patch.object(settings, "model_gateway_max_preview_chars", 50),
    ):
        event = emitter._build_event(
            usage=usage,
            request_payload={
                "model": "anthropic/claude-sonnet-4-5",
                "system": [{"type": "text", "text": "You are a careful reviewer"}],
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Review this diff"}],
                    }
                ],
            },
            response_payload={
                "role": "assistant",
                "content": [{"type": "text", "text": "Looks good overall"}],
            },
        )

    assert event["payload"]["conversation_preview"]["messages"] == [
        {
            "source": "request",
            "role": "system",
            "text": None,
            "redacted": True,
            "truncated": False,
            "original_length": 26,
        },
        {
            "source": "request",
            "role": "user",
            "text": None,
            "redacted": True,
            "truncated": False,
            "original_length": 16,
        },
        {
            "source": "response",
            "role": "assistant",
            "text": None,
            "redacted": True,
            "truncated": False,
            "original_length": 18,
        },
    ]
    assert event["payload"]["capture_policy"]["content_redacted"] is True


def test_build_event_marks_budget_denied_outcome():
    """Budget-enforcement failures should emit the budget_denied outcome."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage(
        status_code=403,
        meta_data={
            "endpoint_kind": "responses",
            "requested_model": "openai/gpt-5",
            "gateway_provider": "preloop",
            "budget": {"hard_limit_exceeded": True},
            "error_detail": "Model gateway budget exceeded: flow monthly limit reached",
        },
    )

    event = emitter._build_event(
        usage=usage,
        request_payload={"input": "prompt"},
        response_payload=None,
    )

    assert event["payload"]["outcome"] == "budget_denied"


def test_emit_for_usage_does_not_require_running_event_loop():
    """Emission should still append logs when called outside an event loop."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()

    with (
        patch(
            "preloop.services.model_gateway_events.crud_flow_execution.append_log"
        ) as mock_append_log,
        patch(
            "preloop.services.model_gateway_events.asyncio.get_running_loop",
            side_effect=RuntimeError,
        ),
    ):
        emitter.emit_for_usage(
            usage=usage,
            request_payload={"input": "prompt"},
            response_payload={"output_text": "answer"},
        )

    mock_append_log.assert_called_once()
    assert mock_append_log.call_args.args[1] == str(usage.flow_execution_id)
    assert mock_append_log.call_args.args[2]["type"] == "model_gateway_call"


@pytest.mark.asyncio
async def test_publish_to_nats_drops_event_when_truncated_payload_is_still_too_large():
    """Gateway events should not publish oversized NATS payloads after truncation."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    event = {
        "execution_id": "execution-1",
        "payload": {
            "already_minimal_but_huge": "x" * 1_000_001,
        },
    }
    nats_client = MagicMock()
    nats_client.is_connected = True

    with patch(
        "preloop.services.model_gateway_events.get_nats_client",
        return_value=nats_client,
    ):
        await emitter._publish_to_nats(event)

    nats_client.publish.assert_not_called()


def test_build_event_strips_nul_and_control_bytes_from_bodies():
    """Regression: gzip/binary bodies must not reach a JSONB column.

    Incident 2026-08-05: an agent fetched a URL that returned gzip, the body
    landed in runtime_session_activity.metadata, and Postgres rejected the NUL
    with UntranslatableCharacter, poisoning the request's session.
    """
    import json

    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()
    gzip_magic = "\x1f\x8b\x08\x00"

    with (
        patch.object(settings, "model_gateway_capture_content", True),
        patch.object(settings, "model_gateway_max_preview_chars", 100000),
        patch.object(settings, "model_gateway_activity_max_body_chars", 100000),
    ):
        event = emitter._build_event(
            usage=usage,
            request_payload={"messages": [{"role": "user", "content": "fetch it"}]},
            response_payload={"body": gzip_magic + "binary\x00payload"},
        )

    payload = event["payload"]
    assert payload["response"]["body"] == "binarypayload"
    # The captured bodies must survive the same encode path psycopg2 uses.
    # (api_key_name is a MagicMock attribute here, so encode only the bodies.)
    encoded = json.dumps(
        {"request": payload["request"], "response": payload["response"]}
    )
    assert "\x00" not in encoded
    assert "\u0000" not in encoded


def test_build_event_caps_activity_bodies_with_an_explicit_marker():
    """533KB activity rows are a DB bloat problem independent of encoding."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()

    with (
        patch.object(settings, "model_gateway_capture_content", True),
        patch.object(settings, "model_gateway_max_preview_chars", 100000),
        patch.object(settings, "model_gateway_activity_max_body_chars", 50),
    ):
        event = emitter._build_event(
            usage=usage,
            request_payload={"messages": [{"role": "user", "content": "hi"}]},
            response_payload={"body": "z" * 500},
        )

    body = event["payload"]["response"]["body"]
    assert body.startswith("z" * 50)
    assert body.endswith("... [truncated 450 bytes]")


def test_build_event_preserves_newlines_and_tabs_in_bodies():
    """Transcripts must keep their shape; only invisible controls are dropped."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()
    text = "def f():\n\treturn 1\r\n"

    with (
        patch.object(settings, "model_gateway_capture_content", True),
        patch.object(settings, "model_gateway_max_preview_chars", 100000),
        patch.object(settings, "model_gateway_activity_max_body_chars", 100000),
    ):
        event = emitter._build_event(
            usage=usage,
            request_payload={"messages": [{"role": "user", "content": "hi"}]},
            response_payload={"body": text},
        )

    assert event["payload"]["response"]["body"] == text


def test_build_event_surfaces_upstream_retry_count():
    """A call the gateway had to retry must say so on the execution timeline.

    Without this the only visible symptom of a flaky provider is a run that
    took longer than usual, which is indistinguishable from a slow model.
    """
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage(
        meta_data={
            "endpoint_kind": "responses",
            "upstream_retries": 2,
            "error_detail": None,
        }
    )
    with patch.object(settings, "model_gateway_capture_content", False):
        event = emitter._build_event(
            usage=usage, request_payload=None, response_payload=None
        )
    assert event["payload"]["retried"] == 2


def test_build_event_reports_zero_retries_for_a_clean_call():
    """`retried` is always present so consumers never special-case its absence."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()
    with patch.object(settings, "model_gateway_capture_content", False):
        event = emitter._build_event(
            usage=usage, request_payload=None, response_payload=None
        )
    assert event["payload"]["retried"] == 0
