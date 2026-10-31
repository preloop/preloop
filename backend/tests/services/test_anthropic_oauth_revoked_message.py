"""An upstream 401 on a subscription OAuth model names the sync, not /login."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from preloop.services.openai_gateway import OpenAIGatewayService

_REVOKED = (
    '{"type":"error","error":{"type":"authentication_error",'
    '"message":"OAuth access token has been revoked"}}'
)


def _model(credential_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        account_id="account-a",
        provider_name="anthropic",
        model_identifier="example-model",
        id="model-a",
        api_endpoint="https://api.example.com/v1",
        credential_type=credential_type,
    )


def test_subscription_401_returns_sync_recovery_message() -> None:
    with patch("preloop.sync.tasks.notify_admins"):
        error = OpenAIGatewayService._anthropic_passthrough_upstream_error(
            401, _REVOKED, ai_model=_model("oauth_anthropic_claude_code")
        )
    assert error.status_code == 401
    assert "revoked" not in error.message.lower()
    sync = 'preloop agents sync-credentials "Claude Code"'
    assert sync in error.message
    assert error.message.index(sync) < error.message.index("--from-local")


@pytest.mark.parametrize("credential_type", ["api_key", ""])
def test_api_key_401_keeps_upstream_text(credential_type: str) -> None:
    with patch("preloop.sync.tasks.notify_admins"):
        error = OpenAIGatewayService._anthropic_passthrough_upstream_error(
            401, _REVOKED, ai_model=_model(credential_type)
        )
    assert "OAuth access token has been revoked" in error.message
