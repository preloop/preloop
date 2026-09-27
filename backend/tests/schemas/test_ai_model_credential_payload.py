"""Write-time shape checks for subscription-OAuth ``credential_payload``.

A payload sent with the provider tool's own key names (``access_token``,
``refresh_token``) used to be stored and marked active, and only failed at the
first completion with "credentials are incomplete". The request schemas now
reject such payloads so the API answers 422 before anything is stored.
"""

from typing import Any, Dict, Type, Union

import pytest
from pydantic import ValidationError

from preloop.schemas.ai_model import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE,
    OPENAI_CODEX_OAUTH_TYPE,
    AIModelCreate,
    AIModelUpdate,
    validate_credential_payload,
)
from preloop.services.secret_service import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
    OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
)

EXPIRES_MS = 1893456000000

CODEX_PAYLOAD: Dict[str, Any] = {
    "access": "codex-access",
    "refresh": "codex-refresh",
    "account_id": "chatgpt-account",
    "expires": EXPIRES_MS,
}
CLAUDE_PAYLOAD: Dict[str, Any] = {
    "access": "claude-access",
    "refresh": "claude-refresh",
    "expires": EXPIRES_MS,
}

SchemaType = Type[Union[AIModelCreate, AIModelUpdate]]


def _build(schema: SchemaType, credential_type: str, payload: Dict[str, Any]):
    """Build a create or update request carrying an inline credential."""
    fields: Dict[str, Any] = {
        "credential_type": credential_type,
        "credential_payload": payload,
    }
    if schema is AIModelCreate:
        fields.update(
            name="Codex", provider_name="openai-codex", model_identifier="gpt-5.5"
        )
    return schema(**fields)


def _error_text(schema: SchemaType, credential_type: str, payload: Dict) -> str:
    with pytest.raises(ValidationError) as error:
        _build(schema, credential_type, payload)
    return str(error.value)


def test_type_constants_match_the_resolver() -> None:
    """The schema must validate the same type names the resolver reads."""
    assert OPENAI_CODEX_OAUTH_TYPE == OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE
    assert ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE == (
        ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE
    )


@pytest.mark.parametrize("schema", [AIModelCreate, AIModelUpdate])
def test_valid_codex_payload_is_accepted(schema: SchemaType) -> None:
    model = _build(schema, OPENAI_CODEX_OAUTH_TYPE, dict(CODEX_PAYLOAD))
    assert model.credential_payload == CODEX_PAYLOAD


@pytest.mark.parametrize("schema", [AIModelCreate, AIModelUpdate])
@pytest.mark.parametrize("key", ["access", "refresh", "account_id", "expires"])
def test_codex_payload_missing_key_is_rejected(schema: SchemaType, key: str) -> None:
    payload = {k: v for k, v in CODEX_PAYLOAD.items() if k != key}
    text = _error_text(schema, OPENAI_CODEX_OAUTH_TYPE, payload)
    assert "invalid credential_payload for oauth_openai_codex" in text
    assert f"missing keys: {key}" in text


def test_codex_empty_payload_lists_every_missing_key() -> None:
    text = _error_text(AIModelUpdate, OPENAI_CODEX_OAUTH_TYPE, {})
    assert "missing keys: access, refresh, account_id, expires" in text


def test_codex_auth_json_key_names_get_a_hint() -> None:
    """The reproduced case: Codex's own auth.json names instead of ours."""
    payload = {
        "access_token": "a",
        "refresh_token": "r",
        "id_token": "i",
        "account_id": "acct",
    }
    text = _error_text(AIModelUpdate, OPENAI_CODEX_OAUTH_TYPE, payload)
    assert "missing keys: access, refresh, expires" in text
    assert "unexpected key 'access_token': use 'access'" in text
    assert "unexpected key 'refresh_token': use 'refresh'" in text


def test_expires_at_alias_gets_a_hint() -> None:
    payload = {k: v for k, v in CODEX_PAYLOAD.items() if k != "expires"}
    payload["expires_at"] = EXPIRES_MS
    text = _error_text(AIModelUpdate, OPENAI_CODEX_OAUTH_TYPE, payload)
    assert "unexpected key 'expires_at': use 'expires'" in text


def test_alias_alongside_canonical_key_is_still_rejected() -> None:
    payload = dict(CODEX_PAYLOAD, access_token="a")
    text = _error_text(AIModelUpdate, OPENAI_CODEX_OAUTH_TYPE, payload)
    assert "unexpected key 'access_token': use 'access'" in text
    assert "missing keys" not in text


