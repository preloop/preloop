"""Real PostgreSQL regression tests for runner slot eligibility."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _account(db: Session, name: str) -> models.Account:
    return crud_account.create(
        db, obj_in={"organization_name": name, "is_active": True}
    )


def _runner(db: Session, account_id: UUID, **overrides: object) -> models.FlowRunner:
    payload = {
        "account_id": account_id,
        "name": f"runner-{uuid4()}",
        "token_hash": "test-token-hash",
        "status": "online",
        "last_heartbeat": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    return crud_flow_runner.create(db, obj_in=payload)


def _execution(db: Session, account_id: UUID) -> models.FlowExecution:
    flow = crud_flow.create(
        db,
        account_id=account_id,
        flow_in=FlowCreate(
            name=f"flow-{uuid4()}",
            account_id=account_id,
            agent_type="codex",
            agent_config={},
            prompt_template="Implement the issue",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
        ),
    )
    return crud_flow_execution.create(
        db, obj_in=FlowExecutionCreate(flow_id=flow.id, status="PENDING")
    )


def test_a_new_runner_gets_the_default_two_slots(db_session: Session) -> None:
    account = _account(db_session, "Runner default concurrency")
    runner = _runner(db_session, account.id)
    assert runner.concurrency == models.DEFAULT_RUNNER_CONCURRENCY
    assert runner.capacity == 2
    assert runner.free_slots == 2


def test_claim_free_slot_allows_a_second_job_and_refuses_a_third(
    db_session: Session,
) -> None:
    account = _account(db_session, "Runner two slots")
    runner = _runner(db_session, account.id)
    first = _execution(db_session, account.id)
    second = _execution(db_session, account.id)

    assert crud_flow_runner.claim_free_slot(db_session, runner_id=runner.id) is not None
    crud_flow_runner.create_assignment(
        db_session, runner_id=runner.id, execution_id=first.id, pending_job={"a": 1}
    )
    assert crud_flow_runner.claim_free_slot(db_session, runner_id=runner.id) is not None
    crud_flow_runner.create_assignment(
        db_session, runner_id=runner.id, execution_id=second.id, pending_job={"a": 2}
    )
    db_session.refresh(runner)
    assert runner.running_count == 2
    assert runner.free_slots == 0
    # Filling the last slot is what makes a runner busy, not holding a job.
    assert runner.status == "busy"
    assert crud_flow_runner.claim_free_slot(db_session, runner_id=runner.id) is None


def test_releasing_one_slot_leaves_the_other_job_running(db_session: Session) -> None:
    account = _account(db_session, "Runner release one")
    runner = _runner(db_session, account.id)
    first = _execution(db_session, account.id)
    second = _execution(db_session, account.id)
    crud_flow_runner.create_assignment(
        db_session, runner_id=runner.id, execution_id=first.id
    )
    crud_flow_runner.create_assignment(
        db_session, runner_id=runner.id, execution_id=second.id
    )

    assert (
        crud_flow_runner.release_assignment(
            db_session, runner_id=runner.id, execution_id=first.id
        )
        == 1
    )
    db_session.refresh(runner)
    assert runner.running_execution_ids == [second.id]
    assert runner.status == "online"
    assert runner.free_slots == 1


def test_halt_is_per_assignment(db_session: Session) -> None:
    account = _account(db_session, "Runner halt one")
    runner = _runner(db_session, account.id)
    first = _execution(db_session, account.id)
    second = _execution(db_session, account.id)
    crud_flow_runner.create_assignment(
        db_session, runner_id=runner.id, execution_id=first.id
    )
    crud_flow_runner.create_assignment(
        db_session, runner_id=runner.id, execution_id=second.id
    )

    assert crud_flow_runner.request_halt(
        db_session, runner_id=runner.id, execution_id=first.id
    )
    db_session.expire_all()
    assert runner.assignment_for(first.id).halt_requested is True
    assert runner.assignment_for(second.id).halt_requested is False


def test_a_reported_concurrency_lowers_capacity_but_cannot_raise_it(
    db_session: Session,
) -> None:
    account = _account(db_session, "Runner reported concurrency")
    runner = _runner(db_session, account.id, concurrency=4)
    crud_flow_runner.set_reported_concurrency(db_session, runner=runner, reported=1)
    assert runner.capacity == 1
    crud_flow_runner.set_reported_concurrency(db_session, runner=runner, reported=99)
    assert runner.capacity == 4


def test_set_concurrency_is_clamped_to_the_supported_range(
    db_session: Session,
) -> None:
    account = _account(db_session, "Runner concurrency clamp")
    runner = _runner(db_session, account.id)
    assert (
        crud_flow_runner.set_concurrency(
            db_session, runner=runner, concurrency=1000
        ).concurrency
        == models.MAX_RUNNER_CONCURRENCY
    )
    assert (
        crud_flow_runner.set_concurrency(db_session, runner=runner, concurrency=0)
    ).concurrency == 1


def test_an_execution_cannot_be_assigned_to_two_runners(db_session: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    account = _account(db_session, "Runner unique execution")
    first_runner = _runner(db_session, account.id)
    second_runner = _runner(db_session, account.id)
    execution = _execution(db_session, account.id)
    crud_flow_runner.create_assignment(
        db_session, runner_id=first_runner.id, execution_id=execution.id
    )
    with pytest.raises(IntegrityError):
        crud_flow_runner.create_assignment(
            db_session, runner_id=second_runner.id, execution_id=execution.id
        )
    db_session.rollback()


def test_find_matching_prefers_the_runner_with_the_most_free_slots(
    db_session: Session,
) -> None:
    account = _account(db_session, "Runner free slot order")
    loaded = _runner(db_session, account.id, concurrency=2)
    empty = _runner(db_session, account.id, concurrency=2)
    crud_flow_runner.create_assignment(
        db_session,
        runner_id=loaded.id,
        execution_id=_execution(db_session, account.id).id,
    )

    ordered = crud_flow_runner.find_matching(
        db_session, account_id=account.id, pool="auto"
    )
    assert [row.id for row in ordered][0] == empty.id
    assert {row.id for row in ordered} == {empty.id, loaded.id}


def test_concurrent_claim_skips_locked_runner(db_engine: Engine) -> None:
    """Two real transactions cannot claim the same last free slot."""
    with Session(db_engine) as setup:
        account = _account(setup, "Concurrent lease test")
        account_id = account.id
        runner = _runner(setup, account_id, concurrency=1)
        runner_id = runner.id
        execution_id = _execution(setup, account_id).id
        setup.commit()
    try:
        with Session(db_engine) as first, Session(db_engine) as second:
            claimed = crud_flow_runner.claim_free_slot(first, runner_id=runner_id)
            assert claimed is not None
            assert crud_flow_runner.claim_free_slot(second, runner_id=runner_id) is None
            crud_flow_runner.create_assignment(
                first, runner_id=runner_id, execution_id=execution_id
            )
            assert crud_flow_runner.claim_free_slot(second, runner_id=runner_id) is None
    finally:
        with Session(db_engine) as cleanup:
            crud_flow_runner.release_assignment(cleanup, runner_id=runner_id)
            crud_flow_runner.delete(cleanup, id=runner_id)
            crud_account.delete(cleanup, id=account_id)


def test_monitor_refreshes_cached_execution(db_session: Session) -> None:
    """Another session's completion must replace a cached queued state."""
    from preloop.models.schemas.flow_execution import FlowExecutionUpdate

    account = _account(db_session, "Monitor refresh test")
    execution = _execution(db_session, account.id)
    with Session(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    ) as writer:
        other = crud_flow_execution.get(writer, id=execution.id)
        crud_flow_execution.update(
            writer, db_obj=other, obj_in=FlowExecutionUpdate(status="SUCCEEDED")
        )
        writer.commit()
    assert execution.status == "PENDING"
    current = crud_flow_execution.get(db_session, id=execution.id, refresh=True)
    assert current is execution
    assert current.status == "SUCCEEDED"
