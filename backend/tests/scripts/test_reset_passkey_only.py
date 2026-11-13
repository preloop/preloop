"""The lost-device recovery script ``scripts/reset_passkey_only.py``.

Runs ``main`` against the Postgres test transaction; nothing else is faked.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.services.authentication_policy import authorize_authentication
from scripts.reset_passkey_only import main


def _passkey_only_user(db: Session) -> models.User:
    unique = uuid.uuid4().hex[:8]
    account = crud_account.create(
        db, obj_in={"organization_name": f"Org {unique}", "is_active": True}
    )
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": f"lost{unique}",
            "email": f"lost{unique}@example.com",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "unused",
            "user_source": "local",
        },
    )
    crud_user.set_passkey_only(
        db, user=user, enabled=True, end_sessions=False, commit=True
    )
    return user


def _sessions(db: Session):
    def factory() -> Generator[Session, None, None]:
        yield db

    return factory


def test_reset_clears_the_flag_and_ends_sessions(db_session: Session, capsys):
    """protects=an operator can restore password sign-in after a lost passkey,
    and every session from before the reset ends;
    fails_when=the column stays true, a pre-reset passkey session still
    refreshes, or the exit status is non-zero;
    no_claim=running inside a real API container (main() is called in-process)."""
    user = _passkey_only_user(db_session)
    claims = {
        "amr": ["passkey"],
        "auth_account_id": str(user.account_id),
        "nsg": user.non_sso_auth_generation,
    }

    status = main(
        ["--email", user.email, "--reason", "lost laptop"], _sessions(db_session)
    )

    assert status == 0
    db_session.refresh(user)
    assert user.passkey_only is False
    with pytest.raises(HTTPException) as revoked:
        authorize_authentication(db_session, user, claims=claims, purpose="refresh")
    assert revoked.value.status_code == 401
    assert "turned off" in capsys.readouterr().out


def test_reset_writes_one_audit_row_with_the_reason(db_session: Session):
    """protects=every reset is attributable in the audit trail;
    fails_when=no row, more than one row, a missing user id or a missing reason;
    no_claim=none beyond protects."""
    user = _passkey_only_user(db_session)

    assert (
        main(
            ["--email", user.email, "--reason", "identity confirmed by phone"],
            _sessions(db_session),
        )
        == 0
    )

    rows = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.user_id == user.id,
            models.AuditLog.action == "auth.passkey_only.reset",
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].details["reason"] == "identity confirmed by phone"
    assert rows[0].details["enabled"] is False


def test_unknown_email_exits_non_zero(db_session: Session, capsys):
    """protects=a typo is reported, not silently treated as success;
    fails_when=an address nobody holds exits 0 or writes an audit row;
    no_claim=none beyond protects."""
    before = db_session.query(models.AuditLog).count()

    status = main(
        ["--email", f"nobody-{uuid.uuid4().hex}@example.com", "--reason", "x"],
        _sessions(db_session),
    )

    assert status == 1
    assert "No user found" in capsys.readouterr().err
    assert db_session.query(models.AuditLog).count() == before
