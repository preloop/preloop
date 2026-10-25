"""Audit sanitizer: word-boundary names and reference-only records."""

from __future__ import annotations

import pytest

from preloop.services.audit.service import AuditService, is_sensitive_name

REDACTED = "[REDACTED]"


@pytest.fixture
def service() -> AuditService:
    return AuditService.__new__(AuditService)


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "api_key",
        "apiKey",
        "APIKey",
        "x-api-key",
        "key",
        "secret",
        "client_secret",
        "clientSecret",
        "token",
        "access_token",
        "x-auth-token",
        "authorization",
        "Authorization",
        "auth",
        "auth_header",
        "credentials",
        "bearer",
        "private_key",
        "signing_key",
        # Review on !128: digits glued to the word, and glued lowercase forms.
        "password1",
        "new_password1",
        "new_password2",
        "key2",
        "token2",
        "apiKey2",
        "api_key_2",
        "userpassword",
        "dbpassword",
        "clientsecret",
        "authtoken",
        "secretkey",
        "privatekey",
        "csrftoken",
    ],
)
def test_sensitive_names_are_redacted(service, name: str) -> None:
    assert is_sensitive_name(name)
    assert service._sanitize_tool_args({name: "v"}) == {name: REDACTED}


@pytest.mark.parametrize(
    "name",
    [
        "arg_keys",
        "monkey",
        "keyboard",
        "keyword",
        "key_id",
        "author",
        "max_tokens",
        "scope",
    ],
)
def test_names_that_only_contain_a_sensitive_substring_are_kept(
    service, name: str
) -> None:
    assert not is_sensitive_name(name)
    assert service._sanitize_tool_args({name: "v"}) == {name: "v"}


def test_nested_values_still_redacted(service) -> None:
    args = {"cfg": {"api_key": "sk-1", "region": "eu"}, "items": [{"token": "t"}]}
    assert service._sanitize_tool_args(args) == {
        "cfg": {"api_key": REDACTED, "region": "eu"},
        "items": [{"token": REDACTED}],
    }


def _record(**overrides):
    record = {
        "_preloop_reference": True,
        "schema": "preloop.sensitive_data.reference/v1",
        "rule_id": "owner-data-reference-only",
        "tool_name": "read_scope",
        "arg_keys": ["scope", "owner", "api_key", "auth_token"],
        "kept": {"$.scope": "daily", "$.key_id": "k-7", "$.api_key": "sk-live"},
        "kept_result": {
            "$result.consent_id": "urn:test:consent:123",
            "$result.auth.token": "t-1",
            "$result.monkey_id": "m-1",
        },
        "args_hmac": "ab" * 32,
        "result_hmac": "cd" * 32,
        "salt_id": "s1",
        "principal": {"managed_agent_id": "a1"},
    }
    record.update(overrides)
    return record


def test_reference_record_keeps_names_and_paths(service) -> None:
    out = service._sanitize_tool_args(_record())
    assert out["arg_keys"] == ["scope", "owner", "api_key", "auth_token"]
    assert out["_preloop_reference"] is True
    assert out["kept"]["$.scope"] == "daily"
    assert out["kept_result"]["$result.consent_id"] == "urn:test:consent:123"
    assert out["kept_result"]["$result.monkey_id"] == "m-1"
    assert out["args_hmac"] == "ab" * 32 and out["result_hmac"] == "cd" * 32
    assert set(out["kept"]) == {"$.scope", "$.key_id", "$.api_key"}


def test_reference_record_blanks_values_under_sensitive_paths(service) -> None:
    out = service._sanitize_tool_args(_record())
    assert out["kept"]["$.api_key"] == REDACTED
    assert out["kept_result"]["$result.auth.token"] == REDACTED
    # "key_id" names a key, it is not one.
    assert out["kept"]["$.key_id"] == "k-7"


def test_dict_that_is_not_a_reference_record_gets_normal_treatment(service) -> None:
    out = service._sanitize_tool_args({"arg_keys": "x", "kept": {"$.api_key": "s"}})
    assert out == {"arg_keys": "x", "kept": {"$.api_key": REDACTED}}
    out = service._sanitize_tool_args({"kept": {"api_key": "s"}})
    assert out == {"kept": {"api_key": REDACTED}}
