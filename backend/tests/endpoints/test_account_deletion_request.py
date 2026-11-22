"""Account deletion requests must be proven before the operators hear of them.

Signed in, the session names the account and only an owner (or the single
member) may ask. Signed out, a one-time link goes to the verified address and
the operators are notified only when it is followed; unknown and unverified
addresses get the same answer and nothing is sent.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator, Optional
from unittest.mock import patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user_optional
from preloop.api.endpoints import account as account_endpoints
from preloop.models.crud import crud_account, crud_role, crud_user, crud_user_role
from preloop.models.db.session import get_db_session
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.user import User
from preloop.services import rate_limit
from preloop.services.account_deletion_requests import (
    AUDIT_ACTION,
    CONFIRMED_BY_EMAIL_LINK,
    CONFIRMED_BY_SESSION,
)
from preloop.services.rate_limit import install_rate_limit_handler
from preloop.utils import email as email_utils
from preloop.utils import tokens
from preloop.utils.email import email_is_configured

URL = "/api/v1/account/deletion-request"
CONFIRM_URL = "/api/v1/account/deletion-request/confirm"
GENERIC = account_endpoints.DELETION_CONFIRMATION_SENT_MESSAGE


@pytest.fixture(autouse=True)
def fresh_limiter() -> Iterator[None]:
    """Each test starts with empty in-process limiter counters."""
    rate_limit.reset_memory_store()
    yield
    rate_limit.reset_memory_store()


@pytest.fixture
def smtp_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a mail host explicitly; never rely on the ambient env."""
    monkeypatch.setattr(email_utils, "SMTP_HOST", "smtp.example.com")
    assert email_is_configured()


