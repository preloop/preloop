"""MFA acceptance against PostgreSQL and the real application.

All identities and secrets are synthetic. No upstream service is contacted.
"""

import json
import time
from typing import Any

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY, decode_token, verify_password
from preloop.models import models
from preloop.models.crud import mfa as crud_mfa
from preloop.services import mfa
from preloop.services.authentication_policy import new_authentication
from preloop.utils.encryption import decrypt_value
from tests.test_passkey_only import PASSWORD, _access, _audit, _bearer, _passkey, _user

from tests import test_passkey_only as passkey_support

api = passkey_support.api
BASE = "/api/v1/auth/mfa"


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Control TOTP time while real JWT expiry remains enabled."""
    value = [float(int(time.time()) // 30 * 30)]
    monkeypatch.setattr(mfa, "clock", lambda: value[0])
    return value


def enroll(
    api: TestClient, db: Session, now: list[float]
) -> tuple[models.User, str, dict[str, Any]]:
    """Enroll an ordinary single-user, self-hosted account."""
    user = _user(db)
    headers = _bearer(_access(user, ["password"]))
    started = api.post(BASE + "/enroll", headers=headers)
    assert started.status_code == 200, started.text
    secret = started.json()["secret"]
    confirmed = api.post(
        BASE + "/enroll/confirm",
        headers=headers,
        json={"code": mfa.totp(secret).at(now[0])},
    )
    assert confirmed.status_code == 200, confirmed.text
    now[0] += 30
    return user, secret, confirmed.json()


def challenge(api: TestClient, user: models.User) -> str:
    """Prove the password and assert that no session escaped the second step."""
    result = api.post(
        "/api/v1/auth/token/json", json={"username": user.email, "password": PASSWORD}
    )
    assert result.status_code == 401, result.text
    assert result.json()["detail"]["code"] == "mfa_required"
    assert "access_token" not in result.json()
    return result.json()["detail"]["challenge_token"]


def test_enrollment_encrypted_recovery_and_revocation(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=encryption, ten hashed codes, single-user enrollment and revocation;
    fails_when=plaintext persists or old session survives; no_claim=NATS delivery."""
    user = _user(db_session)
    old = _access(user, ["password"])
    headers = _bearer(old)
    started = api.post(BASE + "/enroll", headers=headers).json()
    pending = crud_mfa.factor(db_session, user.id)
    assert pending.secret_encrypted != started["secret"]
    assert decrypt_value(pending.secret_encrypted) == started["secret"]
    assert "Preloop" in started["otpauth_uri"]
    assert api.get(BASE + "/status", headers=headers).json()["totp_enabled"] is False
    result = api.post(
        BASE + "/enroll/confirm",
        headers=headers,
        json={"code": mfa.totp(started["secret"]).at(now[0])},
    )
    assert result.status_code == 200, result.text
    data = result.json()
    rows = crud_mfa.recovery_codes(db_session, user.id)
    assert len(rows) == len(data["recovery_codes"]) == 10
    for code in data["recovery_codes"]:
        assert sum(verify_password(code, row.code_hash) for row in rows) == 1
    assert api.get(BASE + "/status", headers=headers).status_code == 401
    assert api.get(BASE + "/status", headers=_bearer(data["access_token"])).json() == {
        "totp_enabled": True,
        "recovery_codes_remaining": 10,
    }
    assert decode_token(data["access_token"]).claims["amr"] == ["password", "otp"]
    assert started["secret"] not in json.dumps(data)


@pytest.mark.parametrize("form", [False, True])
def test_password_two_step_and_consumed_challenge(
    api: TestClient, db_session: Session, now: list[float], form: bool
) -> None:
    """protects=both password transports and one-time challenge exchange;
    fails_when=password alone issues tokens or challenge replays; no_claim=none beyond protects."""
    user, secret, _ = enroll(api, db_session, now)
    args = {"data" if form else "json": {"username": user.email, "password": PASSWORD}}
    result = api.post("/api/v1/auth/token" + ("" if form else "/json"), **args)
    assert result.status_code == 401
    token = result.json()["detail"]["challenge_token"]
    body = {"challenge_token": token, "code": mfa.totp(secret).at(now[0])}
    result = api.post(BASE + "/verify", json=body)
    assert result.status_code == 200, result.text
    assert decode_token(result.json()["access_token"]).claims["amr"] == [
        "password",
        "otp",
    ]
    assert (
        api.post(BASE + "/verify", json=body).json()["detail"]["code"]
        == "challenge_expired"
    )


