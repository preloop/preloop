"""Tests for secret service."""

import json
import pytest
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account
from preloop.models.crud import crud_ai_model
from preloop.models.models import Account
from preloop.models.models.secret_reference import SecretReference
from preloop.services import secret_service as secret_service_module
from preloop.services.secret_service import (
    CredentialRefreshError,
    LOCAL_ENCRYPTED_BACKEND,
    OPENBAO_KV_V2_BACKEND,
    SecretService,
    get_secret_service,
)


def test_resolve_ai_model_api_key_from_secret_reference(
    db_session: Session,
):
    """New AI model credentials should be resolved from secret references."""
    account: Account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Secret Service Test Org",
            "is_active": True,
        },
    )
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Secret-backed Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "super-secret-key",
            "is_default": False,
        },
        account_id=account.id,
    )

    resolved = get_secret_service().resolve_ai_model_api_key(ai_model)

    assert resolved is not None
    assert resolved.value == "super-secret-key"
    assert resolved.backend_type == LOCAL_ENCRYPTED_BACKEND
    assert ai_model.credentials_secret_id is not None
    assert ai_model.api_key is None


def test_resolve_external_secret_reference_with_openbao_backend(monkeypatch):
    """External backend references should resolve through the vault-compatible path."""
    monkeypatch.setattr(secret_service_module.settings.vault_kv_v2, "enabled", True)
    monkeypatch.setattr(
        secret_service_module.settings.vault_kv_v2,
        "url",
        "https://openbao.example.test",
    )
    monkeypatch.setattr(
        secret_service_module.settings.vault_kv_v2, "token", "test-token"
    )
    monkeypatch.setattr(secret_service_module.settings.vault_kv_v2, "mount", "kv")
    monkeypatch.setattr(
        secret_service_module.settings.vault_kv_v2,
        "path_prefix",
        "preloop/providers",
    )

    captured: dict[str, object] = {}

    def fake_read_secret(self, path: str, meta_data: dict) -> dict:
        captured["path"] = path
        captured["meta_data"] = meta_data
        return {"data": {"data": {"api_key": "resolved-from-openbao"}}}

    monkeypatch.setattr(
        secret_service_module.VaultKVV2SecretBackend,
        "_read_secret",
        fake_read_secret,
    )

    service = SecretService()
    secret_ref = SecretReference(
        account_id=uuid4(),
        name="OpenAI API key",
        backend_type=OPENBAO_KV_V2_BACKEND,
        secret_kind="ai_model_api_key",
        external_ref="team-a/openai",
        status="active",
        meta_data={"field": "api_key", "version": 2},
    )

    resolved = service.resolve_secret_reference(secret_ref)

    assert resolved.value == "resolved-from-openbao"
    assert resolved.backend_type == OPENBAO_KV_V2_BACKEND
    assert captured["path"] == "kv/data/preloop/providers/team-a/openai"
    assert captured["meta_data"] == {"field": "api_key", "version": 2}


@pytest.mark.parametrize(
    "external_ref",
    [
        "../team-a/openai",
        "team-a/../openai",
        "team-a//openai",
        "team-a/%2e%2e/openai",
        r"team-a\openai",
    ],
)
def test_resolve_external_secret_reference_rejects_path_traversal(
    monkeypatch, external_ref: str
):
    """External secret refs should reject traversal and malformed path segments."""
    monkeypatch.setattr(secret_service_module.settings.vault_kv_v2, "enabled", True)
    monkeypatch.setattr(
        secret_service_module.settings.vault_kv_v2,
        "url",
        "https://openbao.example.test",
    )
    monkeypatch.setattr(
        secret_service_module.settings.vault_kv_v2, "token", "test-token"
    )
    monkeypatch.setattr(secret_service_module.settings.vault_kv_v2, "mount", "kv")

    service = SecretService()
    secret_ref = SecretReference(
        account_id=uuid4(),
        name="OpenAI API key",
        backend_type=OPENBAO_KV_V2_BACKEND,
        secret_kind="ai_model_api_key",
        external_ref=external_ref,
        status="active",
        meta_data={"field": "api_key"},
    )

    with pytest.raises(ValueError, match="credentials_external_ref"):
        service.resolve_secret_reference(secret_ref)


