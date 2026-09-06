"""Policy and PostgreSQL crash/concurrency tests for durable implementation turns."""

import os
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from collections.abc import Generator
from typing import Any
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_feedback
from preloop.services.flow_feedback import _reconcile, decide
from preloop.services.flow_feedback_provider import FeedbackState, classify_checks

NOW = datetime(2026, 9, 6)


def thread_stub(**changes: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "expires_at": NOW + timedelta(days=7),
            "policy": {},
            "turns": 0,
            "cost": 0,
            "no_progress": 0,
            "cursor": {},
            **changes,
        }
    )


def test_ready_at_budget_limit_requires_current_head_gates() -> None:
    state = FeedbackState("new", checks_passed=True, reviews_passed=True)
    assert decide(thread_stub(turns=5), state, [], now=NOW) == ("ready", None)
    pending = [SimpleNamespace(kind="review", head_sha="new")]
    assert (
        decide(thread_stub(turns=5), state, pending, now=NOW)[1]
        == "turn_budget_exhausted"
    )


def test_pending_ci_waits_and_deadline_blocks() -> None:
    state = FeedbackState("head", checks_pending=True)
    pending = [SimpleNamespace(kind="ci", head_sha="head")]
    assert decide(thread_stub(), state, pending, now=NOW)[0] == "waiting"
    thread = thread_stub(
        cursor={"ci_wait_started": (NOW - timedelta(hours=2)).isoformat()}
    )
    assert decide(thread, state, pending, now=NOW)[1] == "ci_deadline_exceeded"


@pytest.mark.parametrize(
    "outcome, pending, passed",
    [
        ("success", False, True),
        ("skipped", False, True),
        ("neutral", False, True),
        ("failure", False, False),
        ("timed_out", False, False),
        ("cancelled", False, False),
        ("action_required", False, False),
        ("in_progress", True, False),
    ],
)
def test_ci_terminal_semantics(outcome: str, pending: bool, passed: bool) -> None:
    actual = classify_checks([{"name": "tests", "conclusion": outcome}], ["tests"])
    assert actual[:2] == (pending, passed)


def test_missing_required_and_superseded_review_do_not_pass() -> None:
    assert classify_checks([], ["tests"])[2] == "required_checks_missing"
    pending = [SimpleNamespace(kind="ci", head_sha="old")]
    assert (
        decide(
            thread_stub(),
            FeedbackState("new", checks_passed=True, reviews_passed=True),
            pending,
            now=NOW,
        )[0]
        == "ready"
    )


@pytest.fixture
def database() -> Generator[Engine, None, None]:
    """An explicitly supplied disposable database, never the application DB."""
    url = os.environ.get("FLOW_FEEDBACK_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "set FLOW_FEEDBACK_TEST_DATABASE_URL to a disposable PostgreSQL database"
        )
    schema = "feedback_" + uuid.uuid4().hex
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        for name in ("account", "tracker", "ai_model"):
            conn.execute(text(f'CREATE TABLE "{name}" (id UUID PRIMARY KEY)'))
        models.Flow.__table__.create(conn)
        models.FlowExecution.__table__.create(conn)
        models.FlowThread.__table__.create(conn)
        models.FlowFeedback.__table__.create(conn)
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def create_thread(db: Session) -> models.FlowThread:
    account, tracker = uuid.uuid4(), uuid.uuid4()
    for table, key in (("account", account), ("tracker", tracker)):
        db.execute(text(f'INSERT INTO "{table}" (id) VALUES (:id)'), {"id": key})
    flow = models.Flow(
        id=uuid.uuid4(),
        account_id=account,
        name="implementation",
        agent_type="codex",
        agent_config={"feedback": {"enabled": True}},
        prompt_template="repair",
        is_enabled=True,
    )
    db.add(flow)
    db.flush()
    initial = models.FlowExecution(
        id=uuid.uuid4(),
        flow_id=flow.id,
        status="SUCCEEDED",
        cli_session={"agent_type": "codex", "session_id": str(uuid.uuid4())},
    )
    db.add(initial)
    db.commit()
    return crud_flow_feedback.register(
        db,
        values={
            "account_id": account,
            "flow_id": flow.id,
            "tracker_id": tracker,
            "repository_id": "123",
            "pr_number": "7",
            "pr_url": "https://github.com/example/repo/pull/7",
            "provider": "github",
            "branch": "implementation-7",
            "context": {"trigger": {}, "original_issue": {"title": "Seeded issue"}},
            "policy": {"debounce_seconds": 0},
            "latest_execution_id": initial.id,
            "active_execution_id": None,
            "due_at": NOW,
            "expires_at": NOW + timedelta(days=7),
        },
    )


