"""Regression boundaries found during MFA integration review."""

import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import decode_token
from preloop.services import mfa
from tests.test_mfa import BASE, enroll, challenge
from tests.test_passkey_only import _user, _access, _bearer, _audit, PASSWORD
from tests import test_mfa as mfa_support

api = mfa_support.api
now = mfa_support.now


def test_login_success_waits_for_second_factor(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=last_login and success audit mean completed login;
    fails_when=password-only challenge records success; no_claim=notification delivery."""
    user, secret, _ = enroll(api, db_session, now)
    before = user.last_login
    count = len(_audit(db_session, user, "user.login"))
    token = challenge(api, user)
    assert user.last_login == before
    assert len(_audit(db_session, user, "user.login")) == count
    result = api.post(
        BASE + "/verify",
        json={"challenge_token": token, "code": mfa.totp(secret).at(now[0])},
    )
    assert result.status_code == 200
    assert user.last_login is not None
    assert len(_audit(db_session, user, "user.login")) == count + 1
    assert len(_audit(db_session, user, "auth.login.succeeded")) == 1


@pytest.mark.parametrize("factor", [False, True])
def test_registration_needs_recent_proof(
    api: TestClient, db_session: Session, now: list[float], factor: bool
) -> None:
    """protects=registration cannot turn an old session into a TOTP bypass;
    fails_when=stale password registers or fresh first-factor setup is blocked;
    no_claim=hardware enrollment is covered by signed assertion tests."""
    if factor:
        user, _, data = enroll(api, db_session, now)
    else:
        now[0] = time.time()
        user = _user(db_session)
        data = {"access_token": _access(user, ["password"], auth_time=int(now[0]))}
    old = _access(user, ["password"], auth_time=int(now[0]) - 660)
    response = api.post("/api/v1/auth/webauthn/register/options", headers=_bearer(old))
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "step_up_required"
    response = api.post(
        "/api/v1/auth/webauthn/register/options", headers=_bearer(data["access_token"])
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("path", ["options", "verify"])
def test_registration_rejects_api_keys(
    api: TestClient, db_session: Session, path: str
) -> None:
    """protects=API keys cannot install persistent human factors;
    fails_when=registration accepts a key; no_claim=valid attestation is not needed before authentication."""
    from preloop.models.crud import crud_api_key
    from preloop.api.auth.webauthn_router import _issue_challenge_token

    user = _user(db_session)
    _, key = crud_api_key.issue(
        db_session,
        obj_in={
            "name": "Synthetic",
            "user_id": user.id,
            "account_id": user.account_id,
            "scopes": [],
        },
    )
    body = (
        {}
        if path == "options"
        else {
            "credential": {},
            "challenge_token": _issue_challenge_token(
                b"synthetic", "register", str(user.person_id)
            ),
        }
    )
    response = api.post(
        "/api/v1/auth/webauthn/register/" + path, headers=_bearer(key), json=body
    )
    assert response.status_code == 401, response.text


def test_oauth_form_hands_off_to_console(
    api: TestClient,
    db_session: Session,
    now: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=legacy consent form can reach MFA without issuing a grant;
    fails_when=raw challenge escapes or OAuth request parameters are lost;
    no_claim=OAuth client validation and external client callback are stubbed."""
    from preloop.api.endpoints import oauth_consent

    user, _, _ = enroll(api, db_session, now)
    provider = Mock()
    monkeypatch.setattr(oauth_consent, "_get_oauth_provider", lambda: provider)
    monkeypatch.setattr(
        oauth_consent,
        "_validate_client_and_redirect",
        lambda *a: {"error": None, "client_name": "Synthetic client"},
    )
    monkeypatch.setattr(oauth_consent, "get_db_session", lambda: iter([db_session]))
    monkeypatch.setattr(db_session, "close", lambda: None)
    response = api.post(
        "/mcp/authorize/consent",
        data={
            "client_id": "cli",
            "redirect_uri": "http://localhost/callback",
            "code_challenge": "synthetic-pkce",
            "state": "synthetic-state",
            "username": user.username,
            "password": PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code in {302, 303, 307}, response.text
    target = urlparse(response.headers["location"])
    assert target.path == "/console/authorize"
    assert parse_qs(target.query)["state"] == ["synthetic-state"]
    assert parse_qs(target.query)["code_challenge"] == ["synthetic-pkce"]
    assert "challenge_token" not in response.headers["location"]
    provider.create_authorization_code_for_user.assert_not_called()


@pytest.mark.asyncio
async def test_enrolled_channel_approval_reaches_decision(
    api: TestClient,
    db_session: Session,
    now: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=paired chat decisions remain usable without console step-up;
    fails_when=internal channel request raises an interactive challenge;
    no_claim=provider, permissions and decision handler are stubs, transport and JWT are real."""
    from preloop.services import chat_assistant
    from uuid import uuid4

    user, _, _ = enroll(api, db_session, now)
    monkeypatch.setattr(chat_assistant.crud_chat, "principal", lambda *a: user)
    monkeypatch.setattr(chat_assistant.crud_chat, "eligible_approver", lambda *a: True)
    monkeypatch.setattr(chat_assistant, "has_permission", lambda *a: True)
    app = FastAPI()

    @app.post("/api/v1/approval-requests/{request_id}/decide")
    def decide(request: Request) -> dict[str, str]:
        claims = decode_token(request.headers["authorization"].split()[1]).claims
        assert claims["amr"] == ["chat"]
        return {"status": "approved"}

    broker = chat_assistant.ChatBroker(
        db_session, SimpleNamespace(), "synthetic-user", app
    )
    monkeypatch.setattr(broker, "authorize_resource", lambda *a: None)
    result = await broker.request(
        "POST",
        f"/api/v1/approval-requests/{uuid4()}/decide",
        "decide_approvals",
        {"decision": "approve"},
    )
    assert result == {"status": "approved"}


def test_first_passkey_registration_with_fresh_password(
    api: TestClient, db_session: Session
) -> None:
    """protects=first strong factor can bootstrap from a fresh password;
    fails_when=registration demands an existing factor; no_claim=hardware replaced by signed software attestation."""
    import base64
    from tests.endpoints.passkey_crafting import Authenticator

    user = _user(db_session)
    headers = _bearer(_access(user, ["password"]))
    started = api.post("/api/v1/auth/webauthn/register/options", headers=headers).json()
    authenticator = Authenticator()
    proof = authenticator.register(
        base64.urlsafe_b64decode(started["options"]["challenge"] + "=="),
        user_verified=True,
    )
    result = api.post(
        "/api/v1/auth/webauthn/register/verify",
        headers=headers,
        json={"credential": proof, "challenge_token": started["challenge_token"]},
    )
    assert result.status_code == 200, result.text
    assert (
        api.post("/api/v1/auth/webauthn/register/options", headers=headers).status_code
        == 401
    )


@pytest.mark.parametrize(
    "method,purpose", [("password", "exchange"), ("chat", "login")]
)
def test_channel_exception_is_not_a_generic_sign_in_bypass(
    db_session: Session, method: str, purpose: str
) -> None:
    """protects=channel seam cannot waive MFA for another method or login;
    fails_when=misapplied internal flag authorizes; no_claim=trusted callers cannot be attacker-controlled."""
    from fastapi import HTTPException
    from preloop.services.authentication_policy import authorize_authentication

    user = _user(db_session)
    with pytest.raises(HTTPException):
        authorize_authentication(
            db_session,
            user,
            claims={"amr": [method]},
            purpose=purpose,
            channel_action=True,
        )
