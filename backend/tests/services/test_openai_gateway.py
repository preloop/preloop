"""Tests for the OpenAI-compatible gateway service."""

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.models.models.api_usage import ApiUsage
from preloop.models.crud import (
    crud_ai_model,
    crud_runtime_session,
)
from preloop.models.crud import crud_api_key
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.openai_gateway import LiteLLMModelGatewayBackend
from preloop.services.secret_service import CredentialRefreshError


def _parse_sse_payload(event: str):
    data_line = next(line for line in event.splitlines() if line.startswith("data: "))
    payload = data_line.removeprefix("data: ")
    if payload == "[DONE]":
        return payload
    return json.loads(payload)


def test_call_litellm_uses_injected_upstream_backend():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    upstream_backend = MagicMock()
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=upstream_backend
    )
    ai_model = SimpleNamespace(
        provider_name="openai", model_identifier="gpt-5", api_endpoint=None
    )

    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="provider-secret")
        )
        service._call_litellm(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload={"temperature": 0.2},
            provider="openai",
        )

    upstream_backend.completion.assert_called_once_with(
        model="openai/gpt-5",
        messages=[{"role": "user", "content": "Hello"}],
        api_key="provider-secret",
        timeout=600,
        temperature=0.2,
    )


def test_call_litellm_allows_bedrock_ambient_credentials():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    upstream_backend = MagicMock()
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=upstream_backend
    )
    ai_model = SimpleNamespace(
        provider_name="bedrock",
        model_identifier="us.anthropic.claude-opus-4-6-v1",
        api_endpoint=None,
        meta_data={"provider_runtime": {"region": "us-east-1"}},
    )

    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            None
        )
        service._call_litellm(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload={},
            provider="openai",
        )

    upstream_backend.completion.assert_called_once_with(
        model="bedrock/us.anthropic.claude-opus-4-6-v1",
        messages=[{"role": "user", "content": "Hello"}],
        timeout=600,
        aws_region_name="us-east-1",
    )


def test_call_litellm_passes_imported_bedrock_credentials():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    upstream_backend = MagicMock()
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=upstream_backend
    )
    ai_model = SimpleNamespace(
        provider_name="bedrock",
        model_identifier="us.anthropic.claude-opus-4-6-v1",
        api_endpoint=None,
        meta_data={"provider_runtime": {"region": "us-east-1"}},
    )

    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(
                credential_type="api_key",
                value=json.dumps(
                    {
                        "aws_access_key_id": "AKIA_TEST",
                        "aws_secret_access_key": "secret-test",
                        "aws_session_token": "session-test",
                        "aws_region_name": "eu-central-1",
                    }
                ),
            )
        )
        service._call_litellm(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload={},
            provider="openai",
        )

    upstream_backend.completion.assert_called_once_with(
        model="bedrock/us.anthropic.claude-opus-4-6-v1",
        messages=[{"role": "user", "content": "Hello"}],
        timeout=600,
        aws_access_key_id="AKIA_TEST",
        aws_secret_access_key="secret-test",
        aws_session_token="session-test",
        aws_region_name="eu-central-1",
    )


def test_call_litellm_uses_claude_code_oauth_headers():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    db = MagicMock()
    upstream_backend = MagicMock()
    service = OpenAIGatewayService(db, auth_context, upstream_backend=upstream_backend)
    ai_model = SimpleNamespace(
        provider_name="anthropic",
        model_identifier="claude-opus-4-6",
        api_endpoint=None,
        meta_data={},
    )

    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(
                credential_type="oauth_anthropic_claude_code",
                value="sk-ant-oat01-access-token",
            )
        )
        service._call_litellm(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload={"max_tokens": 1},
            provider="anthropic",
        )

    mock_secret_service.return_value.resolve_ai_model_credentials.assert_called_once_with(
        ai_model,
        db=db,
        allow_refresh=True,
    )
    upstream_backend.completion.assert_called_once_with(
        model="anthropic/claude-opus-4-6",
        messages=[{"role": "user", "content": "Hello"}],
        timeout=600,
        _preloop_anthropic_auth_token="sk-ant-oat01-access-token",
        extra_headers={
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-client-platform": "claude-code",
        },
        max_tokens=1,
    )


def test_call_litellm_maps_claude_code_oauth_refresh_failure():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    upstream_backend = MagicMock()
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=upstream_backend
    )
    ai_model = SimpleNamespace(
        provider_name="anthropic",
        model_identifier="claude-opus-4-6",
        api_endpoint=None,
        meta_data={},
    )

    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.side_effect = (
            CredentialRefreshError(
                "Anthropic Claude Code OAuth token refresh failed with status 403",
                provider="anthropic",
                status_code=403,
                code="1010",
            )
        )
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service._call_litellm(
                ai_model,
                messages=[{"role": "user", "content": "Hello"}],
                payload={"max_tokens": 1},
                provider="anthropic",
            )

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_type == "authentication_error"
    assert exc_info.value.code == "1010"
    assert "could not be refreshed" in exc_info.value.message
    upstream_backend.completion.assert_not_called()


def _oauth_service_and_model():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=MagicMock()
    )
    ai_model = SimpleNamespace(
        provider_name="anthropic",
        model_identifier="claude-opus-4-6",
        api_endpoint=None,
        meta_data={},
    )
    return service, ai_model


def test_call_litellm_records_oauth_upstream_credential_type():
    """T12: subscription-OAuth traffic is tagged 'oauth' for savings denomination."""
    service, ai_model = _oauth_service_and_model()
    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(
                credential_type="oauth_anthropic_claude_code",
                value="sk-ant-oat01-access-token",
            )
        )
        service._call_litellm(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload={"max_tokens": 1},
            provider="anthropic",
        )
    assert service._last_upstream_credential_type == "oauth"


def test_call_litellm_records_api_key_upstream_credential_type():
    """T12: API-key traffic is tagged 'api_key' (dollar-denominated savings)."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=MagicMock()
    )
    ai_model = SimpleNamespace(
        provider_name="openai",
        model_identifier="gpt-5",
        api_endpoint=None,
        meta_data={},
    )
    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="sk-provider-key")
        )
        service._call_litellm(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload={"max_tokens": 1},
            provider="openai",
        )
    assert service._last_upstream_credential_type == "api_key"


def test_call_litellm_resets_upstream_credential_type_on_refresh_failure():
    """T12 safety: an errored resolution must not leave a prior request's value."""
    service, ai_model = _oauth_service_and_model()
    # Simulate a stale value carried over from a previous request on this
    # reused service instance.
    service._last_upstream_credential_type = "oauth"
    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.side_effect = (
            CredentialRefreshError(
                "refresh failed",
                provider="anthropic",
                status_code=403,
                code="1010",
            )
        )
        with pytest.raises(ModelGatewayAPIError):
            service._call_litellm(
                ai_model,
                messages=[{"role": "user", "content": "Hello"}],
                payload={"max_tokens": 1},
                provider="anthropic",
            )
    assert service._last_upstream_credential_type is None


def test_litellm_backend_masks_ambient_anthropic_api_key_for_oauth(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-platform-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    observed_env = {}

    def fake_completion(**kwargs):
        observed_env["api_key"] = os.environ.get("ANTHROPIC_API_KEY")
        observed_env["auth_token"] = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        observed_env["kwargs"] = kwargs
        return {"ok": True}

    with patch("preloop.services.openai_gateway.litellm.completion", fake_completion):
        result = LiteLLMModelGatewayBackend().completion(
            model="anthropic/claude-haiku-4-5",
            messages=[{"role": "user", "content": "Hello"}],
            _preloop_anthropic_auth_token="oauth-token",
        )

    assert result == {"ok": True}
    assert observed_env["api_key"] is None
    assert observed_env["auth_token"] == "oauth-token"
    assert "_preloop_anthropic_auth_token" not in observed_env["kwargs"]
    assert os.environ["ANTHROPIC_API_KEY"] == "ambient-platform-key"
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") is None


def test_create_response_uses_openai_codex_adapter():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    ai_model = SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-5.4",
        api_endpoint="https://chatgpt.com/backend-api/codex",
    )

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service,
            "_create_openai_codex_response",
            return_value={
                "id": "resp_codex_1",
                "created_at": 1234,
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "Hello from Codex"}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "total_tokens": 7,
                },
                "output_text": "Hello from Codex",
            },
        ) as mock_create_codex,
        patch.object(service, "_call_litellm") as mock_call_litellm,
    ):
        response = service.create_response({"model": "openai/gpt-5.4", "input": "Hi"})

    mock_create_codex.assert_called_once_with(
        ai_model, {"model": "openai/gpt-5.4", "input": "Hi"}
    )
    mock_call_litellm.assert_not_called()
    assert response["model"] == "openai/gpt-5.4"
    assert response["output_text"] == "Hello from Codex"
    assert response["usage"]["total_tokens"] == 7


def _codex_oauth_service_and_model():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=MagicMock()
    )
    ai_model = SimpleNamespace(
        provider_name="openai-codex",
        model_identifier="gpt-5.4",
        api_endpoint="https://chatgpt.com/backend-api/codex",
        meta_data={},
    )
    return service, ai_model


