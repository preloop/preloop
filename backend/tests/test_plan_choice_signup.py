"""The plan choice recorded at signup, and how long it survives.

One person answers the plan question once. Somebody who arrived from the
pricing page answered it there, so the console must never ask again; somebody
who opened the plain signup page has not answered it, so the console asks on
their first visit. The answer is a column on the user, not a query string and
not localStorage, for a reason these tests pin down: the signup path runs
through an email inbox, and a link opened in another browser (or on a phone)
has neither of those.

Nothing here touches billing. The column lives in core so that OSS keeps the
same user table as cloud, and `plan_choice_made` is reported on the profile
core already serves, so a console with no billing plugin makes no extra
request to discover it.
"""

import time
import uuid
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.models.crud import crud_user
from preloop.models.db.session import get_db_session
from preloop.utils.tokens import create_email_verification_token


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """A client on the test transaction, with the welcome work stubbed out.

    `complete_new_account_setup_background` sends email and provisions
    defaults. Neither is part of the question being asked here, and the first
    of them must never run from a test machine.
    """
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        with patch("preloop.api.auth.router.complete_new_account_setup_background"):
            yield client


def _creds(tag: str) -> dict:
    """Unique credentials for one registration."""
    suffix = f"{tag}{str(int(time.time() * 1_000_000))[-8:]}{uuid.uuid4().hex[:4]}"
    return {
        "username": f"u{suffix}",
        "email": f"u{suffix}@example.com",
        "password": "securepassword123",
        "full_name": "Plan Choice Test",
    }


def _register(client: TestClient, payload: dict):
    return client.post("/api/v1/auth/register", json=payload)


class TestRegistrationRecordsTheChoice:
    """What the signup form sends, and what the row ends up holding."""

    def test_a_plan_from_the_pricing_page_is_recorded(
        self, client: TestClient, db_session: Session
    ):
        payload = _creds("pricing")
        payload["plan_choice"] = "free"

        response = _register(client, payload)

        assert response.status_code == 201, response.text
        user = crud_user.get_by_email(db_session, email=payload["email"])
        assert user.plan_choice_made_at is not None
        # The profile says the same thing, which is what the console reads.
        assert response.json()["plan_choice_made"] is True

    def test_a_plain_signup_leaves_the_question_open(
        self, client: TestClient, db_session: Session
    ):
        payload = _creds("plain")

        response = _register(client, payload)

        assert response.status_code == 201, response.text
        user = crud_user.get_by_email(db_session, email=payload["email"])
        # Null is the only state in which the first-login plan choice is
        # shown. This is the group it exists for.
        assert user.plan_choice_made_at is None
        assert response.json()["plan_choice_made"] is False

    def test_a_mangled_plan_parameter_never_stops_a_signup(
        self, client: TestClient, db_session: Session
    ):
        # The value arrives from a link somebody may have edited, forwarded or
        # truncated. A signup is far too valuable to lose to a bad query
        # string, so the validator drops what it cannot recognise and the
        # person is simply asked to choose on first login.
        payload = _creds("mangled")
        payload["plan_choice"] = "<script>alert(1)</script>"

        response = _register(client, payload)

        assert response.status_code == 201, response.text
        user = crud_user.get_by_email(db_session, email=payload["email"])
        assert user.plan_choice_made_at is None

    def test_an_overlong_plan_parameter_is_refused_not_stored(self, client: TestClient):
        payload = _creds("long")
        payload["plan_choice"] = "a" * 200

        response = _register(client, payload)

        # max_length on the field: rubbish this size is not a mistyped plan.
        assert response.status_code == 422, response.text

    def test_the_plan_id_is_normalised_but_only_its_presence_matters(
        self, client: TestClient, db_session: Session
    ):
        payload = _creds("case")
        payload["plan_choice"] = "  Free  "

        response = _register(client, payload)

        assert response.status_code == 201, response.text
        user = crud_user.get_by_email(db_session, email=payload["email"])
        assert user.plan_choice_made_at is not None


class TestTheChoiceSurvivesVerification:
    """The round trip through the inbox, which is where a tab would be lost."""

    def test_verifying_the_address_in_another_browser_keeps_the_choice(
        self, client: TestClient, db_session: Session
    ):
        payload = _creds("verify")
        payload["plan_choice"] = "free"
        assert _register(client, payload).status_code == 201
        stamped = crud_user.get_by_email(
            db_session, email=payload["email"]
        ).plan_choice_made_at
        assert stamped is not None

        # The link from the email, followed by a client that shares nothing
        # with the tab that signed up: no cookie, no localStorage, no query
        # string. Only the row can carry the answer across this.
        verify = client.post(
            "/api/v1/auth/verify-email",
            json={
                "token": create_email_verification_token(
                    payload["email"],
                    user_id=crud_user.get_by_email(
                        db_session, email=payload["email"]
                    ).id,
                )
            },
        )
        assert verify.status_code == 200, verify.text
        token = verify.json()["access_token"]

        profile = client.get(
            "/api/v1/auth/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert profile.status_code == 200, profile.text
        assert profile.json()["plan_choice_made"] is True
        db_session.expire_all()
        user = crud_user.get_by_email(db_session, email=payload["email"])
        assert user.email_verified is True
        assert user.plan_choice_made_at == stamped

    def test_verification_does_not_answer_the_question_for_a_plain_signup(
        self, client: TestClient, db_session: Session
    ):
        payload = _creds("plainverify")
        assert _register(client, payload).status_code == 201

        verify = client.post(
            "/api/v1/auth/verify-email",
            json={
                "token": create_email_verification_token(
                    payload["email"],
                    user_id=crud_user.get_by_email(
                        db_session, email=payload["email"]
                    ).id,
                )
            },
        )
        assert verify.status_code == 200, verify.text

        profile = client.get(
            "/api/v1/auth/users/me",
            headers={"Authorization": f"Bearer {verify.json()['access_token']}"},
        )
        # Verifying an address says nothing about a plan. This person still
        # gets the choice screen, which is the whole point of the flag.
        assert profile.json()["plan_choice_made"] is False


class TestTheProfileFlag:
    """What `/auth/users/me` reports, which is the console's only gate."""

    def test_an_existing_user_with_a_stamp_reports_the_choice_as_made(
        self, client: TestClient, db_session: Session
    ):
        payload = _creds("existing")
        assert _register(client, payload).status_code == 201
        user = crud_user.get_by_email(db_session, email=payload["email"])
        assert user.plan_choice_made_at is None

        # The migration stamps every row that existed before this shipped, in
        # exactly this way. Nobody who already had an account is asked.
        from datetime import UTC, datetime

        crud_user.update(
            db_session,
            db_obj=user,
            obj_in={"plan_choice_made_at": datetime.now(UTC)},
        )

        login = client.post(
            "/api/v1/auth/token/json",
            json={"username": payload["username"], "password": payload["password"]},
        )
        assert login.status_code == 200, login.text
        profile = client.get(
            "/api/v1/auth/users/me",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        assert profile.json()["plan_choice_made"] is True