@pytest.mark.parametrize(
    "offset,success", [(-2, False), (-1, True), (0, True), (1, True)]
)
def test_window_and_replay(
    api: TestClient, db_session: Session, now: list[float], offset: int, success: bool
) -> None:
    """protects=TOTP window and last-used-step; fails_when=old or repeated proof works;
    no_claim=concurrent requests have a separate test."""
    user, secret, data = enroll(api, db_session, now)
    now[0] += 90
    headers = _bearer(data["access_token"])
    body = {"code": mfa.totp(secret).at(now[0] + offset * 30)}
    response = api.post(BASE + "/step-up", headers=headers, json=body)
    assert response.status_code == (200 if success else 401), response.text
    if success:
        assert (
            api.post(BASE + "/step-up", headers=headers, json=body).json()["detail"][
                "code"
            ]
            == "invalid_code"
        )


def test_recovery_single_use(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=one-time recovery and count; fails_when=code reused;
    no_claim=none beyond protects."""
    user, _, data = enroll(api, db_session, now)
    code = data["recovery_codes"][0]
    first = api.post(
        BASE + "/verify",
        json={"challenge_token": challenge(api, user), "recovery_code": code},
    )
    assert first.status_code == 200, first.text
    second = api.post(
        BASE + "/verify",
        json={"challenge_token": challenge(api, user), "recovery_code": code},
    )
    assert second.status_code == 401
    assert (
        api.get(BASE + "/status", headers=_bearer(first.json()["access_token"])).json()[
            "recovery_codes_remaining"
        ]
        == 9
    )


def test_challenge_lock_and_user_budget(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=fifth failure locks challenge and tenth locks new challenges;
    fails_when=correct proof bypasses spent budget; no_claim=none beyond protects."""
    user, secret, _ = enroll(api, db_session, now)
    for _ in range(2):
        token = challenge(api, user)
        for _ in range(5):
            result = api.post(
                BASE + "/verify", json={"challenge_token": token, "code": "wrong"}
            )
            assert result.status_code == 401, result.text
            assert result.json()["detail"]["code"] == "invalid_code"
        result = api.post(
            BASE + "/verify",
            json={"challenge_token": token, "code": mfa.totp(secret).at(now[0])},
        )
        assert result.status_code == 429
        assert result.json()["detail"]["code"] == "challenge_locked"
    result = api.post(
        BASE + "/verify",
        json={
            "challenge_token": challenge(api, user),
            "code": mfa.totp(secret).at(now[0]),
        },
    )
    assert result.status_code == 429
    assert len(_audit(db_session, user, "auth.mfa.verify_failed")) == 10
    assert len(_audit(db_session, user, "auth.mfa.challenge_locked")) == 2


def test_challenge_typing_and_expiry(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=challenge cannot authenticate APIs, refresh or reset a password;
    fails_when=wrong token type or expired challenge accepted; no_claim=none beyond protects."""
    from preloop.utils.tokens import TokenError, verify_user_token

    user, secret, _ = enroll(api, db_session, now)
    token = challenge(api, user)
    assert api.get("/api/v1/auth/users/me", headers=_bearer(token)).status_code == 401
    assert (
        api.post("/api/v1/auth/refresh", json={"refresh_token": token}).status_code
        == 401
    )
    with pytest.raises(TokenError):
        verify_user_token(token, "password_reset")
    payload = mfa.read_challenge(token)
    payload.update(iat=int(time.time()) - 360, exp=int(time.time()) - 60)
    expired = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    result = api.post(
        BASE + "/verify",
        json={"challenge_token": expired, "code": mfa.totp(secret).at(now[0])},
    )
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "challenge_expired"


@pytest.mark.parametrize("method", ["password", "email_link", "chat", "social"])
def test_weak_methods_challenged(
    api: TestClient, db_session: Session, now: list[float], method: str
) -> None:
    """protects=all weak token-minting methods; fails_when=weak method bypasses gate;
    no_claim=chat/social provider callbacks live in edition plugins."""
    user, _, _ = enroll(api, db_session, now)
    with pytest.raises(HTTPException) as caught:
        new_authentication(db_session, user, method=method)
    assert caught.value.detail["code"] == "mfa_required"


@pytest.mark.parametrize("method", ["passkey", "oidc", "saml"])
def test_strong_methods_and_passkey_only(
    api: TestClient, db_session: Session, now: list[float], method: str
) -> None:
    """protects=strong sign-in unaffected and passkey-only password remains closed;
    fails_when=TOTP reopens password or SSO challenged; no_claim=external IdP protocol."""
    from preloop.models.crud import crud_user

    user, _, _ = enroll(api, db_session, now)
    assert method in new_authentication(db_session, user, method=method)["amr"]
    crud_user.set_passkey_only(db_session, user=user, enabled=True, end_sessions=False)
    result = api.post(
        "/api/v1/auth/token/json", json={"username": user.email, "password": PASSWORD}
    )
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "password_login_disabled"


def test_regenerate_disable_and_audit_privacy(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=regeneration replaces proofs, disable uses own proof, private audits;
    fails_when=old codes survive or secret/code/hash is audited; no_claim=none beyond protects."""
    user, secret, data = enroll(api, db_session, now)
    headers = _bearer(data["access_token"])
    response = api.post(
        BASE + "/recovery-codes",
        headers=headers,
        json={"code": mfa.totp(secret).at(now[0])},
    )
    assert response.status_code == 200, response.text
    new_codes = response.json()["recovery_codes"]
    assert set(data["recovery_codes"]).isdisjoint(new_codes)
    invalid = api.post(
        BASE + "/disable",
        headers=headers,
        json={"recovery_code": data["recovery_codes"][0]},
    )
    assert invalid.status_code == 401
    result = api.post(
        BASE + "/disable", headers=headers, json={"recovery_code": new_codes[0]}
    )
    assert result.status_code == 204, result.text
    assert crud_mfa.factor(db_session, user.id) is None
    assert crud_mfa.remaining(db_session, user.id) == 0
    assert (
        api.post(
            "/api/v1/auth/token/json",
            json={"username": user.email, "password": PASSWORD},
        ).status_code
        == 200
    )
    for action in [
        "enrolled",
        "verify_failed",
        "recovery_codes_regenerated",
        "recovery_code_used",
        "disabled",
    ]:
        rows = _audit(db_session, user, "auth.mfa." + action)
        assert len(rows) == 1
        details = json.dumps(rows[0].details)
        assert secret not in details
        assert all(
            code not in details for code in [*data["recovery_codes"], *new_codes]
        )
        assert set(rows[0].details) <= {"person_id"}


def test_step_up_allows_passkey_delete(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=stale factor deletion requires fresh proof; fails_when=stale succeeds;
    no_claim=none beyond protects."""
    user, secret, _ = enroll(api, db_session, now)
    key = _passkey(db_session, user)
    _passkey(db_session, user)
    stale = _bearer(_access(user, ["password", "otp"], auth_time=int(now[0]) - 660))
    response = api.delete(f"/api/v1/auth/webauthn/credentials/{key.id}", headers=stale)
    assert response.status_code == 401, response.text
    assert "totp" in response.json()["detail"]["methods"]
    upgraded = api.post(
        BASE + "/step-up", headers=stale, json={"code": mfa.totp(secret).at(now[0])}
    )
    assert upgraded.status_code == 200, upgraded.text
    assert (
        api.delete(
            f"/api/v1/auth/webauthn/credentials/{key.id}",
            headers=_bearer(upgraded.json()["access_token"]),
        ).status_code
        == 204
    )


def test_rollback_switch(
    api: TestClient,
    db_session: Session,
    now: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=documented emergency downgrade; fails_when=enrollment remains open;
    no_claim=this switch deliberately removes the second factor requirement."""
    user, _, data = enroll(api, db_session, now)
    monkeypatch.setenv("MFA_TOTP_ENABLED", "false")
    assert (
        api.post(BASE + "/enroll", headers=_bearer(data["access_token"])).status_code
        == 404
    )
    assert (
        api.post(
            "/api/v1/auth/token/json",
            json={"username": user.email, "password": PASSWORD},
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "method,age,totp,passkey,sso,allowed,available",
    [
        ("otp", 0, True, False, False, True, []),
        ("otp", 600, True, False, False, True, []),
        ("otp", 601, True, False, False, False, ["totp"]),
        ("password", 0, False, True, False, False, ["passkey"]),
        ("passkey", 1, False, True, False, True, []),
        ("sso", 599, False, False, True, True, []),
        ("sso", 601, False, False, True, False, ["sso"]),
        ("password", 0, False, False, False, False, ["enroll"]),
        ("otp", -1, True, True, False, False, ["totp", "passkey"]),
    ],
)
def test_step_up_matrix(
    db_session: Session,
    now: list[float],
    method: str,
    age: int,
    totp: bool,
    passkey: bool,
    sso: bool,
    allowed: bool,
    available: list[str],
) -> None:
    """protects=strong method and exact freshness window with available methods;
    fails_when=weak, stale or future assurance passes; no_claim=external IdP protocol."""
    import uuid
    from datetime import UTC, datetime
    from preloop.services.authentication_policy import check_recent_strong_auth
    from preloop.utils.encryption import encrypt_value

    user = _user(db_session)
    if totp:
        obj = crud_mfa.enroll(db_session, user.id, encrypt_value("JBSWY3DPEHPK3PXP"))
        crud_mfa.confirm(db_session, obj, now=datetime.now(UTC))
    if passkey:
        _passkey(db_session, user)
    claims = {
        "amr": ["sso", "oidc"] if sso else [method],
        "passkey_uv": method == "passkey",
        "auth_time": int(now[0]) - age,
    }
    if sso:
        claims["sso_connection_id"] = str(uuid.uuid4())
    if allowed:
        check_recent_strong_auth(db_session, user, claims, 600)
    else:
        with pytest.raises(HTTPException) as refused:
            check_recent_strong_auth(db_session, user, claims, 600)
        assert refused.value.status_code == 401
        assert refused.value.detail == {
            "code": "step_up_required",
            "methods": available,
        }


def test_email_link_and_chat_exchange(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=email-link endpoint and actual chat exchange context require OTP;
    fails_when=either issues a session; no_claim=email delivery and chat transport."""
    from preloop.models.crud import crud_person
    from preloop.utils.tokens import create_email_verification_token
    from preloop.services.authentication_policy import authorize_authentication

    user, _, _ = enroll(api, db_session, now)
    person = crud_person.resolve_for_membership(db_session, membership_id=user.id)
    token = create_email_verification_token(
        person.email, person_id=person.id, auth_generation=person.auth_generation
    )
    response = api.post("/api/v1/auth/verify-email", json={"token": token})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "mfa_required"
    with pytest.raises(HTTPException) as refused:
        authorize_authentication(
            db_session,
            user,
            claims={
                "amr": ["chat"],
                "auth_account_id": str(user.account_id),
                "nsg": user.non_sso_auth_generation,
            },
            purpose="exchange",
        )
    assert refused.value.detail["code"] == "mfa_required"


def test_reset_script(
    api: TestClient,
    db_session: Session,
    now: list[float],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """protects=operator reset clears factors, revokes non-SSO and audits once;
    fails_when=proofs or sessions survive, unknown email exits zero or output echoes email;
    no_claim=main is invoked in-process against the real database."""
    from collections.abc import Generator
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "reset_mfa", Path(__file__).resolve().parents[2] / "scripts/reset_mfa.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main = module.main
    import uuid

    user, secret, data = enroll(api, db_session, now)

    def sessions() -> Generator[Session, None, None]:
        yield db_session

    sso = _access(user, ["sso", "oidc"], sso_connection_id=str(uuid.uuid4()))
    assert main(["--email", user.email, "--reason", "lost device"], sessions) == 0
    assert capsys.readouterr().out == (
        "Authenticator sign-in turned off; non-SSO sessions have ended.\n"
    )
    assert crud_mfa.factor(db_session, user.id) is None
    assert crud_mfa.remaining(db_session, user.id) == 0
    assert (
        api.get(BASE + "/status", headers=_bearer(data["access_token"])).status_code
        == 401
    )
    assert api.get(BASE + "/status", headers=_bearer(sso)).status_code == 200
    events = _audit(db_session, user, "auth.mfa.reset")
    assert len(events) == 1
    assert events[0].details["reason"] == "lost device"
    assert secret not in json.dumps(events[0].details)
    assert (
        main(["--email", "nobody@example.com", "--reason", "lost device"], sessions)
        == 1
    )
    assert "nobody@example.com" not in capsys.readouterr().err


@pytest.mark.parametrize("user_verified", [True, False])
def test_passkey_step_up(
    api: TestClient, db_session: Session, now: list[float], user_verified: bool
) -> None:
    """protects=passkey-only step-up binds current user and requires real UV;
    fails_when=unverified assertion upgrades or verified one fails;
    no_claim=software authenticator substitutes for hardware."""
    import base64
    from tests.endpoints.passkey_crafting import Authenticator
    from preloop.models.crud import crud_user

    user = _user(db_session)
    authenticator = Authenticator()
    _passkey(db_session, user, authenticator)
    crud_user.set_passkey_only(db_session, user=user, enabled=True, end_sessions=False)
    old = _access(user, ["passkey"], auth_time=int(now[0]) - 660)
    options_response = api.post(
        "/api/v1/auth/webauthn/authenticate/options",
        headers=_bearer(old),
        json={"step_up": True, "account_id": str(user.account_id)},
    )
    assert options_response.status_code == 200
    options = options_response.json()
    challenge_bytes = base64.urlsafe_b64decode(options["options"]["challenge"] + "==")
    body = {
        "step_up": True,
        "account_id": str(user.account_id),
        "challenge_token": options["challenge_token"],
        "credential": authenticator.assert_(
            challenge_bytes, user_verified=user_verified
        ),
    }
    response = api.post(
        "/api/v1/auth/webauthn/authenticate/verify", headers=_bearer(old), json=body
    )
    if user_verified:
        assert response.status_code == 200, response.text
        claims = decode_token(response.json()["access_token"]).claims
        assert claims["amr"] == ["passkey"]
        assert int(time.time()) - claims["auth_time"] < 5
        other = _user(db_session)
        assert (
            api.post(
                "/api/v1/auth/webauthn/authenticate/verify",
                headers=_bearer(_access(other, ["password"])),
                json=body,
            ).status_code
            >= 400
        )
    else:
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "passkey_user_verification_required"


def test_no_provider_and_removal_guard(db_session: Session, now: list[float]) -> None:
    """protects=OSS hook is inert and installed policy controls only named actions;
    fails_when=missing provider requires proof; no_claim=edition policy behavior."""
    from preloop.plugins import account_hooks
    from unittest.mock import Mock

    account_hooks.register_step_up_policy(None)
    account_hooks.enforce_step_up_policy(Mock(), Mock(), Mock(), "approval.approve")
    assert account_hooks.get_factor_removal_policy() is None


def test_shared_store_outage_fails_closed(
    api: TestClient,
    db_session: Session,
    now: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=unavailable shared budget never falls back to accepting proofs;
    fails_when=outage issues tokens; no_claim=network failure is simulated at CRUD."""
    from preloop.models.crud import crud_rate_limit
    from sqlalchemy.exc import OperationalError

    user, secret, _ = enroll(api, db_session, now)
    token = challenge(api, user)

    def fail(*args: Any, **kwargs: Any) -> int:
        raise OperationalError("unavailable", {}, None)

    monkeypatch.setattr(crud_rate_limit, "peek", fail)
    with pytest.raises(OperationalError):
        api.post(
            BASE + "/verify",
            json={"challenge_token": token, "code": mfa.totp(secret).at(now[0])},
        )


@pytest.mark.parametrize(
    "body",
    [
        {"code": "PRIVATE" * 20},
        {"code": "123456", "recovery_code": "abcde-fghij"},
        {"code": {"secret": "PRIVATE"}},
    ],
)
def test_invalid_request_does_not_echo_proofs(
    api: TestClient, db_session: Session, body: dict[str, Any]
) -> None:
    """protects=validation never echoes secrets or codes; fails_when=input returns;
    no_claim=none beyond protects."""
    user = _user(db_session)
    response = api.post(
        BASE + "/disable", headers=_bearer(_access(user, ["password"])), json=body
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "invalid_request",
            "message": "Supply the requested authentication proof.",
        }
    }


def test_factor_removal_policy_seam(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=edition removal guard can refuse without consuming proof;
    fails_when=removal or code consumption precedes guard;
    no_claim=the edition's decision logic is represented by a rejecting provider."""
    from preloop.plugins.account_hooks import register_factor_removal_policy

    user, secret, data = enroll(api, db_session, now)

    def refuse(db: Session, member: models.User, kind: str) -> None:
        assert kind == "totp" and member.id == user.id
        raise HTTPException(403, detail={"code": "mfa_required_by_policy"})

    headers = _bearer(data["access_token"])
    body = {"code": mfa.totp(secret).at(now[0])}
    register_factor_removal_policy(refuse)
    try:
        response = api.post(BASE + "/disable", headers=headers, json=body)
        assert response.status_code == 403
        assert crud_mfa.enabled(db_session, user.id)
    finally:
        register_factor_removal_policy(None)
    assert api.post(BASE + "/disable", headers=headers, json=body).status_code == 204


@pytest.mark.parametrize("scenario", ["replay", "mode", "revoked", "uv_rollback"])
def test_passkey_step_up_bound_challenge(
    api: TestClient,
    db_session: Session,
    now: list[float],
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """protects=session-bound, single-use UV proof under rollback and revocation;
    fails_when=replay, mode change or revoked challenge upgrades a session;
    no_claim=software authenticator replaces hardware and revocation is sequential."""
    import base64
    from tests.endpoints.passkey_crafting import Authenticator
    from preloop.services.authentication_policy import check_recent_strong_auth

    user = _user(db_session)
    authenticator = Authenticator()
    _passkey(db_session, user, authenticator)
    old = _access(user, ["password"])
    headers = _bearer(old)
    if scenario == "uv_rollback":
        monkeypatch.setenv("PASSKEYS_REQUIRE_USER_VERIFICATION", "false")
    options = api.post(
        "/api/v1/auth/webauthn/authenticate/options",
        headers=headers,
        json={"step_up": True, "account_id": str(user.account_id)},
    ).json()
    assert options["options"]["userVerification"] == "required"
    challenge_bytes = base64.urlsafe_b64decode(options["options"]["challenge"] + "==")
    body = {
        "step_up": scenario != "mode",
        "account_id": str(user.account_id),
        "challenge_token": options["challenge_token"],
        "credential": authenticator.assert_(challenge_bytes, user_verified=True),
    }
    if scenario == "revoked":
        crud_mfa.end_non_sso_sessions(db_session, user)
        db_session.commit()
        headers = _bearer(_access(user, ["password"]))
    response = api.post(
        "/api/v1/auth/webauthn/authenticate/verify",
        headers=headers,
        json=body,
    )
    if scenario in {"mode", "revoked"}:
        assert response.status_code == 401, response.text
        assert "access_token" not in response.json()
    else:
        assert response.status_code == 200, response.text
        claims = decode_token(response.json()["access_token"]).claims
        now[0] = time.time()
        assert claims["passkey_uv"] is True
        check_recent_strong_auth(db_session, user, claims, 600)
        # A fresh signature with a larger counter must not reuse this challenge.
        body["credential"] = authenticator.assert_(challenge_bytes, user_verified=True)
        replay = api.post(
            "/api/v1/auth/webauthn/authenticate/verify",
            headers=headers,
            json=body,
        )
        assert replay.status_code == 401
        assert replay.json()["detail"] == "Passkey challenge already used"


def test_enrollment_rechecks_session_after_lock(
    api: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=revocation during enrollment authorization prevents secret creation;
    fails_when=revoked session starts enrollment; no_claim=lock boundary simulated."""
    user = _user(db_session)
    old = _access(user, ["password"])
    original = mfa.locked_user

    def revoke_at_lock(db: Session, member: models.User) -> models.User:
        locked = original(db, member)
        crud_mfa.end_non_sso_sessions(db, locked)
        return locked

    monkeypatch.setattr(mfa, "locked_user", revoke_at_lock)
    response = api.post(BASE + "/enroll", headers=_bearer(old))
    assert response.status_code == 401
    assert crud_mfa.factor(db_session, user.id) is None


@pytest.mark.parametrize("uv", [None, False, True])
@pytest.mark.parametrize("configured", ["true", "false"])
def test_step_up_requires_attested_uv_claim(
    db_session: Session,
    now: list[float],
    monkeypatch: pytest.MonkeyPatch,
    uv: bool | None,
    configured: str,
) -> None:
    """protects=only attested UV satisfies passkey freshness across config changes;
    fails_when=legacy or unverified passkey session is strong; no_claim=hardware."""
    from preloop.services.authentication_policy import check_recent_strong_auth

    monkeypatch.setenv("PASSKEYS_REQUIRE_USER_VERIFICATION", configured)
    user = _user(db_session)
    claims = {"amr": ["passkey"], "auth_time": int(now[0])}
    if uv is not None:
        claims["passkey_uv"] = uv
    if uv is True:
        check_recent_strong_auth(db_session, user, claims, 600)
    else:
        with pytest.raises(HTTPException) as refused:
            check_recent_strong_auth(db_session, user, claims, 600)
        assert refused.value.detail["code"] == "step_up_required"