def test_resolve_openai_codex_credentials_maps_refresh_failure_to_401():
    """A burned Codex refresh token must map to a 401, not escape as a 500.

    Regression guard for Codex CLI live validation: OpenAI answered the token
    refresh with 401 ``refresh_token_reused`` and the gateway surfaced it as
    HTTP 500 "Internal server error".
    """
    service, ai_model = _codex_oauth_service_and_model()
    # Simulate a stale value carried over from a previous request on this
    # reused service instance.
    service._last_upstream_credential_type = "oauth"

    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.side_effect = (
            CredentialRefreshError(
                "OpenAI Codex token refresh failed with status 401",
                provider="openai",
                status_code=401,
                code="refresh_token_reused",
            )
        )
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service._resolve_openai_codex_credentials(ai_model)

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_type == "authentication_error"
    assert exc_info.value.code == "refresh_token_reused"
    assert "could not be refreshed" in exc_info.value.message
    assert "codex login" in exc_info.value.message
    assert service._last_upstream_credential_type is None


def test_resolve_openai_codex_credentials_maps_upstream_5xx_refresh_failure_to_502():
    service, ai_model = _codex_oauth_service_and_model()

    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.side_effect = (
            CredentialRefreshError(
                "OpenAI Codex token refresh failed with status 503",
                provider="openai",
                status_code=503,
            )
        )
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service._resolve_openai_codex_credentials(ai_model)

    assert exc_info.value.status_code == 502


