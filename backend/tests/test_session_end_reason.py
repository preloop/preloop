"""A refresh refused because an administrator changed sign-in names why.

The console reads ``X-Session-End-Reason`` to tell the person that the
sign-in policy changed or that their sign-in was reset. It never names the
administrator. All identities are synthetic.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import create_refresh_token, user_auth_generation
from preloop.models.crud import crud_audit_log, crud_user, mfa as crud_mfa
from preloop.services.authentication_policy import new_authentication
from tests.test_passkey_only import _user

from tests import test_passkey_only as passkey_support

api = passkey_support.api
HEADER = "X-Session-End-Reason"


def _refresh_token(db: Session, user: object) -> str:
    context = new_authentication(db, user, method="password")  # type: ignore[arg-type]
    return create_refresh_token(
        str(user.id),  # type: ignore[attr-defined]
        auth_generation=user_auth_generation(user),  # type: ignore[arg-type]
        auth_context=context,
    )


def _refresh(api: TestClient, token: str) -> object:
    return api.post("/api/v1/auth/refresh", json={"refresh_token": token})


def test_policy_change_names_the_policy(api: TestClient, db_session: Session) -> None:
    """protects=policy-ended session reason; fails_when=no reason or actor leaked."""
    user = _user(db_session)
    token = _refresh_token(db_session, user)
    crud_mfa.end_non_sso_sessions(db_session, user)
    crud_audit_log.log_action(
        db_session,
        account_id=user.account_id,
        user_id=None,
        action="account.sign_in_policy.updated",
        resource_type="account",
        resource_id=str(user.account_id),
        status="success",
        commit=False,
    )
    db_session.commit()
    response = _refresh(api, token)
    assert response.status_code == 401  # type: ignore[attr-defined]
    assert response.headers[HEADER] == "sign_in_policy"  # type: ignore[attr-defined]


def test_member_reset_names_the_reset(api: TestClient, db_session: Session) -> None:
    """protects=reset-ended session reason; fails_when=no reason."""
    user = _user(db_session)
    other = _user(db_session)
    token = _refresh_token(db_session, user)
    crud_user.bump_auth_generation(db_session, user_id=user.id, commit=False)
    crud_audit_log.log_action(
        db_session,
        account_id=user.account_id,
        user_id=other.id,
        action="account.member.sign_in_reset",
        resource_type="user",
        resource_id=str(user.id),
        status="success",
        details={"reason": "synthetic reason"},
        commit=False,
    )
    db_session.commit()
    response = _refresh(api, token)
    assert response.status_code == 401  # type: ignore[attr-defined]
    assert response.headers[HEADER] == "sign_in_reset"  # type: ignore[attr-defined]
    assert str(other.id) not in response.text  # type: ignore[attr-defined]
    assert "synthetic reason" not in response.text  # type: ignore[attr-defined]


def test_other_revocation_has_no_reason(api: TestClient, db_session: Session) -> None:
    """protects=no false attribution; fails_when=a reset of another member or an
    ordinary revocation is reported as a policy or reset."""
    user = _user(db_session)
    token = _refresh_token(db_session, user)
    crud_user.bump_auth_generation(db_session, user_id=user.id, commit=False)
    crud_audit_log.log_action(
        db_session,
        account_id=user.account_id,
        action="account.member.sign_in_reset",
        resource_type="user",
        resource_id=str(_user(db_session).id),
        status="success",
        commit=False,
    )
    db_session.commit()
    response = _refresh(api, token)
    assert response.status_code == 401  # type: ignore[attr-defined]
    assert HEADER not in response.headers  # type: ignore[attr-defined]
