"""Passkey-only sign-in: the switch, its gate, and every sign-in path (#2281).

These run the real application (``create_app``) against the Postgres test
database. Tokens are minted with the real JWT helpers; a passkey sign-in in
``TestPasskeySignIn`` uses a software authenticator that signs real
authenticator data (``tests/endpoints/passkey_crafting.py``). Nothing here
talks to an identity provider, an email server or NATS.
"""

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_person,
    crud_user,
    crud_webauthn_credential,
)
from preloop.services.authentication_policy import (
    authorize_authentication,
    new_authentication,
    switched_authentication,
)
from preloop.utils.tokens import (
    create_email_verification_token,
    create_password_reset_token,
)

from tests.endpoints.passkey_crafting import ORIGIN, RP_ID, Authenticator, b64url

PASSWORD = "passkey-only-password-1"
REFUSED = "password_login_disabled"


def _user(db: Session, *, passkey_only: bool = False) -> models.User:
    """An active, verified password user in its own account."""
    unique = uuid.uuid4().hex[:8]
    account = crud_account.create(
        db, obj_in={"organization_name": f"Org {unique}", "is_active": True}
    )
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": f"pk{unique}",
            "email": f"pk{unique}@example.com",
            "full_name": "Passkey Person",
            "is_active": True,
            "email_verified": True,
            "hashed_password": get_password_hash(PASSWORD),
            "user_source": "local",
        },
    )
    if passkey_only:
        crud_user.set_passkey_only(
            db, user=user, enabled=True, end_sessions=False, commit=True
        )
    return user


def _passkey(
    db: Session, user: models.User, authenticator: Authenticator | None = None
) -> models.WebAuthnCredential:
    """Store a passkey for the user's person (the real authenticator's key)."""
    authenticator = authenticator or Authenticator()
    person = crud_person.resolve_for_membership(db, membership_id=user.id)
    return crud_webauthn_credential.create(
        db,
        obj_in={
            "person_id": person.id,
            "user_id": user.id,
            "credential_id": b64url(authenticator.credential_id),
            "public_key": b64url(authenticator.cose_public_key()),
            "sign_count": 0,
            "name": "Laptop",
            "is_active": True,
        },
    )


def _context(user: models.User, amr: list[str], **overrides: Any) -> dict[str, Any]:
    context = {
        "amr": amr,
        "auth_account_id": str(user.account_id),
        "nsg": user.non_sso_auth_generation,
        "auth_nsg": user.non_sso_auth_generation,
        "auth_time": int(time.time()),
    }
    context.update(overrides)
    return context


def _access(user: models.User, amr: list[str], **overrides: Any) -> str:
    return create_access_token(
        data={"sub": str(user.id), "scopes": []},
        auth_generation=user.auth_generation,
        auth_context=_context(user, amr, **overrides),
    )


