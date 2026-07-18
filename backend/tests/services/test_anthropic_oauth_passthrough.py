"""Tests for the Anthropic subscription-OAuth passthrough branch.

Anthropic validates subscription-OAuth (Claude Code Pro/Max) requests
structurally: the first ``system`` block must be exactly the Claude Code
sentinel string, and violations are rejected with a disguised 429
``rate_limit_error``. These tests pin the gateway's passthrough contract:

- OAuth-backed Anthropic-protocol traffic bypasses the litellm transcode
  and forwards the client payload verbatim (system array, cache_control,
  beta headers preserved).
- Preloop-side injection (governance tool-stripping) never disturbs the
  system blocks or cache_control markers.
- API-key traffic keeps using the litellm path unchanged.
- Budget/usage accounting keeps working on the passthrough branch.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.models.crud import crud_ai_model, crud_api_key
from preloop.models.models.api_usage import ApiUsage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import (
    ANTHROPIC_OAUTH_BETA_FLAG,
    OpenAIGatewayService,
)

CLAUDE_CODE_SENTINEL = "You are Claude Code, Anthropic's official CLI for Claude."


def _create_oauth_model(db_session, test_user):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Code Subscription",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "unused-placeholder",
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


def _oauth_credentials():
    return SimpleNamespace(
        credential_type="oauth_anthropic_claude_code",
        value="sk-ant-oat01-access-token",
    )


def _claude_code_payload():
    """A representative Claude Code request: system array + cache_control."""
    return {
        "model": "anthropic/claude-sonnet-4-5",
        "max_tokens": 512,
        "system": [
            {
                "type": "text",
                "text": CLAUDE_CODE_SENTINEL,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "Extra project context.",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Hello",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
        "metadata": {"user_id": "claude-code-user"},
    }


def _upstream_message_response():
    return {
        "id": "msg_native_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "Hello from Anthropic"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 11,
            "output_tokens": 5,
            "cache_read_input_tokens": 6,
            "cache_creation_input_tokens": 2,
        },
    }


def test_oauth_passthrough_preserves_system_array_and_cache_control(
    db_session, test_user
):
    """OAuth traffic must reach Anthropic with the original payload structure."""
    _create_oauth_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    request_payload = _claude_code_payload()

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.json.return_value = _upstream_message_response()

    with (
        patch(
            "preloop.services.openai_gateway.get_secret_service"
        ) as mock_secret_service,
        patch(
            "preloop.services.openai_gateway.httpx.post",
            return_value=upstream_response,
        ) as mock_post,
        patch("preloop.services.openai_gateway.litellm.completion") as mock_completion,
    ):
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            _oauth_credentials()
        )
        response_payload = service.create_message(
            request_payload,
            anthropic_version="2023-06-01",
            anthropic_beta="prompt-caching-2024-07-31",
        )

    mock_completion.assert_not_called()
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args.kwargs
    assert mock_post.call_args.args[0] == "https://api.anthropic.com/v1/messages"

    body = call_kwargs["json"]
    # System survives as an ARRAY of blocks, sentinel first, byte-faithful.
    assert body["system"] == request_payload["system"]
    assert body["system"][0]["text"] == CLAUDE_CODE_SENTINEL
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Messages (with cache_control) forwarded untouched.
    assert body["messages"] == request_payload["messages"]
    assert body["metadata"] == request_payload["metadata"]
    assert body["max_tokens"] == 512
    # Model resolves to the account model's upstream identifier.
    assert body["model"] == "claude-sonnet-4-5"
    assert body["stream"] is False

    headers = call_kwargs["headers"]
    assert headers["authorization"] == "Bearer sk-ant-oat01-access-token"
    assert headers["anthropic-version"] == "2023-06-01"
    # OAuth beta flag merged with (not replacing) the client's beta flags.
    assert headers["anthropic-beta"] == (
        f"{ANTHROPIC_OAUTH_BETA_FLAG},prompt-caching-2024-07-31"
    )

    # The upstream response is returned to the client verbatim.
    assert response_payload == _upstream_message_response()

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.status_code == 200
    assert usage_row.prompt_tokens == 11
    assert usage_row.completion_tokens == 5
    assert usage_row.cache_read_tokens == 6
    assert usage_row.cache_creation_tokens == 2
    # Subscription-covered upstream: $0 spend, tagged oauth.
    assert float(usage_row.estimated_cost or 0.0) == 0.0
    assert usage_row.meta_data["upstream_credential_type"] == "oauth"


def test_oauth_passthrough_governance_strip_keeps_system_untouched(
    db_session, test_user
):
    """Tool governance may drop tools but must never disturb system blocks."""
    _create_oauth_model(db_session, test_user)
    api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Managed Gateway Key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )
    api_key_id = str(api_key.id)
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=test_user, api_key=api_key),
    )
    request_payload = _claude_code_payload()
    request_payload["tools"] = [
        {
            "name": "WebSearch",
            "description": "search",
            "input_schema": {"type": "object"},
        },
        {
            "name": "Bash",
            "description": "run commands",
            "input_schema": {"type": "object"},
            "cache_control": {"type": "ephemeral"},
        },
    ]
    request_payload["tool_choice"] = {"type": "tool", "name": "WebSearch"}
    governance_meta = {
        "subject_governance": {
            "api_keys": {
                api_key_id: {"tool_enabled_overrides": {"WebSearch": False}},
            },
            "managed_agents": {},
        }
    }

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.json.return_value = _upstream_message_response()

    with (
        patch(
            "preloop.services.openai_gateway.get_secret_service"
        ) as mock_secret_service,
        patch(
            "preloop.services.openai_gateway.get_cached_account_meta_data",
            return_value=governance_meta,
        ),
        patch(
            "preloop.services.openai_gateway.httpx.post",
            return_value=upstream_response,
        ) as mock_post,
    ):
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            _oauth_credentials()
        )
        service.create_message(request_payload)

    body = mock_post.call_args.kwargs["json"]
    # The disabled tool is stripped; the surviving tool is untouched
    # (including its cache_control breakpoint).
    assert [tool["name"] for tool in body["tools"]] == ["Bash"]
    assert body["tools"][0]["cache_control"] == {"type": "ephemeral"}
    # A tool_choice naming the stripped tool falls back to auto.
    assert body["tool_choice"] == {"type": "auto"}
    # System blocks and messages remain byte-faithful, sentinel first.
    assert body["system"] == request_payload["system"]
    assert body["system"][0]["text"] == CLAUDE_CODE_SENTINEL
    assert body["messages"] == request_payload["messages"]

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    optimization = usage_row.meta_data.get("context_optimization") or {}
    assert optimization.get("stripped_tools") == ["WebSearch"]


def test_api_key_anthropic_path_still_uses_litellm(db_session, test_user):
    """API-key models keep the existing transcode path (no passthrough)."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude API Key Model",
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

    with (
        patch(
            "preloop.services.openai_gateway.httpx.post",
        ) as mock_post,
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value={
                "id": "msg_1",
                "choices": [
                    {
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
        ) as mock_completion,
    ):
        payload = service.create_message(
            {
                "model": "anthropic/claude-sonnet-4-5",
                "system": "Be concise",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 16,
            }
        )

    mock_post.assert_not_called()
    mock_completion.assert_called_once()
    assert payload["content"][0]["text"] == "ok"


def test_oauth_passthrough_maps_upstream_error(db_session, test_user):
    """Upstream 4xx/5xx surfaces as a gateway error and is recorded."""
    _create_oauth_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    upstream_response = MagicMock()
    upstream_response.status_code = 429
    upstream_response.text = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "message": "Number of request tokens has exceeded your rate limit",
            },
        }
    )

    with (
        patch(
            "preloop.services.openai_gateway.get_secret_service"
        ) as mock_secret_service,
        patch(
            "preloop.services.openai_gateway.httpx.post",
            return_value=upstream_response,
        ),
    ):
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            _oauth_credentials()
        )
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service.create_message(_claude_code_payload())

    assert exc_info.value.status_code == 429
    assert exc_info.value.error_type == "rate_limit_error"
    assert "rate limit" in exc_info.value.message

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.status_code == 429


