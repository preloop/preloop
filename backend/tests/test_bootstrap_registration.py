"""Tests for the bootstrap registration token (first-user claim) flow.

Covers the computed registration rule shared by /register and /features:

- zero users + valid token   -> 201 (regardless of REGISTRATION_ENABLED)
- zero users + missing/wrong token (token configured) -> 403 "Setup link
  required..."
- zero users + token env unset -> today's behavior (REGISTRATION_ENABLED
  decides)
- >=1 user -> REGISTRATION_ENABLED decides, token ignored
- two rapid first-signups -> exactly one 201 (zero-users state re-checked
  under the advisory lock)
- the Postgres advisory lock actually serializes two connections
- account + user + role commit atomically (no orphaned account on failure)
"""

import time
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.api.auth.bootstrap import reset_users_exist_cache
from preloop.config import settings
from preloop.models.crud import crud_account, crud_user
from preloop.models.crud.user import REGISTRATION_BOOTSTRAP_LOCK_KEY
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User

BOOTSTRAP_TOKEN = "testbootstraptoken0123456789abcdef"

SETUP_LINK_COPY = (
    "Setup link required. Use the link printed at the end of the install, "
    "or run create_first_user.py on the server."
)
DISABLED_COPY = (
    "Registration is disabled. Please contact an administrator for an invitation."
)


@pytest.fixture(autouse=True)
def _clear_users_exist_cache():
    """Each test starts with an empty sticky users-exist cache."""
    reset_users_exist_cache()
    yield
    reset_users_exist_cache()


