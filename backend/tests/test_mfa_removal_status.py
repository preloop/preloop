"""Security status says when policy forbids removing the authenticator app.

The console uses ``totp_removal_blocked`` to explain instead of offering a
"Turn off" that the server refuses. Identities are synthetic.
"""

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.plugins.account_hooks import register_factor_removal_policy
from tests import test_mfa as mfa_support
from tests.test_mfa import BASE, enroll
from tests.test_passkey_only import _bearer

from tests import test_passkey_only as passkey_support

api = passkey_support.api
now = mfa_support.now


def test_status_reports_policy_blocked_removal(
    api: TestClient, db_session: Session, now: list[float]
) -> None:
    """protects=console can explain a refused removal up front;
    fails_when=status says removal is allowed while the guard refuses it."""
    user, _, data = enroll(api, db_session, now)
    headers = _bearer(data["access_token"])
    assert (
        api.get(BASE + "/status", headers=headers).json()["totp_removal_blocked"]
        is False
    )

    def refuse(db: Session, member: models.User, kind: str) -> None:
        raise HTTPException(403, detail={"code": "mfa_required_by_policy"})

    register_factor_removal_policy(refuse)
    try:
        status = api.get(BASE + "/status", headers=headers).json()
    finally:
        register_factor_removal_policy(None)
    assert status["totp_removal_blocked"] is True
    assert status["totp_enabled"] is True
