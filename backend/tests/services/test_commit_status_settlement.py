"""Every terminal path replaces the pending commit status exactly once.

A pull request run sets ``pending`` ("Preloop is reviewing: <flow>") when it
starts. The final status must replace it whichever path ends the run: the
worker that ran it, a peer worker that resumed it after a deploy drain, a
stop, a failure or a timeout. The settlement sweep covers the run whose
finishing worker died between the terminal write and the status.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_flow_execution,
    crud_user,
)
from preloop.models.models import Account, Flow
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import commit_status_settlement
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

HEAD = "1" * 40
LATER_HEAD = "2" * 40
FLOW_NAME = "Pull request review"


class FakeTracker:
    """Records every commit status the orchestrator sets."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next = False

    async def create_commit_status(self, **kwargs: Any) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic provider outage")
        self.calls.append(kwargs)

    @property
    def states(self) -> list[tuple[str, str]]:
        return [(call["state"], call["sha"]) for call in self.calls]


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> FakeTracker:
    fake = FakeTracker()

    async def client(self: FlowExecutionOrchestrator) -> FakeTracker:
        return fake

    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_get_tracker_client_for_status", client
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_describe_status_target",
        lambda self, client: "the synthetic repository",
    )
    return fake


@pytest.fixture
def account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={"organization_name": f"Status {uuid4().hex[:8]}", "is_active": True},
    )


@pytest.fixture
def user(db_session: Session, account: Account) -> User:
    created = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": f"status_{uuid4().hex[:8]}@example.com",
            "username": f"status_{uuid4().hex[:8]}",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "test_password",
            "user_source": "local",
        },
    )
    account.primary_user_id = created.id
    db_session.add(account)
    db_session.commit()
    return created


@pytest.fixture
def flow(db_session: Session, account: Account, user: User) -> Flow:
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=FLOW_NAME,
            description="Reviews pull requests",
            trigger_event_source="github",
            trigger_event_types=["pull_request_opened"],
            prompt_template="Review {{payload.pull_request.title}}",
            agent_type="openhands",
            agent_config={"max_iterations": 10},
            account_id=account.id,
        ),
        account_id=account.id,
    )


def trigger(head: str = HEAD) -> dict[str, Any]:
    return {
        "source": "github",
        "type": "pull_request_opened",
        "event_id": f"evt_{uuid4().hex[:8]}",
        "payload": {
            "pull_request": {
                "title": "Add parser",
                "number": 7,
                "head": {"sha": head, "ref": "feature/parser"},
            }
        },
    }


def orchestrator(db: Session, flow: Flow, data: dict[str, Any]) -> Any:
    nats_client = AsyncMock()
    nats_client.is_connected = True
    return FlowExecutionOrchestrator(
        db=db, flow_id=flow.id, trigger_event_data=data, nats_client=nats_client
    )


def executor(status: str = "SUCCEEDED") -> Any:
    from preloop.agents.base import AgentExecutionResult, AgentStatus

    agent_status = AgentStatus[status]
    mock = AsyncMock()
    mock.start = AsyncMock(return_value="agent-session-ref")
    mock.get_status = AsyncMock(return_value=agent_status)
    mock.get_result = AsyncMock(
        return_value=AgentExecutionResult(
            status=agent_status,
            session_reference="agent-session-ref",
            output_summary="done",
            actions_taken=None,
            exit_code=0,
        )
    )
    mock.stop = AsyncMock()
    return mock


