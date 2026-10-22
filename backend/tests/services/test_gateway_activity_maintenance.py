"""Compaction and body retention of stored gateway call rows."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from preloop.config import settings
from preloop.models import models
from preloop.services import gateway_activity_maintenance as maintenance
from preloop.services.gateway_activity_body import COMPACTION_KEY, payload_bytes

from tests.services.test_gateway_activity_body import _call_payload


def _session(db_session, account_id, *, held: bool = False):
    stamp = datetime.now(UTC)
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type="managed_agent",
        session_source_id=f"agent-{uuid.uuid4().hex[:8]}",
        started_at=stamp,
        last_activity_at=stamp,
        legal_hold=held,
    )
    db_session.add(session)
    db_session.flush()
    return session


def _noisy(payload: dict) -> dict:
    """Make message text incompressible, as real conversation text mostly is.

    Postgres compresses stored JSONB, and a pass decides what to visit by the
    stored size, so repeated filler would hide every row from it.
    """
    for message in payload["request"]["messages"]:
        message["content"] = os.urandom(3000).hex()
    for message in payload["conversation_preview"]["messages"]:
        message["text"] = message["text"][:12] + os.urandom(3000).hex()
    return payload


def _row(db_session, session, *, turns: int, age_days: int = 0):
    row = models.RuntimeSessionActivity(
        account_id=session.account_id,
        runtime_session_id=session.id,
        activity_type="model_gateway_call",
        status="success",
        metadata_=_noisy(_call_payload(turns, text_size=600)),
        timestamp=datetime.now(UTC) - timedelta(days=age_days),
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture(autouse=True)
def fresh_cursor():
    """The compaction walk position is per process; start every test clean."""
    maintenance.reset_compaction_cursor()
    yield
    maintenance.reset_compaction_cursor()


@pytest.fixture
def cap(monkeypatch):
    monkeypatch.setattr(settings, "model_gateway_activity_max_stored_bytes", 32768)


def test_compaction_rewrites_legacy_rows_in_batches(db_session, test_user, cap):
    session = _session(db_session, test_user.account_id)
    rows = [_row(db_session, session, turns=n) for n in range(5, 10)]
    before = sum(payload_bytes(r.metadata_) for r in rows)

    first = maintenance.compact_existing_rows(db_session, batch_size=2, max_batches=1)
    assert first.compacted_rows == 2 and not first.exhausted
    rest = maintenance.compact_existing_rows(db_session, batch_size=2, max_batches=10)
    assert rest.compacted_rows == 3 and rest.exhausted

    for row in rows:
        db_session.refresh(row)
        assert COMPACTION_KEY in row.metadata_
    after = sum(payload_bytes(r.metadata_) for r in rows)
    assert after < before / 5
    # Idempotent: a finished backlog has nothing left to visit.
    again = maintenance.compact_existing_rows(db_session, batch_size=2, max_batches=5)
    assert again.compacted_rows == 0 and again.exhausted


def test_compaction_dry_run_writes_nothing(db_session, test_user, cap):
    session = _session(db_session, test_user.account_id)
    row = _row(db_session, session, turns=8)
    result = maintenance.compact_existing_rows(
        db_session, batch_size=10, max_batches=1, dry_run=True
    )
    assert result.compacted_rows == 1 and result.bytes_after < result.bytes_before
    db_session.refresh(row)
    assert COMPACTION_KEY not in row.metadata_


def test_compaction_skips_held_sessions(db_session, test_user, cap):
    held = _session(db_session, test_user.account_id, held=True)
    row = _row(db_session, held, turns=8)
    maintenance.compact_existing_rows(db_session, batch_size=10, max_batches=2)
    db_session.refresh(row)
    assert COMPACTION_KEY not in row.metadata_


def test_retention_strips_only_rows_past_the_plan_window(
    db_session, test_user, monkeypatch
):
    monkeypatch.setattr(
        "preloop.services.analytics_history.storage_history_days",
        lambda db, account: 30,
    )
    session = _session(db_session, test_user.account_id)
    old = _row(db_session, session, turns=3, age_days=45)
    recent = _row(db_session, session, turns=3, age_days=5)

    result = maintenance.strip_expired_bodies(db_session, batch_size=10, max_batches=5)
    assert result.stripped_rows == 1
    db_session.refresh(old)
    db_session.refresh(recent)
    assert "conversation_preview" not in old.metadata_
    assert old.metadata_["total_tokens"] == 120
    assert "conversation_preview" in recent.metadata_
    again = maintenance.strip_expired_bodies(db_session, batch_size=10, max_batches=5)
    assert again.stripped_rows == 0


def test_retention_skips_unlimited_history(db_session, test_user, monkeypatch):
    monkeypatch.setattr(
        "preloop.services.analytics_history.storage_history_days",
        lambda db, account: None,
    )
    session = _session(db_session, test_user.account_id)
    old = _row(db_session, session, turns=3, age_days=900)
    maintenance.strip_expired_bodies(db_session, batch_size=10, max_batches=5)
    db_session.refresh(old)
    assert "conversation_preview" in old.metadata_


def test_run_respects_disabled_halves(db_session, monkeypatch):
    monkeypatch.setattr(settings, "gateway_activity_compaction_enabled", False)
    monkeypatch.setattr(settings, "gateway_activity_body_retention_enabled", False)
    summary = maintenance.run_gateway_activity_maintenance(db_session)
    assert summary["compaction"] is None and summary["retention"] is None


def test_retention_strips_small_rows_too(db_session, test_user, monkeypatch):
    """A short old exchange is content too; no size floor applies."""
    monkeypatch.setattr(
        "preloop.services.analytics_history.storage_history_days",
        lambda db, account: 30,
    )
    session = _session(db_session, test_user.account_id)
    row = models.RuntimeSessionActivity(
        account_id=session.account_id,
        runtime_session_id=session.id,
        activity_type="model_gateway_call",
        status="success",
        metadata_={
            "total_tokens": 3,
            "request": {"messages": [{"role": "user", "content": "hi"}]},
            "response": {"output_text": "hello"},
            "conversation_preview": {"messages": [{"role": "user", "text": "hi"}]},
        },
        timestamp=datetime.now(UTC) - timedelta(days=60),
    )
    db_session.add(row)
    db_session.flush()

    result = maintenance.strip_expired_bodies(db_session, batch_size=10, max_batches=5)
    assert result.stripped_rows == 1
    db_session.refresh(row)
    assert set(row.metadata_) == {"total_tokens", COMPACTION_KEY}


def test_next_pass_resumes_after_the_last_row_walked(db_session, test_user, cap):
    session = _session(db_session, test_user.account_id)
    for n in range(4):
        _row(db_session, session, turns=5 + n)
    maintenance.compact_existing_rows(db_session, batch_size=10, max_batches=5)
    calls = []
    original = maintenance.crud_runtime_session_activity.gateway_call_batch

    def spy(db, **kwargs):
        calls.append(kwargs.get("after"))
        return original(db, **kwargs)

    monkeypatch_target = maintenance.crud_runtime_session_activity
    monkeypatch_target.gateway_call_batch = spy  # type: ignore[method-assign]
    try:
        maintenance.compact_existing_rows(db_session, batch_size=10, max_batches=5)
    finally:
        del monkeypatch_target.gateway_call_batch
    assert calls and calls[0] is not None
