"""The optional REQUIRE_EMAIL_VERIFICATION gate.

Off by default, and "off" has to mean today's behaviour exactly: an
unverified password user signs in, refreshes and uses a passkey as before.
That default is what OSS runs, so it is tested first and in the same detail
as the on state.

When a deployment turns it on, one rule applies wherever a token is minted:
a local password user with an unverified address gets a 403 carrying
``code: email_not_verified``, and the login page can act on that code. OAuth
and SSO identities never reach the gate, because the provider asserted the
address; neither do users created from a completed checkout, because that
checkout attached a payment method to the address and the claim token proves
the person setting the password is the person who completed it.
"""

import uuid

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import email_verification
from preloop.api.auth.jwt import create_refresh_token, get_password_hash
from preloop.config import settings
from preloop.models.crud import crud_account, crud_user

PASSWORD = "verify-me-please-1"


def _user(
    db: Session,
    *,
    verified: bool,
    source: str = "local",
    suffix: str = "",
) -> object:
    """Create an active password user with a known password."""
    unique = suffix or uuid.uuid4().hex[:8]
    account = crud_account.create(
        db, obj_in={"organization_name": f"Org {unique}", "is_active": True}
    )
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": f"user{unique}",
            "email": f"user{unique}@example.com",
            "full_name": "Verify Me",
            "is_active": True,
            "email_verified": verified,
            "hashed_password": get_password_hash(PASSWORD),
            "user_source": source,
        },
    )


@pytest.fixture(autouse=True)
def _clean_rate_limit():
    """Resend buckets are process-global; no test may inherit another's."""
    email_verification.reset_resend_rate_limit()
    yield
    email_verification.reset_resend_rate_limit()


@pytest.fixture
def anon_client(db_session: Session) -> TestClient:
    """A client with no authenticated user: these endpoints are anonymous."""
    from preloop.api.app import create_app
    from preloop.models.db.session import get_db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        yield client


class TestDefaultOff:
    """The OSS default: nothing changes for anyone."""

    def test_setting_defaults_to_false(self):
        assert settings.require_email_verification is False

    def test_unverified_user_signs_in_by_default(
        self, db_session: Session, anon_client: TestClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "require_email_verification", False)
        user = _user(db_session, verified=False)

        response = anon_client.post(
            "/api/v1/auth/token/json",
            json={"username": user.username, "password": PASSWORD},
        )

        assert response.status_code == 200, response.text
        assert response.json()["access_token"]

    def test_unverified_user_refreshes_by_default(
        self, db_session: Session, anon_client: TestClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "require_email_verification", False)
        user = _user(db_session, verified=False)
        token = create_refresh_token(sub=str(user.id), scopes=[])

        response = anon_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": token}
        )

        assert response.status_code == 200, response.text

    def test_gate_helper_is_inert_by_default(self, db_session: Session, monkeypatch):
        monkeypatch.setattr(settings, "require_email_verification", False)
        user = _user(db_session, verified=False)
        # No exception: the helper is the only thing standing between an
        # unverified user and a token, and by default it does nothing.
        email_verification.enforce_verified_email(user)
        assert email_verification.verification_required(user) is False