async def run_until_drained(runner: Any) -> None:
    """Start a run and cancel it mid-monitoring, as a deploy drain does."""
    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor(),
        ),
        patch.object(
            runner,
            "_monitor_agent_execution",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError(),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await runner.run()


async def resume(
    db: Session, flow: Flow, drained: Any, agent_result: dict[str, Any]
) -> Any:
    from preloop.services.flow_execution_runner import resume_existing_execution

    # A peer worker after the redeploy: a fresh orchestrator on the row.
    peer = orchestrator(db, flow, drained.trigger_event_data)
    peer.execution_log = crud_flow_execution.get(db, id=drained.execution_log.id)
    with (
        patch("preloop.agents.create_agent_executor", return_value=AsyncMock()),
        patch.object(
            peer,
            "_monitor_agent_execution",
            new_callable=AsyncMock,
            return_value=agent_result,
        ),
    ):
        await resume_existing_execution(peer, "agent-session-ref")
    return peer


def record(db: Session, execution_id: Any) -> dict[str, Any]:
    execution = crud_flow_execution.get(db, id=execution_id)
    db.refresh(execution)
    return crud_flow_execution.commit_status_record(execution)


# --- the resume path (the reported case) ------------------------------------


@pytest.mark.asyncio
async def test_run_resumed_after_a_deploy_replaces_its_pending_status(
    db_session: Session, flow: Flow, tracker: FakeTracker
) -> None:
    drained = orchestrator(db_session, flow, trigger())
    await run_until_drained(drained)
    assert tracker.states == [("pending", HEAD)]
    assert tracker.calls[0]["description"] == f"Preloop is reviewing: {FLOW_NAME}"
    assert record(db_session, drained.execution_log.id)["pending"]["sha"] == HEAD

    peer = await resume(
        db_session, flow, drained, {"status": "SUCCEEDED", "output_summary": "ok"}
    )

    assert peer._is_recovered is True
    assert tracker.states == [("pending", HEAD), ("success", HEAD)]
    final = tracker.calls[-1]
    assert final["description"] == f"Preloop review completed: {FLOW_NAME}"
    assert final["context"] == tracker.calls[0]["context"]
    assert final["refname"] == tracker.calls[0]["refname"]
    stored = record(db_session, drained.execution_log.id)["final"]
    assert stored["state"] == "success" and stored["posted_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_result", "state", "description"),
    [
        (
            {"status": "FAILED", "error_message": "tests failed"},
            "failure",
            "Preloop review failed: tests failed",
        ),
        (
            {"status": "TIMEOUT", "error_message": "time limit reached"},
            "failure",
            "Preloop review failed: time limit reached",
        ),
        ({"status": "STOPPED"}, "failure", "Preloop execution was stopped"),
    ],
)
async def test_resumed_run_that_failed_timed_out_or_stopped_is_settled(
    db_session: Session,
    flow: Flow,
    tracker: FakeTracker,
    agent_result: dict[str, Any],
    state: str,
    description: str,
) -> None:
    drained = orchestrator(db_session, flow, trigger())
    await run_until_drained(drained)
    await resume(db_session, flow, drained, agent_result)
    assert tracker.states == [("pending", HEAD), (state, HEAD)]
    assert tracker.calls[-1]["description"] == description


@pytest.mark.asyncio
async def test_final_status_goes_to_the_commit_that_got_the_pending_one(
    db_session: Session, flow: Flow, tracker: FakeTracker
) -> None:
    drained = orchestrator(db_session, flow, trigger(HEAD))
    await run_until_drained(drained)
    # Trigger data seen by the resuming worker names a later head.
    drained.trigger_event_data = trigger(LATER_HEAD)
    await resume(db_session, flow, drained, {"status": "SUCCEEDED"})
    assert tracker.states == [("pending", HEAD), ("success", HEAD)]


@pytest.mark.asyncio
async def test_recovered_run_without_a_pending_status_still_makes_no_call(
    db_session: Session, flow: Flow, tracker: FakeTracker
) -> None:
    runner = orchestrator(db_session, flow, trigger())
    runner._get_flow_details()
    runner.execution_log = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="SUCCEEDED", trigger_event_details=trigger()
        ),
    )
    runner._is_recovered = True
    await runner.settle_commit_status("SUCCEEDED")
    assert tracker.calls == []


# --- the main run and its error paths ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "state"), [("SUCCEEDED", "success"), ("FAILED", "failure")]
)
async def test_normal_run_sets_one_final_status(
    db_session: Session, flow: Flow, tracker: FakeTracker, status: str, state: str
) -> None:
    runner = orchestrator(db_session, flow, trigger())
    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor(),
        ),
        patch.object(
            runner,
            "_monitor_agent_execution",
            new_callable=AsyncMock,
            return_value={"status": status, "error_message": "broken"},
        ),
    ):
        await runner.run()
    assert tracker.states == [("pending", HEAD), (state, HEAD)]
    assert record(db_session, runner.execution_log.id)["final"]["posted_at"]


@pytest.mark.asyncio
async def test_error_after_the_final_status_does_not_overwrite_it(
    db_session: Session, flow: Flow, tracker: FakeTracker
) -> None:
    runner = orchestrator(db_session, flow, trigger())
    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor(),
        ),
        patch.object(
            runner,
            "_monitor_agent_execution",
            new_callable=AsyncMock,
            return_value={"status": "SUCCEEDED"},
        ),
        patch.object(
            runner,
            "_retry_after_no_progress",
            new_callable=AsyncMock,
            side_effect=RuntimeError("synthetic"),
        ),
    ):
        await runner.run()
    # The failure branch ran after success was set; it posts nothing more.
    assert tracker.states == [("pending", HEAD), ("success", HEAD)]


