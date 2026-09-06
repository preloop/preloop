"""Policy and PostgreSQL crash/concurrency tests for durable implementation turns."""

import os
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from collections.abc import Generator
from typing import Any
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_feedback
from preloop.services.flow_feedback import _reconcile, decide, ingest_feedback
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


def _ingest_event(**payload: object) -> dict[str, Any]:
    return {
        "type": "check_run",
        "account_id": str(uuid.uuid4()),
        "tracker_id": str(uuid.uuid4()),
        "payload": {"repository": {"id": "123"}, **payload},
    }


def test_ingest_skips_when_pr_cannot_be_determined() -> None:
    db = MagicMock()
    with patch("preloop.services.flow_feedback.crud_flow_feedback.find") as find:
        assert ingest_feedback(db, _ingest_event()) is False
        find.assert_not_called()


def test_ingest_targets_the_event_pr() -> None:
    db = MagicMock()
    with patch("preloop.services.flow_feedback.crud_flow_feedback.find") as find:
        find.return_value = []
        assert ingest_feedback(db, _ingest_event(pull_request={"number": 7})) is False
        assert find.call_args.kwargs["pr_number"] == "7"


def test_register_thread_skips_non_uuid_tracker() -> None:
    from preloop.services.flow_feedback import register_thread

    flow = SimpleNamespace(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        trigger_event_source="webhook",
        agent_config={"feedback": {"enabled": True, "debounce_seconds": 0}},
    )
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        flow_id=flow.id,
        trigger_event_details={
            "source": "github",
            "payload": {"repository": {"id": "123"}},
        },
    )
    with (
        patch("preloop.services.flow_feedback.crud_flow.get", return_value=flow),
        patch("preloop.services.flow_feedback.crud_flow_feedback.register") as register,
    ):
        register_thread(
            MagicMock(),
            execution,
            "https://github.com/example/repo/pull/7",
            "feat/x",
        )
        register.assert_not_called()


@pytest.mark.asyncio
async def test_tick_continues_when_publication_registration_fails() -> None:
    from preloop.services.flow_feedback import run_feedback_tick

    publication = SimpleNamespace(
        id=uuid.uuid4(),
        result={
            "pr_url": "https://github.com/example/repo/pull/7",
            "pr_source_branch": "feat/x",
        },
    )
    db = MagicMock()
    with (
        patch("preloop.services.flow_feedback.crud_flow_feedback") as crud,
        patch(
            "preloop.services.flow_feedback.register_thread",
            side_effect=ValueError("badly formed hexadecimal UUID string"),
        ),
    ):
        crud.unregistered_publications.return_value = [publication]
        crud.claim_due.return_value = []
        assert await run_feedback_tick(db, now=NOW) == 0
        crud.rollback.assert_called_once_with(db)
        crud.claim_due.assert_called_once()


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
        for name in ("account", "tracker", "secret_reference"):
            conn.execute(text(f'CREATE TABLE "{name}" (id UUID PRIMARY KEY)'))
        models.AIModel.__table__.create(conn)
        models.Flow.__table__.create(conn)
        models.FlowExecution.__table__.create(conn)
        models.FlowThread.__table__.create(conn)
        models.FlowFeedback.__table__.create(conn)
        models.FlowArtifact.__table__.create(conn)
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
    model = models.AIModel(
        id=uuid.uuid4(),
        account_id=account,
        name="Test model",
        provider_name="openai",
        model_identifier="test-model",
        api_key="synthetic-key",
    )
    db.add(model)
    db.flush()
    flow = models.Flow(
        id=uuid.uuid4(),
        account_id=account,
        name="implementation",
        ai_model_id=model.id,
        agent_type="codex",
        agent_config={"feedback": {"enabled": True, "debounce_seconds": 0}},
        prompt_template="repair",
        is_enabled=True,
    )
    db.add(flow)
    db.flush()
    initial = models.FlowExecution(
        id=uuid.uuid4(),
        flow_id=flow.id,
        status="SUCCEEDED",
        trigger_event_details={
            "_model_routing": {
                "schema_version": 1,
                "agent_type": "codex",
                "ai_model_id": str(model.id),
            }
        },
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
        original = db.get(models.FlowExecution, thread.latest_execution_id)
        frozen_model = original.trigger_event_details["_model_routing"]["ai_model_id"]
        flow = db.get(models.Flow, thread.flow_id)
        flow.ai_model_id = None
        flow.agent_type = "opencode"
        db.commit()
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
        assert (
            repair.trigger_event_details["_model_routing"]["ai_model_id"]
            == frozen_model
        )
        assert repair.trigger_event_details["_model_routing"]["agent_type"] == "codex"
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
        db.get(models.Flow, thread.flow_id).agent_config = {
            "feedback": {"enabled": True, "debounce_seconds": 30}
        }
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


@pytest.mark.asyncio
async def test_pg_legacy_feedback_blocks_without_reserving_execution(
    database: Engine,
) -> None:
    with Session(database) as db:
        thread = create_thread(db)
        prior = db.get(models.FlowExecution, thread.latest_execution_id)
        prior.trigger_event_details = {}
        db.commit()
        provider = SimpleNamespace(
            read=AsyncMock(
                return_value=FeedbackState("head", feedback=[event("review")])
            )
        )
        with patch(
            "preloop.services.flow_feedback.FeedbackProvider.for_thread",
            AsyncMock(return_value=provider),
        ):
            await _reconcile(db, *crud_flow_feedback.claim_due(db, now=NOW)[0], now=NOW)
        db.refresh(thread)
        assert thread.state == "blocked"
        assert thread.stop_reason == "model_identity_unavailable"
        assert thread.active_execution_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "revoke", "budget"])
