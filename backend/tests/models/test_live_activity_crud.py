"""Live activity rollups, presence state and watermarks (#1889)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models.crud.live_activity import (
    PresenceUpdate,
    RollupDelta,
    crud_live_activity,
    utc_floor,
)
from preloop.models.models import ActivityRollup, PresenceState
from preloop.models.models.live_activity import LATENCY_BUCKETS

BOARD_TABLES = {
    "activity_rollup",
    "presence_state",
    "activity_settings",
    "activity_worker_watermark",
    "user",
    "managed_agent",
    "runtime_session",
    "team",
}
RAW_TABLES = {"api_usage", "runtime_session_activity", "approval_request", "event"}


@pytest.fixture
def account_id(test_user: Any) -> uuid.UUID:
    return test_user.account_id


def _minute(offset: int = 0) -> datetime:
    return utc_floor(datetime.now(UTC), 60) - timedelta(minutes=offset)


def _rollup(db: Session, account_id: Any, subject_id: str) -> ActivityRollup:
    return (
        db.query(ActivityRollup)
        .filter(
            ActivityRollup.account_id == account_id,
            ActivityRollup.subject_id == subject_id,
        )
        .one()
    )


def test_upserts_add_counts_histograms_and_prices(
    db_session: Session, account_id: uuid.UUID
) -> None:
    bucket = _minute()
    first = RollupDelta(account_id, 60, bucket, "account", str(account_id), requests=2)
    first.reply_latency_hist[3] = 1
    crud_live_activity.upsert_rollups(db_session, deltas=[first], pass_id=uuid.uuid4())
    row = _rollup(db_session, account_id, str(account_id))
    assert row.spend_usd is None  # unpriced is not $0
    second = RollupDelta(
        account_id,
        60,
        bucket,
        "account",
        str(account_id),
        requests=3,
        tool_calls=4,
        spend_usd=Decimal("0.25"),
    )
    second.reply_latency_hist[3] = 2
    second.reply_latency_hist[10] = 1
    crud_live_activity.upsert_rollups(db_session, deltas=[second], pass_id=uuid.uuid4())
    db_session.expire_all()
    row = _rollup(db_session, account_id, str(account_id))
    assert (row.requests, row.tool_calls) == (5, 4)
    assert row.spend_usd == Decimal("0.25")
    assert len(row.reply_latency_hist) == LATENCY_BUCKETS
    assert row.reply_latency_hist[3] == 3 and row.reply_latency_hist[10] == 1


def test_a_repeated_pass_does_not_double_count(
    db_session: Session, account_id: uuid.UUID
) -> None:
    pass_id = uuid.uuid4()
    delta = RollupDelta(account_id, 60, _minute(), "person", "p1", prompts=1)
    crud_live_activity.upsert_rollups(db_session, deltas=[delta], pass_id=pass_id)
    crud_live_activity.upsert_rollups(db_session, deltas=[delta], pass_id=pass_id)
    db_session.expire_all()
    assert _rollup(db_session, account_id, "p1").prompts == 1


def test_read_rollups_is_bounded_by_window_and_granularity(
    db_session: Session, account_id: uuid.UUID
) -> None:
    pass_id = uuid.uuid4()
    crud_live_activity.upsert_rollups(
        db_session,
        deltas=[
            RollupDelta(account_id, 60, _minute(0), "account", "a", requests=1),
            RollupDelta(account_id, 60, _minute(90), "account", "a", requests=1),
            RollupDelta(account_id, 3600, _minute(0), "account", "a", requests=9),
        ],
        pass_id=pass_id,
    )
    rows = crud_live_activity.read_rollups(
        db_session,
        account_id=account_id,
        granularities=[60],
        since=_minute(60),
        subject_types=["account"],
    )
    assert [(r.granularity, r.requests) for r in rows] == [(60, 1)]
    other = crud_live_activity.read_rollups(
        db_session,
        account_id=uuid.uuid4(),
        granularities=[60],
        since=None,
        subject_types=["account"],
    )
    assert other == []


def test_fold_moves_old_minutes_into_hours(
    db_session: Session, account_id: uuid.UUID
) -> None:
    hour = utc_floor(datetime.now(UTC) - timedelta(days=3), 3600)
    deltas = []
    for minute in (1, 2):
        delta = RollupDelta(
            account_id,
            60,
            hour + timedelta(minutes=minute),
            "account",
            "fold",
            requests=minute,
            spend_usd=Decimal("0.1"),
        )
        delta.approval_latency_hist[0] = 1
        deltas.append(delta)
    crud_live_activity.upsert_rollups(db_session, deltas=deltas, pass_id=uuid.uuid4())
    folded = crud_live_activity.fold_minutes_into_hours(
        db_session, older_than=datetime.now(UTC) - timedelta(hours=48)
    )
    assert folded == 2
    db_session.expire_all()
    rows = (
        db_session.query(ActivityRollup)
        .filter(ActivityRollup.subject_id == "fold")
        .all()
    )
    assert [(r.granularity, r.bucket_start, r.requests) for r in rows] == [
        (3600, hour, 3)
    ]
    assert rows[0].spend_usd == Decimal("0.2")
    assert rows[0].approval_latency_hist[0] == 2
    assert (
        crud_live_activity.fold_minutes_into_hours(
            db_session, older_than=datetime.now(UTC) - timedelta(hours=48)
        )
        == 0
    )


def test_retention_deletes_in_batches(
    db_session: Session, account_id: uuid.UUID
) -> None:
    old = utc_floor(datetime.now(UTC) - timedelta(days=500), 3600)
    crud_live_activity.upsert_rollups(
        db_session,
        deltas=[
            RollupDelta(account_id, 3600, old + timedelta(hours=i), "account", "old")
            for i in range(3)
        ],
        pass_id=uuid.uuid4(),
    )
    before = datetime.now(UTC) - timedelta(days=400)
    assert (
        crud_live_activity.delete_rollups_before(
            db_session, granularity=3600, before=before, batch=2
        )
        == 2
    )
    assert (
        crud_live_activity.delete_rollups_before(
            db_session, granularity=3600, before=before, batch=2
        )
        == 1
    )


def test_state_changes_close_the_current_row(
    db_session: Session, account_id: uuid.UUID
) -> None:
    now = datetime.now(UTC)
    _, changed = crud_live_activity.apply_state(
        db_session,
        account_id=account_id,
        update_=PresenceUpdate("person", "u1", "active", last_signal_at=now),
        now=now,
    )
    assert changed
    _, changed = crud_live_activity.apply_state(
        db_session,
        account_id=account_id,
        update_=PresenceUpdate(
            "person", "u1", "active", last_signal_at=now + timedelta(seconds=5)
        ),
        now=now + timedelta(seconds=5),
    )
    assert not changed
    row, changed = crud_live_activity.apply_state(
        db_session,
        account_id=account_id,
        update_=PresenceUpdate("person", "u1", "idle", last_signal_at=now),
        now=now + timedelta(minutes=6),
    )
    assert changed and row.since == now + timedelta(minutes=6)
    history = (
        db_session.query(PresenceState)
        .filter(PresenceState.subject_id == "u1")
        .order_by(PresenceState.since)
        .all()
    )
    assert [(h.state, h.until is None) for h in history] == [
        ("active", False),
        ("idle", True),
    ]
    current = crud_live_activity.list_current_states(
        db_session, account_id=account_id, subject_types=["person"]
    )
    assert [c.state for c in current] == ["idle"]
    assert (
        crud_live_activity.delete_closed_states_before(
            db_session, before=now + timedelta(days=91)
        )
        == 1
    )


def test_only_one_current_state_per_subject(
    db_session: Session, account_id: uuid.UUID
) -> None:
    now = datetime.now(UTC)
    for _ in range(2):
        db_session.add(
            PresenceState(
                account_id=account_id,
                subject_type="agent",
                subject_id="a1",
                state="working",
                since=now,
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_watermark_never_moves_back(db_session: Session) -> None:
    source = f"test-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    assert crud_live_activity.get_watermark(db_session, source=source) is None
    crud_live_activity.set_watermark(db_session, source=source, watermark=now)
    crud_live_activity.set_watermark(
        db_session, source=source, watermark=now - timedelta(hours=1)
    )
    assert crud_live_activity.get_watermark(db_session, source=source) == now


def test_presence_is_enabled_without_a_settings_row(
    db_session: Session, account_id: uuid.UUID, test_user: Any
) -> None:
    assert crud_live_activity.presence_enabled(db_session, account_id=account_id)
    assert crud_live_activity.board_settings(db_session, account_id=account_id)[0]
    crud_live_activity.set_presence_enabled(
        db_session, account_id=account_id, enabled=False, user_id=test_user.id
    )
    assert not crud_live_activity.presence_enabled(db_session, account_id=account_id)
    assert not crud_live_activity.board_settings(db_session, account_id=account_id)[0]


def test_worker_lock_admits_one_holder(db_engine: Any) -> None:
    with Session(db_engine) as first, Session(db_engine) as second:
        assert crud_live_activity.try_worker_lock(first)
        assert not crud_live_activity.try_worker_lock(second)
        first.rollback()
        assert crud_live_activity.try_worker_lock(second)
        second.rollback()


@pytest.fixture
def statements(db_session: Session) -> Iterator[list[str]]:
    seen: list[str] = []
    engine = db_session.get_bind().engine

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


def _tables(sql: str) -> set[str]:
    return {
        name.strip('"')
        for name in re.findall(r'\b(?:FROM|JOIN)\s+("?[a-z_]+"?)', sql, re.IGNORECASE)
    }


def test_board_readers_touch_no_raw_event_table(
    db_session: Session, account_id: uuid.UUID, statements: list[str]
) -> None:
    """The performance budget: the board never scans raw event tables."""
    some = [uuid.uuid4()]
    crud_live_activity.read_rollups(
        db_session,
        account_id=account_id,
        granularities=[60, 3600],
        since=_minute(60),
        subject_types=["account", "person", "agent"],
    )
    crud_live_activity.list_current_states(
        db_session, account_id=account_id, subject_types=["person", "agent"]
    )
    crud_live_activity.presence_enabled(db_session, account_id=account_id)
    crud_live_activity.board_settings(db_session, account_id=account_id)
    crud_live_activity.get_install_floor(db_session)
    crud_live_activity.users_brief(db_session, account_id=account_id, user_ids=some)
    crud_live_activity.agents_brief(db_session, account_id=account_id, agent_ids=some)
    crud_live_activity.sessions_brief(
        db_session, account_id=account_id, session_ids=some
    )
    crud_live_activity.account_has_agents(db_session, account_id=account_id)
    touched = set().union(*(_tables(s) for s in statements))
    assert touched, statements
    assert touched <= BOARD_TABLES, touched
    assert not touched & RAW_TABLES


def test_source_readers_are_time_bounded(
    db_session: Session, statements: list[str]
) -> None:
    now = datetime.now(UTC)
    after = now - timedelta(minutes=1)
    crud_live_activity.read_gateway_usage(db_session, after=after, until=now)
    crud_live_activity.read_session_activity(db_session, after=after, until=now)
    crud_live_activity.read_session_activity(
        db_session, after=after, until=now, activity_types=["tool_call"]
    )
    crud_live_activity.read_approvals(db_session, after=after, until=now)
    crud_live_activity.read_console_events(db_session, after=after, until=now)
    for sql in statements:
        assert "LIMIT" in sql.upper(), sql
        assert re.search(r"timestamp|requested_at|resolved_at", sql), sql


def test_all_current_states_spans_accounts(
    db_session: Session, account_id: uuid.UUID
) -> None:
    now = datetime.now(UTC)
    crud_live_activity.apply_state(
        db_session,
        account_id=account_id,
        update_=PresenceUpdate("agent", "cold-start", "working", last_signal_at=now),
        now=now,
    )
    rows = crud_live_activity.list_all_current_states(db_session)
    assert any(r.subject_id == "cold-start" and r.until is None for r in rows)


def test_hook_event_reads_use_the_partial_timestamp_index(db_session: Session) -> None:
    """The worker's cross-account hook read can be served by the index."""
    from sqlalchemy import text

    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        row[0]
        for row in db_session.execute(
            text(
                "EXPLAIN SELECT id FROM runtime_session_activity "
                "WHERE timestamp > now() - interval '1 minute' "
                "AND timestamp <= now() "
                "AND activity_type IN ('user_prompt', 'turn_end', 'session_end') "
                "ORDER BY timestamp LIMIT 5000"
            )
        )
    )
    assert "ix_rsa_account_turn_events" in plan, plan
