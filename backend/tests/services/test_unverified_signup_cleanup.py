"""Unverified sign-ups nobody used are removed; anything used is kept.

Accounts are created through the real registration endpoint so the test fails
if registration starts writing a default row the cleanup does not know about.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import auth_router
from preloop.config import settings
from preloop.models.crud import crud_account, crud_api_key, crud_user
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.user import User
from preloop.services import rate_limit
from preloop.services import unverified_signup_cleanup as cleanup
from preloop.services.auth_audit import record_login_event
from preloop.services.rate_limit import install_rate_limit_handler
from preloop.utils import email as email_utils

OLD = timedelta(days=8)


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No mail and a fresh limiter; registration must not reach SMTP."""
    monkeypatch.setattr(email_utils, "SMTP_HOST", "")
    rate_limit.reset_memory_store()
    yield
    rate_limit.reset_memory_store()


@pytest.fixture
def operator_account(
    db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> str:
    """The operator's own account holds the batch audit rows."""
    account_id = str(test_user.account_id)
    monkeypatch.setattr(settings, "operator_account_ids", [account_id])
    return account_id


@pytest.fixture
def register(db_session: Session, test_user: User) -> Any:
    app = FastAPI()
    install_rate_limit_handler(app)
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.dependency_overrides[get_db_session] = lambda: db_session
    client = TestClient(app)
    counter = iter(range(1, 250))

    def _register(*, age: timedelta = OLD) -> User:
        name = "signup" + uuid.uuid4().hex[:10]
        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": name,
                "email": f"{name}@example.com",
                "password": "A-long-enough-pass-123",
                "full_name": "Jane Doe",
            },
            headers={"X-Forwarded-For": f"198.51.100.{next(counter)}"},
        )
        assert response.status_code == 201, response.text
        user = crud_user.get_by_username(db_session, username=name)
        account = crud_account.get(db_session, id=user.account_id)
        account.created = datetime.now(UTC) - age
        db_session.flush()
        return user

    return _register


def _exists(db: Session, account_id: Any) -> bool:
    db.expire_all()
    return db.get(Account, account_id) is not None


def _run(db: Session, mode: str = "apply", **kw: Any) -> cleanup.SignupCleanupResult:
    return cleanup.cleanup_unverified_signups(db, mode=mode, **kw)


def test_old_unused_unverified_signup_is_deleted_with_an_audit_row(
    db_session: Session, register: Any, operator_account: str
) -> None:
    user = register()

    result = _run(db_session)

    assert result.deleted >= 1
    assert not _exists(db_session, user.account_id)
    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.account_id == operator_account,
            AuditLog.action == cleanup.AUDIT_ACTION,
        )
        .all()
    )
    assert rows
    assert any(str(user.account_id) in r.details["account_ids"] for r in rows)


def test_failed_sign_ins_do_not_count_as_use(
    db_session: Session, register: Any, operator_account: str
) -> None:
    user = register()
    record_login_event(db_session, user=user, method="password", succeeded=False)
    db_session.flush()

    _run(db_session)

    assert not _exists(db_session, user.account_id)


def test_dry_run_changes_nothing(
    db_session: Session, register: Any, operator_account: str
) -> None:
    user = register()

    result = _run(db_session, mode="dry-run")

    assert result.matched >= 1
    assert result.deleted == 0
    assert _exists(db_session, user.account_id)


def test_default_mode_is_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cleanup.MODE_ENV, raising=False)
    assert cleanup.configured_mode() == "dry-run"
    monkeypatch.setenv(cleanup.MODE_ENV, "bogus")
    assert cleanup.configured_mode() == "dry-run"
    monkeypatch.delenv(cleanup.AGE_ENV, raising=False)
    assert cleanup.configured_max_age_days() == 7
    monkeypatch.setenv(cleanup.AGE_ENV, "0")
    assert cleanup.configured_max_age_days() == 1


def test_apply_without_operator_account_deletes_nothing(
    db_session: Session, register: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "operator_account_ids", [])
    user = register()

    result = _run(db_session)

    assert result.mode == "dry-run"
    assert result.refused
    assert _exists(db_session, user.account_id)


