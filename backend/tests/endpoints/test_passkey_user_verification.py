"""Passkey ceremonies require user verification (UV), with a rollback switch.

These tests do not mock ``py_webauthn``: a software authenticator
(``passkey_crafting``) signs real authenticator data with or without the UV
flag, and the router verifies it with the real library. Only the database is
a stand-in (``MagicMock`` session, patched CRUD), as in
``test_webauthn_passkeys.py``.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import decode_token, get_current_active_user
from preloop.api.auth.webauthn_router import _issue_challenge_token
from preloop.api.auth.webauthn_router import router as webauthn_router
from preloop.models.models.user import User

from tests.endpoints.passkey_crafting import ORIGIN, RP_ID, Authenticator, b64url

app = FastAPI()
app.include_router(webauthn_router, prefix="/auth/webauthn")
client = TestClient(app)

UV_CODE = "passkey_user_verification_required"


@pytest.fixture(autouse=True)
def relying_party(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the RP ID and origin the crafted responses are bound to."""
    monkeypatch.setenv("WEBAUTHN_RP_ID", RP_ID)
    monkeypatch.setenv("WEBAUTHN_ORIGIN", ORIGIN)
    monkeypatch.delenv("PASSKEYS_REQUIRE_USER_VERIFICATION", raising=False)


@pytest.fixture
def user(monkeypatch: pytest.MonkeyPatch):
    """One active membership whose person owns the passkeys."""
    from preloop.models.crud import crud_account, crud_person
    from preloop.models.db.session import get_db_session

    member = MagicMock(spec=User)
    member.id = uuid.uuid4()
    member.account_id = uuid.uuid4()
    member.person_id = member.id
    member.email_verified = True
    member.auth_generation = 0
    member.non_sso_auth_generation = 0
    member.passkey_only = False
    member.username = "uvuser"
    member.email = "uvuser@example.com"
    member.full_name = "UV User"
    member.is_active = True
    member.last_login = datetime.now(UTC)

    def person() -> SimpleNamespace:
        return SimpleNamespace(
            id=member.person_id,
            username=member.username,
            email=member.email,
            full_name=member.full_name,
            identity_status="active",
            auth_generation=0,
            email_verified_at=datetime.now(UTC),
            last_active_user_id=member.id,
        )

    for name in ("get", "lock_for_session", "resolve_for_membership"):
        monkeypatch.setattr(crud_person, name, lambda *a, **k: person())
    monkeypatch.setattr(crud_person, "has_external_identity", lambda *a, **k: False)
    monkeypatch.setattr(crud_person, "list_memberships", lambda *a, **k: [member])
    monkeypatch.setattr(
        crud_account, "get", lambda *a, **k: SimpleNamespace(is_active=True)
    )
    app.dependency_overrides[get_db_session] = lambda: MagicMock(spec=Session)
    app.dependency_overrides[get_current_active_user] = lambda: member
    try:
        yield member
    finally:
        app.dependency_overrides.clear()


def _register(user, authenticator: Authenticator, *, user_verified: bool):
    challenge = b"registration-challenge-0123456789"
    token = _issue_challenge_token(challenge, "register", str(user.person_id))
    with patch("preloop.api.auth.webauthn_router.crud_webauthn_credential") as crud:
        crud.get_by_credential_id.return_value = None
        crud.create.side_effect = lambda db, *, obj_in: SimpleNamespace(
            id=uuid.uuid4(), created_at=datetime.now(UTC), **obj_in
        )
        response = client.post(
            "/auth/webauthn/register/verify",
            json={
                "credential": authenticator.register(
                    challenge, user_verified=user_verified
                ),
                "challenge_token": token,
                "name": "Laptop",
            },
        )
    return response, crud