def test_stream_response_emits_output_item_before_text_deltas():
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    ai_model = SimpleNamespace(id="model-1", provider_name="openai")
    upstream_stream = iter(
        [
            {
                "choices": [{"delta": {"content": "Hello"}}],
            },
            {
                "choices": [{"delta": {"content": " world"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        ]
    )

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_call_litellm", return_value=upstream_stream),
        patch.object(service, "_record_gateway_request"),
    ):
        events = [
            _parse_sse_payload(event)
            for event in service.stream_response(
                {"model": "openai/gpt-5", "input": "Hi"}
            )
        ]

    event_types = [event["type"] for event in events[:-1]]
    assert event_types[:3] == [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
    ]
    assert event_types[3:5] == [
        "response.output_text.delta",
        "response.output_text.delta",
    ]
    assert event_types[5:] == [
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[1]["item"]["id"] == events[3]["item_id"]
    assert events[2]["part"]["type"] == "output_text"
    assert events[7]["item"]["content"][0]["text"] == "Hello world"
    assert events[8]["response"]["output_text"] == "Hello world"
    assert events[8]["response"]["usage"]["total_tokens"] == 5
    assert events[-1] == "[DONE]"


def test_stream_response_ignores_null_text_deltas():
    """Null upstream text chunks should not leak as literal 'None' output."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    ai_model = SimpleNamespace(id="model-1", provider_name="openai")
    upstream_stream = iter(
        [
            {
                "choices": [{"delta": {"content": "Hello"}}],
            },
            {
                "choices": [{"delta": {"content": None}}],
            },
            {
                "choices": [{"delta": {"content": " world"}}],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        ]
    )

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_call_litellm", return_value=upstream_stream),
        patch.object(service, "_record_gateway_request"),
    ):
        events = [
            _parse_sse_payload(event)
            for event in service.stream_response(
                {"model": "openai/gpt-5", "input": "Hi"}
            )
        ]

    text_deltas = [
        event["delta"]
        for event in events
        if isinstance(event, dict) and event.get("type") == "response.output_text.delta"
    ]
    assert text_deltas == ["Hello", " world"]
    completed = next(
        event
        for event in events
        if isinstance(event, dict) and event.get("type") == "response.completed"
    )
    assert completed["response"]["output_text"] == "Hello world"
    assert "None" not in completed["response"]["output_text"]


def test_list_models_returns_gateway_enabled_aliases(db_session, test_user):
    """Only gateway-enabled models should be listed."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "test-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Non Gateway Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "test-key",
        },
        account_id=test_user.account_id,
    )

    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    payload = service.list_models()

    assert payload["object"] == "list"
    assert [item["id"] for item in payload["data"]] == ["openai/gpt-5"]


def test_create_response_requires_gateway_enabled_default_when_model_omitted(
    db_session, test_user
):
    """Omitted model should not fall back to a non-gateway default."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Direct Default Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "provider-secret",
            "is_default": True,
        },
        account_id=test_user.account_id,
    )
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )

    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )

    with patch("preloop.services.openai_gateway.litellm.completion") as mock_completion:
        with pytest.raises(
            ModelGatewayAPIError, match="No gateway-enabled default model configured"
        ) as exc:
            service.create_response({"instructions": "Be brief", "input": "Hello"})

    assert exc.value.status_code == 404
    mock_completion.assert_not_called()


def test_create_response_normalizes_and_calls_litellm(db_session, test_user):
    """Responses API should normalize input and return OpenAI-style response output."""
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                },
                "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )
    runtime_api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Gateway Runtime Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "runtime_session_id": str(
                crud_runtime_session.upsert_by_source(
                    db_session,
                    account_id=test_user.account_id,
                    session_source_type="custom",
                    session_source_id="gateway-test-session",
                    runtime_principal_type="flow_execution",
                    runtime_principal_id="11111111-1111-1111-1111-111111111111",
                    runtime_principal_name="Test Flow",
                    started_at=ai_model.created_at,
                    last_activity_at=ai_model.created_at,
                ).id
            ),
            "runtime_principal": {
                "type": "flow_execution",
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Test Flow",
            },
        },
    )

    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=test_user, api_key=runtime_api_key),
    )
    litellm_response = {
        "id": "chatcmpl_123",
        "created": 1710000000,
        "choices": [
            {
                "message": {"role": "assistant", "content": "Gateway says hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=litellm_response,
    ) as mock_completion:
        payload = service.create_response(
            {
                "model": "openai/gpt-5",
                "instructions": "Be brief",
                "input": "Hello",
            }
        )

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-5"
    assert kwargs["api_key"] == "provider-secret"
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][1]["role"] == "user"
    assert payload["output_text"] == "Gateway says hello"
    assert payload["usage"]["total_tokens"] == 18

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/responses")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.user_id == test_user.id
    assert usage_row.account_id == test_user.account_id
    assert usage_row.api_key_id == runtime_api_key.id
    assert usage_row.ai_model_id == ai_model.id
    assert usage_row.model_alias == "openai/gpt-5"
    assert usage_row.provider_name == "openai"
    assert usage_row.prompt_tokens == 11
    assert usage_row.completion_tokens == 7
    assert usage_row.total_tokens == 18
    assert usage_row.estimated_cost == 0.00025
    assert usage_row.runtime_session_id is not None
    assert usage_row.runtime_principal_type == "flow_execution"
    assert usage_row.runtime_principal_name == "Test Flow"


def test_create_response_uses_litellm_pricing_when_metadata_missing(
    db_session, test_user
):
    """Gateway request costing should fall back to LiteLLM's model pricing map."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5.4",
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
    litellm_response = {
        "id": "chatcmpl_123",
        "created": 1710000000,
        "choices": [
            {
                "message": {"role": "assistant", "content": "Gateway says hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }

    with (
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value=litellm_response,
        ) as mock_completion,
        patch(
            "preloop.services.model_pricing.litellm.completion_cost",
            return_value=0.00579,
        ) as mock_completion_cost,
        patch(
            "preloop.services.model_pricing.litellm.cost_per_token",
            return_value=(0.00123, 0.00456),
        ) as mock_cost_per_token,
    ):
        payload = service.create_response(
            {
                "model": "openai/gpt-5.4",
                "instructions": "Be brief",
                "input": "Hello",
            }
        )

    assert payload["output_text"] == "Gateway says hello"
    mock_completion.assert_called_once()
    mock_cost_per_token.assert_called_once_with(
        model="openai/gpt-5.4",
        prompt_tokens=4,
        completion_tokens=1024,
    )
    mock_completion_cost.assert_called_once_with(
        model="openai/gpt-5.4",
        completion_response={"usage": litellm_response["usage"]},
    )

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/responses")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.estimated_cost == 0.00579
    assert usage_row.meta_data["usage_details"] == litellm_response["usage"]


def test_create_response_forwards_tools_and_returns_function_call_output(
    db_session, test_user
):
    """Responses tool definitions should be forwarded and tool calls preserved."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
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
    tool_payload = {
        "type": "function",
        "name": "get_pull_request",
        "description": "Fetch one pull request",
        "parameters": {"type": "object", "properties": {}},
    }

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "get_pull_request",
                                    "arguments": '{"pull_request":"owner/repo#1"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        },
    ) as mock_completion:
        payload = service.create_response(
            {
                "model": "openai/gpt-5",
                "input": "Review this PR",
                "tools": [tool_payload],
                "tool_choice": {"type": "function", "name": "get_pull_request"},
            }
        )

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_pull_request",
                "description": "Fetch one pull request",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_pull_request"},
    }
    assert payload["output_text"] == ""
    assert payload["output"][0]["type"] == "function_call"
    assert payload["output"][0]["call_id"] == "call_123"
    assert payload["output"][0]["name"] == "get_pull_request"


def test_create_response_preserves_responses_tool_history_in_chat_messages(
    db_session, test_user
):
    """Responses function call history should remain structured for the upstream model."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
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

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [{"message": {"role": "assistant", "content": "continue"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ) as mock_completion:
        service.create_response(
            {
                "model": "openai/gpt-5",
                "input": [
                    {
                        "role": "assistant",
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Starting review"}],
                    },
                    {
                        "type": "function_call",
                        "name": "mcp__preloop__update_pull_request",
                        "call_id": "call_eyes",
                        "arguments": '{"pull_request":"mr-22","add_reaction":"eyes"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_eyes",
                        "output": '{"result":"reaction added"}',
                    },
                ],
            }
        )

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["messages"] == [
        {"role": "assistant", "content": "Starting review"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_eyes",
                    "type": "function",
                    "function": {
                        "name": "mcp__preloop__update_pull_request",
                        "arguments": '{"pull_request":"mr-22","add_reaction":"eyes"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_eyes",
            "content": '{"result":"reaction added"}',
        },
    ]


def test_create_response_coalesces_multi_tool_turns_for_upstream(db_session, test_user):
    """Contiguous Responses tool calls should become one assistant tool_calls turn."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
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

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [{"message": {"role": "assistant", "content": "continue"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ) as mock_completion:
        service.create_response(
            {
                "model": "openai/gpt-5",
                "input": [
                    {
                        "role": "assistant",
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Starting review"}],
                    },
                    {
                        "type": "function_call",
                        "name": "mcp__preloop__update_pull_request",
                        "call_id": "call_eyes",
                        "arguments": '{"pull_request":"mr-22","add_reaction":"eyes"}',
                    },
                    {
                        "type": "function_call",
                        "name": "mcp__preloop__get_pull_request",
                        "call_id": "call_fetch",
                        "arguments": '{"pull_request":"mr-22","include_diff":true}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_eyes",
                        "output": '{"result":"reaction added"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_fetch",
                        "output": '{"result":"pull request fetched"}',
                    },
                ],
            }
        )

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["messages"] == [
        {"role": "assistant", "content": "Starting review"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_eyes",
                    "type": "function",
                    "function": {
                        "name": "mcp__preloop__update_pull_request",
                        "arguments": '{"pull_request":"mr-22","add_reaction":"eyes"}',
                    },
                },
                {
                    "id": "call_fetch",
                    "type": "function",
                    "function": {
                        "name": "mcp__preloop__get_pull_request",
                        "arguments": '{"pull_request":"mr-22","include_diff":true}',
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_eyes",
            "content": '{"result":"reaction added"}',
        },
        {
            "role": "tool",
            "tool_call_id": "call_fetch",
            "content": '{"result":"pull request fetched"}',
        },
    ]


def test_create_response_rejects_incomplete_tool_history(db_session, test_user):
    """Malformed Responses tool history should fail locally before upstream."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
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

    with patch("preloop.services.openai_gateway.litellm.completion") as mock_completion:
        with pytest.raises(
            ModelGatewayAPIError,
            match="tool_call_ids did not have response messages: call_eyes",
        ):
            service.create_response(
                {
                    "model": "openai/gpt-5",
                    "input": [
                        {
                            "type": "function_call",
                            "name": "mcp__preloop__update_pull_request",
                            "call_id": "call_eyes",
                            "arguments": '{"pull_request":"mr-22","add_reaction":"eyes"}',
                        },
                        {
                            "role": "assistant",
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "I should not appear before tool output",
                                }
                            ],
                        },
                    ],
                }
            )

    mock_completion.assert_not_called()


def test_create_response_downgrades_custom_tools_to_functions(db_session, test_user):
    """Codex freeform `custom` tools must be downgraded to plain functions.

    Regression test for the prod failure on flow execution `0792099a`
    (`api_usage` `dcf29ac5`): this previously wrapped the tool into the nested
    chat-completions `custom` block, which deletes the top-level `name`. For
    any model litellm routes to `/v1/responses` (e.g. `gpt-5.2-codex`) that
    yields `400 Missing required parameter: 'tools[N].name'`, and DeepSeek
    rejects non-`function` tool types outright. A plain function tool is the
    only shape both routes and all providers accept.
    """
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
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

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ) as mock_completion:
        service.create_response(
            {
                "model": "openai/gpt-5",
                "input": "Use the available tools",
                "tools": [
                    {
                        "type": "custom",
                        "name": "get_pull_request",
                        "description": "Fetch one pull request",
                        "input_schema": {"type": "object"},
                    }
                ],
            }
        )

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_pull_request",
                "description": "Fetch one pull request",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "string",
                            "description": (
                                "The complete, raw tool payload as plain text."
                            ),
                        }
                    },
                    "required": ["input"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def test_create_response_inlines_custom_tool_grammar_into_description(
    db_session, test_user
):
    """A downgraded grammar tool must carry its grammar in the description.

    The lark grammar is the only place the payload syntax is written down, so
    dropping it would leave the model unable to produce a valid payload.
    """
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
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

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ) as mock_completion:
        service.create_response(
            {
                "model": "openai/gpt-5",
                "input": "Use the grammar tool",
                "tools": [
                    {
                        "type": "custom",
                        "name": "code_exec",
                        "description": "Executes arbitrary Python code.",
                        "format": {
                            "type": "grammar",
                            "syntax": "lark",
                            "definition": 'start: "hello"',
                        },
                    }
                ],
            }
        )

    kwargs = mock_completion.call_args.kwargs
    tool = kwargs["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "code_exec"
    # The grammar survives, inlined where a plain-function model can read it.
    assert 'start: "hello"' in tool["function"]["description"]
    assert "lark grammar" in tool["function"]["description"]
    assert tool["function"]["parameters"]["required"] == ["input"]


def test_create_response_drops_unsupported_hosted_tools_for_upstream(
    db_session, test_user
):
    """Hosted Responses tools should be filtered out before calling LiteLLM."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
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

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ) as mock_completion:
        service.create_response(
            {
                "model": "openai/gpt-5",
                "input": "Use tools",
                "tools": [
                    {"type": "web_search"},
                    {
                        "type": "function",
                        "name": "get_pull_request",
                        "description": "Fetch one pull request",
                        "parameters": {"type": "object", "properties": {}},
                    },
                ],
            }
        )

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_pull_request",
                "description": "Fetch one pull request",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _codex_responses_payload() -> dict:
    return {
        "id": "resp_codex_chat_1",
        "created_at": 1234,
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Hello from Codex"},
                ],
            }
        ],
        "usage": {
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
        },
        "output_text": "Hello from Codex",
    }


def test_create_chat_completion_routes_codex_oauth_models():
    """Codex OAuth models hit the Codex backend, not litellm."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    ai_model = SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-5.4",
        api_endpoint="https://chatgpt.com/backend-api/codex",
    )

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service,
            "_create_openai_codex_response",
            return_value=_codex_responses_payload(),
        ) as mock_create_codex,
        patch.object(service, "_call_litellm") as mock_call_litellm,
    ):
        response = service.create_chat_completion(
            {
                "model": "openai/gpt-5.4",
                "messages": [{"role": "user", "content": "Hi"}],
            }
        )

    mock_create_codex.assert_called_once()
    mock_call_litellm.assert_not_called()
    assert response["model"] == "openai/gpt-5.4"
    assert response["choices"][0]["message"]["content"] == "Hello from Codex"
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["usage"]["total_tokens"] == 8


def test_stream_chat_completion_emits_codex_chunks_for_oauth_models():
    """Streaming chat completions fake-stream Codex sync responses."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    ai_model = SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-5.4",
        api_endpoint="https://chatgpt.com/backend-api/codex",
    )

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request") as mock_record,
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service,
            "_create_openai_codex_response",
            return_value=_codex_responses_payload(),
        ) as mock_create_codex,
        patch.object(service, "_call_litellm") as mock_call_litellm,
    ):
        events = list(
            service.stream_chat_completion(
                {
                    "model": "openai/gpt-5.4",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                }
            )
        )

    mock_create_codex.assert_called_once()
    mock_call_litellm.assert_not_called()

    payloads = [_parse_sse_payload(event) for event in events]
    assert payloads[-1] == "[DONE]"
    chunks = [p for p in payloads if p != "[DONE]"]

    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    content_chunk = next(c for c in chunks if c["choices"][0]["delta"].get("content"))
    assert content_chunk["choices"][0]["delta"]["content"] == "Hello from Codex"
    final_chunk = next(c for c in chunks if c["choices"][0].get("finish_reason"))
    assert final_chunk["choices"][0]["finish_reason"] == "stop"
    assert final_chunk["usage"]["total_tokens"] == 8

    mock_record.assert_called_once()
    record_kwargs = mock_record.call_args.kwargs
    assert record_kwargs["status_code"] == 200
    assert record_kwargs["endpoint"] == "/openai/v1/chat/completions"
    assert record_kwargs["endpoint_kind"] == "chat_completions_stream"


def test_stream_chat_completion_propagates_codex_errors():
    """Codex upstream errors during streaming surface as gateway errors."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    ai_model = SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-5.4",
        api_endpoint="https://chatgpt.com/backend-api/codex",
    )

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request") as mock_record,
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service,
            "_create_openai_codex_response",
            side_effect=ModelGatewayAPIError(
                provider="openai",
                status_code=401,
                message="OpenAI Codex OAuth credentials are not configured",
            ),
        ),
    ):
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            list(
                service.stream_chat_completion(
                    {
                        "model": "openai/gpt-5.4",
                        "messages": [{"role": "user", "content": "Hi"}],
                        "stream": True,
                    }
                )
            )

    assert exc_info.value.status_code == 401
    mock_record.assert_called_once()
    assert mock_record.call_args.kwargs["status_code"] == 401


