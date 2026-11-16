"""Core session boundaries for optional account sign-in enforcement."""

from collections.abc import Callable
from typing import Any
from sqlalchemy.orm import Session
from preloop.models import models
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from preloop.plugins import account_hooks
from preloop.services import authentication_policy as policy


def test_absent_policy_keeps_oss_sessions_unrestricted() -> None:
    """protects=OSS remains unrestricted; fails_when=stale enrollment claim sticks;
    no_claim=the plugin's decision and HTTP authentication are not exercised."""
    account_hooks.register_sign_in_strength_policy(None)
    claims = {"amr": ["password"], "enroll_only": True}
    account_hooks.apply_sign_in_strength_policy(Mock(), Mock(), claims, "refresh")
    assert "enroll_only" not in claims


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("GET", "/api/v1/users/me", True),
        ("GET", "/api/v1/me/memberships", True),
        ("POST", "/api/v1/auth/mfa/enroll", True),
        ("POST", "/api/v1/auth/mfa/enroll/confirm", True),
        ("GET", "/api/v1/auth/mfa/status", True),
        ("POST", "/api/v1/auth/webauthn/register/options", True),
        ("POST", "/api/v1/auth/webauthn/register/verify", True),
        ("GET", "/api/v1/auth/webauthn/credentials", True),
        ("POST", "/api/v1/auth/logout", True),
        ("GET", "/api/v1/agents", False),
        ("POST", "/api/v1/auth/api-keys", False),
        ("POST", "/api/v1/users/me", False),
        ("POST", "/api/v1/auth/mfa/disable", False),
        ("GET", "/api/v1/auth/mfa/status/extra", False),
    ],
)
def test_enrollment_allowlist(method: str, path: str, allowed: bool) -> None:
    """protects=restricted sessions only enroll or leave; fails_when=other API opens;
    no_claim=JWT verification is upstream of this request guard."""
    request = Request({"type": "http", "method": method, "path": path, "headers": []})
    if allowed:
        policy.enforce_enrollment_session({"enroll_only": True}, request)
    else:
        with pytest.raises(HTTPException) as exc:
            policy.enforce_enrollment_session({"enroll_only": True}, request)
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "strong_sign_in_required"


def test_restricted_session_cannot_authenticate_without_http_request() -> None:
    """protects=WebSocket and gateway fail closed; fails_when=no request bypasses;
    no_claim=transport-specific callers are covered separately."""
    with pytest.raises(HTTPException):
        policy.enforce_enrollment_session({"enroll_only": True}, None)


def test_enrollment_claim_is_bounded() -> None:
    """protects=enrollment context survives tokens; fails_when=claim is dropped;
    no_claim=the signed JWT is exercised in integration tests."""
    assert policy.authentication_claims({"enroll_only": True}) == {"enroll_only": True}
    with pytest.raises(HTTPException):
        policy.authentication_claims({"enroll_only": "false"})


def test_hook_decides_for_selected_membership() -> None:
    """protects=policy sees exact account and purpose; fails_when=hook is skipped;
    no_claim=CRUD and factor verification are mocked."""
    provider = Mock(
        side_effect=lambda db, user, claims, purpose: claims.update(enroll_only=True)
    )
    user = SimpleNamespace(non_sso_auth_generation=0, passkey_only=False)
    account_hooks.register_sign_in_strength_policy(provider)
    try:
        claims = {"amr": ["password"]}
        account_hooks.apply_sign_in_strength_policy(Mock(), user, claims, "login")
        assert claims["enroll_only"] is True
        assert provider.call_args.args[1] is user
        assert provider.call_args.args[3] == "login"
    finally:
        account_hooks.register_sign_in_strength_policy(None)


@pytest.mark.asyncio
async def test_live_and_websocket_sessions_recheck_enrollment(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=restricted sessions cannot open or retain authenticated sockets;
    fails_when=transport ignores current policy; no_claim=NATS delivery is replaced."""
    from preloop.api.auth.jwt import create_access_token, decode_token
    from preloop.api.middleware.websocket_auth import WebSocketAuthMiddleware
    from preloop.services import auth_revocation, db_executor

    token = create_access_token(data={"sub": str(test_user.id)})

    async def run(callback: Callable[[Session], Any], *args: Any, **kwargs: Any) -> Any:
        """Reuse the transactional test database instead of a worker session."""
        return callback(db_session)

    monkeypatch.setattr(db_executor, "run_db_async", run)
    monkeypatch.setattr(db_executor, "detach_user", lambda db, user: user)
    monkeypatch.setattr(auth_revocation, "run_db_async", run)
    middleware = WebSocketAuthMiddleware(Mock())
    account_hooks.register_sign_in_strength_policy(
        lambda db, user, claims, purpose: claims.update(enroll_only=True)
    )
    try:
        assert await middleware._validate_token(token) is None
        assert await auth_revocation.session_is_revoked(decode_token(token))
        account_hooks.register_sign_in_strength_policy(None)
        assert await middleware._validate_token(token) is not None
        assert not await auth_revocation.session_is_revoked(decode_token(token))
    finally:
        account_hooks.register_sign_in_strength_policy(None)


@pytest.mark.parametrize("purpose", ["refresh", "switch", "exchange", "login"])
def test_delegation_cannot_be_exchanged(purpose: str) -> None:
    """protects=delegated actor proof never becomes a target-owned session;
    fails_when=an exchange accepts delegation; no_claim=signature validation is upstream."""
    from uuid import uuid4

    with pytest.raises(HTTPException) as exc:
        policy.authorize_authentication(
            Mock(), Mock(), claims={"impersonated_by": str(uuid4())}, purpose=purpose
        )
    assert exc.value.status_code == 403


def test_delegated_proof_is_only_visible_to_access_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=bounded actor proof is distinct from target authentication;
    fails_when=proof escapes the hook or carries account-specific authority;
    no_claim=revocation and signatures are exercised by integration tests."""
    from uuid import uuid4

    user = SimpleNamespace(
        account_id=uuid4(), non_sso_auth_generation=0, passkey_only=True
    )
    gate = Mock()
    monkeypatch.setattr(
        policy, "get_plugin_manager", lambda: Mock(get_service=lambda name: gate)
    )
    provider = Mock()
    account_hooks.register_sign_in_strength_policy(provider)
    try:
        result = policy.authorize_authentication(
            Mock(),
            user,
            claims={
                "impersonated_by": str(uuid4()),
                "impersonator_auth": {
                    "amr": ["password", "otp"],
                    "auth_time": 123,
                    "sso_break_glass": True,
                },
            },
            purpose="access",
        )
        assert result == {}
        assert gate.authorize.call_args.kwargs["claims"]["delegated_auth"] == {
            "amr": ["password", "otp"],
            "auth_time": 123,
        }
        assert provider.call_args.args[2]["delegated_auth"] == {
            "amr": ["password", "otp"],
            "auth_time": 123,
        }
    finally:
        account_hooks.register_sign_in_strength_policy(None)