@pytest.fixture
def register_client(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient wired to the test transaction, background tasks patched."""
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session

    with TestClient(app) as client:
        with patch("preloop.api.auth.router.complete_new_account_setup_background"):
            yield client


def _unique_creds(tag: str) -> dict:
    """Unique alphanumeric username + email for one registration attempt."""
    suffix = str(int(time.time() * 1_000_000))[-8:]
    username = f"boot{tag}{suffix}"
    return {
        "username": username,
        "email": f"{username}@example.com",
        "password": "securepassword123",
        "full_name": "Bootstrap Test",
    }


def _patch_zero_users():
    """Force the zero-users (unclaimed instance) state for the rule."""
    return patch.object(crud_user, "has_any_users", lambda db: False)


class TestBootstrapTokenRule:
    """Computed rule on a zero-user instance with a token configured."""

    def test_zero_users_valid_token_registers(self, register_client, monkeypatch):
        """Valid token claims the instance even with registration disabled."""
        monkeypatch.setattr(settings, "bootstrap_token", BOOTSTRAP_TOKEN)
        monkeypatch.setattr(settings, "registration_enabled", False)
        creds = _unique_creds("ok")

        with _patch_zero_users():
            response = register_client.post(
                "/api/v1/auth/register",
                json={**creds, "bootstrap_token": BOOTSTRAP_TOKEN},
            )

        assert response.status_code == 201, response.json()
        assert response.json()["username"] == creds["username"]

    def test_zero_users_missing_token_403(self, register_client, monkeypatch):
        """Missing token is refused even with registration ENABLED."""
        monkeypatch.setattr(settings, "bootstrap_token", BOOTSTRAP_TOKEN)
        monkeypatch.setattr(settings, "registration_enabled", True)

        with _patch_zero_users():
            response = register_client.post(
                "/api/v1/auth/register", json=_unique_creds("miss")
            )

        assert response.status_code == 403
        assert response.json()["detail"] == SETUP_LINK_COPY

    def test_zero_users_wrong_token_403(self, register_client, monkeypatch):
        """A wrong token is refused with the setup-link copy."""
        monkeypatch.setattr(settings, "bootstrap_token", BOOTSTRAP_TOKEN)
        monkeypatch.setattr(settings, "registration_enabled", True)

        with _patch_zero_users():
            response = register_client.post(
                "/api/v1/auth/register",
                json={**_unique_creds("bad"), "bootstrap_token": "wrongtoken"},
            )

        assert response.status_code == 403
        assert response.json()["detail"] == SETUP_LINK_COPY


class TestNoTokenRegression:
    """Token env unset: today's behavior is preserved exactly."""

    def test_zero_users_no_token_env_open_registration(
        self, register_client, monkeypatch
    ):
        """Zero users, no token configured, registration enabled -> 201."""
        monkeypatch.setattr(settings, "bootstrap_token", "")
        monkeypatch.setattr(settings, "registration_enabled", True)

        with _patch_zero_users():
            response = register_client.post(
                "/api/v1/auth/register", json=_unique_creds("reg")
            )

        assert response.status_code == 201, response.json()

    def test_zero_users_no_token_env_registration_disabled(
        self, register_client, monkeypatch
    ):
        """Zero users, no token configured, registration disabled -> 403."""
        monkeypatch.setattr(settings, "bootstrap_token", "")
        monkeypatch.setattr(settings, "registration_enabled", False)

        with _patch_zero_users():
            response = register_client.post(
                "/api/v1/auth/register", json=_unique_creds("dis")
            )

        assert response.status_code == 403
        assert response.json()["detail"] == DISABLED_COPY


class TestClaimedInstance:
    """>=1 user: REGISTRATION_ENABLED decides; the token is ignored."""

    def test_users_exist_registration_disabled_403(
        self, register_client, monkeypatch, test_user: User
    ):
        """Even a valid token cannot register on a claimed instance."""
        monkeypatch.setattr(settings, "bootstrap_token", BOOTSTRAP_TOKEN)
        monkeypatch.setattr(settings, "registration_enabled", False)

        response = register_client.post(
            "/api/v1/auth/register",
            json={**_unique_creds("claim"), "bootstrap_token": BOOTSTRAP_TOKEN},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == DISABLED_COPY

    def test_users_exist_registration_enabled_201(
        self, register_client, monkeypatch, test_user: User
    ):
        """Regression: open registration keeps working once users exist."""
        monkeypatch.setattr(settings, "bootstrap_token", "")
        monkeypatch.setattr(settings, "registration_enabled", True)

        response = register_client.post(
            "/api/v1/auth/register", json=_unique_creds("open")
        )

        assert response.status_code == 201, response.json()


class TestConcurrentFirstSignup:
    """Two rapid first-signups: exactly one may claim the instance."""

    def test_double_first_signup_single_201(self, register_client, monkeypatch):
        """The second signup re-reads the users state under the lock and is
        refused once the first has committed."""
        monkeypatch.setattr(settings, "bootstrap_token", BOOTSTRAP_TOKEN)
        monkeypatch.setattr(settings, "registration_enabled", False)

        creds_one = _unique_creds("one")
        creds_two = _unique_creds("two")
        test_usernames = {creds_one["username"], creds_two["username"]}

        def only_test_users_exist(db):
            return (
                db.query(User.id).filter(User.username.in_(test_usernames)).first()
                is not None
            )

        with patch.object(crud_user, "has_any_users", only_test_users_exist):
            first = register_client.post(
                "/api/v1/auth/register",
                json={**creds_one, "bootstrap_token": BOOTSTRAP_TOKEN},
            )
            second = register_client.post(
                "/api/v1/auth/register",
                json={**creds_two, "bootstrap_token": BOOTSTRAP_TOKEN},
            )

        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [201, 403], (first.json(), second.json())

    def test_advisory_lock_blocks_second_connection(self, db_engine):
        """pg_advisory_xact_lock on the constant key serializes two
        connections: the second blocks until the first's transaction ends."""
        conn_a = db_engine.connect()
        conn_b = db_engine.connect()
        try:
            conn_a.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": REGISTRATION_BOOTSTRAP_LOCK_KEY},
            )

            conn_b.execute(text("SET lock_timeout = '300ms'"))
            with pytest.raises(OperationalError):
                conn_b.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": REGISTRATION_BOOTSTRAP_LOCK_KEY},
                )
            conn_b.rollback()

            # Releasing the first transaction lets the second proceed.
            conn_a.rollback()
            conn_b.execute(text("SET lock_timeout = '5s'"))
            conn_b.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": REGISTRATION_BOOTSTRAP_LOCK_KEY},
            )
            conn_b.rollback()
        finally:
            conn_a.close()
            conn_b.close()


class TestAtomicFirstUserCreation:
    """Account + user + role commit together: no orphaned account rows."""

    def test_failed_role_assignment_leaves_no_orphan(
        self, register_client, db_session: Session, monkeypatch
    ):
        """A failure after account creation rolls the whole signup back."""
        monkeypatch.setattr(settings, "bootstrap_token", "")
        monkeypatch.setattr(settings, "registration_enabled", True)
        creds = _unique_creds("atomic")

        with (
            _patch_zero_users(),
            patch(
                "preloop.api.auth.router.crud_user_role.create",
                side_effect=RuntimeError("boom"),
            ),
        ):
            response = register_client.post("/api/v1/auth/register", json=creds)

        assert response.status_code == 500
        assert crud_user.get_by_username(db_session, username=creds["username"]) is None
        orphan_accounts = crud_account.get_multi(
            db_session,
            organization_name=f"{creds['username']}'s Organization",
        )
        assert orphan_accounts == []