@pytest.mark.asyncio
async def test_run_that_crashes_before_finishing_sets_failure_once(
    db_session: Session, flow: Flow, tracker: FakeTracker
) -> None:
    runner = orchestrator(db_session, flow, trigger())
    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor(),
        ),
        patch.object(
            runner,
            "_monitor_agent_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("synthetic"),
        ),
    ):
        await runner.run()
    assert tracker.states == [("pending", HEAD), ("failure", HEAD)]


@pytest.mark.asyncio
async def test_stop_before_launch_settles_the_pending_status(
    db_session: Session, flow: Flow, tracker: FakeTracker
) -> None:
    from preloop.services.flow_orchestrator import ExecutionStoppedError

    runner = orchestrator(db_session, flow, trigger())
    with patch.object(
        runner,
        "_prepare_execution_context",
        new_callable=AsyncMock,
        side_effect=ExecutionStoppedError("stopped"),
    ):
        await runner.run()
    assert tracker.states == [("pending", HEAD), ("failure", HEAD)]
    assert (
        tracker.calls[-1]["description"]
        == "Preloop execution was stopped before it started"
    )


@pytest.mark.asyncio
async def test_parked_run_keeps_pending_and_its_release_hands_over(
    db_session: Session, flow: Flow, tracker: FakeTracker, monkeypatch: Any
) -> None:
    """Parked: still alive, so still pending. Released: the continuation owns
    the final status and the sweep does not post a second one."""
    parked = orchestrator(db_session, flow, trigger())
    await run_until_drained(parked)
    continuation = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="SUCCEEDED", trigger_event_details=trigger()
        ),
    )
    execution = crud_flow_execution.get(db_session, id=parked.execution_log.id)
    crud_flow_execution.update(
        db_session,
        db_obj=execution,
        obj_in={
            "status": "SUCCEEDED",
            "end_time": datetime.now(timezone.utc) - timedelta(minutes=5),
            "resume_execution_id": continuation.id,
        },
    )
    db_session.commit()
    _use_session(monkeypatch, db_session)
    counts = await commit_status_settlement.settle_commit_statuses()
    assert counts["skipped"] >= 1
    assert tracker.states == [("pending", HEAD)]
    final = record(db_session, execution.id)["final"]
    assert final["handed_to"] == str(continuation.id)
    again = await commit_status_settlement.settle_commit_statuses()
    assert execution.id not in _found_ids(db_session)
    assert again["settled"] == 0 and tracker.states == [("pending", HEAD)]


# --- the settlement sweep ----------------------------------------------------


def _use_session(monkeypatch: Any, db: Session) -> None:
    from preloop.models.db import session as db_session_module

    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(db_session_module, "get_db_session", lambda: iter([db]))


def _found_ids(db: Session) -> list[Any]:
    return crud_flow_execution.unsettled_commit_status_ids(
        db,
        ended_within=commit_status_settlement.SETTLE_WINDOW,
        settle_after=commit_status_settlement.SETTLE_AFTER,
    )


async def _worker_died_after_terminal_write(
    db: Session,
    flow: Flow,
    status: str = "SUCCEEDED",
    ended_ago: timedelta = timedelta(minutes=5),
) -> Any:
    """A run that set pending, then whose terminal status was written by a
    path that never set the final one (the worker died in between)."""
    runner = orchestrator(db, flow, trigger())
    await run_until_drained(runner)
    execution = crud_flow_execution.get(db, id=runner.execution_log.id)
    crud_flow_execution.update(
        db,
        db_obj=execution,
        obj_in={
            "status": status,
            "error_message": "agent exited" if status != "SUCCEEDED" else None,
            "end_time": datetime.now(timezone.utc) - ended_ago,
        },
    )
    db.commit()
    return execution


