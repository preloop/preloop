"""Byte-compat tests: row recompute vs #137 landed event_fingerprint."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from preloop.schemas.usage_import import UsageImportEvent
from preloop.services.usage_fingerprint import fingerprint_from_usage_row
from preloop.services.usage_import import event_fingerprint


def test_fingerprint_from_usage_row_matches_landed_event_fingerprint() -> None:
    """Recompute from persisted row fields must match #137's helper."""
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
        UsageImportEvent(
            timestamp=ts,
            model="composer-1",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            cache_read_tokens=1,
            cache_creation_tokens=2,
            cost_usd=0.5,
            session_id="sess-9",
        ),
        source="cursor_csv",
        agent_principal_id="cursor-survivor",
    )
    assert got == want
    assert len(got) == 64


def test_fingerprint_none_cost_matches_landed_helper() -> None:
    """None cost must stay the literal ``None`` segment in the payload."""
    ts = datetime(2026, 7, 15, 12, 30, 0, tzinfo=UTC)
    event = UsageImportEvent(
        timestamp=ts,
        model="composer-1",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=1,
        session_id=None,
    )
    landed = event_fingerprint(
        event, source="cursor_csv", agent_principal_id="cursor-abcdef123456"
    )
    row = SimpleNamespace(
        timestamp=ts,
        model_alias="composer-1",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=1,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        estimated_cost=None,
        meta_data={"import_source": "cursor_csv"},
    )
    assert fingerprint_from_usage_row(
        row, agent_principal_id="cursor-abcdef123456"
    ) == (landed)