def event(key: str, head: str = "head") -> dict[str, Any]:
    return {
        "event_key": key,
        "kind": "review",
        "head_sha": head,
        "payload": {"body": "fix test"},
    }


def test_pg_claim_reservation_duplicate_delivery_and_late_feedback(
    database: Engine,
) -> None:
    with Session(database) as first, Session(database) as second:
        thread = create_thread(first)
        crud_flow_feedback.ingest(
            first, thread_id=thread.id, events=[event("one"), event("one")], now=NOW
        )
        assert len(crud_flow_feedback.pending(first, thread.id)) == 1
        claim = crud_flow_feedback.claim_due(first, now=NOW)[0]
        assert crud_flow_feedback.claim_due(second, now=NOW) == []
        receipts = crud_flow_feedback.pending(first, thread.id)
        execution = crud_flow_feedback.reserve(
            first,
            *claim,
            event_data={"_thread_id": str(thread.id)},
            receipt_ids=[r.id for r in receipts],
            head_sha="head",
            now=NOW,
        )
        assert execution is not None
        # A crash before dispatch leaves one durable PENDING execution. The
        # already-consumed lease cannot create a second turn.
        assert (
            crud_flow_feedback.reserve(
                second, *claim, event_data={}, receipt_ids=[], head_sha="head", now=NOW
            )
            is None
        )
        assert second.get(models.FlowExecution, execution.id).status == "PENDING"
        crud_flow_feedback.ingest(
            second, thread_id=thread.id, events=[event("two")], now=NOW
        )
        assert [r.event_key for r in crud_flow_feedback.pending(second, thread.id)] == [
            "two"
        ]


def test_pg_expired_lease_and_cross_account_lookup(database: Engine) -> None:
    with Session(database) as db:
        thread = create_thread(db)
        old = crud_flow_feedback.claim_due(db, now=NOW)[0]
        new = crud_flow_feedback.claim_due(db, now=NOW + timedelta(seconds=121))[0]
        assert old != new
        assert (
            crud_flow_feedback.reserve(
                db,
                *old,
                event_data={},
                receipt_ids=[],
                head_sha="h",
                now=NOW + timedelta(seconds=121),
            )
            is None
        )
        assert (
            crud_flow_feedback.find(
                db,
                account_id=uuid.uuid4(),
                tracker_id=thread.tracker_id,
                repository_id="123",
                pr_number="7",
            )
            == []
        )


@pytest.mark.asyncio
async def test_pg_fake_provider_repairs_same_session_then_ready(
    database: Engine,
) -> None:
    with Session(database) as db:
        thread = create_thread(db)
        native_id = db.get(
            models.FlowExecution, thread.latest_execution_id
        ).cli_session["session_id"]
        provider = SimpleNamespace(
            read=AsyncMock(
                return_value=FeedbackState(
                    "head", feedback=[event("review"), {**event("ci"), "kind": "ci"}]
                )
            )
        )
        with (
            patch(
                "preloop.services.flow_feedback.FeedbackProvider.for_thread",
                AsyncMock(return_value=provider),
            ),
            patch(
                "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
                return_value=True,
            ),
            patch(
                "preloop.services.flow_execution_dispatcher.dispatch_execute",
                AsyncMock(return_value=False),
            ),
        ):
            await _reconcile(db, *crud_flow_feedback.claim_due(db, now=NOW)[0], now=NOW)
        db.refresh(thread)
        repair = db.get(models.FlowExecution, thread.active_execution_id)
        assert repair.id != thread.latest_execution_id
        assert (
            repair.trigger_event_details["_resume"]["cli_session"]["session_id"]
            == native_id
        )
        assert (
            repair.trigger_event_details["_resume"]["source_branch"]
            == "implementation-7"
        )
        assert len(repair.trigger_event_details["_feedback"]["items"]) == 2
        assert repair.trigger_event_details["payload"]["repository"]["id"] == "123"
        assert (
            repair.trigger_event_details["payload"]["issue"]["pull_request"]["html_url"]
            == thread.pr_url
        )
        repair.status = "SUCCEEDED"
        repair.cli_session = {"agent_type": "codex", "session_id": native_id}
        db.commit()
        provider.read.return_value = FeedbackState(
            "fixed", checks_passed=True, reviews_passed=True
        )
        with patch(
            "preloop.services.flow_feedback.FeedbackProvider.for_thread",
            AsyncMock(return_value=provider),
        ):
            await _reconcile(
                db,
                *crud_flow_feedback.claim_due(db, now=NOW + timedelta(seconds=31))[0],
                now=NOW + timedelta(seconds=31),
            )
        db.refresh(thread)
        assert thread.state == "ready"
        assert thread.turns == 1
        assert thread.latest_execution_id == repair.id