def test_resolve_ai_model_credentials_refreshes_openai_codex_oauth(
    db_session: Session,
):
    """Structured Codex OAuth credentials should refresh and persist."""
    account: Account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Codex OAuth Org",
            "is_active": True,
        },
    )
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Codex OAuth Model",
            "provider_name": "openai-codex",
            "model_identifier": "gpt-5.4",
            "credential_type": "oauth_openai_codex",
            "credential_payload": {
                "access": "old-access",
                "refresh": "refresh-token",
                "expires": 1,
                "account_id": "acct-old",
            },
        },
        account_id=account.id,
    )

    service = SecretService()
    service._refresh_openai_codex_token = lambda refresh_token: {
        "access": "new-access",
        "refresh": refresh_token + "-updated",
        "expires": 1893456000000,
        "account_id": "acct-new",
    }

    resolved = service.resolve_ai_model_credentials(
        ai_model,
        db=db_session,
        allow_refresh=True,
    )

    assert resolved is not None
    assert resolved.credential_type == "oauth_openai_codex"
    assert resolved.value == "new-access"
    assert resolved.payload == {
        "type": "oauth_openai_codex",
        "access": "new-access",
        "refresh": "refresh-token-updated",
        "expires": 1893456000000,
        "account_id": "acct-new",
    }

    db_session.refresh(ai_model)
    assert ai_model.credentials_secret is not None
    stored = json.loads(
        service.resolve_secret_reference(ai_model.credentials_secret).value
    )
    assert stored["access"] == "new-access"
    assert stored["account_id"] == "acct-new"


def test_refresh_anthropic_claude_code_token_uses_claude_code_request_shape():
    """The refresh request must look like the real Claude Code client.

    platform.claude.com is behind Cloudflare, which 403/1010-blocks a bare
    form-POST with no User-Agent before the OAuth handler runs. Regression
    guard: assert a JSON body + a Claude Code User-Agent are sent.
    """
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 28800,
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = req.data.decode("utf-8")
        return _FakeResp()

    original = secret_service_module.urllib_request.urlopen
    secret_service_module.urllib_request.urlopen = fake_urlopen
    try:
        result = SecretService()._refresh_anthropic_claude_code_token("refresh-token")
    finally:
        secret_service_module.urllib_request.urlopen = original

    assert captured["headers"].get("content-type") == "application/json"
    assert captured["headers"].get("user-agent")  # non-empty UA defeats CF 1010
    parsed = json.loads(captured["body"])
    assert parsed["grant_type"] == "refresh_token"
    assert parsed["refresh_token"] == "refresh-token"
    assert result["access"] == "new-access"
    assert result["refresh"] == "new-refresh"


def test_resolve_ai_model_credentials_marks_anthropic_oauth_refresh_failure(
    db_session: Session,
):
    """Failed Claude Code OAuth refresh should mark stored credentials unhealthy."""
    account: Account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Claude OAuth Org",
            "is_active": True,
        },
    )
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude OAuth Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-opus-4-6",
            "credential_type": "oauth_anthropic_claude_code",
            "credential_payload": {
                "access": "old-access",
                "refresh": "refresh-token",
                "expires": 1,
            },
        },
        account_id=account.id,
    )

    service = SecretService()

    def fail_refresh(refresh_token: str):
        raise CredentialRefreshError(
            "Anthropic Claude Code OAuth token refresh failed with status 403",
            provider="anthropic",
            status_code=403,
            code="1010",
        )

    service._refresh_anthropic_claude_code_token = fail_refresh

    with pytest.raises(CredentialRefreshError):
        service.resolve_ai_model_credentials(
            ai_model,
            db=db_session,
            allow_refresh=True,
        )

    db_session.refresh(ai_model.credentials_secret)
    assert ai_model.credentials_secret.status == "error"
    assert ai_model.credentials_secret.meta_data["last_refresh_status_code"] == 403
    assert ai_model.credentials_secret.meta_data["last_refresh_code"] == "1010"
