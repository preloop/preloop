"""Limits that moved from per-process counters onto the shared limiter.

Passkey challenges (``auth.passkey_challenge``), verification resend and
forgot-password (``auth.email_send``), OTLP ingest
(``telemetry.otlp_ingest``) and admin test pushes
(``notifications.test_push``) keep their defaults, setting names and keys.
Each rule here: refuses with 429 ``rate_limited`` and ``Retry-After``, counts
the hit metric under its own name, keys per identity, shares one budget
across two replicas on the Postgres store, and stays up when the store fails.
OTLP's HTTP shape is covered in ``test_telemetry_otlp.py``.
"""

import uuid
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from preloop.api.auth import auth_router, email_verification, webauthn_router
from preloop.api.endpoints import notification_preferences, telemetry_otlp
from preloop.models.db.session import get_db_session
from preloop.models.models.rate_limit import RateLimitBucket
from preloop.services import rate_limit
from preloop.services.rate_limit import RateLimitExceeded, install_rate_limit_handler

NOW = 1_800_000_030.0  # 30 s into a 60 s window and into a 900 s window


@pytest.fixture(autouse=True)
def frozen_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the limiter clock mid-window so a slow run cannot roll it over."""
    monkeypatch.setattr(rate_limit, "_clock", lambda: NOW)


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    install_rate_limit_handler(app)
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.include_router(webauthn_router.router, prefix="/api/v1/auth/webauthn")
    app.dependency_overrides[get_db_session] = lambda: db_session
    yield TestClient(app)


def _ip(n: int) -> dict[str, str]:
    return {"X-Forwarded-For": f"203.0.113.{n}"}


def _assert_limited(response: Any, detail: str, retry_after: int) -> None:
    assert response.status_code == 429, response.text
    body = response.json()
    assert body == {
        "detail": detail,
        "code": "rate_limited",
        "retry_after": retry_after,
    }
    assert response.headers["Retry-After"] == str(retry_after)


def _metric_calls() -> tuple[list[tuple[str, str]], Any]:
    calls: list[tuple[str, str]] = []
    patcher = patch.object(
        rate_limit,
        "_count",
        side_effect=lambda name, _desc, rule: calls.append((name, rule.name)),
    )
    return calls, patcher


# ---------------------------------------------------------------------------
# Passkey challenges: per client address, WEBAUTHN_CHALLENGE_RATE_LIMIT / 60 s
# ---------------------------------------------------------------------------

CHALLENGE = "/api/v1/auth/webauthn/authenticate/options"
CHALLENGE_DETAIL = "Too many passkey challenge requests; try again shortly"


def test_passkey_challenge_default_is_unchanged() -> None:
    assert webauthn_router._RATE_LIMIT_MAX_CALLS == 30
    assert webauthn_router._RATE_LIMIT_WINDOW_SECONDS == 60


def test_passkey_challenge_is_limited_per_ip(client: TestClient) -> None:
    for _ in range(30):
        assert client.post(CHALLENGE, headers=_ip(1)).status_code == 200
    _assert_limited(client.post(CHALLENGE, headers=_ip(1)), CHALLENGE_DETAIL, 31)
    # Another address keeps its own budget.
    assert client.post(CHALLENGE, headers=_ip(2)).status_code == 200


def test_passkey_challenge_hit_increments_metric(client: TestClient) -> None:
    calls, patcher = _metric_calls()
    with patch.object(webauthn_router, "_RATE_LIMIT_MAX_CALLS", 1), patcher:
        client.post(CHALLENGE, headers=_ip(3))
        client.post(CHALLENGE, headers=_ip(3))
    assert calls == [("preloop_rate_limit_hits_total", "auth.passkey_challenge")]


# ---------------------------------------------------------------------------
# Verification resend and forgot-password: per client address and per target
# address, one shared budget, EMAIL_VERIFICATION_RESEND_LIMIT / _WINDOW
# ---------------------------------------------------------------------------

RESEND = "/api/v1/auth/resend-verification"
FORGOT = "/api/v1/auth/forgot-password"
RESEND_DETAIL = email_verification.RESEND_RATE_LIMITED_MESSAGE


def _nobody() -> str:
    return f"nobody-{uuid.uuid4().hex[:8]}@example.com"


def test_email_send_default_is_unchanged() -> None:
    assert email_verification.resend_max_per_window() == 3
    assert email_verification.resend_window_seconds() == 900


def test_resend_is_limited_per_address(client: TestClient) -> None:
    email = _nobody()
    for n in range(3):
        response = client.post(RESEND, json={"email": email}, headers=_ip(10 + n))
        assert response.status_code == 200
    # A fourth client address still cannot reach the spent address.
    _assert_limited(
        client.post(RESEND, json={"email": email}, headers=_ip(13)),
        RESEND_DETAIL,
        871,
    )


def test_forgot_password_is_limited_per_ip_and_shares_the_budget(
    client: TestClient,
) -> None:
    # Resend and forgot-password have always drawn from one budget.
    assert client.post(RESEND, json={"email": _nobody()}, headers=_ip(20)).is_success
    assert client.post(FORGOT, json={"email": _nobody()}, headers=_ip(20)).is_success
    assert client.post(FORGOT, json={"email": _nobody()}, headers=_ip(20)).is_success
    _assert_limited(
        client.post(FORGOT, json={"email": _nobody()}, headers=_ip(20)),
        RESEND_DETAIL,
        871,
    )
    assert client.post(FORGOT, json={"email": _nobody()}, headers=_ip(21)).is_success


def test_email_send_hit_increments_metric() -> None:
    calls, patcher = _metric_calls()
    with patcher:
        for _ in range(3):
            email_verification.check_resend_rate_limit("198.51.100.1")
        with pytest.raises(RateLimitExceeded):
            email_verification.check_resend_rate_limit("198.51.100.1")
    assert calls == [("preloop_rate_limit_hits_total", "auth.email_send")]


# ---------------------------------------------------------------------------
# Admin test push: per user, 5 / 60 s
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_push_is_limited_per_user() -> None:
    for _ in range(5):
        await notification_preferences._enforce_test_push_rate_limit("admin-a")
    with pytest.raises(RateLimitExceeded) as refused:
        await notification_preferences._enforce_test_push_rate_limit("admin-a")
    assert refused.value.status_code == 429
    assert refused.value.headers == {"Retry-After": "31"}
    assert refused.value.body() == {
        "detail": "Rate limit exceeded: 5 test notifications per minute.",
        "code": "rate_limited",
        "retry_after": 31,
    }
    await notification_preferences._enforce_test_push_rate_limit("admin-b")


@pytest.mark.asyncio
async def test_test_push_hit_increments_metric() -> None:
    calls, patcher = _metric_calls()
    with patcher:
        for _ in range(5):
            await notification_preferences._enforce_test_push_rate_limit("admin-m")
        with pytest.raises(RateLimitExceeded):
            await notification_preferences._enforce_test_push_rate_limit("admin-m")
    assert calls == [("preloop_rate_limit_hits_total", "notifications.test_push")]


# ---------------------------------------------------------------------------
# Every migrated rule: shared budget on Postgres, fail open on store outage
# ---------------------------------------------------------------------------


def _passkey(identity: str) -> None:
    request = MagicMock()
    request.headers = {"x-forwarded-for": identity}
    with patch.object(webauthn_router, "_RATE_LIMIT_MAX_CALLS", 2):
        webauthn_router._check_challenge_rate_limit(request)


def _email(identity: str) -> None:
    with patch.object(
        email_verification.settings, "email_verification_resend_limit", 2
    ):
        email_verification.check_resend_rate_limit(identity)


def _otlp(identity: str) -> None:
    with patch.object(telemetry_otlp, "RATE_LIMIT_PER_MINUTE", 2):
        retry_after = telemetry_otlp._rate_limited(identity)
    if retry_after is not None:
        raise RateLimitExceeded(rate_limit.TELEMETRY_OTLP_INGEST, retry_after)


def _test_push(identity: str) -> None:
    import asyncio

    with patch.object(notification_preferences, "_TEST_PUSH_LIMIT_PER_MINUTE", 2):
        asyncio.run(notification_preferences._enforce_test_push_rate_limit(identity))


MIGRATED: list[tuple[str, Callable[[str], None]]] = [
    ("auth.passkey_challenge", _passkey),
    ("auth.email_send", _email),
    ("telemetry.otlp_ingest", _otlp),
    ("notifications.test_push", _test_push),
]


@pytest.fixture
def pg_factory(db_engine: Any) -> Iterator[Any]:
    """A committing session factory; rows under the test prefix are removed."""
    factory = sessionmaker(bind=db_engine)
    yield factory
    with factory() as db:
        db.execute(delete(RateLimitBucket).where(RateLimitBucket.key.like("pgtest:%")))
        db.commit()


@pytest.mark.parametrize(("rule_name", "charge"), MIGRATED)
def test_two_replicas_share_one_budget(
    rule_name: str,
    charge: Callable[[str], None],
    pg_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "postgres")
    monkeypatch.setattr(
        rate_limit, "_key", lambda rule, ident: f"pgtest:{rule.name}:{ident}"
    )
    identity = f"203.0.113.{uuid.uuid4().int % 250}-{uuid.uuid4().hex[:6]}"
    replica_a = rate_limit.PostgresStore(pg_factory)
    replica_b = rate_limit.PostgresStore(pg_factory)
    monkeypatch.setattr(rate_limit, "_postgres_store", replica_a)
    charge(identity)
    monkeypatch.setattr(rate_limit, "_postgres_store", replica_b)
    charge(identity)
    with pytest.raises(RateLimitExceeded) as refused:
        charge(identity)
    assert refused.value.rule.name == rule_name
    with pg_factory() as db:
        keys = db.query(RateLimitBucket.key).filter(
            RateLimitBucket.key == f"pgtest:{rule_name}:{identity}"
        )
        assert keys.count() == 1


@pytest.mark.parametrize(("rule_name", "charge"), MIGRATED)
def test_store_outage_stays_up_and_throttled(
    rule_name: str,
    charge: Callable[[str], None],
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
            charge("198.51.100.77")
            charge("198.51.100.77")
        assert "Rate limit store unavailable" in caplog.text
        # The in-process fallback still enforces the same budget.
        with pytest.raises(RateLimitExceeded) as refused:
            charge("198.51.100.77")
        assert refused.value.rule.name == rule_name
    finally:
        rate_limit.set_postgres_session_factory(None)