async def test_policy_changed_during_provider_read_cannot_reserve(
    database: Engine, change: str
) -> None:
    """The operator edit commits while network I/O is in flight."""
    with Session(database) as db, Session(database) as editor:
        thread = create_thread(db)
        flow_id = thread.flow_id

        async def read() -> FeedbackState:
            flow = editor.get(models.Flow, flow_id)
            policy = dict(flow.agent_config["feedback"])
            if change == "disable":
                policy["enabled"] = False
            elif change == "revoke":
                policy["trusted_reviewer_ids"] = []
            else:
                policy["max_cost"] = 0.01
            flow.agent_config = {"feedback": policy}
            editor.commit()
            return FeedbackState("head", feedback=[event("review")])

        provider = SimpleNamespace(read=read)
        with (
            patch(
                "preloop.services.flow_feedback.FeedbackProvider.for_thread",
                AsyncMock(return_value=provider),
            ),
            patch(
                "preloop.services.flow_execution_dispatcher.dispatch_execute",
                AsyncMock(),
            ) as dispatch,
        ):
            await _reconcile(db, *crud_flow_feedback.claim_due(db, now=NOW)[0], now=NOW)
        db.refresh(thread)
        assert thread.active_execution_id is None
        assert thread.turns == 0
        assert len(crud_flow_feedback.pending(db, thread.id)) == 1
        dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_feedback_pause_keeps_running_turn_and_reenable_keeps_budget(
    database: Engine,
) -> None:
    with Session(database) as db:
        thread = create_thread(db)
        flow = db.get(models.Flow, thread.flow_id)
        active = models.FlowExecution(
            id=uuid.uuid4(), flow_id=flow.id, status="RUNNING"
        )
        db.add(active)
        db.flush()
        thread.active_execution_id = active.id
        thread.turns, thread.cost, thread.no_progress = 2, 8.0, 1
        deadline = thread.expires_at
        flow.agent_config = {"feedback": {"enabled": False}}
        db.commit()
        with patch(
            "preloop.services.flow_feedback.FeedbackProvider.for_thread"
        ) as provider:
            await _reconcile(db, *crud_flow_feedback.claim_due(db, now=NOW)[0], now=NOW)
            provider.assert_not_called()
        db.refresh(thread)
        assert thread.state == "paused"
        assert db.get(models.FlowExecution, active.id).status == "RUNNING"
        assert (thread.turns, thread.cost, thread.no_progress) == (2, 8.0, 1)
        assert thread.expires_at == deadline
        crud_flow_feedback.sync_policy(
            db, thread, {"enabled": True, "max_age_hours": 8760}
        )
        assert thread.expires_at == deadline
        thread.created_at = NOW
        db.commit()
        crud_flow_feedback.sync_policy(
            db, thread, {"enabled": True, "max_age_hours": 2}
        )
        assert thread.expires_at == NOW + timedelta(hours=2)
        assert (thread.turns, thread.cost, thread.no_progress) == (2, 8.0, 1)


