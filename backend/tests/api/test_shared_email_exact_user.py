"""Verification and password reset act on one exact user row.

``user.email`` is not unique: the enterprise invitation flow gives an address
that is already registered a second row, with its own password, in the
inviting account. Every test here therefore starts from two rows that share
one address in two accounts, and pins down that a link issued for one row can
never verify, or change the password of, the other.
"""

import ast
import pathlib
import uuid
from typing import Generator, List, Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import email_verification
from preloop.api.auth.jwt import get_password_hash, verify_password
from preloop.models.crud import crud_account, crud_user

OLD_PASSWORD = "shared-address-old-1"
NEW_PASSWORD = "shared-address-new-1"
BACKEND_SRC = pathlib.Path(__file__).resolve().parents[2] / "preloop"


def _row(db: Session, *, email: str, verified: bool) -> object:
    """One password user, in an account of its own, holding ``email``."""
    unique = uuid.uuid4().hex[:10]
    account = crud_account.create(
        db, obj_in={"organization_name": f"Org {unique}", "is_active": True}
    )
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": f"shared{unique}",
            "email": email,
            "full_name": "Shared Address",
            "is_active": True,
            "email_verified": verified,
            "hashed_password": get_password_hash(OLD_PASSWORD),
            "user_source": "local",
        },
    )


@pytest.fixture
def shared_pair(db_session: Session) -> Tuple[object, object]:
    """Two rows, two accounts, one address."""
    email = f"shared-{uuid.uuid4().hex[:10]}@example.com"
    first = _row(db_session, email=email, verified=False)
    second = _row(db_session, email=email, verified=False)
    assert first.account_id != second.account_id
    return first, second


@pytest.fixture(autouse=True)
def _clean_rate_limit():
    """Resend buckets are process-global; no test may inherit another's."""
    email_verification.reset_resend_rate_limit()
    yield
    email_verification.reset_resend_rate_limit()


@pytest.fixture
def anon_client(db_session: Session) -> Generator[TestClient, None, None]:
    """An anonymous client on the test transaction."""
    from preloop.api.app import create_app
    from preloop.models.db.session import get_db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        with patch("preloop.api.auth.router.complete_new_account_setup_background"):
            yield client


def _captured_tokens(mock_sender) -> List[str]:
    return [call.kwargs["token"] for call in mock_sender.call_args_list]


def _reload(db: Session, user) -> object:
    db.expire_all()
    return crud_user.get(db, id=user.id)


class TestCrudNeverPicksARow:
    """The lookup itself refuses to choose between rows."""

    def test_unscoped_lookup_on_a_shared_address_refuses_to_choose(
        self, db_session: Session, shared_pair
    ):
        from preloop.models.crud.user import AmbiguousEmailError

        first, _second = shared_pair
        with pytest.raises(AmbiguousEmailError):
            crud_user.get_by_email(db_session, email=first.email)

    def test_list_returns_every_row(self, db_session: Session, shared_pair):
        first, second = shared_pair
        rows = crud_user.list_by_email(db_session, email=first.email)
        assert {row.id for row in rows} == {first.id, second.id}

    def test_account_scope_returns_that_accounts_row(
        self, db_session: Session, shared_pair
    ):
        first, second = shared_pair
        for row in (first, second):
            found = crud_user.get_by_email(
                db_session, email=row.email, account_id=row.account_id
            )
            assert found.id == row.id

    def test_a_single_row_is_still_returned_unscoped(self, db_session: Session):
        only = _row(
            db_session, email=f"solo-{uuid.uuid4().hex[:8]}@example.com", verified=True
        )
        assert crud_user.get_by_email(db_session, email=only.email).id == only.id


class TestVerification:
    """A verification link verifies the row it was issued for, and no other."""

    def test_token_for_one_row_verifies_only_that_row(
        self, db_session: Session, anon_client: TestClient, shared_pair
    ):
        from preloop.utils.tokens import create_email_verification_token

        first, second = shared_pair
        # Mint for the second row on purpose: whichever row the database
        # returns first, a lookup by address could not reliably land on it.
        token = create_email_verification_token(second.email, user_id=second.id)

        response = anon_client.post("/api/v1/auth/verify-email", json={"token": token})

        assert response.status_code == 200, response.text
        assert _reload(db_session, second).email_verified is True
        assert _reload(db_session, first).email_verified is False

    def test_resend_sends_one_bound_link_per_unverified_row(
        self, db_session: Session, anon_client: TestClient, shared_pair
    ):
        first, second = shared_pair
        with patch("preloop.api.auth.router.send_verification_email") as sender:
            response = anon_client.post(
                "/api/v1/auth/resend-verification", json={"email": first.email}
            )
        assert response.status_code == 200, response.text
        tokens = _captured_tokens(sender)
        assert len(tokens) == 2

        # Following one link verifies exactly one row.
        response = anon_client.post(
            "/api/v1/auth/verify-email", json={"token": tokens[0]}
        )
        assert response.status_code == 200, response.text
        states = sorted(
            [
                _reload(db_session, first).email_verified,
                _reload(db_session, second).email_verified,
            ]
        )
        assert states == [False, True]

    def test_token_without_a_user_binding_is_refused(
        self, db_session: Session, anon_client: TestClient, shared_pair
    ):
        """An address-only link (the old format) names no row, so it verifies none."""
        from preloop.utils.tokens import create_token

        first, second = shared_pair
        token = create_token(first.email, "email_verification")

        response = anon_client.post("/api/v1/auth/verify-email", json={"token": token})

        assert response.status_code == 400, response.text
        assert _reload(db_session, first).email_verified is False
        assert _reload(db_session, second).email_verified is False

    def test_token_is_void_once_the_row_changes_address(
        self, db_session: Session, anon_client: TestClient, shared_pair
    ):
        """The link verifies the address it was sent to, not a later one."""
        from preloop.utils.tokens import create_email_verification_token

        first, _second = shared_pair
        token = create_email_verification_token(first.email, user_id=first.id)
        crud_user.update(
            db_session,
            db_obj=first,
            obj_in={"email": f"moved-{uuid.uuid4().hex[:8]}@example.com"},
        )

        response = anon_client.post("/api/v1/auth/verify-email", json={"token": token})

        assert response.status_code == 400, response.text
        assert _reload(db_session, first).email_verified is False