def test_codex_response_to_chat_completion_dict_extracts_tool_calls():
    """Codex function_call output items become chat-completion tool_calls."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    response_dict = {
        "id": "resp_codex_2",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_abc",
                "name": "lookup_user",
                "arguments": '{"id":"u-1"}',
            }
        ],
        "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
    }

    chat_dict = service._codex_response_to_chat_completion_dict(response_dict)

    message = chat_dict["choices"][0]["message"]
    assert message["content"] == ""
    assert message["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "lookup_user", "arguments": '{"id":"u-1"}'},
        }
    ]
    assert chat_dict["choices"][0]["finish_reason"] == "tool_calls"


def test_build_openai_codex_payload_from_chat_completion_extracts_system_message():
    """System messages must move to ``instructions`` (Codex requires it)."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    payload = {
        "model": "openai/gpt-5.4",
        "temperature": 0.5,
    }
    messages = [
        {"role": "system", "content": "You are a billing assistant."},
        {"role": "user", "content": "Pay $50 to Jon"},
    ]

    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload=payload, messages=messages
    )

    assert upstream["instructions"] == "You are a billing assistant."
    assert upstream["model"] == "openai/gpt-5.4"
    # Codex rejects ``temperature`` (HTTP 400 Unsupported parameter); it
    # must NOT be forwarded.
    assert "temperature" not in upstream
    # System message must NOT show up in input items.
    assert all(
        item.get("role") != "system"
        for item in upstream["input"]
        if item.get("type") == "message"
    )
    user_item = upstream["input"][0]
    assert user_item["type"] == "message"
    assert user_item["role"] == "user"
    assert user_item["content"][0] == {
        "type": "input_text",
        "text": "Pay $50 to Jon",
    }


def test_build_openai_codex_payload_from_chat_completion_supplies_default_instructions():
    """Codex rejects requests without ``instructions``; we must provide a
    default when the chat-completions client doesn't send a system message."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload={"model": "openai/gpt-5.4"},
        messages=[{"role": "user", "content": "hi"}],
    )

    assert isinstance(upstream["instructions"], str)
    assert upstream["instructions"].strip(), "instructions must be a non-empty string"


def test_build_openai_codex_payload_from_chat_completion_translates_tool_round_trip():
    """Assistant tool_calls and tool results from a previous turn must be
    translated to Codex ``function_call``/``function_call_output`` items so
    Codex can resume after a tool call (which is when Hermes was failing)."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    payload = {"model": "openai/gpt-5.4"}
    messages = [
        {"role": "system", "content": "You can use tools."},
        {"role": "user", "content": "Pay $50 to Jon"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_xyz",
                    "type": "function",
                    "function": {
                        "name": "pay",
                        "arguments": '{"amount":50,"recipient":"Jon"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_xyz",
            "content": "Payment of $50 to Jon submitted",
        },
    ]

    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload=payload, messages=messages
    )

    assert upstream["instructions"] == "You can use tools."
    types = [item["type"] for item in upstream["input"]]
    assert types == ["message", "function_call", "function_call_output"]

    function_call = upstream["input"][1]
    assert function_call == {
        "type": "function_call",
        "call_id": "call_xyz",
        "name": "pay",
        "arguments": '{"amount":50,"recipient":"Jon"}',
    }
    function_output = upstream["input"][2]
    assert function_output == {
        "type": "function_call_output",
        "call_id": "call_xyz",
        "output": "Payment of $50 to Jon submitted",
    }


def test_build_openai_codex_payload_assistant_text_uses_output_text():
    """Assistant text content in chat history must use ``output_text`` rather
    than ``input_text`` when re-encoded for the Codex Responses API."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload={"model": "openai/gpt-5.4"},
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello!"},
        ],
    )
    assistant_item = next(
        item
        for item in upstream["input"]
        if item.get("type") == "message" and item.get("role") == "assistant"
    )
    assert assistant_item["content"] == [
        {"type": "output_text", "text": "hello!"},
    ]


def test_build_openai_codex_payload_drops_orphan_tool_messages():
    """A ``role: tool`` message with no ``tool_call_id`` cannot be linked to
    any prior call, so it must be dropped to avoid a malformed Codex request."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload={"model": "openai/gpt-5.4"},
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "stray"},
        ],
    )
    assert all(item.get("type") != "function_call_output" for item in upstream["input"])


def test_build_openai_codex_payload_sets_store_false():
    """Codex rejects requests without ``store: false`` (HTTP 400 "Store must
    be set to false"). The chat-completions translator must set it explicitly,
    just like the native codex-cli does."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload={"model": "openai/gpt-5.4"},
        messages=[{"role": "user", "content": "hi"}],
    )
    assert upstream["store"] is False


def test_build_openai_codex_payload_overrides_model_with_ai_model_identifier():
    """The Codex backend identifies models by the upstream provider id
    (e.g. ``gpt-5-codex``), not the gateway alias the chat-completions client
    sent. When an ``ai_model`` is passed in we must use its identifier so the
    upstream call resolves correctly."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    ai_model = SimpleNamespace(model_identifier="gpt-5-codex")

    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload={"model": "openai/gpt-5.4"},
        messages=[{"role": "user", "content": "hi"}],
        ai_model=ai_model,
    )
    assert upstream["model"] == "gpt-5-codex"


def _codex_service() -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context)


def _codex_ai_model() -> SimpleNamespace:
    return SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-5.6-sol",
        api_endpoint="https://chatgpt.com/backend-api/codex",
    )


def test_build_openai_codex_payload_from_chat_completion_drops_rejected_tunables(
    caplog,
):
    """The Codex backend 400s on ``max_completion_tokens`` (prod failure
    2026-07-29 22:22 UTC, follow-up to #109) and on every other OpenAI
    tunable; none of them may be forwarded, and the drop must be logged."""
    service = _codex_service()
    payload = {
        "model": "openai/gpt-5.6-sol",
        "max_completion_tokens": 1024,
        "max_tokens": 512,
        "temperature": 0.7,
        "top_p": 0.9,
        "frequency_penalty": 0.1,
        "presence_penalty": 0.1,
        "seed": 42,
        "stop": ["END"],
        "user": "hermes",
        "response_format": {"type": "text"},
        "parallel_tool_calls": True,
    }

    with caplog.at_level("DEBUG", logger="preloop.services.openai_gateway"):
        upstream = service._build_openai_codex_payload_from_chat_completion(
            payload=payload,
            messages=[{"role": "user", "content": "hi"}],
        )

    for rejected in (
        "max_completion_tokens",
        "max_tokens",
        "max_output_tokens",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "stop",
        "user",
        "response_format",
    ):
        assert rejected not in upstream, rejected
    # The one tunable Codex accepts must survive.
    assert upstream["parallel_tool_calls"] is True
    assert upstream["store"] is False
    dropped_logs = [
        record.message
        for record in caplog.records
        if "unsupported by the OpenAI Codex backend" in record.message
    ]
    assert dropped_logs, "dropped parameters must be logged at debug level"
    assert "max_completion_tokens" in dropped_logs[0]


def test_build_openai_codex_payload_from_chat_completion_maps_reasoning_effort():
    """chat-completions ``reasoning_effort`` maps to the accepted
    Responses-API ``reasoning.effort`` shape instead of being dropped."""
    service = _codex_service()

    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload={"model": "openai/gpt-5.6-sol", "reasoning_effort": "low"},
        messages=[{"role": "user", "content": "hi"}],
    )

    assert upstream["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in upstream


def test_build_openai_codex_payload_sanitizes_responses_payload(caplog):
    """The direct Responses-API codex path (``_build_openai_codex_payload``)
    must strip rejected params too and force ``store: false``."""
    service = _codex_service()
    ai_model = _codex_ai_model()
    payload = {
        "model": "openai/gpt-5.6-sol",
        "instructions": "Be terse.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
        "max_output_tokens": 256,
        "max_completion_tokens": 256,
        "temperature": 0.3,
        "metadata": {"k": "v"},
        "truncation": "auto",
        "store": True,
        "reasoning": {"effort": "medium"},
        "prompt_cache_key": "cache-1",
    }

    with caplog.at_level("DEBUG", logger="preloop.services.openai_gateway"):
        upstream = service._build_openai_codex_payload(ai_model, payload, stream=True)

    for rejected in (
        "max_output_tokens",
        "max_completion_tokens",
        "temperature",
        "metadata",
        "truncation",
    ):
        assert rejected not in upstream, rejected
    assert upstream["model"] == "gpt-5.6-sol"
    assert upstream["store"] is False
    assert upstream["stream"] is True
    assert upstream["instructions"] == "Be terse."
    assert upstream["reasoning"] == {"effort": "medium"}
    assert upstream["prompt_cache_key"] == "cache-1"
    assert any(
        "unsupported by the OpenAI Codex backend" in record.message
        for record in caplog.records
    )


def test_create_chat_completion_codex_sanitizes_client_params():
    """Non-streaming codex chat completions must not forward
    ``max_completion_tokens`` upstream (the exact prod failure shape)."""
    service = _codex_service()
    ai_model = _codex_ai_model()

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service,
            "_create_openai_codex_response",
            return_value=_codex_responses_payload(),
        ) as mock_create_codex,
    ):
        service.create_chat_completion(
            {
                "model": "preloop/openai/gpt-5.6-sol",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_completion_tokens": 4096,
                "temperature": 0.2,
            }
        )

    upstream_payload = mock_create_codex.call_args.args[1]
    assert "max_completion_tokens" not in upstream_payload
    assert "temperature" not in upstream_payload
    assert upstream_payload["store"] is False


