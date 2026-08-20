"""Tests for the Anthropic-compatible gateway service."""

from unittest.mock import patch

import pytest

from preloop.models.crud import (
    crud_ai_model,
    crud_api_key,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService


def test_create_message_returns_anthropic_shape(db_session, test_user):
    """Anthropic messages requests should return Anthropic-shaped responses."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Gateway Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "anthropic/claude-sonnet-4-5",
                    "provider_adapter": "preloop",
                },
                "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "msg_123",
            "created": 1710000000,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello from Claude gateway",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        },
    ) as mock_completion:
        payload = service.create_message(
            {
                "model": "anthropic/claude-sonnet-4-5",
                "system": "Be concise",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 256,
            }
        )

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["model"] == "anthropic/claude-sonnet-4-5"
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][1]["role"] == "user"
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert payload["content"][0]["type"] == "text"
    assert payload["content"][0]["text"] == "Hello from Claude gateway"
    assert payload["stop_reason"] == "end_turn"
    assert payload["usage"]["input_tokens"] == 5
    assert payload["usage"]["output_tokens"] == 7

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.model_alias == "anthropic/claude-sonnet-4-5"
    assert usage_row.provider_name == "anthropic"
    assert usage_row.prompt_tokens == 5
    assert usage_row.completion_tokens == 7


def test_stream_message_emits_anthropic_events_and_records_usage(db_session, test_user):
    """Anthropic streaming should emit SSE events and persist usage/event facts."""
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Gateway Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "anthropic/claude-sonnet-4-5",
                    "provider_adapter": "preloop",
                },
                "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Anthropic Gateway Flow",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            ai_model_id=ai_model.id,
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(flow_id=flow.id, status="RUNNING"),
    )
    runtime_api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Gateway Runtime Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "flow_id": str(flow.id),
            "flow_execution_id": str(execution.id),
            "runtime_session_id": str(
                crud_runtime_session.upsert_by_source(
                    db_session,
                    account_id=test_user.account_id,
                    session_source_type="flow_execution",
                    session_source_id=str(execution.id),
                    session_reference="anthropic-gateway-session",
                    runtime_principal_type="flow_execution",
                    runtime_principal_id=str(execution.id),
                    runtime_principal_name="Anthropic Gateway Flow",
                    started_at=execution.start_time,
                    last_activity_at=execution.start_time,
                ).id
            ),
            "runtime_principal": {
                "type": "flow_execution",
                "id": str(execution.id),
                "name": "Anthropic Gateway Flow",
            },
        },
    )
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=test_user, api_key=runtime_api_key),
    )
    stream_chunks = iter(
        [
            {
                "id": "msg_123",
                "choices": [{"index": 0, "delta": {"content": "Hello "}}],
            },
            {
                "id": "msg_123",
                "choices": [{"index": 0, "delta": {"content": "Claude"}}],
            },
            {
                "id": "msg_123",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                    "total_tokens": 12,
                },
            },
        ]
    )

    with (
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value=stream_chunks,
        ),
        patch(
            "preloop.services.model_gateway_events.crud_flow_execution.append_log"
        ) as mock_append_log,
    ):
        events = list(
            service.stream_message(
                {
                    "model": "anthropic/claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 256,
                    "stream": True,
                }
            )
        )
        service.flush_deferred_stream_record()

    assert any("event: message_start" in event for event in events)
    assert any("event: content_block_delta" in event for event in events)
    assert any("Hello " in event for event in events)
    assert any("Claude" in event for event in events)
    assert any("end_turn" in event for event in events)
    assert events[-1] == 'event: message_stop\ndata: {"type": "message_stop"}\n\n'

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.total_tokens == 12
    assert usage_row.estimated_cost == 0.00019
    assert usage_row.runtime_session_id is not None
    assert usage_row.meta_data["endpoint_kind"] == "anthropic_messages_stream"

    mock_append_log.assert_called_once()
    event = mock_append_log.call_args.args[2]
    assert event["type"] == "model_gateway_call"
    assert event["runtime_session_id"] is not None
    assert event["payload"]["endpoint_kind"] == "anthropic_messages_stream"
    assert event["payload"]["outcome"] == "success"


def test_create_message_maps_upstream_status_to_anthropic_error(db_session, test_user):
    """Upstream errors should become Anthropic-native gateway errors."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Gateway Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "anthropic/claude-sonnet-4-5",
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

    class FakeLiteLLMAuthError(Exception):
        status_code = 401
        message = "Invalid provider credential"

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        side_effect=FakeLiteLLMAuthError(),
    ):
        with pytest.raises(ModelGatewayAPIError) as exc:
            service.create_message(
                {
                    "model": "anthropic/claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 256,
                }
            )

    assert exc.value.status_code == 401
    assert exc.value.to_payload() == {
        "type": "error",
        "error": {
            "type": "authentication_error",
            "message": "Invalid provider credential",
        },
    }