@pytest.mark.asyncio
async def test_sweep_settles_once_and_the_second_pass_does_nothing(
    db_session: Session,
    flow: Flow,
    tracker: FakeTracker,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    succeeded = await _worker_died_after_terminal_write(db_session, flow)
    failed = await _worker_died_after_terminal_write(db_session, flow, "FAILED")
    assert {succeeded.id, failed.id} <= set(_found_ids(db_session))
    _use_session(monkeypatch, db_session)
    caplog.set_level("INFO", logger=commit_status_settlement.__name__)

    first = await commit_status_settlement.settle_commit_statuses()

    assert first["settled"] >= 2 and first["failed"] == 0
    finals = [call for call in tracker.calls if call["state"] != "pending"]
    assert sorted(call["state"] for call in finals) == ["failure", "success"]
    assert {call["sha"] for call in finals} == {HEAD}
    by_state = {call["state"]: call["description"] for call in finals}
    assert by_state["success"] == f"Preloop review completed: {FLOW_NAME}"
    assert by_state["failure"] == "Preloop review failed: agent exited"
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Commit status settlement:")
    ]
    assert len(lines) == 1 and "settled" in lines[0]

    second = await commit_status_settlement.settle_commit_statuses()

    assert second["settled"] == 0 and second["found"] == 0
    assert [c for c in tracker.calls if c["state"] != "pending"] == finals
    assert succeeded.id not in _found_ids(db_session)


@pytest.mark.asyncio
async def test_sweep_respects_the_window_and_the_grace_period(
    db_session: Session, flow: Flow, tracker: FakeTracker
) -> None:
    old = await _worker_died_after_terminal_write(
        db_session, flow, ended_ago=timedelta(days=8)
    )
    fresh = await _worker_died_after_terminal_write(
        db_session, flow, ended_ago=timedelta(seconds=10)
    )
    due = await _worker_died_after_terminal_write(db_session, flow)
    found = _found_ids(db_session)
    assert due.id in found
    assert old.id not in found and fresh.id not in found


@pytest.mark.asyncio
async def test_sweep_batch_is_bounded(
    db_session: Session, flow: Flow, tracker: FakeTracker
) -> None:
    for _ in range(3):
        await _worker_died_after_terminal_write(db_session, flow)
    assert (
        len(
            crud_flow_execution.unsettled_commit_status_ids(
                db_session,
                ended_within=timedelta(days=7),
                settle_after=timedelta(minutes=2),
                limit=2,
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_failed_post_is_retried_by_the_sweep_then_given_up(
    db_session: Session, flow: Flow, tracker: FakeTracker, monkeypatch: Any
) -> None:
    execution = await _worker_died_after_terminal_write(db_session, flow)
    _use_session(monkeypatch, db_session)
    monkeypatch.setattr(commit_status_settlement, "CLAIM_STALE_AFTER", timedelta(0))
    tracker.fail_next = True
    first = await commit_status_settlement.settle_commit_statuses()
    assert first["failed"] >= 1
    assert record(db_session, execution.id)["final"]["failed_at"]
    second = await commit_status_settlement.settle_commit_statuses()
    assert second["settled"] >= 1
    assert [c["state"] for c in tracker.calls] == ["pending", "success"]
    # Once posted it is never claimed again.
    assert not crud_flow_execution.claim_final_commit_status(
        db_session, execution_id=execution.id, state="success", stale_after=timedelta(0)
    )


@pytest.mark.asyncio
async def test_sweep_gives_up_after_three_failed_posts(
    db_session: Session, flow: Flow, tracker: FakeTracker, monkeypatch: Any
) -> None:
    execution = await _worker_died_after_terminal_write(db_session, flow)
    _use_session(monkeypatch, db_session)
    monkeypatch.setattr(commit_status_settlement, "CLAIM_STALE_AFTER", timedelta(0))
    for _ in range(3):
        tracker.fail_next = True
        await commit_status_settlement.settle_commit_statuses()
    final = record(db_session, execution.id)["final"]
    assert final["attempts"] == 3 and final["gave_up"] is True
    assert execution.id not in _found_ids(db_session)
    await commit_status_settlement.settle_commit_statuses()
    assert [c["state"] for c in tracker.calls] == ["pending"]


@pytest.mark.asyncio
async def test_run_whose_tracker_is_gone_leaves_the_batch(
    db_session: Session, flow: Flow, tracker: FakeTracker, monkeypatch: Any
) -> None:
    execution = await _worker_died_after_terminal_write(db_session, flow)

    async def no_client(self: FlowExecutionOrchestrator) -> None:
        return None

    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_get_tracker_client_for_status", no_client
    )
    _use_session(monkeypatch, db_session)
    monkeypatch.setattr(commit_status_settlement, "CLAIM_STALE_AFTER", timedelta(0))
    passes = 0
    while execution.id in _found_ids(db_session):
        passes += 1
        assert passes <= 3, "an unsettleable run must leave the batch"
        await commit_status_settlement.settle_commit_statuses()
    assert record(db_session, execution.id)["final"]["gave_up"] is True
    assert [c["state"] for c in tracker.calls] == ["pending"]