def test_pg_forged_execution_cannot_resume_another_issue_in_same_flow(
    database: Engine,
) -> None:
    from preloop.services.flow_feedback import resolve_native_checkpoint

    with Session(database) as db:
        thread = create_thread(db)
        forged_execution = uuid.uuid4()
        with pytest.raises(ValueError, match="binding mismatch"):
            resolve_native_checkpoint(
                db,
                account_id=thread.account_id,
                flow_id=thread.flow_id,
                execution_id=forged_execution,
                resume={
                    "thread_id": str(thread.id),
                    "execution_id": str(thread.latest_execution_id),
                },
            )
        with pytest.raises(ValueError, match="binding mismatch"):
            resolve_native_checkpoint(
                db,
                account_id=uuid.uuid4(),
                flow_id=thread.flow_id,
                execution_id=forged_execution,
                resume={
                    "thread_id": str(thread.id),
                    "execution_id": str(thread.latest_execution_id),
                },
            )


def test_pg_simultaneous_workers_claim_only_one_thread(database: Engine) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with Session(database) as db:
        create_thread(db)
    barrier = Barrier(2)

    def claim() -> list[tuple[uuid.UUID, uuid.UUID]]:
        with Session(database) as session:
            barrier.wait(timeout=10)
            return crud_flow_feedback.claim_due(session, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(claim), pool.submit(claim)
        assert sorted([len(first.result()), len(second.result())]) == [0, 1]


def test_pg_failed_reservation_transaction_does_not_ack_feedback(
    database: Engine,
) -> None:
    with Session(database) as db:
        thread = create_thread(db)
        crud_flow_feedback.ingest(
            db, thread_id=thread.id, events=[event("durable")], now=NOW
        )
        claim = crud_flow_feedback.claim_due(db, now=NOW)[0]
        receipts = crud_flow_feedback.pending(db, thread.id)
        with patch.object(
            db, "commit", side_effect=RuntimeError("simulated crash before commit")
        ):
            with pytest.raises(RuntimeError):
                crud_flow_feedback.reserve(
                    db,
                    *claim,
                    event_data={},
                    receipt_ids=[r.id for r in receipts],
                    head_sha="head",
                    now=NOW,
                )
        db.rollback()
        db.refresh(thread)
        assert thread.active_execution_id is None
        assert [r.event_key for r in crud_flow_feedback.pending(db, thread.id)] == [
            "durable"
        ]


@pytest.mark.asyncio
async def test_pg_worker_tick_recovers_discovery_and_debounces_feedback(
    database: Engine,
) -> None:
    from preloop.services.flow_feedback import run_feedback_tick

    with Session(database) as db:
        thread = create_thread(db)
        thread.policy = {"debounce_seconds": 30}
        db.commit()
        provider = SimpleNamespace(
            read=AsyncMock(
                return_value=FeedbackState(
                    "head",
                    checks_passed=True,
                    reviews_passed=True,
                    feedback=[event("review")],
                )
            )
        )
        with (
            patch(
                "preloop.services.flow_feedback.FeedbackProvider.for_thread",
                AsyncMock(return_value=provider),
            ),
            patch(
                "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
                return_value=True,
            ),
            patch(
                "preloop.services.flow_execution_dispatcher.dispatch_execute",
                AsyncMock(return_value=False),
            ) as dispatch,
        ):
            assert await run_feedback_tick(db, now=NOW) == 1
            db.refresh(thread)
            assert thread.state == "waiting"
            assert thread.stop_reason == "feedback_debounce"
            assert thread.active_execution_id is None
            dispatch.assert_not_called()
            assert await run_feedback_tick(db, now=NOW + timedelta(seconds=15)) == 0
            assert await run_feedback_tick(db, now=NOW + timedelta(seconds=31)) == 1
            db.refresh(thread)
            assert thread.active_execution_id is not None
            assert thread.turns == 1
            dispatch.assert_awaited_once()
            assert await run_feedback_tick(db, now=NOW + timedelta(seconds=62)) == 1
            assert thread.turns == 1
            dispatch.assert_awaited_once()
