"""Authentication assurance survives session rotation without arbitrary claims."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.services import authentication_policy as policy


def test_context_carries_only_authentication_claims() -> None:
    connection = str(uuid4())
    assert policy.authentication_claims(
        {
            "amr": ["sso", "oidc"],
            "sso_connection_id": connection,
            "nsg": 2,
            "auth_time": 10,
            "sso_break_glass": True,
            "sub": "another-user",
            "gen": 900,
            "scopes": ["admin"],
            "secret": "discard",
        }
    ) == {
        "amr": ["sso", "oidc"],
        "sso_connection_id": connection,
        "nsg": 2,
        "auth_time": 10,
        "sso_break_glass": True,
    }


@pytest.mark.parametrize(
    "claims",
    [
        {"amr": "sso"},
        {"amr": ["sso", "oidc"], "sso_connection_id": "invalid"},
        {"amr": ["unknown"]},
        {"nsg": -1},
        {"nsg": True},
        {"auth_time": "now"},
    ],
)
def test_malformed_authentication_context_fails_closed(claims: dict) -> None:
    with pytest.raises(HTTPException):
        policy.authentication_claims(claims)


def test_absent_policy_does_not_change_membership_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        policy,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda name: None),
    )
    user = SimpleNamespace(id=uuid4(), account_id=uuid4(), non_sso_auth_generation=3)
    context = policy.new_authentication(Mock(), user, method="password")
    assert context["amr"] == ["password"]
    assert context["nsg"] == 3
    assert isinstance(context["auth_time"], int)
    assert "sso_connection_id" not in context


def test_installed_policy_receives_exact_destination_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = Mock()
    monkeypatch.setattr(
        policy,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda name: gate),
    )
    db = Mock()
    user = SimpleNamespace(id=uuid4(), account_id=uuid4(), non_sso_auth_generation=4)
    context = policy.new_authentication(
        db, user, method="passkey", source_ip="192.0.2.1"
    )
    gate.authorize.assert_called_once_with(
        db, user=user, claims=context, purpose="login", source_ip="192.0.2.1"
    )


def test_sso_assurance_requires_protocol_and_connection() -> None:
    assert not policy.has_sso_context({"amr": ["sso"]})
    assert not policy.has_sso_context({"amr": ["sso", "oidc"]})
    assert policy.has_sso_context(
        {"amr": ["sso", "oidc"], "sso_connection_id": str(uuid4())}
    )


def test_refresh_round_trip_preserves_bounded_assurance():
    from preloop.api.auth.jwt import create_refresh_token, decode_token

    account = str(uuid4())
    context = {
        "amr": ["sso", "oidc"],
        "sso_connection_id": str(uuid4()),
        "auth_account_id": account,
        "nsg": 7,
        "auth_nsg": 7,
        "auth_time": 100,
        "sub": "injected",
        "gen": 900,
    }
    token = decode_token(
        create_refresh_token("real-user", auth_generation=3, auth_context=context)
    )
    assert token.sub == "real-user" and token.gen == 3
    assert policy.authentication_claims(token.claims) == policy.authentication_claims(
        context
    )


def test_switch_retains_original_account_and_generation(monkeypatch):
    monkeypatch.setattr(
        policy,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda name: None),
    )
    source = uuid4()
    target = SimpleNamespace(account_id=uuid4(), non_sso_auth_generation=8)
    context = policy.switched_authentication(
        Mock(),
        target,
        claims={
            "amr": ["password"],
            "auth_account_id": str(source),
            "auth_nsg": 2,
            "nsg": 2,
        },
        source_account_id=source,
    )
    assert context["auth_account_id"] == str(source)
    assert context["auth_nsg"] == 2 and context["nsg"] == 8


@pytest.mark.parametrize("change", ["account", "user", "generation"])
def test_persisted_oauth_session_cannot_bypass_tenant_or_normal_revocation(change):
    user = SimpleNamespace(id=uuid4(), account_id=uuid4(), auth_generation=3)
    record = SimpleNamespace(
        user_id=user.id, account_id=user.account_id, auth_generation=3, auth_context={}
    )
    if change == "account":
        record.account_id = uuid4()
    elif change == "user":
        record.user_id = uuid4()
    else:
        record.auth_generation = 2
    with pytest.raises(HTTPException):
        policy.authorize_session_record(Mock(), user, record, purpose="refresh")