class TestSettingOn:
    """Cloud: a verified address is a precondition for a session."""

    def test_login_is_refused_with_the_machine_readable_code(
        self, db_session: Session, anon_client: TestClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "require_email_verification", True)
        user = _user(db_session, verified=False)

        response = anon_client.post(
            "/api/v1/auth/token/json",
            json={"username": user.username, "password": PASSWORD},
        )

        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "email_not_verified"
        # The address is echoed so the login page can offer the resend without
        # asking for it again. It is only ever the address of the account
        # whose password was just accepted.
        assert detail["email"] == user.email
        assert "—" not in detail["message"]

    def test_form_login_is_refused_too(
        self, db_session: Session, anon_client: TestClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "require_email_verification", True)
        user = _user(db_session, verified=False)

        response = anon_client.post(
            "/api/v1/auth/token",
            data={"username": user.username, "password": PASSWORD},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "email_not_verified"

    def test_refresh_stops_rotating_for_an_unverified_user(
        self, db_session: Session, anon_client: TestClient, monkeypatch
    ):
        # A user who was signed in when the setting went on must not keep the
        # session alive forever by rotating the refresh token.
        monkeypatch.setattr(settings, "require_email_verification", True)
        user = _user(db_session, verified=False)
        token = create_refresh_token(sub=str(user.id), scopes=[])

        response = anon_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": token}
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "email_not_verified"

    def test_verified_user_signs_in_normally(
        self, db_session: Session, anon_client: TestClient, monkeypatch
    ):
        monkeypatch.setattr(settings, "require_email_verification", True)
        user = _user(db_session, verified=True)

        response = anon_client.post(
            "/api/v1/auth/token/json",
            json={"username": user.username, "password": PASSWORD},
        )

        assert response.status_code == 200, response.text

    def test_oauth_user_is_verified_by_construction(
        self, db_session: Session, monkeypatch
    ):
        # The provider asserted the address. Gating an SSO identity on our own
        # verification email would lock out a whole instance's users.
        monkeypatch.setattr(settings, "require_email_verification", True)
        user = _user(db_session, verified=False, source="oauth")

        assert email_verification.verification_required(user) is False
        email_verification.enforce_verified_email(user)

    def test_local_unverified_user_is_gated(self, db_session: Session, monkeypatch):
        monkeypatch.setattr(settings, "require_email_verification", True)
        user = _user(db_session, verified=False)

        assert email_verification.verification_required(user) is True


class TestResendVerification:
    """The one action the login page can offer a refused user."""

    def test_answer_is_the_same_for_any_address(
        self, db_session: Session, anon_client: TestClient
    ):
        # Never say whether an address has an account: this endpoint is
        # anonymous, so a different answer would make it an account oracle.
        user = _user(db_session, verified=False)

        known = anon_client.post(
            "/api/v1/auth/resend-verification", json={"email": user.email}
        )
        unknown = anon_client.post(
            "/api/v1/auth/resend-verification",
            json={"email": "nobody-here@example.com"},
        )

        assert known.status_code == 200
        assert unknown.status_code == 200
        assert known.json() == unknown.json()

    def test_mails_only_an_unverified_address(
        self, db_session: Session, anon_client: TestClient, monkeypatch
    ):
        sent: list[str] = []
        monkeypatch.setattr(
            "preloop.api.auth.router.send_verification_email",
            lambda user_email, token, username=None: sent.append(user_email),
        )
        unverified = _user(db_session, verified=False)
        verified = _user(db_session, verified=True)

        anon_client.post(
            "/api/v1/auth/resend-verification", json={"email": unverified.email}
        )
        anon_client.post(
            "/api/v1/auth/resend-verification", json={"email": verified.email}
        )

        assert sent == [unverified.email]

    def test_budget_is_finite(self, db_session: Session, anon_client: TestClient):
        # The mail sender is the resource being protected, so the limit has to
        # bite regardless of which address is asked for.
        user = _user(db_session, verified=False)
        statuses = [
            anon_client.post(
                "/api/v1/auth/resend-verification", json={"email": user.email}
            ).status_code
            for _ in range(email_verification.resend_max_per_window() + 1)
        ]

        assert statuses[:-1] == [200] * email_verification.resend_max_per_window()
        assert statuses[-1] == 429

    def test_a_refused_request_charges_nothing(self):
        # Charging a refused request would let a caller starve one bucket by
        # hammering another, and the limit would never recover.
        for _ in range(email_verification.resend_max_per_window()):
            email_verification.check_resend_rate_limit("1.2.3.4", "a@example.com")

        with pytest.raises(HTTPException) as refused:
            email_verification.check_resend_rate_limit("1.2.3.4", "b@example.com")
        assert refused.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS

        # The second address was never charged, so a different client can
        # still reach it.
        email_verification.check_resend_rate_limit("5.6.7.8", "b@example.com")


class TestVerifyLinkSignsIn:
    """Following the link is the sign-in, not a detour to the login page."""

    def test_link_returns_a_session(self, db_session: Session, anon_client: TestClient):
        from preloop.utils.tokens import create_email_verification_token

        user = _user(db_session, verified=False)
        token = create_email_verification_token(user.email, user_id=user.id)

        response = anon_client.post("/api/v1/auth/verify-email", json={"token": token})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["message"]
        assert body["access_token"]
        assert body["refresh_token"]
        db_session.refresh(user)
        assert user.email_verified is True

    def test_deactivated_user_verifies_without_a_session(
        self, db_session: Session, anon_client: TestClient
    ):
        # is_active is an account-level decision. Verifying an address must
        # not reopen a closed account.
        from preloop.utils.tokens import create_email_verification_token

        user = _user(db_session, verified=False)
        crud_user.update(db_session, db_obj=user, obj_in={"is_active": False})
        token = create_email_verification_token(user.email, user_id=user.id)

        response = anon_client.post("/api/v1/auth/verify-email", json={"token": token})

        assert response.status_code == 200, response.text
        assert "access_token" not in response.json()


class TestResendBudgetIsConfigured:
    """The two resend knobs are settings, not import-time env reads.

    A malformed rate-limit value used to raise while importing a module that
    the router imports, which means the whole server refused to start over a
    tuning typo. They now go through the same forgiving env factory as every
    other int setting.
    """

    def test_defaults_match_the_documented_budget(self):
        assert settings.email_verification_resend_limit == 3
        assert settings.email_verification_resend_window_seconds == 900

    def test_the_limiter_reads_the_setting_at_call_time(self, monkeypatch):
        monkeypatch.setattr(settings, "email_verification_resend_limit", 1)
        email_verification.reset_resend_rate_limit()

        email_verification.check_resend_rate_limit("9.9.9.9")
        with pytest.raises(HTTPException) as refused:
            email_verification.check_resend_rate_limit("9.9.9.9")

        assert refused.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS

    def test_a_malformed_env_value_falls_back_instead_of_crashing(self, monkeypatch):
        from preloop.config import Settings

        monkeypatch.setenv("EMAIL_VERIFICATION_RESEND_LIMIT", "three")
        monkeypatch.setenv("EMAIL_VERIFICATION_RESEND_WINDOW", "")

        parsed = Settings.from_env()

        assert parsed.email_verification_resend_limit == 3
        assert parsed.email_verification_resend_window_seconds == 900