def test_stream_chat_completion_codex_sanitizes_client_params():
    """Streaming codex chat completions must not forward
    ``max_completion_tokens`` upstream either."""
    service = _codex_service()
    ai_model = _codex_ai_model()

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service,
            "_create_openai_codex_response",
            return_value=_codex_responses_payload(),
        ) as mock_create_codex,
    ):
        events = list(
            service.stream_chat_completion(
                {
                    "model": "preloop/openai/gpt-5.6-sol",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                    "max_completion_tokens": 4096,
                    "top_p": 0.9,
                }
            )
        )

    assert events, "stream must still produce chunks"
    upstream_payload = mock_create_codex.call_args.args[1]
    assert "max_completion_tokens" not in upstream_payload
    assert "top_p" not in upstream_payload
    assert upstream_payload["store"] is False


def test_create_openai_codex_response_sends_sanitized_body():
    """The wire body sent to chatgpt.com/backend-api/codex must contain only
    supported parameters (covers create_response and stream_response, which
    both sanitize inside ``_create_openai_codex_response``)."""
    service = _codex_service()
    ai_model = _codex_ai_model()
    credentials = SimpleNamespace(
        value="oauth-token",
        payload={"account_id": "acct-1"},
    )
    sse_body = (
        b'data: {"type":"response.output_item.added","item":'
        b'{"id":"msg_1","type":"message","role":"assistant"}}\n'
        b"\n"
        b'data: {"type":"response.output_text.done","item_id":"msg_1",'
        b'"text":"OK"}\n'
        b"\n"
        b'data: {"type":"response.completed","response":{"id":"resp_1",'
        b'"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        b"\n"
    )

    class _FakeResponse:
        def __enter__(self):
            return iter(sse_body.splitlines(keepends=True))

        def __exit__(self, *args):
            return False

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse()

    with (
        patch.object(
            service, "_resolve_openai_codex_credentials", return_value=credentials
        ),
        patch(
            "preloop.services.openai_gateway.urllib_request.urlopen",
            side_effect=fake_urlopen,
        ),
    ):
        service._create_openai_codex_response(
            ai_model,
            {
                "model": "preloop/openai/gpt-5.6-sol",
                "instructions": "Be terse.",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                    }
                ],
                "max_completion_tokens": 128,
                "temperature": 0.5,
            },
        )

    body = captured["body"]
    assert "max_completion_tokens" not in body
    assert "temperature" not in body
    assert body["store"] is False
    assert body["stream"] is True
    assert body["model"] == "gpt-5.6-sol"


def _anthropic_oauth_model() -> SimpleNamespace:
    return SimpleNamespace(
        id="model-a1",
        provider_name="anthropic",
        model_identifier="claude-fable-5",
        api_endpoint=None,
    )


def test_prepare_anthropic_passthrough_translates_and_drops_openai_params(
    caplog,
):
    """OpenAI-dialect strays must be removed before the OAuth passthrough:
    one unknown top-level key 400s the whole Anthropic request. Token-limit
    spellings translate to ``max_tokens``; Anthropic-native content
    (system blocks, cache_control, tools) stays byte-identical."""
    service = _codex_service()
    ai_model = _anthropic_oauth_model()
    system_blocks = [
        {
            "type": "text",
            "text": "You are Claude Code",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages = [{"role": "user", "content": "hi"}]
    tools = [
        {
            "name": "get_time",
            "description": "time",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    payload = {
        "model": "claude-fable-5",
        "system": system_blocks,
        "messages": messages,
        "tools": tools,
        "max_completion_tokens": 2048,
        "frequency_penalty": 0.2,
        "seed": 7,
        "stream_options": {"include_usage": True},
        "metadata": {"user_id": "u-1"},
    }

    with (
        patch.object(
            service, "_strip_anthropic_passthrough_tools", side_effect=lambda p: p
        ),
        patch.object(service, "_capture_tools_meta"),
        caplog.at_level("DEBUG", logger="preloop.services.openai_gateway"),
    ):
        _url, _headers, body = service._prepare_anthropic_passthrough(
            ai_model=ai_model,
            payload=payload,
            oauth_token="oauth-token",
            anthropic_version="2023-06-01",
            anthropic_beta=None,
            stream=False,
        )

    # OpenAI token-limit spelling translated to the Anthropic one.
    assert body["max_tokens"] == 2048
    assert "max_completion_tokens" not in body
    for stray in ("frequency_penalty", "seed", "stream_options"):
        assert stray not in body, stray
    # Anthropic-native fields forwarded verbatim (same objects, untouched).
    assert body["system"] is system_blocks
    assert body["messages"] is messages
    assert body["tools"] is tools
    # ``metadata`` is a real Anthropic Messages parameter; it must survive.
    assert body["metadata"] == {"user_id": "u-1"}
    assert body["stream"] is False
    assert any(
        "unsupported by the Anthropic Messages API" in record.message
        for record in caplog.records
    )


def test_prepare_anthropic_passthrough_keeps_client_max_tokens():
    """When the client already sent ``max_tokens``, the OpenAI spellings are
    dropped rather than overwriting it."""
    service = _codex_service()
    ai_model = _anthropic_oauth_model()
    payload = {
        "model": "claude-fable-5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "max_completion_tokens": 999,
        "max_output_tokens": 888,
    }

    with (
        patch.object(
            service, "_strip_anthropic_passthrough_tools", side_effect=lambda p: p
        ),
        patch.object(service, "_capture_tools_meta"),
    ):
        _url, _headers, body = service._prepare_anthropic_passthrough(
            ai_model=ai_model,
            payload=payload,
            oauth_token="oauth-token",
            anthropic_version="2023-06-01",
            anthropic_beta=None,
            stream=True,
        )

    assert body["max_tokens"] == 100
    assert "max_completion_tokens" not in body
    assert "max_output_tokens" not in body
    assert body["stream"] is True


def test_create_message_passthrough_sends_sanitized_body():
    """Non-streaming OAuth passthrough requests are sanitized end to end."""
    service = _codex_service()
    ai_model = _anthropic_oauth_model()

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service, "_anthropic_oauth_passthrough_token", return_value="oauth-token"
        ),
        patch.object(
            service, "_strip_anthropic_passthrough_tools", side_effect=lambda p: p
        ),
        patch.object(service, "_capture_tools_meta"),
        patch.object(
            service,
            "_anthropic_oauth_passthrough_complete",
            return_value={
                "id": "msg_1",
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ) as mock_complete,
    ):
        service.create_message(
            {
                "model": "claude-fable-5",
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 64,
                "seed": 3,
            },
            anthropic_version="2023-06-01",
        )

    body = mock_complete.call_args.kwargs["body"]
    assert body["max_tokens"] == 64
    assert "max_completion_tokens" not in body
    assert "seed" not in body


def test_stream_message_passthrough_sends_sanitized_body():
    """Streaming OAuth passthrough requests are sanitized end to end."""
    service = _codex_service()
    ai_model = _anthropic_oauth_model()

    upstream_response = MagicMock()
    upstream_response.iter_text.return_value = iter(
        [
            (
                'data: {"type":"message_start","message":{"id":"msg_1",'
                '"usage":{"input_tokens":1}}}\n\n'
            ),
            (
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                '"usage":{"output_tokens":1}}\n\n'
            ),
        ]
    )
    upstream_client = MagicMock()

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service, "_anthropic_oauth_passthrough_token", return_value="oauth-token"
        ),
        patch.object(
            service, "_strip_anthropic_passthrough_tools", side_effect=lambda p: p
        ),
        patch.object(service, "_capture_tools_meta"),
        patch.object(
            service,
            "_open_anthropic_oauth_passthrough_stream",
            return_value=(upstream_client, upstream_response),
        ) as mock_open,
    ):
        events = list(
            service.stream_message(
                {
                    "model": "claude-fable-5",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                    "max_output_tokens": 32,
                    "frequency_penalty": 0.5,
                },
                anthropic_version="2023-06-01",
            )
        )

    assert events, "passthrough stream must relay upstream chunks"
    body = mock_open.call_args.kwargs["body"]
    assert body["max_tokens"] == 32
    assert "max_output_tokens" not in body
    assert "frequency_penalty" not in body
    assert body["stream"] is True


def test_aggregate_codex_sse_stream_builds_assistant_message_from_streaming_events():
    """An assistant text turn must be reconstructed from
    ``output_item.added`` + ``output_text.delta`` + ``output_text.done``,
    independent of whether the giant ``response.completed`` event is present
    or parseable (chatgpt.com sometimes truncates that event — see
    vercel/ai#14473)."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    sse_lines = [
        b'data: {"type":"response.created","response":{"id":"resp_codex_msg"}}\n',
        b"\n",
        b'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"msg_1","type":"message","role":"assistant","content":[]}}\n',
        b"\n",
        b'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":"hello "}\n',
        b"\n",
        b'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,"content_index":0,"delta":"world"}\n',
        b"\n",
        b'data: {"type":"response.output_text.done","item_id":"msg_1","output_index":0,"content_index":0,"text":"hello world"}\n',
        b"\n",
        b'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"msg_1","type":"message","role":"assistant","content":[{"type":"output_text","text":"hello world"}]}}\n',
        b"\n",
    ]

    result = service._aggregate_codex_sse_stream(iter(sse_lines))

    assert result["id"] == "resp_codex_msg"
    assert result["output_text"] == "hello world"
    assert len(result["output"]) == 1
    msg = result["output"][0]
    assert msg["type"] == "message"
    assert msg["role"] == "assistant"
    assert msg["content"] == [{"type": "output_text", "text": "hello world"}]


def test_aggregate_codex_sse_stream_captures_function_call_with_arguments_deltas():
    """A tool-only turn (no text deltas) must still surface the
    ``function_call`` item with its accumulated arguments. This is the
    failure mode Hermes hit when asking ``pay $6 to Joe`` — the previous
    aggregator returned an empty output because it relied on
    ``response.completed`` and on text deltas, both of which are
    insufficient for tool-only turns."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    sse_lines = [
        b'data: {"type":"response.created","response":{"id":"resp_codex_tool"}}\n',
        b"\n",
        b'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"fc_1","type":"function_call","call_id":"call_abc","name":"pay","arguments":""}}\n',
        b"\n",
        b'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":"{\\"amount\\":6"}\n',
        b"\n",
        b'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":",\\"recipient\\":\\"Joe\\"}"}\n',
        b"\n",
        b'data: {"type":"response.function_call_arguments.done","item_id":"fc_1","output_index":0,"arguments":"{\\"amount\\":6,\\"recipient\\":\\"Joe\\"}"}\n',
        b"\n",
        b'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"fc_1","type":"function_call","call_id":"call_abc","name":"pay","arguments":"{\\"amount\\":6,\\"recipient\\":\\"Joe\\"}","status":"completed"}}\n',
        b"\n",
    ]

    result = service._aggregate_codex_sse_stream(iter(sse_lines))

    assert result["id"] == "resp_codex_tool"
    assert result["output_text"] == ""
    assert len(result["output"]) == 1
    fc = result["output"][0]
    assert fc["type"] == "function_call"
    assert fc["name"] == "pay"
    assert fc["call_id"] == "call_abc"
    assert fc["arguments"] == '{"amount":6,"recipient":"Joe"}'

    # The whole point: the chat-completion converter must turn this into a
    # ``tool_calls`` array so Hermes sees something other than an empty
    # response.
    chat = service._codex_response_to_chat_completion_dict(result)
    message = chat["choices"][0]["message"]
    assert message["content"] == ""
    assert message["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {
                "name": "pay",
                "arguments": '{"amount":6,"recipient":"Joe"}',
            },
        }
    ]
    assert chat["choices"][0]["finish_reason"] == "tool_calls"


