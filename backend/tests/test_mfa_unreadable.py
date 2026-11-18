"""An authenticator secret that this server's key cannot decrypt.

This happens after a database restore under a different encryption key. The
factor is unusable: no code is ever accepted, every endpoint answers with a
stable 409 instead of a server error, recovery codes keep working, and the
signed-in user can remove the factor with a recovery code and enrol again.
All identities and secrets are synthetic.
"""

import json
import logging
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from preloop.models.crud import mfa as crud_mfa, sign_in_policy
from preloop.services import mfa
from tests import test_mfa as mfa_support
from tests.test_mfa import BASE, challenge, enroll
from tests.test_passkey_only import _access, _bearer, _user

from tests import test_passkey_only as passkey_support

api = passkey_support.api
now = mfa_support.now


def _rekey(db: Session, user_id: object) -> None:
    """Store the factor as another server's key would have written it."""
    obj = crud_mfa.factor(db, user_id)  # type: ignore[arg-type]
    assert obj is not None
    other = Fernet(Fernet.generate_key())
    obj.secret_encrypted = other.encrypt(b"JBSWY3DPEHPK3PXP").decode()
    db.flush()


def _assert_unreadable(response: Response, secret: str) -> None:
    status = response.status_code
    text = response.text
    assert status == 409, text
    detail = json.loads(text)["detail"]
    assert detail["code"] == "factor_unreadable"
    assert "recovery code" in detail["message"]
    assert "administrator" in detail["message"]
    assert secret not in text


def test_verify_with_unreadable_factor_is_409_and_recovery_still_works(
    api: TestClient,
    db_session: Session,
    now: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """protects=key-changed restore gives a stable 409 not a 500; fails_when=500,
    a code is accepted, or secret/code is logged; no_claim=key rotation."""
    user, secret, data = enroll(api, db_session, now)
    _rekey(db_session, user.id)
    code = mfa.totp(secret).at(now[0])
    token = challenge(api, user)
    with caplog.at_level(logging.DEBUG):
        response = api.post(
            BASE + "/verify", json={"challenge_token": token, "code": code}
        )
    _assert_unreadable(response, secret)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert str(user.id) in errors[0].getMessage()
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert code not in record.getMessage()
    # The same challenge still accepts a recovery code.
    recovered = api.post(
        BASE + "/verify",
        json={"challenge_token": token, "recovery_code": data["recovery_codes"][0]},
    )
    assert recovered.status_code == 200, recovered.text
    status = api.get(
        BASE + "/status", headers=_bearer(recovered.json()["access_token"])
    ).json()
    assert status["totp_enabled"] is True
    assert status["totp_needs_setup"] is True


def test_every_factor_proof_refuses_unreadable_factor(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=step-up, regenerate, disable and enrol-confirm share the handling;
    fails_when=any returns 500 or accepts a code; no_claim=none beyond protects."""
    user, secret, data = enroll(api, db_session, now)
    headers = _bearer(data["access_token"])
    _rekey(db_session, user.id)
    code = mfa.totp(secret).at(now[0])
    for path in ("/step-up", "/recovery-codes", "/disable"):
        _assert_unreadable(
            api.post(BASE + path, headers=headers, json={"code": code}), secret
        )
    assert crud_mfa.factor(db_session, user.id) is not None
    # Removal with a recovery code works, then the user enrols again.
    removed = api.post(
        BASE + "/disable",
        headers=headers,
        json={"recovery_code": data["recovery_codes"][0]},
    )
    assert removed.status_code == 204, removed.text
    again = api.post(BASE + "/enroll", headers=headers)
    assert again.status_code == 200, again.text


def test_enroll_confirm_refuses_unreadable_pending_factor(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=enrol confirm shares the handling; fails_when=500."""
    user = _user(db_session)
    headers = _bearer(_access(user, ["password"]))
    started = api.post(BASE + "/enroll", headers=headers)
    secret = started.json()["secret"]
    _rekey(db_session, user.id)
    response = api.post(
        BASE + "/enroll/confirm",
        headers=headers,
        json={"code": mfa.totp(secret).at(now[0])},
    )
    _assert_unreadable(response, secret)


def test_coverage_reports_unreadable_factor_as_unusable(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=admin coverage does not count an unusable factor;
    fails_when=it shows as a working authenticator."""
    user, _, _ = enroll(api, db_session, now)
    assert sign_in_policy.factor_coverage(db_session, user.account_id)[user.id] == {
        "passkey": False,
        "totp": True,
        "totp_unusable": False,
    }
    _rekey(db_session, user.id)
    obj = crud_mfa.factor(db_session, user.id)
    assert obj is not None and obj.confirmed_at <= datetime.now(UTC)
    assert sign_in_policy.factor_coverage(db_session, user.account_id)[user.id] == {
        "passkey": False,
        "totp": False,
        "totp_unusable": True,
    }
