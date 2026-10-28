"""Database allocation and snapshot account boundaries for realtime recovery."""

from uuid import uuid4

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.crud import execution_realtime
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def test_sequence_domains_and_authoritative_snapshot(db_session, test_user) -> None:
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Lifecycle test",
            trigger_event_source="github",
            trigger_event_types=["push"],
            prompt_template="review",
            agent_type="codex",
            agent_config={},
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="RUNNING",
        ),
    )
    assert execution_realtime.allocate_sequence(
        db_session, str(execution.id), lifecycle=True
    ) == (1, 1)
    assert execution_realtime.allocate_sequence(db_session, str(execution.id)) == (2, 1)
    assert execution_realtime.allocate_sequence(
        db_session, str(execution.id), lifecycle=True
    ) == (3, 2)
    snapshot = execution_realtime.lifecycle_snapshot(
        db_session, execution.id, test_user.account_id
    )
    assert snapshot["seq"] == 3
    assert snapshot["lifecycle_seq"] == 2
    assert snapshot["status"] == "RUNNING"
    assert isinstance(snapshot["start_time"], str)
    assert snapshot["stop_requested_at"] is None
    assert (
        execution_realtime.lifecycle_snapshot(db_session, execution.id, uuid4()) is None
    )


def test_concurrent_publishers_share_atomic_sequence(db_engine) -> None:
    """Independent API/worker sessions cannot reuse a cursor or skip increments."""
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import Session
    from preloop.models.crud import crud_account

    with Session(db_engine) as db:
        account = crud_account.create(
            db, obj_in={"organization_name": "Realtime concurrency", "is_active": True}
        )
        account_id = account.id
        flow = crud_flow.create(
            db,
            flow_in=FlowCreate(
                name="Concurrent realtime",
                trigger_event_source="github",
                trigger_event_types=["push"],
                prompt_template="review",
                agent_type="codex",
                agent_config={},
                account_id=account_id,
            ),
            account_id=account_id,
        )
        flow_id = flow.id
        execution = crud_flow_execution.create(
            db, obj_in=FlowExecutionCreate(flow_id=flow_id, status="RUNNING")
        )
        execution_id = execution.id
        db.commit()

    def publish(index: int) -> tuple[int, int]:
        with Session(db_engine) as db:
            return execution_realtime.allocate_sequence(
                db, str(execution_id), lifecycle=index % 2 == 0
            )

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            allocated = list(pool.map(publish, range(20)))
        assert sorted(seq for seq, _ in allocated) == list(range(1, 21))
        with Session(db_engine) as db:
            snapshot = execution_realtime.lifecycle_snapshot(
                db, execution_id, account_id
            )
        assert snapshot["seq"] == 20
        assert snapshot["lifecycle_seq"] == 10
    finally:
        with Session(db_engine) as db:
            crud_flow_execution.delete(db, id=execution_id)
            crud_flow.remove(db, id=flow_id, account_id=account_id)
            crud_account.delete(db, id=account_id)
