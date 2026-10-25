"""Real PostgreSQL lock-order tests for artifact PUTs (prod deadlock 2026-10-19).

Billing, hosted spend, halt admission and retention lock the account row first
and then touch the execution (an UPDATE, or a child insert whose foreign key
takes KEY SHARE on it). An artifact PUT that locked the execution first and
the account second closed the cycle and PostgreSQL aborted it with
DeadlockDetected. Each test runs in its own random schema.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import cast

import pytest
from sqlalchemy import Table, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import flow_artifact
from preloop.models.crud.billing import billing as crud_billing


@pytest.fixture
def lock_database(db_engine: Engine) -> Iterator[Engine]:
    """Account, flow, execution and usage tables in an isolated schema."""
    if db_engine.dialect.name != "postgresql":
        pytest.skip("Row lock ordering needs PostgreSQL")
    schema = "artifact_lock_" + uuid.uuid4().hex
    engine = create_engine(
        db_engine.url, connect_args={"options": f"-csearch_path={schema},public"}
    )
    pending: list[Table] = [
        cast(Table, model.__table__)
        for model in (models.Account, models.FlowExecution, models.ApiUsage)
    ]
    tables: dict[str, Table] = {}
    while pending:
        table = pending.pop()
        if table.name in tables:
            continue
        tables[table.name] = table
        pending.extend(fk.column.table for fk in table.foreign_keys)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        models.Base.metadata.create_all(
            connection, tables=list(tables.values()), checkfirst=False
        )
    try:
        yield engine
    finally:
        engine.dispose()
        with db_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
def running_execution(lock_database: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    with Session(lock_database) as db:
        account = models.Account(organization_name="Synthetic lock order")
        db.add(account)
        db.flush()
        flow = models.Flow(
            account_id=account.id,
            name="Synthetic flow",
            prompt_template="Synthetic",
            agent_config={},
        )
        db.add(flow)
        db.flush()
        execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
        db.add(execution)
        db.commit()
        return account.id, execution.id


def _usage_insert(db: Session, account_id: uuid.UUID, execution_id: uuid.UUID) -> None:
    """Gateway usage row: its foreign key takes KEY SHARE on the execution."""
    db.add(
        models.ApiUsage(
            account_id=account_id,
            endpoint="/v1/messages",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            flow_execution_id=execution_id,
        )
    )
    db.flush()


def _execution_update(
    db: Session, account_id: uuid.UUID, execution_id: uuid.UUID
) -> None:
    """Launch/stop admission: account lock, then an execution UPDATE."""
    db.execute(
        text("UPDATE flow_execution SET updated_at = now() WHERE id = :id"),
        {"id": execution_id},
    )


def _wait_until_blocked(engine: Engine, pid: int) -> None:
    deadline = time.monotonic() + 5
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).scalar()
            if waiting == "Lock":
                return
            time.sleep(0.02)
    pytest.fail("artifact PUT never waited on the account lock")


@pytest.mark.parametrize("second_step", [_usage_insert, _execution_update])
def test_artifact_put_and_account_first_writer_do_not_deadlock(
    lock_database: Engine,
    running_execution: tuple[uuid.UUID, uuid.UUID],
    second_step: Callable[[Session, uuid.UUID, uuid.UUID], None],
) -> None:
    account_id, execution_id = running_execution
    errors: list[BaseException] = []
    pid: dict[str, int] = {}
    ready = threading.Event()

    def artifact_put() -> None:
        try:
            with Session(lock_database) as db:
                db.execute(text("SET deadlock_timeout = '100ms'"))
                db.execute(text("SET lock_timeout = '10s'"))
                pid["value"] = int(db.execute(text("SELECT pg_backend_pid()")).scalar())
                ready.set()
                flow_artifact._lock_for_put(
                    db,
                    account_id=account_id,
                    execution_id=execution_id,
                    require_execution_open=True,
                )
                db.commit()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
            ready.set()

    with Session(lock_database) as billing:
        billing.execute(text("SET deadlock_timeout = '100ms'"))
        billing.execute(text("SET lock_timeout = '10s'"))
        crud_billing.lock_account(billing, str(account_id))
        worker = threading.Thread(target=artifact_put)
        worker.start()
        assert ready.wait(5)
        _wait_until_blocked(lock_database, pid["value"])
        try:
            second_step(billing, account_id, execution_id)
            billing.commit()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
            billing.rollback()
        worker.join(15)
    assert not worker.is_alive()
    assert errors == []
