"""Unit tests for the encryption-at-rest column helpers (no database)."""

import pytest

from preloop.models.db.encrypted_types import (
    SEALED_PREFIX,
    EncryptedText,
    SecretFieldsJSON,
    count_plaintext_fields,
    is_sealed,
    seal,
    seal_document,
    unseal,
    unseal_document,
)

SECRET = "synthetic-credential-value"


def test_seal_round_trips_and_hides_the_plaintext() -> None:
    sealed = seal(SECRET)
    assert sealed.startswith(SEALED_PREFIX)
    assert SECRET not in sealed
    assert unseal(sealed) == SECRET


def test_seal_is_idempotent_and_leaves_empty_values() -> None:
    sealed = seal(SECRET)
    assert seal(sealed) == sealed
    assert seal("") == ""


def test_unseal_returns_legacy_plaintext_unchanged() -> None:
    assert unseal(SECRET) == SECRET
    assert not is_sealed(SECRET)


def test_unseal_fails_closed_on_a_corrupt_token() -> None:
    with pytest.raises(ValueError):
        unseal(SEALED_PREFIX + "not-a-fernet-token")


def test_mcp_auth_config_seals_secret_leaves_only() -> None:
    config = {
        "token": SECRET,
        "client_id": "public-client",
        "token_endpoint": "https://idp.example/token",
        "headers": {"Authorization": "Bearer " + SECRET, "X-Trace": "on"},
        "expires_at": 123,
    }
    sealed = seal_document("mcp_auth_config", config)
    assert is_sealed(sealed["token"])
    assert is_sealed(sealed["headers"]["Authorization"])
    assert sealed["headers"]["X-Trace"] == "on"
    assert sealed["client_id"] == "public-client"
    assert sealed["token_endpoint"] == "https://idp.example/token"
    assert sealed["expires_at"] == 123
    assert SECRET not in repr(sealed)
    assert config["token"] == SECRET, "input must not be mutated"
    assert unseal_document("mcp_auth_config", sealed) == config
    assert count_plaintext_fields("mcp_auth_config", config) == 2
    assert count_plaintext_fields("mcp_auth_config", sealed) == 0


def test_webhook_secret_selector_seals_only_the_secret() -> None:
    config = {
        "webhook_secret": SECRET,
        "employee_secret": SECRET,
        "api_key": SECRET,
        "webhook_url": "https://hooks.example/x",
    }
    sealed = seal_document("webhook_secret", config)
    for key in ("webhook_secret", "employee_secret", "api_key"):
        assert is_sealed(sealed[key]), key
    assert sealed["webhook_url"] == config["webhook_url"]
    assert unseal_document("webhook_secret", sealed) == config


def test_policy_snapshot_seals_server_credentials_and_webhook_secrets() -> None:
    snapshot = {
        "mcp_servers": [
            {"name": "srv", "auth_config": {"api_key": SECRET, "scope": "read"}},
            {"name": "open", "auth_config": None},
        ],
        "approval_workflows": [{"approval_config": {"webhook_secret": SECRET}}],
        "tools": [{"name": "token_counter"}],
    }
    sealed = seal_document("policy_snapshot", snapshot)
    assert SECRET not in repr(sealed)
    assert sealed["mcp_servers"][0]["auth_config"]["scope"] == "read"
    assert sealed["tools"] == snapshot["tools"]
    assert unseal_document("policy_snapshot", sealed) == snapshot
    assert count_plaintext_fields("policy_snapshot", snapshot) == 2


def test_column_types_seal_on_bind_and_decrypt_on_result() -> None:
    text_type = EncryptedText()
    stored = text_type.process_bind_param(SECRET, None)  # type: ignore[arg-type]
    assert is_sealed(stored)
    assert text_type.process_result_value(stored, None) == SECRET  # type: ignore[arg-type]
    assert text_type.process_bind_param(None, None) is None  # type: ignore[arg-type]

    json_type = SecretFieldsJSON("webhook_secret")
    doc = json_type.process_bind_param({"webhook_secret": SECRET}, None)  # type: ignore[arg-type]
    assert is_sealed(doc["webhook_secret"])
    assert json_type.process_result_value(doc, None) == {"webhook_secret": SECRET}  # type: ignore[arg-type]


def test_unknown_selector_is_rejected() -> None:
    with pytest.raises(ValueError):
        SecretFieldsJSON("nope")


def test_agent_config_seals_nested_credentials() -> None:
    config = {
        "baseUrl": "https://gw.example",
        "apiKey": SECRET,
        "env": {"ANTHROPIC_AUTH_TOKEN": SECRET, "ANTHROPIC_MODEL": "m"},
        "runtime_session_id": "r1",
    }
    sealed = seal_document("agent_config", config)
    assert SECRET not in repr(sealed)
    assert sealed["env"]["ANTHROPIC_MODEL"] == "m"
    assert sealed["runtime_session_id"] == "r1"
    assert unseal_document("agent_config", sealed) == config


def test_policy_snapshot_seals_approval_provider_keys() -> None:
    snapshot = {"approval_workflows": [{"approval_config": {"api_key": SECRET}}]}
    assert SECRET not in repr(seal_document("policy_snapshot", snapshot))
