"""Tests for model gateway event capture and redaction behavior."""

import json
from unittest.mock import MagicMock, patch


from preloop.services.gateway_activity_body import payload_bytes
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
    db = MagicMock()
    db.get.return_value = None
    emitter = ModelGatewayEventEmitter(db)
    usage = _build_usage()

    with (
        patch(
            "preloop.services.model_gateway_events.crud_flow_execution.append_log"
        ) as mock_append_log,
        patch("preloop.services.model_gateway_events.publish_realtime") as publish,
        patch(
            "preloop.services.execution_realtime.stamp_execution_event",
            side_effect=lambda e: {**e, "seq": 1},
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
    assert publish.call_args_list[0].args[0] == (
        f"flow-updates.{usage.flow_execution_id}"
    )


def test_publish_to_nats_drops_event_when_truncated_payload_is_still_too_large():
    """Gateway events should not publish oversized NATS payloads after truncation."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    event = {
        "execution_id": "execution-1",
        "payload": {
            "already_minimal_but_huge": "x" * 1_000_001,
        },
    }
    with (
        patch("preloop.services.model_gateway_events.publish_realtime") as publish,
        patch(
            "preloop.services.execution_realtime.stamp_execution_event",
            side_effect=lambda e: {**e, "seq": 1},
        ),
    ):
        emitter._publish_to_nats(event)

    publish.assert_not_called()


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


def test_derive_outcome_treats_allowlist_denial_as_budget_denied():
    """No policy_denied vocabulary exists, so allowlist denials stay budget_denied."""
    detail = (
        "Model 'vendor/alpha-chat' is not in this agent's allowed models "
        "(Alpha Chat). Edit the agent's governance in the Preloop console or "
        "pick an allowed model."
    )
    assert ModelGatewayEventEmitter._derive_outcome(403, detail) == "budget_denied"


def test_emit_budget_webhooks_commits_before_a_later_caller_rollback():
    """Gateway rollbacks must not discard a budget.threshold / budget.exceeded row.

    ``emit_budget_event`` inserts into the caller's session. The gateway can
    roll that session back (or skip ``touch_activity(commit=True)``), so the
    emitter commits after a successful enqueue.
    """
    db = MagicMock()
    emitter = ModelGatewayEventEmitter(db)
    usage = _build_usage()
    queued = MagicMock()
    queued.delivery_ids = [uuid4()]

    with patch(
        "preloop.services.event_webhooks.emitters.emit_budget_event",
        return_value=queued,
    ) as emit:
        emitter._emit_budget_webhooks(
            usage,
            {
                "hard_limit_exceeded": True,
                "enforcement_reason": "account_hard_limit",
                "account_limit_usd": 10.0,
                "account_current_spend_usd": 12.5,
            },
        )
        db.rollback()

    emit.assert_called_once()
    db.commit.assert_called_once()
    names = [call[0] for call in db.method_calls]
    assert names.index("commit") < names.index("rollback")


def test_event_preserves_captured_resolved_billing_path() -> None:
    """Alias naming never substitutes for the resolved request's billing source."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage(
        meta_data={
            "billing_path": "your_key",
            "billing_model_id": "own-model",
            "billing_model_name": "Own example",
            "requested_model": "hosted-looking-alias",
        }
    )
    event = emitter._build_event(usage=usage, request_payload={}, response_payload={})
    assert event["payload"]["billing_path"] == "your_key"
    assert event["payload"]["billing_model_id"] == "own-model"
    assert event["payload"]["billing_model_name"] == "Own example"


def test_derive_outcome_treats_budget_429_as_budget_denied():
    """The budget denial is a 429 since #1447; policy stays 403 only."""
    budget = "Model gateway budget exceeded: account monthly limit reached"
    hosted = "Preloop trial limit for hosted model reached. Configure a key."
    assert ModelGatewayEventEmitter._derive_outcome(429, budget) == "budget_denied"
    assert ModelGatewayEventEmitter._derive_outcome(429, hosted) == "budget_denied"
    assert ModelGatewayEventEmitter._derive_outcome(403, budget) == "budget_denied"
    assert ModelGatewayEventEmitter._derive_outcome(429, "slow down") == "error"


def test_emit_for_usage_stores_slim_row_but_publishes_full_event():
    """The activity row keeps only this call's messages; the live event all."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage(flow_execution_id=None, flow_id=None)
    history = []
    for turn in range(40):
        history.append({"role": "user", "content": f"question {turn} " + "q" * 2000})
        history.append({"role": "assistant", "content": f"answer {turn}"})
    history.append({"role": "user", "content": "latest question"})

    with (
        patch(
            "preloop.services.model_gateway_events.crud_runtime_session_activity"
            ".log_model_gateway_call"
        ) as mock_log,
        patch("preloop.services.model_gateway_events.emit_account_event") as live,
        patch.object(settings, "model_gateway_capture_content", True),
    ):
        emitter.emit_for_usage(
            usage=usage,
            request_payload={"model": "m", "messages": history},
            response_payload={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]
            },
        )

    stored = mock_log.call_args.kwargs["metadata"]
    assert stored["request"]["messages"] == [
        {"role": "user", "content": "latest question"}
    ]
    texts = [m["text"] for m in stored["conversation_preview"]["messages"]]
    assert texts == ["latest question", "ok"]
    assert stored["total_tokens"] == 18
    assert stored["body_compaction"]["original_bytes"] > 80_000
    assert stored["body_compaction"]["stored_bytes"] < 4096
    live_payload = live.call_args_list[0].args[0]
    assert "body_compaction" not in str(live_payload)


def test_emit_for_usage_stores_slim_flow_log_but_publishes_full_event():
    """The flow log row keeps only this call's messages; the live event all."""
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage()
    history = []
    for turn in range(40):
        history.append({"role": "user", "content": f"question {turn} " + "q" * 2000})
        history.append({"role": "assistant", "content": f"answer {turn}"})
    history.append({"role": "user", "content": "latest question"})
    published: list[dict] = []

    def capture(event: dict) -> None:
        published.append(event)

    with (
        patch(
            "preloop.services.model_gateway_events.crud_flow_execution.append_log"
        ) as mock_append_log,
        patch.object(emitter, "_publish_to_nats", side_effect=capture),
        patch.object(settings, "model_gateway_capture_content", True),
        patch.object(settings, "model_gateway_activity_max_stored_bytes", 32768),
    ):
        emitter.emit_for_usage(
            usage=usage,
            request_payload={"model": "m", "messages": history},
            response_payload={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]
            },
        )

    stored = mock_append_log.call_args.args[2]
    assert stored["type"] == "model_gateway_call"
    payload = stored["payload"]
    assert payload["body_compaction"]["version"] == 1
    assert payload_bytes(payload) <= 32768
    stored_texts = [m.get("content") for m in payload["request"]["messages"]]
    assert stored_texts == ["latest question"]
    # The token and cost accounting stays on the stored row.
    assert payload.get("outcome") == published[0]["payload"].get("outcome")
    # The live event still carries the whole replayed conversation.
    assert len(published) == 1
    assert len(published[0]["payload"]["request"]["messages"]) == len(history)