class TestPasswordReset:
    """A reset link changes the password of the row it was issued for only."""

    def test_forgot_password_sends_one_bound_link_per_row(
        self, db_session: Session, anon_client: TestClient, shared_pair
    ):
        first, second = shared_pair
        with patch("preloop.api.auth.router.send_password_reset_email") as sender:
            response = anon_client.post(
                "/api/v1/auth/forgot-password", json={"email": first.email}
            )
        assert response.status_code == 200, response.text
        # One message per row, each naming the username it resets, so the
        # person picks by following the link for the account they mean.
        assert len(sender.call_args_list) == 2
        named = {call.kwargs.get("username") for call in sender.call_args_list}
        assert named == {first.username, second.username}

    def test_reset_for_one_row_cannot_change_the_other(
        self, db_session: Session, anon_client: TestClient, shared_pair
    ):
        first, second = shared_pair
        with patch("preloop.api.auth.router.send_password_reset_email") as sender:
            anon_client.post(
                "/api/v1/auth/forgot-password", json={"email": first.email}
            )
        by_username = {
            call.kwargs.get("username"): call.kwargs["token"]
            for call in sender.call_args_list
        }
        token_for_first = by_username[first.username]

        response = anon_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token_for_first, "new_password": NEW_PASSWORD},
        )

        assert response.status_code == 200, response.text
        assert verify_password(NEW_PASSWORD, _reload(db_session, first).hashed_password)
        assert verify_password(
            OLD_PASSWORD, _reload(db_session, second).hashed_password
        )

    def test_reset_token_minted_for_the_second_row_lands_on_it(
        self, db_session: Session, anon_client: TestClient, shared_pair
    ):
        from preloop.utils.tokens import create_password_reset_token

        first, second = shared_pair
        token = create_password_reset_token(second.email, user_id=second.id)

        response = anon_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": NEW_PASSWORD},
        )

        assert response.status_code == 200, response.text
        assert verify_password(
            NEW_PASSWORD, _reload(db_session, second).hashed_password
        )
        assert verify_password(OLD_PASSWORD, _reload(db_session, first).hashed_password)

    def test_reset_token_without_a_user_binding_is_refused(
        self, db_session: Session, anon_client: TestClient, shared_pair
    ):
        from preloop.utils.tokens import create_token

        first, second = shared_pair
        token = create_token(first.email, "password_reset")

        response = anon_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": NEW_PASSWORD},
        )

        assert response.status_code == 400, response.text
        for row in (first, second):
            assert verify_password(
                OLD_PASSWORD, _reload(db_session, row).hashed_password
            )


class TestRegistrationUnchanged:
    """Registration still refuses any address that is already held."""

    def test_register_rejects_an_address_held_by_several_rows(
        self, anon_client: TestClient, shared_pair
    ):
        first, _second = shared_pair
        response = anon_client.post(
            "/api/v1/auth/register",
            json={
                "username": f"new{uuid.uuid4().hex[:10]}",
                "email": first.email,
                "password": "securepassword123",
            },
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Email already registered"


class TestNoArbitraryRowLookups:
    """No code path takes ``.first()`` of an email query without an account."""

    def test_only_the_user_crud_queries_user_rows_by_email(self):
        offenders = []
        for path in BACKEND_SRC.rglob("*.py"):
            if path.parts[-3:] == ("models", "crud", "user.py"):
                continue
            text = path.read_text(encoding="utf-8")
            if "User.email" in text and (
                "User.email ==" in text or "User.email.i" in text
            ):
                offenders.append(str(path.relative_to(BACKEND_SRC)))
        assert offenders == [], offenders

    def test_user_crud_email_lookups_never_take_first(self):
        path = BACKEND_SRC / "models" / "crud" / "user.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        email_methods = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and "email" in node.name
        ]
        assert email_methods
        for method in email_methods:
            calls = {
                sub.func.attr
                for sub in ast.walk(method)
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
            }
            assert "first" not in calls, method.name
