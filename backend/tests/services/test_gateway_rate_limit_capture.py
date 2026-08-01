"""Gateway capture of upstream rate-limit headers onto usage rows (#136).

Pins the end-to-end detection contract: when an upstream response (success or
429) carries rate-limit headers, the recorded ``ApiUsage`` row gets the
``rate_limit_retry_after_ms`` column and a ``meta_data["rate_limit"]``
snapshot (verbatim headers + normalized fields + subtype for 429s).
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.models.crud import crud_ai_model
from preloop.models.models.api_usage import ApiUsage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService

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


def _create_api_key_model(db_session, test_user):
    return crud_ai_model.create_with_account(
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


def _oauth_credentials():
    return SimpleNamespace(
        credential_type="oauth_anthropic_claude_code",
        value="sk-ant-oat01-access-token",
    )


def _claude_code_payload():
    return {
        "model": "anthropic/claude-sonnet-4-5",
        "max_tokens": 512,
        "system": [{"type": "text", "text": CLAUDE_CODE_SENTINEL}],
        "messages": [{"role": "user", "content": "Hello"}],
    }


def _latest_usage_row(db_session):
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )


def test_passthrough_429_records_rate_limit_telemetry(db_session, test_user):
    """A rate-limited passthrough response persists headers + retry-after."""
    _create_oauth_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    upstream_response = MagicMock()
    upstream_response.status_code = 429
    upstream_response.headers = {
        "retry-after": "30",
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-requests-remaining": "0",
        "anthropic-ratelimit-requests-reset": "2026-08-01T13:00:00Z",
    }
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
        with pytest.raises(ModelGatewayAPIError):
            service.create_message(_claude_code_payload())

    row = _latest_usage_row(db_session)
    assert row is not None
    assert row.status_code == 429
    assert row.rate_limit_retry_after_ms == 30_000
    rate_limit = row.meta_data["rate_limit"]
    assert rate_limit["retry_after_ms"] == 30_000
    assert rate_limit["requests_remaining"] == 0
    assert rate_limit["requests_limit"] == 50
    # Verbatim header capture survives.
    assert rate_limit["headers"]["anthropic-ratelimit-requests-reset"] == (
        "2026-08-01T13:00:00Z"
    )
    # No quota markers in the message: transient overload.
    assert rate_limit["subtype"] == "transient"
    assert rate_limit["subtype_source"] in ("taxonomy", "heuristic")


def test_passthrough_success_records_headroom_snapshot(db_session, test_user):
    """Successful subscription responses persist the observed headroom."""
    _create_oauth_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = {
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-requests-remaining": "49",
        "anthropic-ratelimit-unified-status": "allowed",
    }
    upstream_response.json.return_value = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }

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
        service.create_message(_claude_code_payload())

    row = _latest_usage_row(db_session)
    assert row is not None
    assert row.status_code == 200
    assert row.rate_limit_retry_after_ms is None
    rate_limit = row.meta_data["rate_limit"]
    assert rate_limit["requests_remaining"] == 49
    assert rate_limit["requests_limit"] == 50
    # Undocumented unified headers are preserved raw, never normalized.
    assert rate_limit["headers"]["anthropic-ratelimit-unified-status"] == "allowed"
    assert "subtype" not in rate_limit


def test_litellm_429_exception_headers_are_captured(db_session, test_user):
    """A LiteLLM-path 429 with response headers persists telemetry too."""
    _create_api_key_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    class _FakeRateLimitError(Exception):
        def __init__(self):
            super().__init__("rate limited")
            self.status_code = 429
            self.message = "You exceeded your current quota"
            self.response = SimpleNamespace(
                headers={
                    "retry-after": "12",
                    "x-ratelimit-limit-requests": "500",
                    "x-ratelimit-remaining-requests": "0",
                    "x-ratelimit-reset-requests": "1m30s",
                }
            )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        side_effect=_FakeRateLimitError(),
    ):
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service.create_message(
                {
                    "model": "anthropic/claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 16,
                }
            )

    assert exc_info.value.status_code == 429

    row = _latest_usage_row(db_session)
    assert row is not None
    assert row.status_code == 429
    assert row.rate_limit_retry_after_ms == 12_000
    rate_limit = row.meta_data["rate_limit"]
    assert rate_limit["requests_remaining"] == 0
    assert rate_limit["requests_limit"] == 500
    assert rate_limit["requests_reset_after_ms"] == 90_000
    # Quota marker in the upstream message: quota exhaustion.
    assert rate_limit["subtype"] == "quota_exhausted"


def test_429_without_headers_still_gets_subtype(db_session, test_user):
    """Rows for 429s with no headers still carry a labeled subtype."""
    _create_api_key_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    class _BareRateLimitError(Exception):
        def __init__(self):
            super().__init__("rate limited")
            self.status_code = 429
            self.message = "Too many requests"

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        side_effect=_BareRateLimitError(),
    ):
        with pytest.raises(ModelGatewayAPIError):
            service.create_message(
                {
                    "model": "anthropic/claude-sonnet-4-5",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 16,
                }
            )

    row = _latest_usage_row(db_session)
    assert row is not None
    assert row.status_code == 429
    assert row.rate_limit_retry_after_ms is None
    rate_limit = row.meta_data["rate_limit"]
    assert rate_limit["subtype"] == "transient"
    # No headers were observed, so no fabricated numbers appear.
    assert "requests_remaining" not in rate_limit
    assert rate_limit.get("headers") in (None, {})


def test_success_without_headers_records_no_snapshot(db_session, test_user):
    """No observed headers means no rate_limit payload at all (no guessing)."""
    _create_api_key_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    with patch(
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
    ):
        service.create_message(
            {
                "model": "anthropic/claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 16,
            }
        )

    row = _latest_usage_row(db_session)
    assert row is not None
    assert row.status_code == 200
    assert row.rate_limit_retry_after_ms is None
    assert row.meta_data["rate_limit"] is None
