"""Gateway integration tests for model I/O content policies."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.policy.schema import ModelIORule


def _service(account_id="account-1"):
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id=account_id),
    )
    return OpenAIGatewayService(MagicMock(), auth_context)


def _ai_model():
    return SimpleNamespace(
        id="model-1",
        provider_name="openai",
        model_identifier="gpt-5",
        api_endpoint=None,
    )


def _deny_pii_rule():
    return ModelIORule.model_validate(
        {
            "id": "deny-pii",
            "target": "model.request",
            "detectors": {"pii": {"types": ["email"]}},
            "conditions": [
                {"expression": "pii.found == true", "action": "deny"},
            ],
        }
    )


def _deny_response_rule():
    return ModelIORule.model_validate(
        {
            "id": "deny-out",
            "target": "model.response",
            "detectors": {"pii": True},
            "conditions": [
                {"expression": "pii.found == true", "action": "deny"},
            ],
        }
    )


def test_request_deny_short_circuits_before_provider():
    service = _service()
    mock_call = MagicMock()
    with (
        patch.object(service, "_resolve_requested_model", return_value=_ai_model()),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(service, "_call_litellm", mock_call),
        patch(
            "preloop.services.model_content_policy.load_model_io_rules",
            return_value=[_deny_pii_rule()],
        ),
    ):
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service.create_chat_completion(
                {
                    "model": "gpt-5",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Write to alice@example.com",
                        }
                    ],
                }
            )
    mock_call.assert_not_called()
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "content_policy_denied"
    assert "deny-pii" in exc_info.value.message


def test_response_deny_after_provider_returns():
    service = _service()
    upstream = {
        "id": "chatcmpl_1",
        "created": 1,
        "choices": [
            {
                "message": {"role": "assistant", "content": "Email bob@example.com"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    mock_call = MagicMock(return_value=upstream)
    with (
        patch.object(service, "_resolve_requested_model", return_value=_ai_model()),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(service, "_call_litellm", mock_call),
        patch.object(service, "_is_openai_codex_model", return_value=False),
        patch.object(service, "_response_to_dict", return_value=upstream),
        patch(
            "preloop.services.model_content_policy.load_model_io_rules",
            return_value=[_deny_response_rule()],
        ),
    ):
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service.create_chat_completion(
                {
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "Hi"}],
                }
            )
    mock_call.assert_called_once()
    assert exc_info.value.code == "content_policy_denied"
    assert "deny-out" in exc_info.value.message


def test_require_approval_rejected_does_not_call_provider():
    service = _service()
    rule = ModelIORule.model_validate(
        {
            "id": "hold-req",
            "target": "model.request",
            "approval_workflow": "high-risk",
            "detectors": {"moderation": {"backend": "local"}},
            "conditions": [
                {"expression": "true", "action": "require_approval"},
            ],
        }
    )
    mock_call = MagicMock()
    with (
        patch.object(service, "_resolve_requested_model", return_value=_ai_model()),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(service, "_call_litellm", mock_call),
        patch(
            "preloop.services.model_content_policy.load_model_io_rules",
            return_value=[rule],
        ),
        patch(
            "preloop.services.model_content_policy.hold_for_model_io_approval",
            return_value=False,
        ) as hold,
    ):
        with pytest.raises(ModelGatewayAPIError):
            service.create_chat_completion(
                {
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "Hi"}],
                }
            )
    hold.assert_called_once()
    mock_call.assert_not_called()


def test_require_approval_approved_continues_to_provider():
    service = _service()
    rule = ModelIORule.model_validate(
        {
            "id": "hold-out",
            "target": "model.response",
            "approval_workflow": "high-risk",
            "detectors": {"moderation": {"backend": "local"}},
            "conditions": [
                {"expression": "true", "action": "require_approval"},
            ],
        }
    )
    upstream = {
        "id": "chatcmpl_1",
        "created": 1,
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    with (
        patch.object(service, "_resolve_requested_model", return_value=_ai_model()),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(service, "_call_litellm", return_value=upstream),
        patch.object(service, "_is_openai_codex_model", return_value=False),
        patch.object(service, "_response_to_dict", return_value=upstream),
        patch(
            "preloop.services.model_content_policy.load_model_io_rules",
            return_value=[rule],
        ),
        patch(
            "preloop.services.model_content_policy.hold_for_model_io_approval",
            return_value=True,
        ),
    ):
        result = service.create_chat_completion(
            {
                "model": "gpt-5",
                "messages": [{"role": "user", "content": "Hi"}],
            }
        )
    assert result["choices"][0]["message"]["content"] == "ok"


def test_streaming_request_deny_does_not_open_upstream():
    service = _service()
    mock_open = MagicMock()
    with (
        patch.object(service, "_resolve_requested_model", return_value=_ai_model()),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(service, "_open_upstream_stream", mock_open),
        patch(
            "preloop.services.model_content_policy.load_model_io_rules",
            return_value=[_deny_pii_rule()],
        ),
    ):
        with pytest.raises(ModelGatewayAPIError):
            service.stream_chat_completion(
                {
                    "model": "gpt-5",
                    "messages": [{"role": "user", "content": "alice@example.com"}],
                }
            )
    mock_open.assert_not_called()