def _refresh(user: models.User, amr: list[str]) -> str:
    return create_refresh_token(
        sub=str(user.id),
        scopes=[],
        auth_generation=user.auth_generation,
        auth_context=_context(user, amr),
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _audit(db: Session, user: models.User, action: str) -> list[models.AuditLog]:
    return (
        db.query(models.AuditLog)
        .filter(models.AuditLog.user_id == user.id, models.AuditLog.action == action)
        .all()
    )


@pytest.fixture
def api(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app, bound to the test transaction, with no email delivery."""
    from preloop.api.app import create_app
    from preloop.models.db.session import get_db_session

    monkeypatch.setenv("WEBAUTHN_RP_ID", RP_ID)
    monkeypatch.setenv("WEBAUTHN_ORIGIN", ORIGIN)
    monkeypatch.delenv("PASSKEYS_REQUIRE_USER_VERIFICATION", raising=False)
    monkeypatch.setattr("preloop.api.auth.router.send_verification_email", MagicMock())
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        yield client


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Revocation events the real commit hook emits, without a NATS server."""
    from preloop.services import auth_revocation

    auth_revocation.install_revocation_notifications()
    messages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        auth_revocation,
        "publish_realtime",
        lambda subject, data: messages.append(json.loads(data)),
    )
    return messages


class TestSwitch:
    """GET/PUT /api/v1/auth/webauthn/passkey-only."""

    def test_get_reports_state_and_passkey_count(self, api, db_session):
        """protects=the console reads the real state and passkey count;
        fails_when=enabled or passkey_count disagree with the database;
        no_claim=none beyond protects."""
        user = _user(db_session)
        _passkey(db_session, user)

        response = api.get(
            "/api/v1/auth/webauthn/passkey-only",
            headers=_bearer(_access(user, ["password"])),
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"enabled": False, "passkey_count": 1}

    @pytest.mark.parametrize(
        "amr,age_seconds",
        [(["password"], 0), (["passkey"], 11 * 60), (["email_link"], 0)],
        ids=["password-session", "stale-passkey-session", "email-link-session"],
    )
    def test_change_needs_a_fresh_passkey_session(
        self, api, db_session, amr, age_seconds
    ):
        """protects=only a passkey sign-in in the last 10 minutes changes the setting;
        fails_when=a password, email-link or 11-minute-old passkey session gets
        anything but 403 passkey_sign_in_required, or the column changes;
        no_claim=none beyond protects."""
        user = _user(db_session)
        _passkey(db_session, user)
        token = _access(user, amr, auth_time=int(time.time()) - age_seconds)

        for enabled in (True, False):
            response = api.put(
                "/api/v1/auth/webauthn/passkey-only",
                json={"enabled": enabled},
                headers=_bearer(token),
            )
            assert response.status_code == 403, response.text
            assert response.json()["detail"]["code"] == "passkey_sign_in_required"
        db_session.refresh(user)
        assert user.passkey_only is False

    def test_enable_from_fresh_passkey_session(self, api, db_session, published):
        """protects=enabling sets the column, ends other non-SSO sessions (including
        live sockets via the revocation event), audits, and returns a working pair;
        fails_when=no new pair, the old password refresh token still rotates, no
        revocation event is published, or the audit row is missing or carries
        credential material;
        no_claim=NATS delivery and the socket close itself (publish is captured;
        socket teardown on that event is covered by test_auth_revocation.py)."""
        user = _user(db_session)
        cred = _passkey(db_session, user)
        old_refresh = _refresh(user, ["password"])
        published.clear()

        response = api.put(
            "/api/v1/auth/webauthn/passkey-only",
            json={"enabled": True},
            headers=_bearer(_access(user, ["passkey"])),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["enabled"] is True and body["token_type"] == "bearer"
        db_session.refresh(user)
        assert user.passkey_only is True
        assert user.non_sso_auth_generation == 1
        assert [m["user_ids"] for m in published] == [[str(user.id)]]

        fresh = decode_token(body["access_token"]).claims
        assert fresh["amr"] == ["passkey"] and fresh["nsg"] == 1
        me = api.get("/api/v1/auth/users/me", headers=_bearer(body["access_token"]))
        assert me.status_code == 200, me.text
        rotated = api.post(
            "/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
        assert rotated.status_code == 200, rotated.text

        stale = api.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert stale.status_code == 401
        assert "access_token" not in stale.json()

        (event,) = _audit(db_session, user, "auth.passkey_only.enabled")
        assert event.user_id == user.id
        assert cred.public_key not in json.dumps(event.details)
        assert cred.credential_id not in json.dumps(event.details)

    @pytest.mark.asyncio
    async def test_other_live_sessions_fail_the_socket_recheck(
        self, api, db_session, monkeypatch
    ):
        """protects=the recheck a revocation event triggers closes the other
        sessions' sockets but not the session that enabled the setting;
        fails_when=session_is_revoked returns False for an older password or
        passkey session, or True for the new pair;
        no_claim=the WebSocket transport (session_is_revoked is what it calls)."""
        from preloop.services import auth_revocation

        async def run(operation):
            return operation(db_session)

        monkeypatch.setattr(auth_revocation, "run_db_async", run)
        user = _user(db_session)
        _passkey(db_session, user)
        other_password = _access(user, ["password"])
        other_passkey = _access(user, ["passkey"])
        response = api.put(
            "/api/v1/auth/webauthn/passkey-only",
            json={"enabled": True},
            headers=_bearer(_access(user, ["passkey"])),
        )
        assert response.status_code == 200, response.text

        for token in (other_password, other_passkey):
            assert await auth_revocation.session_is_revoked(decode_token(token))
        assert not await auth_revocation.session_is_revoked(
            decode_token(response.json()["access_token"])
        )

    def test_enable_without_a_passkey_is_refused(self, api, db_session):
        """protects=nobody locks themselves out with zero passkeys;
        fails_when=enable succeeds or returns anything but 409 no_passkey_registered;
        no_claim=the passkey session is a minted token (no passkey row backs it)."""
        user = _user(db_session)

        response = api.put(
            "/api/v1/auth/webauthn/passkey-only",
            json={"enabled": True},
            headers=_bearer(_access(user, ["passkey"])),
        )

        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "no_passkey_registered"
        db_session.refresh(user)
        assert user.passkey_only is False

    def test_enable_refused_while_user_verification_is_rolled_back(
        self, api, db_session, monkeypatch
    ):
        """protects=PASSKEYS_REQUIRE_USER_VERIFICATION=false blocks new opt-ins but
        keeps existing ones enforced;
        fails_when=enable succeeds while UV is off, or an enabled user regains
        password sign-in;
        no_claim=none beyond protects."""
        monkeypatch.setenv("PASSKEYS_REQUIRE_USER_VERIFICATION", "false")
        user = _user(db_session)
        _passkey(db_session, user)
        enforced = _user(db_session, passkey_only=True)

        response = api.put(
            "/api/v1/auth/webauthn/passkey-only",
            json={"enabled": True},
            headers=_bearer(_access(user, ["passkey"])),
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "user_verification_disabled"

        login = api.post(
            "/api/v1/auth/token/json",
            json={"username": enforced.username, "password": PASSWORD},
        )
        assert login.status_code == 401
        assert login.json()["detail"]["code"] == REFUSED

    def test_disable_from_fresh_passkey_session(self, api, db_session):
        """protects=a passkey session can turn the setting off, and it is audited;
        fails_when=the column stays true, password sign-in stays refused, or no
        disabled audit row is written;
        no_claim=none beyond protects."""
        user = _user(db_session, passkey_only=True)
        _passkey(db_session, user)

        response = api.put(
            "/api/v1/auth/webauthn/passkey-only",
            json={"enabled": False},
            headers=_bearer(_access(user, ["passkey"])),
        )

        assert response.status_code == 200, response.text
        assert response.json()["enabled"] is False
        db_session.refresh(user)
        assert user.passkey_only is False
        assert len(_audit(db_session, user, "auth.passkey_only.disabled")) == 1
        login = api.post(
            "/api/v1/auth/token/json",
            json={"username": user.username, "password": PASSWORD},
        )
        assert login.status_code == 200, login.text

    def test_last_passkey_cannot_be_deleted_while_enabled(self, api, db_session):
        """protects=a passkey-only user always keeps one passkey;
        fails_when=deleting the last passkey succeeds, or deleting one of two fails;
        no_claim=concurrency (the row lock is exercised in
        test_enable_and_delete_take_the_same_lock)."""
        user = _user(db_session, passkey_only=True)
        first = _passkey(db_session, user)
        second = _passkey(db_session, user)
        headers = _bearer(_access(user, ["passkey"]))

        ok = api.delete(
            f"/api/v1/auth/webauthn/credentials/{first.id}", headers=headers
        )
        assert ok.status_code == 204, ok.text
        last = api.delete(
            f"/api/v1/auth/webauthn/credentials/{second.id}", headers=headers
        )
        assert last.status_code == 409, last.text
        assert last.json()["detail"]["code"] == "last_passkey_required"
        db_session.refresh(second)
        assert second.is_active is True


def test_enable_and_delete_take_the_same_lock(monkeypatch):
    """protects=enable and passkey delete serialize on the person and user rows;
    fails_when=either path stops locking, or they lock in different orders;
    no_claim=Postgres lock semantics (the calls are recorded, not raced)."""
    from types import SimpleNamespace

    from preloop.api.auth import webauthn_router

    calls: list[str] = []
    user = SimpleNamespace(id=uuid.uuid4(), account_id=uuid.uuid4(), passkey_only=False)

    def identity(db, member, *, lock=False):
        calls.append(f"person lock={lock}")
        return SimpleNamespace(id=uuid.uuid4())

    def lock(db, *, user_id):
        calls.append("user lock")
        return user

    monkeypatch.setattr(webauthn_router, "require_active_identity", identity)
    monkeypatch.setattr(webauthn_router.crud_user, "lock", lock)
    monkeypatch.setattr(
        webauthn_router,
        "_require_fresh_passkey_session",
        lambda request, member: {"amr": ["passkey"], "auth_time": int(time.time())},
    )
    monkeypatch.setattr(
        webauthn_router.crud_webauthn_credential,
        "list_for_person",
        lambda db, *, person_id: [],
    )
    with pytest.raises(HTTPException):
        webauthn_router.put_passkey_only(
            webauthn_router.PasskeyOnlyUpdate(enabled=True),
            MagicMock(),
            user,
            MagicMock(),
        )
    enable_calls, calls[:] = list(calls), []
    monkeypatch.setattr(
        webauthn_router.crud_webauthn_credential, "get", lambda db, *, id: None
    )
    with pytest.raises(HTTPException):
        webauthn_router.delete_credential(uuid.uuid4(), user, MagicMock())
    assert enable_calls == calls == ["person lock=True", "user lock"]


class TestSignInPaths:
    """The enforcement table, one path per test."""

    @pytest.mark.parametrize("path", ["/api/v1/auth/token/json", "/api/v1/auth/token"])
    def test_correct_password_is_refused_without_tokens(self, api, db_session, path):
        """protects=a stolen password no longer opens a passkey-only account;
        fails_when=a correct password returns tokens or a code other than
        password_login_disabled, or no login_refused audit row is written;
        no_claim=none beyond protects."""
        user = _user(db_session, passkey_only=True)
        credentials = {"username": user.username, "password": PASSWORD}
        if path.endswith("json"):
            response = api.post(path, json=credentials)
        else:
            response = api.post(path, data=credentials)

        assert response.status_code == 401, response.text
        assert response.json()["detail"]["code"] == REFUSED
        assert "access_token" not in response.json()
        (event,) = _audit(db_session, user, "auth.passkey_only.login_refused")
        assert event.details["method"] == "password"
        assert PASSWORD not in json.dumps(event.details)

    @pytest.mark.parametrize("path", ["/api/v1/auth/token/json", "/api/v1/auth/token"])
    def test_wrong_password_reveals_nothing(self, api, db_session, path):
        """protects=the setting is invisible to someone without the password;
        fails_when=a wrong password for a passkey-only user differs in status,
        body or WWW-Authenticate from one for an ordinary user;
        no_claim=timing differences."""
        responses = []
        for passkey_only in (True, False):
            user = _user(db_session, passkey_only=passkey_only)
            credentials = {"username": user.username, "password": "not-it"}
            if path.endswith("json"):
                response = api.post(path, json=credentials)
            else:
                response = api.post(path, data=credentials)
            responses.append(
                (
                    response.status_code,
                    response.json(),
                    response.headers.get("www-authenticate"),
                )
            )
        assert responses[0] == responses[1]
        assert responses[0][0] == 401

    def test_email_link_sign_in_is_refused(self, api, db_session):
        """protects=an email link does not sign a passkey-only user in;
        fails_when=verify-email returns tokens or another code;
        no_claim=email delivery (the link token is minted directly)."""
        user = _user(db_session, passkey_only=True)
        person = crud_person.resolve_for_membership(db_session, membership_id=user.id)
        token = create_email_verification_token(
            person.email, person_id=person.id, auth_generation=person.auth_generation
        )

        response = api.post("/api/v1/auth/verify-email", json={"token": token})

        assert response.status_code == 401, response.text
        assert response.json()["detail"]["code"] == REFUSED
        assert "access_token" not in response.json()

    def test_chat_exchange_is_refused(self, db_session):
        """protects=a chat-bridged action cannot act as a passkey-only user;
        fails_when=the exchange chat_assistant.py performs (amr=chat, purpose
        exchange) is authorized;
        no_claim=the chat bridge itself; this calls the same helper with the
        same claims chat_assistant.py passes before minting its token."""
        user = _user(db_session, passkey_only=True)

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

        assert refused.value.status_code == 401
        assert refused.value.detail["code"] == REFUSED

    def test_social_sign_in_is_refused_and_sso_is_not(self, db_session):
        """protects=social sign-in is refused; SSO and passkey still mint;
        fails_when=new_authentication(method="social") returns a context, or the
        oidc/saml/passkey methods are refused;
        no_claim=the social callback lives in an identity plugin outside this
        repository; it calls new_authentication(method="social"), tested here."""
        user = _user(db_session, passkey_only=True)

        with pytest.raises(HTTPException) as refused:
            new_authentication(db_session, user, method="social")
        assert refused.value.detail["code"] == REFUSED
        for method in ("passkey", "oidc", "saml"):
            assert "amr" in new_authentication(db_session, user, method=method)

    def test_password_session_cannot_switch_into_a_passkey_only_membership(
        self, db_session
    ):
        """protects=account switching is not a way around the setting;
        fails_when=a password session switches into a passkey-only membership;
        no_claim=the switch endpoint wiring (this is the helper it calls)."""
        source = _user(db_session)
        target = _user(db_session, passkey_only=True)

        with pytest.raises(HTTPException) as refused:
            switched_authentication(
                db_session,
                target,
                claims=_context(source, ["password"]),
                source_account_id=source.account_id,
            )
        assert refused.value.detail["code"] == REFUSED

    def test_password_reset_changes_hash_but_never_signs_in(self, api, db_session):
        """protects=reset still works for a passkey-only user, without a session
        and without clearing the setting;
        fails_when=reset returns tokens, leaves the old hash, or clears passkey_only;
        no_claim=email delivery (the reset token is minted directly)."""
        user = _user(db_session, passkey_only=True)
        person = crud_person.resolve_for_membership(db_session, membership_id=user.id)
        token = create_password_reset_token(
            person.email, person_id=person.id, auth_generation=person.auth_generation
        )

        response = api.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "a-brand-new-password-2"},
        )

        assert response.status_code == 200, response.text
        assert "access_token" not in response.json()
        db_session.expire_all()
        person = crud_person.resolve_for_membership(db_session, membership_id=user.id)
        assert verify_password("a-brand-new-password-2", person.hashed_password)
        assert crud_user.get(db_session, id=user.id).passkey_only is True


class TestPasskeySignIn:
    def test_passkey_sign_in_still_works(self, api, db_session):
        """protects=a passkey-only user signs in with their passkey;
        fails_when=a valid, user-verified assertion is refused or its tokens lack
        amr=passkey;
        no_claim=real authenticator hardware (a software key signs real data)."""
        user = _user(db_session, passkey_only=True)
        authenticator = Authenticator()
        _passkey(db_session, user, authenticator)
        options = api.post("/api/v1/auth/webauthn/authenticate/options").json()
        challenge_token = options["challenge_token"]
        challenge = decode_challenge(challenge_token)

        response = api.post(
            "/api/v1/auth/webauthn/authenticate/verify",
            json={
                "credential": authenticator.assert_(challenge, user_verified=True),
                "challenge_token": challenge_token,
            },
        )

        assert response.status_code == 200, response.text
        claims = decode_token(response.json()["access_token"]).claims
        assert claims["amr"] == ["passkey"]


def decode_challenge(challenge_token: str) -> bytes:
    """The raw challenge bytes signed into a passkey challenge token."""
    from preloop.api.auth.webauthn_router import _read_challenge_token

    return _read_challenge_token(challenge_token, "authenticate")["challenge_bytes"]