@pytest.mark.parametrize("key", ["access", "refresh", "account_id"])
@pytest.mark.parametrize("value", ["", "   ", None, 123])
def test_codex_string_keys_must_be_non_empty_strings(key: str, value: Any) -> None:
    payload = dict(CODEX_PAYLOAD, **{key: value})
    text = _error_text(AIModelUpdate, OPENAI_CODEX_OAUTH_TYPE, payload)
    assert f"{key} must be a non-empty string" in text


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (1789000000, "expires looks like epoch seconds"),
        (1789000000000000, "expires is too large for epoch milliseconds"),
        (1789000000000000000, "expires is too large for epoch milliseconds"),
        (0, "expires must be a positive integer"),
        (-5, "expires must be a positive integer"),
        ("1789000000000", "expires must be an integer"),
        (1789000000000.5, "expires must be an integer"),
        (True, "expires must be an integer"),
        (None, "expires must be an integer"),
    ],
)
def test_codex_expires_must_be_epoch_millis(value: Any, message: str) -> None:
    payload = dict(CODEX_PAYLOAD, expires=value)
    text = _error_text(AIModelUpdate, OPENAI_CODEX_OAUTH_TYPE, payload)
    assert message in text


@pytest.mark.parametrize("value", [10**12, 10**14])
def test_codex_expires_bounds_are_inclusive(value: int) -> None:
    payload = dict(CODEX_PAYLOAD, expires=value)
    model = _build(AIModelUpdate, OPENAI_CODEX_OAUTH_TYPE, payload)
    assert model.credential_payload["expires"] == value


def test_unknown_extra_keys_are_ignored() -> None:
    payload = dict(CODEX_PAYLOAD, id_token="i", type=OPENAI_CODEX_OAUTH_TYPE)
    model = _build(AIModelUpdate, OPENAI_CODEX_OAUTH_TYPE, payload)
    assert model.credential_payload == payload


@pytest.mark.parametrize("schema", [AIModelCreate, AIModelUpdate])
def test_valid_claude_payload_is_accepted(schema: SchemaType) -> None:
    model = _build(schema, ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE, dict(CLAUDE_PAYLOAD))
    assert model.credential_payload == CLAUDE_PAYLOAD


def test_claude_access_only_payload_is_accepted() -> None:
    """Long-lived Claude Code tokens have no refresh token or expiry.

    The resolver never refreshes such a payload and the CLI sends it when only
    a bare OAuth token is available, so it must stay valid.
    """
    model = _build(AIModelUpdate, ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE, {"access": "a"})
    assert model.credential_payload == {"access": "a"}


def test_claude_does_not_require_account_id() -> None:
    validate_credential_payload(ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE, CLAUDE_PAYLOAD)


def test_claude_missing_access_is_rejected() -> None:
    payload = {"refresh": "r", "expires": EXPIRES_MS}
    text = _error_text(AIModelUpdate, ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE, payload)
    assert "invalid credential_payload for oauth_anthropic_claude_code" in text
    assert "missing keys: access" in text


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"refresh": ""}, "refresh must be a non-empty string"),
        ({"expires": 1789000000}, "expires looks like epoch seconds"),
        ({"expires": "soon"}, "expires must be an integer"),
        ({"expires": 1789000000000000}, "expires is too large"),
    ],
)
def test_claude_optional_keys_are_validated_when_present(
    override: Dict[str, Any], message: str
) -> None:
    payload = dict(CLAUDE_PAYLOAD, **override)
    text = _error_text(AIModelUpdate, ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE, payload)
    assert message in text


def test_claude_aliases_get_a_hint() -> None:
    payload = {"access_token": "a", "refresh_token": "r"}
    text = _error_text(AIModelUpdate, ANTHROPIC_CLAUDE_CODE_OAUTH_TYPE, payload)
    assert "missing keys: access" in text
    assert "unexpected key 'access_token': use 'access'" in text
    assert "unexpected key 'refresh_token': use 'refresh'" in text


@pytest.mark.parametrize("credential_type", ["api_key", "custom_bundle"])
def test_other_credential_types_are_unchanged(credential_type: str) -> None:
    payload = {"access_token": "anything", "expires": 1}
    model = _build(AIModelUpdate, credential_type, payload)
    assert model.credential_payload == payload