def test_young_signup_is_kept(
    db_session: Session, register: Any, operator_account: str
) -> None:
    user = register(age=timedelta(days=2))

    _run(db_session)

    assert _exists(db_session, user.account_id)


def test_verified_signup_is_kept(
    db_session: Session, register: Any, operator_account: str
) -> None:
    user = register()
    user.person.email_verified_at = datetime.now(UTC)
    db_session.flush()

    _run(db_session)

    assert _exists(db_session, user.account_id)


def test_signed_in_signup_is_kept(
    db_session: Session, register: Any, operator_account: str
) -> None:
    user = register()
    user.last_login = datetime.now(UTC)
    db_session.flush()

    _run(db_session)

    assert _exists(db_session, user.account_id)


def test_successful_sign_in_audit_keeps_the_account(
    db_session: Session, register: Any, operator_account: str
) -> None:
    user = register()
    record_login_event(db_session, user=user, method="password", succeeded=True)
    db_session.flush()

    _run(db_session)

    assert _exists(db_session, user.account_id)


def test_signup_with_an_api_key_is_kept(
    db_session: Session, register: Any, operator_account: str
) -> None:
    user = register()
    crud_api_key.create(
        db_session,
        obj_in={
            "name": "k",
            "key": "pl_" + uuid.uuid4().hex,
            "user_id": user.id,
            "account_id": user.account_id,
            "expires_at": datetime.now(UTC) + timedelta(days=1),
        },
    )
    db_session.flush()

    _run(db_session)

    assert _exists(db_session, user.account_id)


def test_signup_with_data_is_kept(
    db_session: Session, register: Any, operator_account: str
) -> None:
    from preloop.models.crud import crud_tracker

    user = register()
    crud_tracker.create(
        db_session,
        obj_in={
            "name": "t",
            "tracker_type": "github",
            "url": "https://github.com/example",
            "api_key": "x",
            "account_id": user.account_id,
            "is_active": True,
        },
    )
    db_session.flush()

    _run(db_session)

    assert _exists(db_session, user.account_id)


def test_every_operator_account_is_never_deleted(
    db_session: Session, register: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = register()
    first = register()
    second = register()
    monkeypatch.setattr(
        settings,
        "operator_account_ids",
        [str(first.account_id), str(second.account_id)],
    )

    _run(db_session)

    assert _exists(db_session, first.account_id)
    assert _exists(db_session, second.account_id)
    assert not _exists(db_session, user.account_id)


def test_account_verified_after_the_scan_is_kept(
    db_session: Session,
    register: Any,
    operator_account: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verification that lands between the scan and the delete wins."""
    user = register()
    scan = crud_account.list_unused_unverified_signups

    def scan_then_verify(db: Session, **kw: Any) -> Any:
        found = scan(db, **kw)
        if not kw.get("lock"):
            user.person.email_verified_at = datetime.now(UTC)
            db.flush()
        return found

    monkeypatch.setattr(
        crud_account, "list_unused_unverified_signups", scan_then_verify
    )

    result = _run(db_session)

    assert _exists(db_session, user.account_id)
    assert result.deleted == 0


def test_batches_are_bounded(
    db_session: Session, register: Any, operator_account: str
) -> None:
    for _ in range(3):
        register()

    result = _run(db_session, mode="dry-run", batch_size=1, max_batches=2)

    assert result.batches == 2
    assert result.complete is False


def test_hourly_hygiene_task_runs_the_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from unittest.mock import MagicMock

    from preloop.services import run_key_maintenance
    from preloop.sync import tasks

    db = MagicMock()
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([db]))
    monkeypatch.setattr(
        run_key_maintenance,
        "deactivate_ended_run_keys",
        lambda _db, dry_run: run_key_maintenance.RunKeyMaintenanceResult(
            dry_run=dry_run
        ),
    )
    monkeypatch.setattr(
        cleanup,
        "cleanup_unverified_signups",
        lambda _db: cleanup.SignupCleanupResult(mode="dry-run", matched=2),
    )

    result = asyncio.run(tasks.deactivate_ended_run_keys())

    assert result["unverified_signups"]["matched"] == 2
    assert result["mode"] == "apply"