def test_oauth_passthrough_stream_relays_sse_verbatim(db_session, test_user):
    """Streaming passthrough must forward upstream SSE bytes unmodified."""
    _create_oauth_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    request_payload = {**_claude_code_payload(), "stream": True}

    sse_chunks = [
        (
            "event: message_start\n"
            + 'data: {"type":"message_start","message":{"id":"msg_stream_1",'
            + '"usage":{"input_tokens":9,"cache_read_input_tokens":3}}}\n\n'
        ),
        (
            "event: content_block_start\n"
            + 'data: {"type":"content_block_start","index":0,'
            + '"content_block":{"type":"text","text":""}}\n\n'
        ),
        (
            "event: content_block_delta\n"
            + 'data: {"type":"content_block_delta","index":0,'
            + '"delta":{"type":"text_delta","text":"Hi there"}}\n\n'
        ),
        (
            "event: message_delta\n"
            + 'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            + '"usage":{"output_tokens":4}}\n\n'
        ),
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.iter_text.return_value = iter(sse_chunks)
    upstream_client = MagicMock()
    upstream_client.send.return_value = upstream_response

    with (
        patch(
            "preloop.services.openai_gateway.get_secret_service"
        ) as mock_secret_service,
        patch(
            "preloop.services.openai_gateway.httpx.Client",
            return_value=upstream_client,
        ),
        patch("preloop.services.openai_gateway.litellm.completion") as mock_completion,
    ):
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            _oauth_credentials()
        )
        emitted = list(service.stream_message(request_payload))

    mock_completion.assert_not_called()
    # Chunks relayed verbatim, in order.
    assert emitted == sse_chunks
    # The outbound request body preserved the system array + cache_control.
    request = upstream_client.build_request.call_args
    body = request.kwargs["json"]
    assert body["system"] == request_payload["system"]
    assert body["system"][0]["text"] == CLAUDE_CODE_SENTINEL
    assert body["stream"] is True
    upstream_response.close.assert_called_once()
    upstream_client.close.assert_called_once()

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.status_code == 200
    assert usage_row.prompt_tokens == 9
    assert usage_row.completion_tokens == 4
    assert usage_row.cache_read_tokens == 3
    assert usage_row.meta_data["endpoint_kind"] == "anthropic_messages_stream"
    assert usage_row.meta_data["upstream_credential_type"] == "oauth"


def test_oauth_passthrough_stream_upstream_error_raises_before_stream(
    db_session, test_user
):
    """A 4xx on the streaming connection surfaces as a gateway error."""
    _create_oauth_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    upstream_response = MagicMock()
    upstream_response.status_code = 429
    upstream_response.read.return_value = json.dumps(
        {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    ).encode("utf-8")
    upstream_client = MagicMock()
    upstream_client.send.return_value = upstream_response

    with (
        patch(
            "preloop.services.openai_gateway.get_secret_service"
        ) as mock_secret_service,
        patch(
            "preloop.services.openai_gateway.httpx.Client",
            return_value=upstream_client,
        ),
    ):
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            _oauth_credentials()
        )
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service.stream_message({**_claude_code_payload(), "stream": True})

    assert exc_info.value.status_code == 429
    assert exc_info.value.error_type == "rate_limit_error"
    upstream_response.close.assert_called_once()
    upstream_client.close.assert_called_once()