@pytest.fixture
def smtp_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly no mail host."""
    monkeypatch.setattr(email_utils, "SMTP_HOST", "")
    assert not email_is_configured()


class _Session:
    """Holder for the user the optional-auth override returns."""

    user: Optional[User] = None


@pytest.fixture
def session() -> _Session:
    return _Session()


@pytest.fixture
def client(db_session: Session, session: _Session) -> Iterator[TestClient]:
    app = FastAPI()
    install_rate_limit_handler(app)
    app.include_router(account_endpoints.public_router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user_optional] = lambda: session.user
    yield TestClient(app)


@pytest.fixture
def notified() -> Iterator[list[tuple[str, str, Optional[str]]]]:
    calls: list[tuple[str, str, Optional[str]]] = []
    with patch(
        "preloop.sync.tasks.notify_admins",
        side_effect=lambda s, m, h=None: calls.append((s, m, h)),
    ):
        yield calls


@pytest.fixture
def mailed() -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    with patch.object(
        email_utils,
        "send_account_deletion_confirmation_email",
        side_effect=lambda **kw: calls.append(kw),
    ):
        yield calls


def _member(
    db: Session,
    *,
    email: str,
    username: str,
    role: Optional[str] = "owner",
    verified: bool = True,
    account_id: Any = None,
) -> User:
    if account_id is None:
        account_id = crud_account.create(
            db, obj_in={"organization_name": f"{username} org", "is_active": True}
        ).id
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": email,
            "username": username,
            "is_active": True,
            "email_verified": verified,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    if role:
        role_row = crud_role.get_by_name(db, name=role)
        crud_user_role.create(db, obj_in={"user_id": user.id, "role_id": role_row.id})
    db.refresh(user)
    user.person.email_verified_at = datetime.now(UTC) if verified else None
    db.flush()
    return user


def _audit_rows(db: Session, account_id: Any) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.account_id == account_id, AuditLog.action == AUDIT_ACTION)
        .all()
    )


# --------------------------------------------------------------------------
# Signed in
# --------------------------------------------------------------------------


def test_signed_in_owner_request_uses_session_not_body(
    client: TestClient,
    db_session: Session,
    session: _Session,
    notified: list,
) -> None:
    owner = _member(db_session, email="jane@example.com", username="jane")
    session.user = owner

    response = client.post(
        URL,
        json={
            "email": "someone-else@example.com",
            "username": "not-jane",
            "account_id": str(uuid.uuid4()),
            "org_name": "Forged Org",
            "reason": "moving on",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "received"
    assert len(notified) == 1
    subject, text, html = notified[0]
    assert "signed-in session" in subject
    assert "jane" in subject
    assert "jane@example.com" in text
    assert str(owner.account_id) in text
    assert "not-jane" not in text + subject
    assert "someone-else@example.com" not in text
    assert "Forged Org" not in text
    rows = _audit_rows(db_session, owner.account_id)
    assert len(rows) == 1
    assert rows[0].details["confirmed_by"] == CONFIRMED_BY_SESSION
    assert rows[0].user_id == owner.id


def test_signed_in_non_owner_is_refused(
    client: TestClient,
    db_session: Session,
    session: _Session,
    notified: list,
) -> None:
    owner = _member(db_session, email="owner@example.com", username="owner1")
    viewer = _member(
        db_session,
        email="viewer@example.com",
        username="viewer1",
        role="viewer",
        account_id=owner.account_id,
    )
    session.user = viewer

    response = client.post(URL, json={})

    assert response.status_code == 403
    assert notified == []
    assert _audit_rows(db_session, owner.account_id) == []


def test_single_member_without_owner_role_may_ask(
    client: TestClient,
    db_session: Session,
    session: _Session,
    notified: list,
) -> None:
    solo = _member(db_session, email="solo@example.com", username="solo", role=None)
    session.user = solo

    response = client.post(URL, json={})

    assert response.status_code == 200, response.text
    assert len(notified) == 1


def test_signed_in_requests_are_rate_limited_per_account(
    client: TestClient,
    db_session: Session,
    session: _Session,
    notified: list,
) -> None:
    owner = _member(db_session, email="jane@example.com", username="jane")
    session.user = owner
    for n in range(3):
        headers = {"X-Forwarded-For": f"203.0.113.{n + 1}"}
        assert client.post(URL, json={}, headers=headers).status_code == 200

    response = client.post(URL, json={}, headers={"X-Forwarded-For": "203.0.113.9"})

    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert len(notified) == 3


def test_reason_is_length_capped(
    client: TestClient, session: _Session, db_session: Session, notified: list
) -> None:
    session.user = _member(db_session, email="jane@example.com", username="jane")

    response = client.post(URL, json={"reason": "x" * 1001})

    assert response.status_code == 422
    assert notified == []


def test_reason_markup_is_escaped_for_chat_and_email(
    client: TestClient, session: _Session, db_session: Session, notified: list
) -> None:
    session.user = _member(db_session, email="jane@example.com", username="jane")

    client.post(URL, json={"reason": "<!channel> <b>bye</b>"})

    _, text, html = notified[0]
    assert "<!channel>" not in text
    assert "<b>" not in (html or "")


# --------------------------------------------------------------------------
# Signed out
# --------------------------------------------------------------------------


def test_signed_out_unknown_address_gets_generic_answer_and_nothing_is_sent(
    client: TestClient, smtp_on: None, notified: list, mailed: list
) -> None:
    response = client.post(
        URL,
        json={
            "email": "nobody@example.com",
            "username": "nobody",
            "account_id": str(uuid.uuid4()),
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "confirmation_sent", "message": GENERIC}
    assert notified == []
    assert mailed == []


def test_signed_out_unverified_address_gets_same_answer_and_nothing_is_sent(
    client: TestClient,
    db_session: Session,
    smtp_on: None,
    notified: list,
    mailed: list,
) -> None:
    _member(db_session, email="new@example.com", username="newbie", verified=False)

    response = client.post(URL, json={"email": "new@example.com"})

    assert response.json() == {"status": "confirmation_sent", "message": GENERIC}
    assert notified == []
    assert mailed == []


def test_signed_out_verified_address_gets_a_link_and_operators_wait(
    client: TestClient,
    db_session: Session,
    smtp_on: None,
    notified: list,
    mailed: list,
) -> None:
    _member(db_session, email="jane@example.com", username="jane")

    response = client.post(
        URL, json={"email": "Jane@Example.com", "reason": "not needed"}
    )

    assert response.json() == {"status": "confirmation_sent", "message": GENERIC}
    assert notified == []
    assert len(mailed) == 1
    assert mailed[0]["user_email"] == "jane@example.com"


def test_signed_out_without_email_service_says_sign_in(
    client: TestClient,
    db_session: Session,
    smtp_off: None,
    notified: list,
    mailed: list,
) -> None:
    _member(db_session, email="jane@example.com", username="jane")

    response = client.post(URL, json={"email": "jane@example.com"})

    assert response.status_code == 200
    assert response.json()["status"] == "sign_in_required"
    assert notified == []
    assert mailed == []


def test_signed_out_requests_are_rate_limited_per_address(
    client: TestClient, smtp_on: None, mailed: list
) -> None:
    for n in range(3):
        response = client.post(
            URL,
            json={"email": "target@example.com"},
            headers={"X-Forwarded-For": f"203.0.113.{n + 1}"},
        )
        assert response.status_code == 200

    response = client.post(
        URL,
        json={"email": "target@example.com"},
        headers={"X-Forwarded-For": "203.0.113.50"},
    )

    assert response.status_code == 429


def test_signed_out_requests_are_rate_limited_per_client_address(
    client: TestClient, smtp_on: None, mailed: list
) -> None:
    headers = {"X-Forwarded-For": "203.0.113.7"}
    for n in range(3):
        response = client.post(
            URL, json={"email": f"a{n}@example.com"}, headers=headers
        )
        assert response.status_code == 200

    response = client.post(URL, json={"email": "a9@example.com"}, headers=headers)

    assert response.status_code == 429


# --------------------------------------------------------------------------
# Confirmation link
# --------------------------------------------------------------------------


def _link_for(client: TestClient, mailed: list, email: str, **body: Any) -> str:
    client.post(URL, json={"email": email, **body})
    return mailed[-1]["token"]


def test_link_notifies_operators_once_and_audits(
    client: TestClient,
    db_session: Session,
    smtp_on: None,
    notified: list,
    mailed: list,
) -> None:
    owner = _member(db_session, email="jane@example.com", username="jane")
    token = _link_for(client, mailed, "jane@example.com", reason="not in link")
    assert "rsn" not in jwt.decode(token, options={"verify_signature": False})

    response = client.post(CONFIRM_URL, json={"token": token, "reason": "done here"})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "received"
    assert len(notified) == 1
    subject, text, _ = notified[0]
    assert "emailed" in subject
    assert "jane@example.com" in text
    assert str(owner.account_id) in text
    assert "done here" in text
    assert "not in link" not in text
    rows = _audit_rows(db_session, owner.account_id)
    assert len(rows) == 1
    assert rows[0].details["confirmed_by"] == CONFIRMED_BY_EMAIL_LINK

    again = client.post(CONFIRM_URL, json={"token": token})

    assert again.status_code == 400
    assert len(notified) == 1


def test_link_for_a_non_owner_does_not_notify(
    client: TestClient,
    db_session: Session,
    smtp_on: None,
    notified: list,
    mailed: list,
) -> None:
    owner = _member(db_session, email="owner@example.com", username="owner1")
    _member(
        db_session,
        email="member@example.com",
        username="member1",
        role="viewer",
        account_id=owner.account_id,
    )
    token = _link_for(client, mailed, "member@example.com")

    response = client.post(CONFIRM_URL, json={"token": token})

    assert response.status_code == 200
    assert response.json()["status"] == "not_owner"
    assert notified == []


def test_forged_or_expired_link_is_refused(
    client: TestClient,
    db_session: Session,
    smtp_on: None,
    notified: list,
) -> None:
    owner = _member(db_session, email="jane@example.com", username="jane")
    person = owner.person
    forged = jwt.encode(
        {
            "sub": "jane@example.com",
            "pid": str(person.id),
            "gen": person.auth_generation,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "type": tokens.ACCOUNT_DELETION_TOKEN_TYPE,
            "jti": "x",
        },
        "not-the-secret",
        algorithm="HS256",
    )
    expired = jwt.encode(
        {
            "sub": "jane@example.com",
            "pid": str(person.id),
            "gen": person.auth_generation,
            "iat": int(datetime.now(UTC).timestamp()) - 7200,
            "exp": datetime.now(UTC) - timedelta(minutes=1),
            "type": tokens.ACCOUNT_DELETION_TOKEN_TYPE,
            "jti": "y",
        },
        tokens.SECRET_KEY,
        algorithm="HS256",
    )
    reset_token = tokens.create_password_reset_token(
        "jane@example.com", person_id=person.id, auth_generation=0
    )

    for token in (forged, expired, reset_token, "garbage"):
        response = client.post(CONFIRM_URL, json={"token": token})
        assert response.status_code == 400, token
    assert notified == []


def test_link_stops_working_after_credentials_change(
    client: TestClient,
    db_session: Session,
    smtp_on: None,
    notified: list,
    mailed: list,
) -> None:
    owner = _member(db_session, email="jane@example.com", username="jane")
    token = _link_for(client, mailed, "jane@example.com")
    owner.person.auth_generation += 1
    db_session.flush()

    response = client.post(CONFIRM_URL, json={"token": token})

    assert response.status_code == 400
    assert notified == []


def test_failed_notification_leaves_the_link_usable(
    client: TestClient,
    db_session: Session,
    smtp_on: None,
    mailed: list,
) -> None:
    owner = _member(db_session, email="jane@example.com", username="jane")
    token = _link_for(client, mailed, "jane@example.com")

    with patch("preloop.sync.tasks.notify_admins", side_effect=RuntimeError("down")):
        assert client.post(CONFIRM_URL, json={"token": token}).status_code == 500
    assert _audit_rows(db_session, owner.account_id) == []

    with patch("preloop.sync.tasks.notify_admins") as notify:
        assert client.post(CONFIRM_URL, json={"token": token}).status_code == 200
    notify.assert_called_once()


def test_api_key_callers_cannot_request_deletion(
    client: TestClient,
    db_session: Session,
    session: _Session,
    notified: list,
) -> None:
    owner = _member(db_session, email="jane@example.com", username="jane")
    owner.__dict__["_auth_api_key"] = object()
    session.user = owner

    response = client.post(URL, json={})

    assert response.status_code == 403
    assert notified == []


def test_unresolved_bearer_credential_is_refused_not_signed_out(
    client: TestClient, smtp_on: None, notified: list, mailed: list
) -> None:
    response = client.post(
        URL,
        json={"email": "jane@example.com"},
        headers={"Authorization": "Bearer not-a-session"},
    )

    assert response.status_code in (401, 403)
    assert mailed == []


def test_non_owner_cannot_spend_the_owners_budget(
    client: TestClient,
    db_session: Session,
    session: _Session,
    notified: list,
) -> None:
    owner = _member(db_session, email="owner@example.com", username="owner1")
    viewer = _member(
        db_session,
        email="viewer@example.com",
        username="viewer1",
        role="viewer",
        account_id=owner.account_id,
    )
    session.user = viewer
    for n in range(5):
        headers = {"X-Forwarded-For": f"203.0.113.{n + 20}"}
        assert client.post(URL, json={}, headers=headers).status_code == 403

    session.user = owner
    response = client.post(URL, json={}, headers={"X-Forwarded-For": "203.0.113.99"})

    assert response.status_code == 200, response.text
    assert len(notified) == 1


def test_signed_in_failed_notification_leaves_no_audit_row(
    client: TestClient, db_session: Session, session: _Session
) -> None:
    owner = _member(db_session, email="jane@example.com", username="jane")
    session.user = owner

    with patch("preloop.sync.tasks.notify_admins", side_effect=RuntimeError("down")):
        assert client.post(URL, json={}).status_code == 500

    assert _audit_rows(db_session, owner.account_id) == []


def test_confirm_reason_is_length_capped(client: TestClient) -> None:
    response = client.post(CONFIRM_URL, json={"token": "t", "reason": "x" * 1001})

    assert response.status_code == 422
