"""Request rate limits on public and auth-sensitive endpoints (EE #66).

Each test runs the real router against the test database with the in-process
store (``RATE_LIMIT_BACKEND=memory``, reset per test by the conftest), sends
the rule's budget, and checks the next request is refused with 429, the
stable ``rate_limited`` code and ``Retry-After``.
"""

from typing import Any, Iterator
from unittest.mock import patch
from uuid import uuid4
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import auth_router
from preloop.api.auth.jwt import get_password_hash
from preloop.models.crud import crud_account, crud_user
from preloop.api.endpoints.oauth_server import router as oauth_router
from preloop.api.endpoints.public_approval import router as approval_router
from preloop.models.db.session import get_db_session
from preloop.services import rate_limit
from preloop.services.rate_limit import install_rate_limit_handler


class _NoCloseSession:
    """Hand the shared test session to code that closes what it opens."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def close(self) -> None:
        """Keep the per-test transaction open."""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    install_rate_limit_handler(app)
    app.include_router(oauth_router)
    app.include_router(auth_router, prefix="/api/v1/auth")
    # Production mounts the public approval router without /api/v1.
    app.include_router(approval_router)
    app.dependency_overrides[get_db_session] = lambda: db_session
    with patch(
        "preloop.models.db.session.get_db_session",
        side_effect=lambda: iter([_NoCloseSession(db_session)]),
    ):
        yield TestClient(app)


@pytest.fixture(autouse=True)
def frozen_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the limiter clock mid-window.

    Fixed windows roll over on the minute; a slow runner crossing a boundary
    halfway through a test would reset the budget and the next request would
    not be refused.
    """
    monkeypatch.setattr(rate_limit, "_clock", lambda: 1_800_000_030.0)


def _ip(n: int) -> dict[str, str]:
    """A distinct public client address per ``n`` (TEST-NET-3)."""
    return {"X-Forwarded-For": f"203.0.113.{n}"}


def _assert_limited(response: Any) -> None:
    assert response.status_code == 429, response.text
    body = response.json()
    assert body["code"] == "rate_limited"
    assert isinstance(body["detail"], str)
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 3601
    assert body["retry_after"] == retry_after


def _login(client: TestClient, username: str, headers: dict[str, str]) -> Any:
    return client.post(
        "/api/v1/auth/token/json",
        json={"username": username, "password": "wrong-password"},
        headers=headers,
    )


def test_login_is_limited_per_ip(client: TestClient) -> None:
    for i in range(10):
        assert _login(client, f"user{i}@example.com", _ip(1)).status_code == 401
    _assert_limited(_login(client, "another@example.com", _ip(1)))
    # A different client is unaffected.
    assert _login(client, "another@example.com", _ip(2)).status_code == 401


def test_form_login_shares_the_ip_budget(client: TestClient) -> None:
    for i in range(10):
        response = client.post(
            "/api/v1/auth/token",
            data={"username": f"user{i}@example.com", "password": "wrong"},
            headers=_ip(3),
        )
        assert response.status_code == 401
    _assert_limited(_login(client, "x@example.com", _ip(3)))


PASSWORD = "Correct-horse-battery-1"


def _password_user(db: Session) -> Any:
    unique = uuid.uuid4().hex[:8]
    account = crud_account.create(
        db, obj_in={"organization_name": f"Org {unique}", "is_active": True}
    )
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": f"jane{unique}",
            "email": f"jane{unique}@example.com",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": get_password_hash(PASSWORD),
            "user_source": "local",
        },
    )
    db.flush()
    return user


def _login_with(client: TestClient, username: str, password: str, n: int) -> Any:
    return client.post(
        "/api/v1/auth/token/json",
        json={"username": username, "password": password},
        headers=_ip(n),
    )


def test_login_failures_are_limited_per_account_and_client(
    client: TestClient, db_session: Session
) -> None:
    user = _password_user(db_session)
    # Username and email of one person share the budget.
    for i in range(5):
        name = user.username if i % 2 else user.email.upper()
        assert _login_with(client, name, "wrong", 40).status_code == 401
    # Over budget from that client: refused before the password is checked,
    # even when it is right.
    _assert_limited(_login_with(client, user.username, PASSWORD, 40))


def test_correct_password_from_another_client_survives_a_spent_budget(
    client: TestClient, db_session: Session
) -> None:
    user = _password_user(db_session)
    for _ in range(5):
        assert _login_with(client, user.username, "wrong", 41).status_code == 401
    _assert_limited(_login_with(client, user.username, "wrong", 41))
    # The real user, elsewhere, still signs in.
    assert _login_with(client, user.username, PASSWORD, 42).status_code == 200


def test_successful_logins_do_not_spend_the_failure_budget(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_AUTH_LOGIN_IP", "100/60")
    user = _password_user(db_session)
    for _ in range(8):
        assert _login_with(client, user.username, PASSWORD, 43).status_code == 200


def test_unknown_identities_are_limited_by_normalized_identifier(
    client: TestClient,
) -> None:
    for _ in range(5):
        assert _login(client, "Victim@Example.com", _ip(44)).status_code == 401
    _assert_limited(_login(client, " victim@example.com", _ip(44)))
    assert _login(client, "other@example.com", _ip(44)).status_code == 401


def test_forged_forwarded_prefix_does_not_reset_the_ip_budget(
    client: TestClient,
) -> None:
    for i in range(10):
        headers = {"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.50"}
        assert _login(client, f"u{i}@example.com", headers).status_code == 401
    forged = {"X-Forwarded-For": "198.51.100.200, 203.0.113.50"}
    _assert_limited(_login(client, "z@example.com", forged))


def test_refresh_is_limited_per_ip(client: TestClient) -> None:
    for _ in range(60):
        response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": "junk"}, headers=_ip(5)
        )
        assert response.status_code == 401
    _assert_limited(
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": "junk"}, headers=_ip(5)
        )
    )