def test_aggregate_codex_sse_stream_skips_truncated_response_completed_event():
    """If ``response.completed`` arrives truncated (mirrors the
    vercel/ai#14473 reproduction), aggregation must still produce a valid
    response from the smaller streaming events."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    sse_lines = [
        b'data: {"type":"response.created","response":{"id":"resp_trunc"}}\n',
        b"\n",
        b'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"msg_x","type":"message","role":"assistant","content":[]}}\n',
        b"\n",
        b'data: {"type":"response.output_text.delta","item_id":"msg_x","output_index":0,"content_index":0,"delta":"ok"}\n',
        b"\n",
        b'data: {"type":"response.output_text.done","item_id":"msg_x","output_index":0,"content_index":0,"text":"ok"}\n',
        b"\n",
        b'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"msg_x","type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}\n',
        b"\n",
        # Truncated ``response.completed`` — invalid JSON. Must be skipped.
        b'data: {"type":"response.completed","response":{"id":"resp_trunc","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"o\n',
        b"\n",
    ]

    result = service._aggregate_codex_sse_stream(iter(sse_lines))

    assert result["id"] == "resp_trunc"
    assert result["output_text"] == "ok"
    assert result["output"][0]["content"] == [{"type": "output_text", "text": "ok"}]


def test_aggregate_codex_sse_stream_raises_on_response_failed_event():
    """A ``response.failed`` event must surface as a ``ModelGatewayAPIError``
    so the gateway records a proper error and the client sees a non-empty
    failure response instead of an empty success."""
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    sse_lines = [
        b'data: {"type":"response.created","response":{"id":"resp_err"}}\n',
        b"\n",
        b'data: {"type":"response.failed","response":{"id":"resp_err","error":{"message":"upstream blew up"}}}\n',
        b"\n",
    ]

    with pytest.raises(ModelGatewayAPIError) as exc_info:
        service._aggregate_codex_sse_stream(iter(sse_lines))
    assert "upstream blew up" in str(exc_info.value)


def test_iter_sse_events_skips_non_data_lines_and_done_marker():
    """The SSE parser must ignore ``event:``, ``id:``, comments, and the
    ``[DONE]`` sentinel, and must only surface JSON ``data:`` payloads."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    sse_lines = [
        b": comment\n",
        b"event: response.foo\n",
        b"id: 1\n",
        b'data: {"type":"response.foo","ok":true}\n',
        b"\n",
        b"data: [DONE]\n",
        b"\n",
    ]

    events = list(service._iter_sse_events(iter(sse_lines)))
    assert events == [{"type": "response.foo", "ok": True}]


def test_build_openai_codex_payload_flattens_chat_completion_tools():
    """Chat-completions tools nest the function spec under ``function``; the
    Codex Responses API expects ``name``/``description``/``parameters`` to be
    on the tool entry itself. Sending the chat-completions shape unchanged
    triggers ``HTTP 400: Missing required parameter: 'tools[0].name'``."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    payload = {
        "model": "openai/gpt-5.4",
        "messages": [],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "pay",
                    "description": "Send a payment",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "number"},
                            "recipient": {"type": "string"},
                        },
                        "required": ["amount", "recipient"],
                    },
                    "strict": True,
                },
            }
        ],
        "tool_choice": "auto",
    }

    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload=payload,
        messages=[],
    )

    assert upstream["tools"] == [
        {
            "type": "function",
            "name": "pay",
            "description": "Send a payment",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "recipient": {"type": "string"},
                },
                "required": ["amount", "recipient"],
            },
            "strict": True,
        }
    ]
    assert upstream["tool_choice"] == "auto"


def test_build_openai_codex_payload_flattens_forced_tool_choice():
    """A forced tool_choice ``{"type": "function", "function": {"name": ...}}``
    must be flattened to ``{"type": "function", "name": ...}`` for Codex."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload={
            "model": "openai/gpt-5.4",
            "messages": [],
            "tool_choice": {"type": "function", "function": {"name": "pay"}},
        },
        messages=[],
    )

    assert upstream["tool_choice"] == {"type": "function", "name": "pay"}


def test_build_openai_codex_payload_passes_through_responses_api_tools():
    """Tools already in Responses-API shape (no nested ``function`` field)
    must be forwarded verbatim, so callers can pre-translate when needed."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    already_flat = [
        {
            "type": "function",
            "name": "pay",
            "description": "Send a payment",
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    upstream = service._build_openai_codex_payload_from_chat_completion(
        payload={
            "model": "openai/gpt-5.4",
            "messages": [],
            "tools": already_flat,
        },
        messages=[],
    )

    assert upstream["tools"] == already_flat


def _gateway_model(name: str, alias: str) -> SimpleNamespace:
    """Build a gateway-enabled AIModel stand-in with an explicit alias."""
    return SimpleNamespace(
        id=name,
        name=name,
        provider_name="anthropic",
        model_identifier=alias,
        api_endpoint=None,
        is_default=False,
        model_parameters=None,
        meta_data={"gateway": {"enabled": True, "model_alias": alias}},
    )


def _resolver_service(models):
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    service._get_account_models = lambda: list(models)  # type: ignore[method-assign]
    return service


def test_resolve_requested_model_prefers_exact_match_over_earlier_suffix_match():
    """An exact alias match must win even when a suffix match comes first.

    Red under the old resolver: it returned on the first suffix match, so the
    exact row was never reached and the request was priced against the wrong model.
    """
    suffix_row = _gateway_model("suffix", "anthropic/claude-sonnet-4-5")
    exact_row = _gateway_model("exact", "claude-sonnet-4-5")
    service = _resolver_service([suffix_row, exact_row])

    resolved = service._resolve_requested_model(
        "claude-sonnet-4-5", provider="anthropic"
    )

    assert resolved is exact_row


def test_resolve_requested_model_exact_match_wins_regardless_of_position():
    """The exact-match rule must not depend on list position."""
    exact_row = _gateway_model("exact", "claude-sonnet-4-5")
    suffix_row = _gateway_model("suffix", "anthropic/claude-sonnet-4-5")

    for ordering in ([suffix_row, exact_row], [exact_row, suffix_row]):
        service = _resolver_service(ordering)
        resolved = service._resolve_requested_model(
            "claude-sonnet-4-5", provider="anthropic"
        )
        assert resolved is exact_row


def test_resolve_requested_model_suffix_match_is_stable_and_repeatable():
    """With only suffix candidates, the first in the given order wins, every time."""
    first = _gateway_model("first", "anthropic/claude-sonnet-4-5")
    second = _gateway_model("second", "bedrock/claude-sonnet-4-5")
    service = _resolver_service([first, second])

    resolved = [
        service._resolve_requested_model("claude-sonnet-4-5", provider="anthropic")
        for _ in range(5)
    ]

    assert all(row is first for row in resolved)


def test_resolve_requested_model_unknown_model_still_404s():
    """Unknown models must keep raising a loud 404 rather than falling back."""
    service = _resolver_service([_gateway_model("only", "anthropic/claude-opus-4-1")])

    with pytest.raises(ModelGatewayAPIError) as excinfo:
        service._resolve_requested_model("gpt-5", provider="anthropic")

    assert excinfo.value.status_code == 404


def _codex_freeform_service() -> OpenAIGatewayService:
    """A service whose current request declared a freeform `apply_patch`."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    service._codex_freeform_tool_names = {"apply_patch"}
    return service


