"""Pool sizing and timeout defaults.

The 2026-09-03 staging incident had two pool-shaped contributors: a 30 second
``pool_timeout`` that turned a short capacity spike into a half-minute hang,
and code defaults that did not match the deployed helm values. These tests pin
the defaults so a change is deliberate.
"""

from __future__ import annotations

import pytest

from preloop.models.db.session import _database_pool_kwargs


@pytest.fixture(autouse=True)
def _clean_pool_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against defaults, not whatever the developer has exported."""
    for name in (
        "DATABASE_POOL_SIZE",
        "DATABASE_MAX_OVERFLOW",
        "DATABASE_POOL_TIMEOUT",
        "DATABASE_POOL_RECYCLE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_pool_timeout_fails_fast() -> None:
    """A saturated pool must give up in seconds, not half a minute."""
    assert _database_pool_kwargs()["pool_timeout"] == 5


def test_pool_defaults_fit_a_default_postgres() -> None:
    """Two engines per process must fit a stock ``max_connections`` of 100.

    Each API process builds a sync engine, an async engine and a
    one-connection health engine, so the ceiling is
    ``(pool_size + max_overflow) * 2 + 1``. The previous defaults (20 + 40)
    put a single-process compose install at 121 connections, above the
    Postgres default of 100.
    """
    kwargs = _database_pool_kwargs()
    ceiling = (kwargs["pool_size"] + kwargs["max_overflow"]) * 2 + 1
    assert ceiling <= 100


def test_pool_settings_are_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployments tune the pool through the environment."""
    monkeypatch.setenv("DATABASE_POOL_SIZE", "12")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "24")
    monkeypatch.setenv("DATABASE_POOL_TIMEOUT", "3")

    kwargs = _database_pool_kwargs()

    assert kwargs["pool_size"] == 12
    assert kwargs["max_overflow"] == 24
    assert kwargs["pool_timeout"] == 3
