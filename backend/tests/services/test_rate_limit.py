"""Unit tests for the shared request rate limiter (EE #66)."""

from typing import Any, Iterator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from preloop.models.models.rate_limit import RateLimitBucket
from preloop.services import rate_limit
from preloop.services.rate_limit import (
    MemoryStore,
    PostgresStore,
    RateLimitExceeded,
    Rule,
    client_ip_for_rate_limit,
    enforce,
)

# ---------------------------------------------------------------------------
# Memory store
# ---------------------------------------------------------------------------


def test_memory_store_counts_within_a_window() -> None:
    store = MemoryStore()
    assert store.hit("k", 60, 120.0) == (1, 180.0)
    assert store.hit("k", 60, 150.0) == (2, 180.0)
    assert store.peek("k", 60, 179.0) == (2, 180.0)


def test_memory_store_rolls_over_at_the_window_end() -> None:
    store = MemoryStore()
    store.hit("k", 60, 120.0)
    store.hit("k", 60, 121.0)
    assert store.peek("k", 60, 180.0) == (0, 240.0)
    assert store.hit("k", 60, 180.0) == (1, 240.0)


def test_memory_store_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MemoryStore, "MAX_KEYS", 3)
    store = MemoryStore()
    for i in range(3):
        store.hit(f"k{i}", 60, 0.0)
    store.hit("new", 60, 61.0)
    assert set(store._buckets) == {"new"}


def test_enforce_denies_at_limit_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    rule = Rule("test.unit", 2, 60)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_000_020.0)
    enforce(rule, "a")
    enforce(rule, "a")
    with pytest.raises(RateLimitExceeded) as exc_info:
        enforce(rule, "a")
    # Window [1_000_020 - 1_000_020 % 60, +60) ends at 1_000_080.
    assert exc_info.value.retry_after == 61
    assert exc_info.value.headers == {"Retry-After": "61"}
    assert exc_info.value.body()["code"] == "rate_limited"
    enforce(rule, "b")  # other identities keep their budget


def test_enforce_skips_empty_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    rule = Rule("test.empty", 1, 60)
    for _ in range(3):
        enforce(rule, "")
        enforce(rule, None)


def test_invalid_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = Rule("test.override", 7, 30)
    monkeypatch.setenv("RATE_LIMIT_TEST_OVERRIDE", "lots")
    assert rule.effective() == (7, 30)
    monkeypatch.setenv("RATE_LIMIT_TEST_OVERRIDE", "3/10")
    assert rule.effective() == (3, 10)
    monkeypatch.setenv("RATE_LIMIT_TEST_OVERRIDE", "0/60")
    for _ in range(5):
        enforce(rule, "x")  # 0 disables the rule


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------


def _request(peer: str | None, xff: str | None = None) -> Any:
    request = MagicMock()
    request.client = MagicMock(host=peer) if peer else None
    request.headers = {"x-forwarded-for": xff} if xff else {}
    return request


@pytest.mark.parametrize(
    ("peer", "xff", "expected"),
    [
        # Direct connection from the internet.
        ("198.51.100.7", None, "198.51.100.7"),
        # Helm: client -> ingress -> console nginx -> API.
        ("10.0.0.5", "203.0.113.9, 10.0.0.4", "203.0.113.9"),
        # Forged prefix is never reached.
        ("10.0.0.5", "1.2.3.4, 203.0.113.9, 10.0.0.4", "203.0.113.9"),
        # A public peer is not a trusted proxy: its forwarded header is ignored.
        ("198.51.100.7", "203.0.113.9", "198.51.100.7"),
        # All-private network: the leftmost address.
        ("172.18.0.3", "192.168.1.20, 172.18.0.2", "192.168.1.20"),
        # Garbage entries are skipped; IPv4 with port and bracketed IPv6.
        ("10.0.0.5", "nonsense, 203.0.113.9:4431", "203.0.113.9"),
        ("::1", "[2001:db8::1]:443", "2001:db8::1"),
        (None, None, "unknown"),
    ],
)
def test_client_ip_walks_trusted_hops_from_the_right(
    peer: str | None, xff: str | None, expected: str
) -> None:
    assert client_ip_for_rate_limit(_request(peer, xff)) == expected


def test_trusted_proxies_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXIES", "10.0.0.0/8")
    # 192.168.1.20 is no longer trusted, so it is the client.
    request = _request("10.0.0.5", "203.0.113.9, 192.168.1.20")
    assert client_ip_for_rate_limit(request) == "192.168.1.20"


# ---------------------------------------------------------------------------
# Postgres store
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_factory(db_engine: Any) -> Iterator[Any]:
    """A committing session factory; rows written under the test prefix are removed."""
    factory = sessionmaker(bind=db_engine)
    yield factory
    with factory() as db:
        db.execute(delete(RateLimitBucket).where(RateLimitBucket.key.like("pgtest:%")))
        db.commit()


def test_postgres_store_shares_budget_across_replicas(pg_factory: Any) -> None:
    replica_a = PostgresStore(pg_factory)
    replica_b = PostgresStore(pg_factory)
    key = f"pgtest:{uuid4()}"
    assert replica_a.hit(key, 60, 1_000_000.0)[0] == 1
    assert replica_b.hit(key, 60, 1_000_001.0)[0] == 2
    assert replica_a.hit(key, 60, 1_000_002.0)[0] == 3
    assert replica_b.peek(key, 60, 1_000_003.0)[0] == 3


def test_postgres_store_rolls_over(pg_factory: Any) -> None:
    store = PostgresStore(pg_factory)
    key = f"pgtest:{uuid4()}"
    store.hit(key, 60, 1_000_000.0)
    store.hit(key, 60, 1_000_001.0)
    count, end = store.hit(key, 60, 1_000_100.0)
    assert count == 1
    assert end == 1_000_140.0


def test_postgres_enforce_across_two_replicas(
    pg_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "postgres")
    monkeypatch.setattr(rate_limit, "_key", lambda rule, ident: f"pgtest:{ident}")
    rule = Rule("test.pg", 2, 60)
    identity = str(uuid4())
    replica_a = PostgresStore(pg_factory)
    replica_b = PostgresStore(pg_factory)
    monkeypatch.setattr(rate_limit, "_postgres_store", replica_a)
    enforce(rule, identity)
    monkeypatch.setattr(rate_limit, "_postgres_store", replica_b)
    enforce(rule, identity)
    with pytest.raises(RateLimitExceeded):
        enforce(rule, identity)


def test_postgres_store_purges_expired_rows(pg_factory: Any) -> None:
    store = PostgresStore(pg_factory)
    old = f"pgtest:{uuid4()}"
    store.hit(old, 60, 1_000_000.0)
    store._last_purge = 0.0
    store.hit(f"pgtest:{uuid4()}", 60, 2_000_000.0)
    assert store.peek(old, 60, 1_000_000.0)[0] == 0


def test_postgres_store_error_is_wrapped() -> None:
    def broken() -> Any:
        raise RuntimeError("down")

    with pytest.raises(rate_limit.RateLimitStoreError):
        PostgresStore(broken).hit("k", 60, 0.0)
