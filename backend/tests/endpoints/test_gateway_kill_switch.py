"""Gateway enforcement tests for the account kill switch (#157).

While the ``gateway`` scope of the kill switch is active, every
model-gateway ingress (OpenAI, Anthropic, and — via the shared Responses
path — Gemini) must reject requests with a distinct 403 before any
upstream dispatch, and record the rejection on the usage ledger.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from preloop.api.endpoints.anthropic_gateway import get_anthropic_gateway_auth_context
from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models.crud import crud_account_halt, crud_ai_model
from preloop.models.models.api_usage import ApiUsage
from preloop.services.kill_switch import invalidate_kill_switch_cache
from preloop.services.model_gateway_auth import ModelGatewayAuthContext

ALL_SCOPES = ["gateway", "tools", "flows"]


@pytest.fixture(autouse=True)
def _clear_kill_switch_cache():
    invalidate_kill_switch_cache()
    yield
    invalidate_kill_switch_cache()


def _create_gateway_model(db_session, account_id):
    return crud_ai_model.create_with_account(
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
                    # Mocked litellm.completion below drives the transcode
                    # path; native /responses would POST the dummy key.
                    "responses_api": "transcode",
                }
            },
            "is_default": True,
        },
        account_id=account_id,
    )


def _halt_gateway(db_session, account_id):
    crud_account_halt.set_scopes(
        db_session,
        account_id=account_id,
        scopes=["gateway"],
        active=True,
        user_id=None,
        reason="runaway agent",
    )
    invalidate_kill_switch_cache(account_id)


def _lift_gateway(db_session, account_id):
    crud_account_halt.set_scopes(
        db_session,
        account_id=account_id,
        scopes=["gateway"],
        active=False,
        user_id=None,
    )
    invalidate_kill_switch_cache(account_id)


def test_chat_completion_rejected_while_halted(app, client, db_session, test_user):
    """A halted account gets a distinct 403 and no upstream call."""
    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )
    _halt_gateway(db_session, test_user.account_id)

    with patch("preloop.services.openai_gateway.litellm.completion") as mock_upstream:
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "preloop_account_halted"
    assert "kill switch" in error["message"].lower()
    assert "runaway agent" in error["message"]
    assert response.headers["X-Preloop-Error-Class"] == "kill_switch"
    mock_upstream.assert_not_called()

    # Rejected traffic is logged and attributed to the kill switch.
    halted_rows = (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.account_id == test_user.account_id,
            ApiUsage.status_code == 403,
        )
        .all()
    )
    assert halted_rows, "expected a usage row for the rejected request"
    assert all(row.error_class == "kill_switch" for row in halted_rows)


def test_responses_rejected_while_halted(app, client, db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )
    _halt_gateway(db_session, test_user.account_id)

    with patch("preloop.services.openai_gateway.litellm.completion") as mock_upstream:
        response = client.post(
            "/openai/v1/responses",
            headers={"Authorization": "Bearer ignored"},
            json={"model": "openai/gpt-5", "input": "Hello"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "preloop_account_halted"
    mock_upstream.assert_not_called()


def test_anthropic_messages_rejected_with_native_error_shape(
    app, client, db_session, test_user
):
    """Anthropic-format clients get the Anthropic-native error envelope."""
    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_anthropic_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )
    _halt_gateway(db_session, test_user.account_id)

    with patch("preloop.services.openai_gateway.litellm.completion") as mock_upstream:
        response = client.post(
            "/anthropic/v1/messages",
            headers={
                "x-api-key": "ignored",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "openai/gpt-5",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 403
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "permission_error"
    assert "kill switch" in payload["error"]["message"].lower()
    mock_upstream.assert_not_called()


def test_gateway_traffic_resumes_after_staged_reenable(
    app, client, db_session, test_user
):
    """Lifting only the gateway scope restores model traffic immediately."""
    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )
    _halt_gateway(db_session, test_user.account_id)
    _lift_gateway(db_session, test_user.account_id)

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ):
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"


def test_tools_only_halt_does_not_block_gateway(app, client, db_session, test_user):
    """A staged halt of tools alone leaves the model gateway alone."""
    _create_gateway_model(db_session, test_user.account_id)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=test_user)
    )
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["tools"],
        active=True,
        user_id=None,
    )
    invalidate_kill_switch_cache(test_user.account_id)

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value={
            "id": "chatcmpl_123",
            "created": 1710000000,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ) as upstream:
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
    assert response.status_code == 200
    upstream.assert_called_once()