def test_stream_response_emits_freeform_tool_call_as_custom_tool_call():
    """Codex aborts the run if its freeform tool is answered as a function_call.

    Verified live against `codex-cli`: a `function_call` reply produces
    `Fatal error: tool apply_patch invoked with incompatible payload` and no
    file is written, while a `custom_tool_call` carrying the raw patch text
    applies cleanly. Both prod failures were on this streaming path
    (`endpoint_kind: responses_stream`), so it is the path that must be right.
    """
    service = _codex_freeform_service()
    ai_model = SimpleNamespace(id="model-1", provider_name="openai")
    upstream_stream = iter(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "apply_patch"},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": '{"input": "*** Begin Patch"}'
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        ]
    )

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_call_litellm", return_value=upstream_stream),
        patch.object(service, "_record_gateway_request"),
    ):
        events = [
            _parse_sse_payload(event)
            for event in service.stream_response(
                {"model": "openai/gpt-5", "input": "Edit the file"}
            )
        ]

    added = [
        event
        for event in events
        if isinstance(event, dict) and event.get("type") == "response.output_item.added"
    ]
    # The item is announced as custom_tool_call from the very first event: a
    # client that saw `function_call` first would already have rejected it.
    assert len(added) == 1
    assert added[0]["item"]["type"] == "custom_tool_call"
    assert added[0]["item"]["name"] == "apply_patch"

    completed = events[-2]
    output_item = completed["response"]["output"][0]
    assert output_item["type"] == "custom_tool_call"
    assert output_item["call_id"] == "call_1"
    # Raw patch text, not the JSON envelope the model was asked to produce.
    assert output_item["input"] == "*** Begin Patch"
    assert "arguments" not in output_item

    # No function-call argument deltas may leak for a freeform tool.
    assert not [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("type", "").startswith("response.function_call_arguments")
    ]