def test_future_only_discovery_does_not_backfill_legacy_publication(
    database: Engine,
) -> None:
    from preloop.services.flow_feedback import register_thread

    with Session(database) as db:
        thread = create_thread(db)
        initial = db.get(models.FlowExecution, thread.latest_execution_id)
        pr_url, branch = thread.pr_url, thread.branch
        details = {
            "source": "github",
            "tracker_id": str(thread.tracker_id),
            "payload": {"repository": {"id": "123"}},
        }
        initial.trigger_event_details = details
        initial.result = {"pr_url": pr_url, "pr_source_branch": branch}
        db.delete(thread)
        db.commit()
        assert crud_flow_feedback.unregistered_publications(db) == []
        assert register_thread(db, initial, pr_url, branch) is None
        initial.trigger_event_details = {
            **details,
            "_session_thread_id": str(uuid.uuid4()),
        }
        db.commit()
        assert [x.id for x in crud_flow_feedback.unregistered_publications(db)] == [
            initial.id
        ]
        registered = register_thread(db, initial, pr_url, branch)
        assert registered is not None
        assert crud_flow_feedback.unregistered_publications(db) == []


@pytest.mark.asyncio
async def test_explicit_cold_source_reserves_once_then_requires_own_checkpoint(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.config import settings
    from preloop.services.flow_feedback import resolve_native_checkpoint
    from preloop.services.checkpoint_runtime import checkpoint_context
    from preloop.agents.session_runtime import native_session_blocks

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    with Session(database) as db:
        thread = create_thread(db)
        source_id = thread.latest_execution_id
        thread.context = {
            **thread.context,
            "adoption": {
                "source_execution_id": str(source_id),
                "recovery_mode": "published_branch_handoff",
            },
        }
        db.commit()
        provider = SimpleNamespace(
            read=AsyncMock(
                return_value=FeedbackState(
                    "head",
                    feedback=[event("review"), event("review"), event("old", "stale")],
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
                AsyncMock(),
            ) as dispatch,
        ):
            await _reconcile(db, *crud_flow_feedback.claim_due(db, now=NOW)[0], now=NOW)
            repair = db.get(models.FlowExecution, thread.active_execution_id)
            assert repair is not None
            assert len(repair.trigger_event_details["_feedback"]["items"]) == 1
            resume = repair.trigger_event_details["_resume"]
            assert "cli_session" not in resume
            resolved = resolve_native_checkpoint(
                db,
                account_id=thread.account_id,
                flow_id=thread.flow_id,
                execution_id=repair.id,
                resume=resume,
            )
            assert resolved == {"cold_handoff_authorized": True}
            context = {
                "account_id": str(thread.account_id),
                "flow_id": str(thread.flow_id),
                "execution_id": str(repair.id),
                "trigger_event_data": repair.trigger_event_details,
                "checkpoint_resume_authorized": True,
                "published_branch_handoff_authorized": True,
            }
            with patch(
                "preloop.api.endpoints.flow_artifacts.mint_artifact_capability",
                return_value="scoped",
            ):
                env = checkpoint_context(db, context)
            assert "PRELOOP_CHECKPOINT_GET_TOKEN" not in env
            assert env["PRELOOP_CHECKPOINT_PUT_TOKEN"] == "scoped"
            blocks = native_session_blocks(context, "codex", "/tmp/codex", '"$sid"')
            assert "explicit_published_branch_adoption" in blocks["restore"]
            assert "cold_handoff" in blocks["restore"]
            with pytest.raises(ValueError, match="binding mismatch"):
                resolve_native_checkpoint(
                    db,
                    account_id=uuid.uuid4(),
                    flow_id=thread.flow_id,
                    execution_id=repair.id,
                    resume=resume,
                )
            with pytest.raises(ValueError, match="branch binding mismatch"):
                resolve_native_checkpoint(
                    db,
                    account_id=thread.account_id,
                    flow_id=thread.flow_id,
                    execution_id=repair.id,
                    resume={**resume, "source_branch": "attacker"},
                )
            later = NOW + timedelta(seconds=31)
            await _reconcile(
                db, *crud_flow_feedback.claim_due(db, now=later)[0], now=later
            )
            assert dispatch.await_count == 1
            assert thread.turns == 1
            repair.status = "SUCCEEDED"
            repair.cli_session = None
            db.commit()
            provider.read.return_value = FeedbackState(
                "new", feedback=[event("next", "new")]
            )
            later += timedelta(seconds=31)
            await _reconcile(
                db, *crud_flow_feedback.claim_due(db, now=later)[0], now=later
            )
            next_turn = db.get(models.FlowExecution, thread.active_execution_id)
            assert next_turn.id != repair.id
            with pytest.raises(ValueError, match="native checkpoint missing"):
                resolve_native_checkpoint(
                    db,
                    account_id=thread.account_id,
                    flow_id=thread.flow_id,
                    execution_id=next_turn.id,
                    resume=next_turn.trigger_event_details["_resume"],
                )


@pytest.mark.parametrize(
    "existing_mode", ["new", "unadopted", "native_resume", "other_source"]
)
def test_adoption_registers_only_selected_legacy_pr_and_is_idempotent(
    database: Engine,
    existing_mode: str,
) -> None:
    from preloop.services import flow_continuation_adoption as adoption
    from preloop.schemas.flow_continuation import (
        ContinuationAdoptRequest,
        ContinuationPreview,
    )
    from sqlalchemy.orm import sessionmaker

    with Session(database) as db:
        old_thread = create_thread(db)
        account, source_id, flow_id = (
            old_thread.account_id,
            old_thread.latest_execution_id,
            old_thread.flow_id,
        )
        pr_url, branch, tracker_id = (
            old_thread.pr_url,
            old_thread.branch,
            old_thread.tracker_id,
        )
        source = db.get(models.FlowExecution, source_id)
        source.trigger_event_details = {
            **source.trigger_event_details,
            "source": "github",
            "tracker_id": str(tracker_id),
            "payload": {"repository": {"id": "123"}, "issue": {"number": 185}},
        }
        source.result = {"pr_url": pr_url, "pr_source_branch": branch}
        if existing_mode == "new":
            db.delete(old_thread)
        else:
            old_thread.turns, old_thread.cost, old_thread.state = 3, 14.0, "stopped"
            if existing_mode != "unadopted":
                old_thread.context = {
                    **old_thread.context,
                    "adoption": {
                        "source_execution_id": str(uuid.uuid4())
                        if existing_mode == "other_source"
                        else str(source_id),
                        "recovery_mode": "native_resume"
                        if existing_mode == "native_resume"
                        else "published_branch_handoff",
                    },
                }
        db.commit()
        readiness = ContinuationPreview(
            execution_id=source_id,
            flow_id=flow_id,
            pr_url=pr_url,
            branch=branch,
            head_sha="a" * 40,
            feedback_enabled=True,
            artifact_upload_enabled=True,
            feedback_readable=True,
            native_resume_available=False,
            allowed_recovery_modes=["published_branch_handoff"],
            warnings=[],
        )
        request = ContinuationAdoptRequest(
            recovery_mode="published_branch_handoff",
            expected_head_sha="a" * 40,
            acknowledge_fresh_conversation=True,
        )
        with (
            patch.object(adoption, "preview_continuation", return_value=readiness),
            patch.object(
                adoption, "get_session_factory", return_value=sessionmaker(database)
            ),
        ):
            if existing_mode != "new":
                stored = (
                    old_thread.context,
                    old_thread.turns,
                    old_thread.cost,
                    old_thread.state,
                    old_thread.expires_at,
                )
                with pytest.raises(
                    adoption.ContinuationAdoptionError,
                    match="already has a continuation",
                ) as error:
                    adoption.adopt_continuation(account, source_id, request)
                assert error.value.status_code == 409
                db.refresh(old_thread)
                assert (
                    old_thread.context,
                    old_thread.turns,
                    old_thread.cost,
                    old_thread.state,
                    old_thread.expires_at,
                ) == stored
                return
            first = adoption.adopt_continuation(account, source_id, request)
            assert first.thread_id == source_id
            thread = db.get(models.FlowThread, first.thread_id)
            deadline = thread.expires_at
            thread.turns, thread.cost, thread.state = 3, 14.0, "stopped"
            db.commit()
            duplicate = adoption.adopt_continuation(account, source_id, request)
            assert duplicate.thread_id == first.thread_id
            assert duplicate.state == "stopped"
            db.refresh(thread)
            assert (thread.turns, thread.cost, thread.expires_at) == (3, 14.0, deadline)
            assert thread.context["adoption"]["source_execution_id"] == str(source_id)
            assert (
                thread.context["adoption"]["recovery_mode"]
                == "published_branch_handoff"
            )


@pytest.mark.asyncio
async def test_provider_materializes_identity_and_releases_transaction_before_network(
    database: Engine,
) -> None:
    from preloop.services.flow_feedback_provider import FeedbackProvider

    with Session(database) as db:
        thread = create_thread(db)
        tracker = SimpleNamespace(
            account_id=thread.account_id,
            tracker_type="github",
            id=thread.tracker_id,
            resolved_api_key="synthetic",
            url="https://github.com",
            connection_details={},
        )
        thread_id = thread.id
        thread.stop_reason = "pending-local-work"

        async def create_client(*args: Any) -> object:
            assert not db.in_transaction()
            assert database.pool.checkedout() == 0
            return object()

        with (
            patch(
                "preloop.services.flow_feedback_provider.crud_tracker.get",
                return_value=tracker,
            ),
            patch(
                "preloop.services.flow_feedback_provider.create_tracker_client",
                side_effect=create_client,
            ),
        ):
            provider = await FeedbackProvider.for_thread(db, thread)
        assert not db.in_transaction()
        assert provider.thread.repository_id == "123"
        assert provider.thread.pr_number == "7"
        assert db.get(models.FlowThread, thread_id).stop_reason == "pending-local-work"


@pytest.mark.asyncio
async def test_native_repair_resolves_encrypted_workspace_and_selected_session(
    database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from datetime import UTC
    from preloop.config import settings
    from preloop.services.flow_artifacts import put_artifact, get_artifact
    from preloop.services.flow_feedback import resolve_native_checkpoint
    from preloop.services.checkpoint_runtime import checkpoint_context
    from preloop.agents.session_manifest import pack_session, unpack_session
    from backend.tests.services.test_flow_artifacts import archive_with

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    with Session(database) as db:
        thread = create_thread(db)
        source = db.get(models.FlowExecution, thread.latest_execution_id)
        sid = source.cli_session["session_id"]
        identity = {
            "harness": "codex",
            "harness_version": "0.153.4",
            "session_id": sid,
            "thread_id": str(thread.id),
        }
        files = {
            "rollout.jsonl": json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": sid, "fact": "selected conversation"},
                }
            ).encode()
        }
        native_bytes = pack_session(
            files, **identity, expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
        scope = {
            "account_id": thread.account_id,
            "flow_id": thread.flow_id,
            "execution_id": source.id,
            "thread_id": str(thread.id),
        }

        def store(
            session: Session, *, values: dict[str, Any], quota_bytes: int
        ) -> models.FlowArtifact:
            # This fixture's minimal account table omits unrelated account fields.
            # Exercise real encrypted artifact service plus real scoped rows.
            row = models.FlowArtifact(**values)
            session.add(row)
            session.commit()
            return row

        with patch("preloop.services.flow_artifacts.crud.store", side_effect=store):
            workspace_ref = put_artifact(
                db,
                **scope,
                kind="workspace",
                archive=archive_with(
                    "workspace/unpublished.txt", b"unpublished changes"
                ),
            )
            native_ref = put_artifact(
                db, **scope, kind="native_session", archive=native_bytes
            )
        source.cli_session = {
            **source.cli_session,
            "artifact_reference": native_ref.model_dump(mode="json"),
        }
        db.commit()
        provider = SimpleNamespace(
            read=AsyncMock(return_value=FeedbackState("head", feedback=[event("ci")]))
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
                AsyncMock(),
            ),
        ):
            await _reconcile(db, *crud_flow_feedback.claim_due(db, now=NOW)[0], now=NOW)
        repair = db.get(models.FlowExecution, thread.active_execution_id)
        resume = repair.trigger_event_details["_resume"]
        native = resolve_native_checkpoint(
            db,
            account_id=thread.account_id,
            flow_id=thread.flow_id,
            execution_id=repair.id,
            resume=resume,
        )
        assert native["session_id"] == sid
        context = {
            "account_id": str(thread.account_id),
            "flow_id": str(thread.flow_id),
            "execution_id": str(repair.id),
            "trigger_event_data": repair.trigger_event_details,
            "checkpoint_resume_authorized": True,
            "native_session_reference": native["artifact_reference"],
        }
        with patch(
            "preloop.api.endpoints.flow_artifacts.mint_artifact_capability",
            return_value="scoped",
        ):
            env = checkpoint_context(db, context)
        assert "PRELOOP_CHECKPOINT_GET_TOKEN" in env
        assert "PRELOOP_NATIVE_SESSION_GET_TOKEN" in env
        read_scope = {k: v for k, v in scope.items() if k != "execution_id"}
        restored = get_artifact(db, **read_scope, reference=native_ref)
        assert unpack_session(restored, **identity) == files
        assert b"unpublished changes" in __import__("gzip").decompress(
            get_artifact(db, **read_scope, reference=workspace_ref)
        )
        saved_session = dict(source.cli_session)
        source.cli_session = {
            "agent_type": "codex",
            "artifact_reference": native_ref.model_dump(mode="json"),
        }
        db.commit()
        with pytest.raises(ValueError, match="invalid native session identity"):
            resolve_native_checkpoint(
                db,
                account_id=thread.account_id,
                flow_id=thread.flow_id,
                execution_id=repair.id,
                resume=resume,
            )
        source.cli_session = saved_session
        db.commit()
        monkeypatch.setattr(settings, "flow_artifact_direct_upload", False)
        with pytest.raises(ValueError, match="checkpoint uploads disabled"):
            resolve_native_checkpoint(
                db,
                account_id=thread.account_id,
                flow_id=thread.flow_id,
                execution_id=repair.id,
                resume=resume,
            )
        monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
        native_row = db.get(models.FlowArtifact, native_ref.artifact_id)
        native_row.ciphertext = None
        db.commit()
        with pytest.raises(ValueError, match="native checkpoint unavailable"):
            resolve_native_checkpoint(
                db,
                account_id=thread.account_id,
                flow_id=thread.flow_id,
                execution_id=repair.id,
                resume=resume,
            )


def test_reservation_waits_for_parent_before_locking_thread(database: Engine) -> None:
    """A flow deletion owns the parent and must still acquire its child lock."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from sqlalchemy import event as sql_event, select, delete

    with Session(database) as setup:
        thread = create_thread(setup)
        flow_id, thread_id = thread.flow_id, thread.id
        claim = crud_flow_feedback.claim_due(setup, now=NOW)[0]
    waiting = Event()

    def before_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "FOR SHARE" in statement:
            waiting.set()

    def reserve() -> None:
        with Session(database) as worker:
            assert (
                crud_flow_feedback.reserve(
                    worker,
                    *claim,
                    event_data={},
                    receipt_ids=[],
                    head_sha="head",
                    now=NOW,
                )
                is None
            )

    sql_event.listen(database, "before_cursor_execute", before_execute)
    try:
        with Session(database) as deletion, ThreadPoolExecutor(max_workers=1) as pool:
            flow = deletion.execute(
                select(models.Flow).where(models.Flow.id == flow_id).with_for_update()
            ).scalar_one()
            future = pool.submit(reserve)
            try:
                assert waiting.wait(timeout=5)
                # NOWAIT would raise if reservation had locked the child first.
                deletion.execute(
                    select(models.FlowThread)
                    .where(models.FlowThread.id == thread_id)
                    .with_for_update(nowait=True)
                ).scalar_one()
                deletion.execute(
                    delete(models.FlowExecution).where(
                        models.FlowExecution.flow_id == flow.id
                    )
                )
                deletion.execute(delete(models.Flow).where(models.Flow.id == flow.id))
                deletion.commit()
            finally:
                deletion.rollback()
            future.result(timeout=5)
    finally:
        sql_event.remove(database, "before_cursor_execute", before_execute)