def test_reset_password_is_limited_per_ip(client: TestClient) -> None:
    payload = {"token": "junk", "new_password": "Sufficiently-long-1"}
    for _ in range(10):
        response = client.post(
            "/api/v1/auth/reset-password", json=payload, headers=_ip(6)
        )
        assert response.status_code != 429
    _assert_limited(
        client.post("/api/v1/auth/reset-password", json=payload, headers=_ip(6))
    )


def test_oauth_token_is_limited_per_ip(client: TestClient) -> None:
    form = {"grant_type": "authorization_code", "code": "junk"}
    for _ in range(30):
        response = client.post("/oauth/token", data=form, headers=_ip(7))
        assert response.status_code != 429
    _assert_limited(client.post("/oauth/token", data=form, headers=_ip(7)))


def test_oauth_register_is_limited_per_ip(client: TestClient) -> None:
    with patch(
        "preloop.api.endpoints.oauth_consent.get_oauth_provider", return_value=None
    ):
        for _ in range(10):
            response = client.post("/oauth/register", json={}, headers=_ip(8))
            assert response.status_code != 429
        _assert_limited(client.post("/oauth/register", json={}, headers=_ip(8)))


def test_oauth_authorize_is_limited_per_ip(client: TestClient) -> None:
    url = "/oauth/authorize?redirect_uri=http://127.0.0.1:9/cb"
    for _ in range(60):
        response = client.get(url, headers=_ip(9), follow_redirects=False)
        assert response.status_code == 302
    _assert_limited(client.get(url, headers=_ip(9), follow_redirects=False))


def test_public_approval_is_limited_per_request_and_client(
    client: TestClient,
) -> None:
    request_id = uuid4()
    url = f"/approval/{request_id}/data?token=junk"
    for _ in range(10):
        assert client.get(url, headers=_ip(100)).status_code != 429
    _assert_limited(client.get(url, headers=_ip(100)))
    # Another approver of the same request is unaffected...
    assert client.get(url, headers=_ip(101)).status_code != 429
    # ...and so is another request from the same client.
    other = f"/approval/{uuid4()}/data?token=junk"
    assert client.get(other, headers=_ip(100)).status_code != 429


def test_public_approval_decision_shares_the_request_budget(
    client: TestClient,
) -> None:
    request_id = uuid4()
    for _ in range(10):
        url = f"/approval/{request_id}/approve?token=junk"
        assert client.post(url, headers=_ip(102)).status_code != 429
    _assert_limited(
        client.get(f"/approval/{request_id}/data?token=junk", headers=_ip(102))
    )


def test_register_is_limited_per_ip(client: TestClient) -> None:
    for i in range(10):
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"new{i}@example.com",
                "username": f"new{i}",
                "password": "x",
            },
            headers=_ip(103),
        )
        assert response.status_code != 429
    _assert_limited(
        client.post(
            "/api/v1/auth/register",
            json={"email": "z@example.com", "username": "z", "password": "x"},
            headers=_ip(103),
        )
    )


def test_public_approval_decision_is_limited_per_ip(client: TestClient) -> None:
    for _ in range(30):
        url = f"/approval/{uuid4()}/approve?token=junk"
        assert client.post(url, headers=_ip(11)).status_code != 429
    url = f"/approval/{uuid4()}/approve?token=junk"
    _assert_limited(client.post(url, headers=_ip(11)))


def test_disabled_limiter_never_refuses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    for i in range(15):
        assert _login(client, f"u{i}@example.com", _ip(12)).status_code == 401


@pytest.mark.parametrize(
    ("setting", "value"),
    [("RATE_LIMIT_ENABLED", "false"), ("RATE_LIMIT_AUTH_LOGIN_USER", "0/60")],
)
@pytest.mark.parametrize("correct_password", [False, True])
def test_disabled_login_rule_skips_identity_lookup(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str,
    correct_password: bool,
) -> None:
    user = _password_user(db_session)
    monkeypatch.setenv(setting, value)
    with patch("preloop.api.auth.router._resolve_login_person_id") as resolve:
        response = _login_with(
            client, user.username, PASSWORD if correct_password else "wrong", 14
        )
    assert response.status_code == (200 if correct_password else 401)
    resolve.assert_not_called()


def test_rule_override_from_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_AUTH_LOGIN_IP", "2/60")
    assert _login(client, "a@example.com", _ip(13)).status_code == 401
    assert _login(client, "b@example.com", _ip(13)).status_code == 401
    _assert_limited(_login(client, "c@example.com", _ip(13)))


def test_store_outage_keeps_login_up_and_throttled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_session() -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setenv("RATE_LIMIT_BACKEND", "postgres")
    monkeypatch.setattr(rate_limit, "_last_store_error_log", 0.0)
    rate_limit.set_postgres_session_factory(broken_session)
    try:
        with caplog.at_level("ERROR", logger="preloop.services.rate_limit"):
            for i in range(10):
                assert _login(client, f"u{i}@example.com", _ip(14)).status_code == 401
        assert "Rate limit store unavailable" in caplog.text
        # The in-process fallback still enforces the budget.
        _assert_limited(_login(client, "z@example.com", _ip(14)))
    finally:
        rate_limit.set_postgres_session_factory(None)


def test_hit_increments_metric(client: TestClient) -> None:
    calls: list[tuple[str, str]] = []
    with patch.object(
        rate_limit,
        "_count",
        side_effect=lambda name, _desc, rule: calls.append((name, rule.name)),
    ):
        for i in range(11):
            _login(client, f"u{i}@example.com", _ip(15))
    assert ("preloop_rate_limit_hits_total", "auth.login.ip") in calls