def test_event_keeps_safe_upstream_diagnostics_when_content_capture_is_disabled() -> (
    None
):
    emitter = ModelGatewayEventEmitter(MagicMock())
    usage = _build_usage(
        meta_data={
            "upstream_parameters": {"max_tokens": 8192, "reasoning_effort": "low"},
            "usage_warnings": ["reasoning_tokens_exceed_output_tokens"],
        }
    )
    with patch.object(settings, "model_gateway_capture_content", False):
        payload = emitter._build_event(
            usage=usage, request_payload=None, response_payload=None
        )["payload"]
    assert payload["upstream_parameters"] == {
        "max_tokens": 8192,
        "reasoning_effort": "low",
    }
    assert payload["usage_warnings"] == ["reasoning_tokens_exceed_output_tokens"]


def test_tool_evidence_capture_survives_execution_sequence_stamping() -> None:
    """Raw tool evidence and the sequenced live copy keep their distinct limits."""
    db = MagicMock()
    db.get.return_value = None
    emitter = ModelGatewayEventEmitter(db)
    usage = _build_usage()
    arguments = json.dumps({"command": "echo " + "é" * 50000}, ensure_ascii=False)
    response = {
        "output": [
            {
                "type": "function_call",
                "call_id": "integration-call",
                "name": "exec_command",
                "arguments": arguments,
            }
        ]
    }
    with (
        patch.object(settings, "model_gateway_capture_content", True),
        patch(
            "preloop.models.crud.execution_tool_evidence.persist_tool_evidence"
        ) as save,
        patch("preloop.services.model_gateway_events.crud_flow_execution.append_log"),
        patch("preloop.services.model_gateway_events.emit_account_event"),
        patch("preloop.services.model_gateway_events.publish_realtime") as publish,
        patch("preloop.services.execution_realtime.run_db_sync", return_value=(17, 3)),
    ):
        emitter.emit_for_usage(
            usage=usage, request_payload={}, response_payload=response
        )
    document = save.call_args.kwargs["document"]
    assert document["capture_complete"] is True
    assert document["observations"][0]["value"] == arguments
    published = json.loads(publish.call_args.args[1])
    assert published["seq"] == 17
    assert published["lifecycle_seq"] == 3
    assert published["sequence_domain"] == "execution"
    assert published["execution_id"] == str(usage.flow_execution_id)
