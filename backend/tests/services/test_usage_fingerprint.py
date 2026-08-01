"""Golden tests for imported-usage fingerprint recompute (#112 / #123)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from preloop.services.usage_fingerprint import (
    event_fingerprint,
    fingerprint_from_usage_row,
)


def test_event_fingerprint_golden_stable_bytes() -> None:
    """Fingerprint payload must stay byte-stable for merge/rekey recomputes."""
    ts = datetime(2026, 7, 15, 12, 30, 0, tzinfo=UTC)
    fp = event_fingerprint(
        source="cursor_csv",
        agent_principal_id="cursor-abcdef123456",
        timestamp=ts,
        model="composer-1",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        cache_read_tokens=1,
        cache_creation_tokens=2,
        cost=1.23456789,
        session_id="sess-1",
    )
    # Cost is formatted to 6 decimal places; None stays the literal "None".
    assert fp == event_fingerprint(
        source="cursor_csv",
        agent_principal_id="cursor-abcdef123456",
        timestamp=ts,
        model="composer-1",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        cache_read_tokens=1,
        cache_creation_tokens=2,
        cost=1.23456789,
        session_id="sess-1",
    )
    assert len(fp) == 64
    none_fp = event_fingerprint(
        source="cursor_csv",
        agent_principal_id="cursor-abcdef123456",
        timestamp=ts,
        model="composer-1",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost=None,
        session_id=None,
    )
    assert none_fp != fp


def test_fingerprint_from_usage_row_matches_event_helper() -> None:
    """Recompute from persisted row fields must match event_fingerprint."""
    ts = datetime(2026, 7, 15, 12, 30, 0)
    row = SimpleNamespace(
        timestamp=ts,
        model_alias="composer-1",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        cache_read_tokens=1,
        cache_creation_tokens=2,
        estimated_cost=0.5,
        meta_data={
            "import_source": "cursor_csv",
            "source_session_id": "sess-9",
        },
    )
    got = fingerprint_from_usage_row(row, agent_principal_id="cursor-survivor")
    want = event_fingerprint(
        source="cursor_csv",
        agent_principal_id="cursor-survivor",
        timestamp=ts,
        model="composer-1",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        cache_read_tokens=1,
        cache_creation_tokens=2,
        cost=0.5,
        session_id="sess-9",
    )
    assert got == want