def test_stream_response_still_emits_plain_function_calls_normally():
    """A non-freeform tool keeps the ordinary function_call event sequence."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    ai_model = SimpleNamespace(id="model-1", provider_name="openai")
    upstream_stream = iter(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "exec_command",
                                        "arguments": '{"cmd":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ' "ls"}'}}
                            ]
                        }
                    }
                ]
            },
        ]
    )

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_call_litellm", return_value=upstream_stream),
        patch.object(service, "_record_gateway_request"),
    ):
        events = [
            _parse_sse_payload(event)
            for event in service.stream_response(
                {"model": "openai/gpt-5", "input": "List files"}
            )
        ]

    types = [
        event["type"] for event in events if isinstance(event, dict) and "type" in event
    ]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    assert "response.custom_tool_call_input.done" not in types

    output_item = events[-2]["response"]["output"][0]
    assert output_item["type"] == "function_call"
    assert output_item["arguments"] == '{"cmd": "ls"}'


def test_responses_input_accepts_echoed_custom_tool_call_history():
    """Turn 2 of every Codex session replays these items; they must not 400."""
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)

    messages = service._normalize_responses_input(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": "Edit the file",
                },
                {
                    "type": "custom_tool_call",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "input": "*** Begin Patch",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_1",
                    "output": "Success. Updated: hello.txt",
                },
            ]
        }
    )

    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["id"] == "call_1"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "apply_patch"
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Success. Updated: hello.txt",
    }


def test_create_response_round_trips_a_freeform_tool_call():
    """Full request->response symmetry on the non-streaming path."""
    service = _codex_freeform_service()
    ai_model = SimpleNamespace(id="model-1", provider_name="openai")

    upstream = {
        "id": "chatcmpl_1",
        "created": 1710000000,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "apply_patch",
                                "arguments": '{"input": "*** Begin Patch"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    with (
        patch.object(service, "_resolve_requested_model", return_value=ai_model),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_call_litellm", return_value=upstream),
        patch.object(service, "_record_gateway_request"),
        patch.object(service, "_emit_gateway_request_started"),
    ):
        response = service.create_response(
            {
                "model": "openai/gpt-5",
                "input": "Edit the file",
                "tools": [
                    {
                        "type": "custom",
                        "name": "apply_patch",
                        "description": "Apply a patch",
                    }
                ],
            }
        )

    output_item = response["output"][0]
    assert output_item["type"] == "custom_tool_call"
    assert output_item["name"] == "apply_patch"
    assert output_item["input"] == "*** Begin Patch"


def _openrouter_service_and_model(**model_overrides):
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    upstream_backend = MagicMock()
    service = OpenAIGatewayService(
        MagicMock(), auth_context, upstream_backend=upstream_backend
    )
    model_kwargs = {
        "provider_name": "openrouter",
        "model_identifier": "openrouter/auto-beta",
        "api_endpoint": "https://openrouter.ai/api/v1",
        "meta_data": {},
    }
    model_kwargs.update(model_overrides)
    return service, SimpleNamespace(**model_kwargs), upstream_backend


def _call_with_api_key(service, ai_model, *, payload=None, stream=False):
    with patch(
        "preloop.services.openai_gateway.get_secret_service"
    ) as mock_secret_service:
        mock_secret_service.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="sk-or-key")
        )
        service._call_litellm(
            ai_model,
            messages=[{"role": "user", "content": "Hello"}],
            payload=payload or {},
            stream=stream,
            provider="openai",
        )


def test_openrouter_upstream_requests_usage_accounting():
    """OpenRouter (direct provider) gets usage: {"include": true} so every
    response carries the authoritative cost (Auto Router has no catalog
    price; without this flag its usage stays unpriced)."""
    service, ai_model, backend = _openrouter_service_and_model()
    _call_with_api_key(service, ai_model)
    kwargs = backend.completion.call_args.kwargs
    assert kwargs["extra_body"]["usage"] == {"include": True}


def test_openai_compatible_openrouter_base_url_requests_usage_accounting():
    """OpenRouter reached via an openai-compatible base_url is still OpenRouter."""
    service, ai_model, backend = _openrouter_service_and_model(
        provider_name="openai-compatible",
    )
    _call_with_api_key(service, ai_model)
    kwargs = backend.completion.call_args.kwargs
    assert kwargs["extra_body"]["usage"] == {"include": True}


def test_openrouter_usage_accounting_applies_to_streaming_requests():
    """The flag rides on streamed requests too (final usage chunk carries cost)."""
    service, ai_model, backend = _openrouter_service_and_model()
    _call_with_api_key(service, ai_model, stream=True)
    kwargs = backend.completion.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["extra_body"]["usage"] == {"include": True}


def test_non_openrouter_upstreams_never_get_usage_accounting_flag():
    """The OpenRouter-only knob must not leak to other upstreams."""
    service, ai_model, backend = _openrouter_service_and_model(
        provider_name="openai",
        model_identifier="gpt-5",
        api_endpoint=None,
    )
    _call_with_api_key(service, ai_model)
    kwargs = backend.completion.call_args.kwargs
    assert "extra_body" not in kwargs


def test_openrouter_usage_accounting_config_gate(monkeypatch):
    """OPENROUTER_USAGE_ACCOUNTING=false disables the outbound flag."""
    monkeypatch.setenv("OPENROUTER_USAGE_ACCOUNTING", "false")
    service, ai_model, backend = _openrouter_service_and_model()
    _call_with_api_key(service, ai_model)
    kwargs = backend.completion.call_args.kwargs
    assert "extra_body" not in kwargs


def test_gateway_records_provider_reported_cost_as_authoritative(db_session, test_user):
    """An OpenRouter usage-accounting response prices the row from the
    provider ledger (cost_source='provider') and budget spend consumes it.

    The Auto Router (openrouter/auto-beta) has catalog price -1 by design, so
    without the provider-reported figure this row would land unpriced with
    zero spend.
    """
    from preloop.models.models.budget import BudgetSpendActivity

    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenRouter Auto",
            "provider_name": "openrouter",
            "model_identifier": "openrouter/auto-beta",
            "api_endpoint": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openrouter/auto-beta",
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
    litellm_response = {
        "id": "gen-abc",
        "created": 1710000000,
        "choices": [
            {
                "message": {"role": "assistant", "content": "routed answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 973,
            "completion_tokens": 15,
            "total_tokens": 988,
            "cost_details": {
                "upstream_inference_cost": 0.00001946,
                "upstream_inference_prompt_cost": 0.00001,
                "upstream_inference_completions_cost": 0.00000946,
            },
        },
    }

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=litellm_response,
    ) as mock_completion:
        service.create_chat_completion(
            {
                "model": "openrouter/auto-beta",
                "messages": [{"role": "user", "content": "Hello"}],
            }
        )

    # The outbound request asked OpenRouter for usage accounting.
    assert mock_completion.call_args.kwargs["extra_body"]["usage"] == {"include": True}

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/chat/completions")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.cost_source == "provider"
    assert usage_row.estimated_cost == pytest.approx(0.00001946)
    # No unpriced marker; the provider figure priced the row.
    assert usage_row.prompt_tokens == 973

    spend = (
        db_session.query(BudgetSpendActivity)
        .filter(
            BudgetSpendActivity.account_id == test_user.account_id,
            BudgetSpendActivity.subject_type == "account",
            BudgetSpendActivity.model_alias == "openrouter/auto-beta",
        )
        .first()
    )
    assert spend is not None
    assert float(spend.spend_usd) == pytest.approx(0.00001946)


def test_gateway_without_provider_cost_keeps_catalog_pricing(db_session, test_user):
    """No cost fields in usage -> catalog pricing exactly as before."""
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-4o",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-4o",
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
    litellm_response = {
        "id": "chatcmpl_1",
        "created": 1710000000,
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=litellm_response,
    ):
        service.create_chat_completion(
            {
                "model": "openai/gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            }
        )

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/chat/completions")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.cost_source == "catalog"
    assert usage_row.estimated_cost is not None and usage_row.estimated_cost > 0


def test_responses_endpoint_records_provider_cost_and_requests_accounting(
    db_session, test_user
):
    """The /responses path shares the flag injection and provider pricing.

    Both endpoint kinds build upstream kwargs via ``_build_completion_kwargs``,
    so this pins that the usage-accounting flag and provider-cost ingestion
    hold on the Responses API path too, not only chat completions.
    """
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenRouter Auto",
            "provider_name": "openai-compatible",
            "model_identifier": "openrouter/auto-beta",
            "api_endpoint": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openrouter/auto-beta",
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
    litellm_response = {
        "id": "gen-resp",
        "created": 1710000000,
        "choices": [
            {
                "message": {"role": "assistant", "content": "routed"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 42,
            "completion_tokens": 7,
            "total_tokens": 49,
            "cost": 0.0000305,
        },
    }

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=litellm_response,
    ) as mock_completion:
        service.create_response(
            {
                "model": "openrouter/auto-beta",
                "input": "Hello",
            }
        )

    assert mock_completion.call_args.kwargs["extra_body"]["usage"] == {"include": True}

    usage_row = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/responses")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    assert usage_row is not None
    assert usage_row.cost_source == "provider"
    assert usage_row.estimated_cost == pytest.approx(0.0000305)


# ---------------------------------------------------------------------------
# #219: provider-reported cost must survive litellm's STREAMING transcode.
#
# litellm's CustomStreamWrapper rebuilds the final usage chunk from token
# counts only (stream_chunk_builder.calculate_usage), so OpenRouter's
# usage-accounting fields (usage.cost / usage.cost_details) are dropped from
# the transcoded stream even though the raw provider chunk carried them. The
# tests below drive the REAL litellm streaming pipeline (mocked HTTP
# transport, no network) so the transcode is exercised as in production --
# the #208 tests mocked litellm.completion with plain dicts and missed this.
# ---------------------------------------------------------------------------

_OPENROUTER_STREAM_USAGE = {
    "prompt_tokens": 973,
    "completion_tokens": 15,
    "total_tokens": 988,
    "prompt_tokens_details": {"cached_tokens": 100},
    "cost": 0.0000305,
    "cost_details": {"upstream_inference_cost": 0.00001946},
}

# cost >= upstream_inference_cost is the credits shape: usage.cost is the
# full charge and cost_details is informational, so provider_reported_cost
# takes cost alone -- summing would double-count (#224).
_OPENROUTER_EXPECTED_TOTAL = 0.0000305


def _real_litellm_openrouter_stream(raw_usage):
    """Build a real litellm CustomStreamWrapper over raw OpenRouter SSE chunks.

    Uses litellm's own HTTP handler with a mocked httpx transport, so the
    chunks flow through the genuine OpenRouter chunk parser and streaming
    transcode -- exactly what the gateway consumes in production.
    """
    import httpx
    import litellm
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    def sse(data):
        return f"data: {json.dumps(data)}\n\n".encode()

    body = b"".join(
        [
            sse(
                {
                    "id": "gen-stream-1",
                    "object": "chat.completion.chunk",
                    "created": 1710000000,
                    "model": "openrouter/auto-beta",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "Hel"},
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            sse(
                {
                    "id": "gen-stream-1",
                    "object": "chat.completion.chunk",
                    "created": 1710000000,
                    "model": "openrouter/auto-beta",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "lo"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ),
            # OpenRouter's final usage-accounting chunk: empty choices, usage
            # with cost + cost_details (per their usage accounting docs).
            sse(
                {
                    "id": "gen-stream-1",
                    "object": "chat.completion.chunk",
                    "created": 1710000000,
                    "model": "openrouter/auto-beta",
                    "choices": [],
                    "usage": raw_usage,
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )

    def handler(request: "httpx.Request") -> "httpx.Response":
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    client = HTTPHandler(client=httpx.Client(transport=httpx.MockTransport(handler)))
    return litellm.completion(
        model="openrouter/openrouter/auto-beta",
        messages=[{"role": "user", "content": "Hello"}],
        api_key="sk-or-test",
        stream=True,
        stream_options={"include_usage": True},
        extra_body={"usage": {"include": True}},
        client=client,
    )


def _create_openrouter_gateway_model(db_session, test_user):
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenRouter Auto",
            "provider_name": "openrouter",
            "model_identifier": "openrouter/auto-beta",
            "api_endpoint": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openrouter/auto-beta",
                    "provider_adapter": "preloop",
                }
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )


def _latest_usage_row(db_session, endpoint):
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == endpoint)
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )


def test_responses_stream_persists_openrouter_provider_cost(db_session, test_user):
    """Streaming /responses via OpenRouter prices the row from usage.cost.

    Regression for #219: all post-#208 prod rows (streaming /responses) stayed
    unpriced because litellm's synthetic final usage chunk drops the cost
    fields.
    """
    _create_openrouter_gateway_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    stream = _real_litellm_openrouter_stream(dict(_OPENROUTER_STREAM_USAGE))

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=stream,
    ):
        events = list(
            service.stream_response(
                {"model": "openrouter/auto-beta", "input": "Hello", "stream": True}
            )
        )
    assert any('"response.completed"' in event for event in events)

    usage_row = _latest_usage_row(db_session, "/openai/v1/responses")
    assert usage_row is not None
    usage_details = (usage_row.meta_data or {}).get("usage_details") or {}
    # Token detail must be intact...
    assert usage_details.get("prompt_tokens") == 973
    assert usage_details.get("prompt_tokens_details", {}).get("cached_tokens") == 100
    # ...AND the provider-reported cost fields must survive persistence.
    assert usage_details.get("cost") == pytest.approx(0.0000305)
    assert usage_details.get("cost_details", {}).get(
        "upstream_inference_cost"
    ) == pytest.approx(0.00001946)
    assert usage_row.cost_source == "provider"
    assert usage_row.estimated_cost == pytest.approx(_OPENROUTER_EXPECTED_TOTAL)


def test_chat_completions_stream_persists_openrouter_provider_cost(
    db_session, test_user
):
    """Streaming /chat/completions shares the same recovery path."""
    _create_openrouter_gateway_model(db_session, test_user)
    service = OpenAIGatewayService(
        db_session, ModelGatewayAuthContext(token="t", user=test_user)
    )
    stream = _real_litellm_openrouter_stream(dict(_OPENROUTER_STREAM_USAGE))

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=stream,
    ):
        events = list(
            service.stream_chat_completion(
                {
                    "model": "openrouter/auto-beta",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                }
            )
        )
    assert events  # stream produced output

    usage_row = _latest_usage_row(db_session, "/openai/v1/chat/completions")
    assert usage_row is not None
    usage_details = (usage_row.meta_data or {}).get("usage_details") or {}
    assert usage_details.get("cost") == pytest.approx(0.0000305)
    assert usage_details.get("cost_details", {}).get(
        "upstream_inference_cost"
    ) == pytest.approx(0.00001946)
    assert usage_row.cost_source == "provider"
    assert usage_row.estimated_cost == pytest.approx(_OPENROUTER_EXPECTED_TOTAL)


def test_provider_cost_fields_recovery_is_fail_open():
    """Absent litellm internals must never break the stream accounting."""
    assert OpenAIGatewayService._provider_cost_fields(iter([])) == {}
    assert OpenAIGatewayService._provider_cost_fields(None) == {}
    assert (
        OpenAIGatewayService._provider_cost_fields(SimpleNamespace(chunks="nope")) == {}
    )
