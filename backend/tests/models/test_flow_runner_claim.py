"""Real PostgreSQL regression tests for idle runner lease eligibility."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import JSON, null, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account
from preloop.models.crud.flow_runner import crud_flow_runner


@pytest.mark.parametrize("pending_job", [None, JSON.NULL, null()])
def test_claim_idle_accepts_both_null_representations(
    db_session: Session, pending_job: object
) -> None:
    account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Runner lease test",
            "is_active": True,
        },
    )
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "name": f"runner-{uuid4()}",
            "token_hash": "test-token-hash",
            "status": "online",
            "last_heartbeat": datetime.now(timezone.utc),
            "pending_job": pending_job,
        },
    )
    assert runner.pending_job is None
    assert crud_flow_runner.find_matching(
        db_session, account_id=account.id, pool="auto"
    ) == [runner]
    # Python None and JSON.NULL persist as JSON null under the existing model.
    is_sql_null = db_session.scalar(
        select(models.FlowRunner.pending_job.is_(None)).where(
            models.FlowRunner.id == runner.id
        )
    )
    assert is_sql_null is (not (pending_job is None or pending_job is JSON.NULL))
    assert crud_flow_runner.claim_idle(db_session, runner_id=runner.id) is not None


@pytest.mark.parametrize("pending_job", [{}, {"execution_id": "pending"}, [], False])
def test_claim_idle_rejects_non_null_jobs(
    db_session: Session, pending_job: object
) -> None:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Busy runner test"}
    )
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "name": "occupied",
            "token_hash": "test-token",
            "status": "online",
            "pending_job": pending_job,
        },
    )
    assert crud_flow_runner.claim_idle(db_session, runner_id=runner.id) is None


def test_cleared_runner_can_be_claimed_again(db_session: Session) -> None:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Completion test"}
    )
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "name": "completed",
            "token_hash": "test-token",
            "status": "busy",
            "pending_job": {"execution_id": "previous"},
        },
    )
    assert crud_flow_runner.claim_idle(db_session, runner_id=runner.id) is None
    crud_flow_runner.update(
        db_session, db_obj=runner, obj_in={"pending_job": None, "status": "online"}
    )
    assert crud_flow_runner.claim_idle(db_session, runner_id=runner.id) is not None


def test_concurrent_claim_skips_locked_runner(db_engine: Engine) -> None:
    """Two real transactions cannot claim the same JSON-null runner."""
    with Session(db_engine) as setup:
        account = crud_account.create(
            setup, obj_in={"organization_name": "Concurrent lease test"}
        )
        account_id = account.id
        runner = crud_flow_runner.create(
            setup,
            obj_in={
                "account_id": account_id,
                "name": "concurrent",
                "token_hash": "test-token",
                "status": "online",
                "pending_job": None,
            },
        )
        runner_id = runner.id
    try:
        with Session(db_engine) as first, Session(db_engine) as second:
            claimed = crud_flow_runner.claim_idle(first, runner_id=runner_id)
            assert claimed is not None
            assert crud_flow_runner.claim_idle(second, runner_id=runner_id) is None
            crud_flow_runner.update(
                first,
                db_obj=claimed,
                obj_in={
                    "status": "busy",
                    "pending_job": {"execution_id": "winner"},
                },
            )
            assert crud_flow_runner.claim_idle(second, runner_id=runner_id) is None
    finally:
        with Session(db_engine) as cleanup:
            crud_flow_runner.delete(cleanup, id=runner_id)
            crud_account.delete(cleanup, id=account_id)


def test_monitor_refreshes_cached_execution(db_session: Session) -> None:
    """Another session's completion must replace a cached queued state."""
    from preloop.models.crud import crud_flow, crud_flow_execution
    from preloop.models.schemas.flow import FlowCreate
    from preloop.models.schemas.flow_execution import (
        FlowExecutionCreate,
        FlowExecutionUpdate,
    )

    account = crud_account.create(
        db_session, obj_in={"organization_name": "Monitor refresh test"}
    )
    flow = crud_flow.create(
        db_session,
        account_id=account.id,
        flow_in=FlowCreate(
            name="Runner flow",
            account_id=account.id,
            agent_type="codex",
            agent_config={},
            prompt_template="Implement the issue",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
        ),
    )
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="PENDING",
        ),
    )
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
