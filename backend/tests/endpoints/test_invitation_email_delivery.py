"""Invite and resend report whether the invitation email went out.

Addresses are synthetic; the mail sender is mocked.
"""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_user_invitation
from preloop.models.models.user import User
from preloop.utils.email import EmailError

TARGET = "preloop.api.endpoints.invitations.send_invitation_email"
REASON = "The mail server does not offer STARTTLS; set SMTP_SECURITY to match it."


@patch(TARGET, side_effect=EmailError("raw server text", reason=REASON))
def test_create_reports_failed_email(
    _send: MagicMock, client: TestClient, test_user: User, db_session: Session
) -> None:
    """protects=admin learns mail failed; fails_when=500 or silent success."""
    response = client.post("/api/v1/invitations", json={"email": "jane@example.com"})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["email_sent"] is False
    assert data["email_error"] == REASON
    assert "token" not in data
    assert "raw server text" not in response.text


@patch(TARGET, return_value=False)
def test_create_reports_unconfigured_email(
    _send: MagicMock, client: TestClient, test_user: User, db_session: Session
) -> None:
    """protects=no SMTP_HOST is visible; fails_when=reported as sent."""
    data = client.post(
        "/api/v1/invitations", json={"email": "jane2@example.com"}
    ).json()
    assert data["email_sent"] is False
    assert "SMTP_HOST" in data["email_error"]


@patch(TARGET, return_value=True)
def test_create_reports_sent(
    _send: MagicMock, client: TestClient, test_user: User, db_session: Session
) -> None:
    """protects=the normal case; fails_when=success reported as failure."""
    data = client.post(
        "/api/v1/invitations", json={"email": "jane3@example.com"}
    ).json()
    assert data["email_sent"] is True
    assert data["email_error"] is None


@patch(TARGET, side_effect=EmailError("raw server text", reason=REASON))
def test_resend_reports_failed_email(
    _send: MagicMock, client: TestClient, test_user: User, db_session: Session
) -> None:
    """protects=resend tells the caller; fails_when=HTTP 500."""
    invitation = crud_user_invitation.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "jane4@example.com",
            "invited_by": test_user.id,
        },
    )
    db_session.commit()
    response = client.post(f"/api/v1/invitations/{invitation.id}/resend")
    assert response.status_code == 200, response.text
    assert response.json() == {"email_sent": False, "email_error": REASON}
