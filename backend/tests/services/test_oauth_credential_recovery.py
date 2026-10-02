"""Credential recovery must not roll back a provider's rotating OAuth grant."""

import json
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.services.secret_service import (
    CredentialRefreshError,
    LOCAL_ENCRYPTED_BACKEND,
    OAUTH_CONSUMED_REFRESH_HASHES,
    OAUTH_REFRESH_HISTORY_LIMIT,
    SecretService,
)
from preloop.utils.encryption import encrypt_value


@pytest.fixture(params=["oauth_anthropic_claude_code", "oauth_openai_codex"])
def oauth_model(request: pytest.FixtureRequest) -> models.AIModel:
    """A synthetic expired credential; tests never contact a provider or DB."""
    secret = models.SecretReference(
        id=uuid4(),
        account_id=uuid4(),
        name="Synthetic subscription",
        backend_type=LOCAL_ENCRYPTED_BACKEND,
        secret_kind="ai_model_credentials",
        encrypted_value=encrypt_value(
            json.dumps(
                {"type": request.param, "access": "old", "refresh": "old", "expires": 1}
            )
        ),
        status="active",
        meta_data={"credential_type": request.param},
    )
    return models.AIModel(
        id=uuid4(),
        name="Synthetic model",
        provider_name="synthetic",
        model_identifier="synthetic",
        credentials_secret=secret,
    )


def refresh(
    service: SecretService, model: models.AIModel, db: MagicMock
) -> dict[str, Any]:
    """Choose a provider path and use the secret's actual stored payload."""
    payload = json.loads(
        service.resolve_secret_reference(model.credentials_secret).value
    )
    method = (
        service._refresh_anthropic_claude_code_ai_model_credentials
        if payload["type"] == "oauth_anthropic_claude_code"
        else service._refresh_openai_codex_ai_model_credentials
    )
    return method(model, payload, db=db)