def _authenticate(user, authenticator: Authenticator, *, user_verified: bool):
    challenge = b"authentication-challenge-0123456"
    stored = SimpleNamespace(
        id=uuid.uuid4(),
        person_id=user.person_id,
        user_id=user.id,
        public_key=b64url(authenticator.cose_public_key()),
        sign_count=0,
    )
    with (
        patch("preloop.api.auth.webauthn_router.crud_webauthn_credential") as crud,
        patch("preloop.api.auth.webauthn_router.login_membership", return_value=user),
        patch("preloop.api.auth.webauthn_router.crud_audit_log"),
        patch("preloop.services.auth_audit.crud_audit_log"),
    ):
        crud.get_by_credential_id.return_value = stored
        response = client.post(
            "/auth/webauthn/authenticate/verify",
            json={
                "credential": authenticator.assert_(
                    challenge, user_verified=user_verified
                ),
                "challenge_token": _issue_challenge_token(challenge, "authenticate"),
            },
        )
    return response, crud


def test_registration_without_uv_flag_is_rejected(user):
    """protects=a passkey that never verified the user is not stored;
    fails_when=attestation without the UV flag returns 200 or a generic 400;
    no_claim=real authenticators, attestation formats other than "none";
    the DB is a MagicMock with patched CRUD."""
    response, crud = _register(user, Authenticator(), user_verified=False)

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == UV_CODE
    assert "PIN or biometric" in response.json()["detail"]["message"]
    crud.create.assert_not_called()


def test_registration_with_uv_flag_is_stored(user):
    """protects=a verified passkey still registers;
    fails_when=the UV requirement rejects a correctly flagged attestation;
    no_claim=real authenticators; the DB is a MagicMock with patched CRUD."""
    authenticator = Authenticator()
    response, crud = _register(user, authenticator, user_verified=True)

    assert response.status_code == 200, response.text
    stored = crud.create.call_args.kwargs["obj_in"]
    assert stored["credential_id"] == b64url(authenticator.credential_id)


def test_sign_in_without_uv_flag_is_rejected(user):
    """protects=a passkey assertion that did not verify the user issues no token;
    fails_when=an assertion without the UV flag signs in or maps to a generic 401;
    no_claim=real authenticators; the DB is a MagicMock with patched CRUD."""
    response, crud = _authenticate(user, Authenticator(), user_verified=False)

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == UV_CODE
    assert "access_token" not in response.json()
    crud.touch.assert_not_called()


def test_sign_in_with_uv_flag_issues_passkey_tokens(user):
    """protects=a verified passkey assertion signs in with amr=passkey;
    fails_when=a correctly flagged, correctly signed assertion is refused;
    no_claim=real authenticators; the DB is a MagicMock with patched CRUD."""
    response, _ = _authenticate(user, Authenticator(), user_verified=True)

    assert response.status_code == 200, response.text
    claims = decode_token(response.json()["access_token"]).claims
    assert claims["amr"] == ["passkey"]


def test_options_request_required_and_rollback_switch_restores_preferred(
    user, monkeypatch: pytest.MonkeyPatch
):
    """protects=browsers are asked for UV, and the rollback switch undoes it;
    fails_when=options say "preferred" by default, or the switch has no effect,
    or a no-UV assertion is still refused while the switch is off;
    no_claim=how a given browser reacts to the requirement; DB is mocked."""
    with patch("preloop.api.auth.webauthn_router.crud_webauthn_credential") as crud:
        crud.list_for_person.return_value = []
        register = client.post("/auth/webauthn/register/options").json()
    login = client.post("/auth/webauthn/authenticate/options").json()
    assert register["options"]["authenticatorSelection"]["userVerification"] == (
        "required"
    )
    assert login["options"]["userVerification"] == "required"

    monkeypatch.setenv("PASSKEYS_REQUIRE_USER_VERIFICATION", "false")
    login = client.post("/auth/webauthn/authenticate/options").json()
    assert login["options"]["userVerification"] == "preferred"
    response, _ = _authenticate(user, Authenticator(), user_verified=False)
    assert response.status_code == 200, response.text
