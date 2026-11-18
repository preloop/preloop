"""The two MFA lockouts tell the user what to do.

A spent challenge can be restarted at once; a spent per-user budget cannot,
and says when it ends. No limit is loosened: a recovery code is refused under
the per-user lock too. All identities and secrets are synthetic.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.services import mfa
from tests import test_mfa as mfa_support
from tests.test_mfa import BASE, challenge, enroll

from tests import test_passkey_only as passkey_support

api = passkey_support.api
now = mfa_support.now


def test_challenge_lock_and_user_lock_are_distinct(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=actionable lock messages; fails_when=both locks look alike, the
    user lock omits its duration, or a recovery code bypasses it."""
    user, secret, data = enroll(api, db_session, now)
    for _ in range(2):
        token = challenge(api, user)
        for _ in range(5):
            api.post(BASE + "/verify", json={"challenge_token": token, "code": "x"})
        locked = api.post(
            BASE + "/verify",
            json={"challenge_token": token, "code": mfa.totp(secret).at(now[0])},
        )
        assert locked.status_code == 429
        detail = locked.json()["detail"]
        assert detail["code"] == "challenge_locked"
        assert detail["message"] == (
            "Too many incorrect codes for this sign-in. Start again."
        )
    for proof in (
        {"code": mfa.totp(secret).at(now[0])},
        {"recovery_code": data["recovery_codes"][0]},
    ):
        result = api.post(
            BASE + "/verify", json={"challenge_token": challenge(api, user), **proof}
        )
        assert result.status_code == 429, result.text
        detail = result.json()["detail"]
        assert detail["code"] == "mfa_user_locked"
        remaining = 3600 - int(now[0]) % 3600
        assert detail["retry_after_seconds"] == remaining
        assert result.headers["Retry-After"] == str(remaining)
        assert "Try again in" in detail["message"]
        assert "recovery code" not in detail["message"]