def test_rotation_refuses_unexpired_consumed_token_import(
    oauth_model: models.AIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An access token can be unexpired while its refresh token is already spent."""
    service = SecretService()
    monkeypatch.setattr(
        service,
        "_lock_and_reload_oauth_payload",
        lambda db, secret, payload, **kw: payload,
    )
    next_bundle = {"access": "new", "refresh": "new", "expires": 4102444800000}
    monkeypatch.setattr(
        service, "_refresh_anthropic_claude_code_token", lambda token: next_bundle
    )
    monkeypatch.setattr(
        service, "_refresh_openai_codex_token", lambda token: next_bundle
    )
    refresh(service, oauth_model, MagicMock())
    secret = oauth_model.credentials_secret
    assert secret.status == "active"
    assert secret.meta_data[OAUTH_CONSUMED_REFRESH_HASHES] == [
        service._refresh_token_fingerprint("old")
    ]
    before = secret.encrypted_value
    incoming = json.dumps(
        {
            "type": secret.meta_data["credential_type"],
            "refresh": "old",
            "access": "old",
            "expires": 4102444800000,
        }
    )
    with pytest.raises(ValueError, match="consumed or revoked"):
        service._validate_oauth_replacement(secret, incoming)
    assert secret.encrypted_value == before
    # A new authorization is accepted without throwing away the spent-token history.
    incoming = json.dumps(
        {"type": secret.meta_data["credential_type"], "refresh": "fresh-login"}
    )
    assert service._validate_oauth_replacement(secret, incoming) == [
        service._refresh_token_fingerprint("old")
    ]


def test_terminal_failure_is_not_retried_until_reconnected(
    oauth_model: models.AIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SecretService()
    monkeypatch.setattr(
        service,
        "_lock_and_reload_oauth_payload",
        lambda db, secret, payload, **kw: payload,
    )
    calls = []

    def fail(token: str) -> dict[str, Any]:
        calls.append(token)
        raise CredentialRefreshError(
            "synthetic failure",
            provider="synthetic",
            status_code=400,
            code="invalid_grant",
        )

    monkeypatch.setattr(service, "_refresh_anthropic_claude_code_token", fail)
    monkeypatch.setattr(service, "_refresh_openai_codex_token", fail)
    for _ in range(2):
        with pytest.raises(CredentialRefreshError) as error:
            refresh(service, oauth_model, MagicMock())
        assert error.value.code == "invalid_grant"
    assert calls == ["old"]
    secret = oauth_model.credentials_secret
    incoming = json.dumps(
        {"type": secret.meta_data["credential_type"], "refresh": "old"}
    )
    with pytest.raises(ValueError, match="consumed or revoked"):
        service._validate_oauth_replacement(secret, incoming)
    # Credential-only reconnect clears terminal errors and preserves token history.
    from preloop.services import secret_service as module

    monkeypatch.setattr(
        module.crud_secret_reference, "get_for_update", lambda *a, **kw: secret
    )
    service.create_local_secret_reference(
        MagicMock(),
        account_id=secret.account_id,
        name=secret.name,
        secret_kind=secret.secret_kind,
        existing_secret_id=secret.id,
        secret_value=json.dumps(
            {
                "type": secret.meta_data["credential_type"],
                "refresh": "new-login",
                "access": "new",
                "expires": 1,
            }
        ),
        meta_data={"credential_type": secret.meta_data["credential_type"]},
    )
    assert secret.status == "active"
    assert "last_refresh_code" not in secret.meta_data
    assert (
        service._refresh_token_fingerprint("old")
        in secret.meta_data[OAUTH_CONSUMED_REFRESH_HASHES]
    )
    monkeypatch.setattr(
        service,
        "_refresh_anthropic_claude_code_token",
        lambda token: {"access": "new", "refresh": "next", "expires": 4102444800000},
    )
    monkeypatch.setattr(
        service,
        "_refresh_openai_codex_token",
        lambda token: {"access": "new", "refresh": "next", "expires": 4102444800000},
    )
    assert refresh(service, oauth_model, MagicMock())["refresh"] == "next"


def test_transient_provider_failure_can_be_retried(
    oauth_model: models.AIModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SecretService()
    monkeypatch.setattr(
        service,
        "_lock_and_reload_oauth_payload",
        lambda db, secret, payload, **kw: payload,
    )
    calls = []

    def fail(token: str) -> dict[str, Any]:
        calls.append(token)
        raise CredentialRefreshError(
            "unavailable", provider="synthetic", status_code=503
        )

    monkeypatch.setattr(service, "_refresh_anthropic_claude_code_token", fail)
    monkeypatch.setattr(service, "_refresh_openai_codex_token", fail)
    for _ in range(2):
        with pytest.raises(CredentialRefreshError):
            refresh(service, oauth_model, MagicMock())
    assert calls == ["old", "old"]
    monkeypatch.setattr(
        service,
        "_refresh_anthropic_claude_code_token",
        lambda token: {"access": "new", "refresh": "next", "expires": 4102444800000},
    )
    monkeypatch.setattr(
        service,
        "_refresh_openai_codex_token",
        lambda token: {"access": "new", "refresh": "next", "expires": 4102444800000},
    )
    refresh(service, oauth_model, MagicMock())
    assert oauth_model.credentials_secret.status == "active"
    assert "last_refresh_error" not in oauth_model.credentials_secret.meta_data
    assert "last_refresh_code" not in oauth_model.credentials_secret.meta_data


def test_consumed_history_is_bounded_and_does_not_store_tokens(
    oauth_model: models.AIModel,
) -> None:
    service = SecretService()
    secret = oauth_model.credentials_secret
    for i in range(OAUTH_REFRESH_HISTORY_LIMIT + 10):
        service._record_consumed_refresh(secret, f"synthetic-{i}", f"synthetic-{i + 1}")
    history = secret.meta_data[OAUTH_CONSUMED_REFRESH_HASHES]
    assert len(history) == OAUTH_REFRESH_HISTORY_LIMIT
    assert all(len(value) == 64 and "synthetic" not in value for value in history)
    assert history[-1] == service._refresh_token_fingerprint(
        f"synthetic-{OAUTH_REFRESH_HISTORY_LIMIT + 9}"
    )


@pytest.mark.parametrize(
    "provider,agent,login",
    [
        ("anthropic", "Claude Code", "claude auth login --claudeai"),
        ("openai", "Codex CLI", "codex login"),
    ],
)
def test_terminal_error_names_credential_only_recovery(
    provider: str, agent: str, login: str
) -> None:
    error = CredentialRefreshError(
        "synthetic", provider=provider, code="invalid_grant", status_code=400
    )
    message = error.recovery_message()
    assert login in message
    assert f'reconnect "{agent}" --from-local' in message
    assert "enrollment is preserved" in message
    assert "onboarding" not in message


def test_transient_error_does_not_request_provider_login() -> None:
    error = CredentialRefreshError("synthetic", provider="anthropic", status_code=503)
    message = error.recovery_message()
    assert "retry later" in message
    assert "login" not in message


def test_token_fingerprint_is_bound_to_instance_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored history is not an unkeyed verifier for the provider credential."""
    from preloop.services import secret_service as module

    monkeypatch.setattr(module.settings.security, "encryption_key", "")
    monkeypatch.setattr(
        module.settings.security, "secret_key", "synthetic-instance-one"
    )
    first = SecretService._refresh_token_fingerprint("synthetic-provider-token")
    assert first == SecretService._refresh_token_fingerprint("synthetic-provider-token")
    monkeypatch.setattr(
        module.settings.security, "secret_key", "synthetic-instance-two"
    )
    assert first != SecretService._refresh_token_fingerprint("synthetic-provider-token")
