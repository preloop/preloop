"""Real PostgreSQL regressions for chain initialization and head locking."""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud.audit_chain import FOREIGN_KEY_VIOLATION, postgres_sqlstate
from preloop.services import audit_chain


@pytest.fixture
def committed_account(db_engine: Engine) -> Iterator[UUID]:
    """Make only this account visible to independent transactions, then remove it."""
    account_id = uuid4()
    with Session(db_engine) as db:
        db.add(models.Account(id=account_id, organization_name="chain-race-fixture"))
        db.commit()
    try:
        yield account_id
    finally:
        with Session(db_engine) as db:
            db.execute(delete(models.Account).where(models.Account.id == account_id))
            db.commit()


def _race_initializers(db_engine: Engine, operation: Callable[[], Any]) -> list[Any]:
    """Both callers reach their first INSERT before either can create the row.

    A barrier at thread startup would not prove they both observed a missing
    row. At INSERT, it also deterministically exposes the old select/add race.
    """
    barrier = Barrier(2, timeout=10)

    def before_insert(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("INSERT INTO audit_chain_state "):
            barrier.wait()

    event.listen(db_engine, "before_cursor_execute", before_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(operation) for _ in range(2)]
            return [future.result(timeout=20) for future in futures]
    finally:
        event.remove(db_engine, "before_cursor_execute", before_insert)


def test_concurrent_genesis_preserves_caller_transactions(
    db_engine: Engine, committed_account: UUID
) -> None:
    """The losing creator must retain its savepoint and unrelated pending work."""

    def initialize() -> UUID:
        with Session(db_engine, autoflush=False) as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            with db.begin_nested() as savepoint:
                row = models.AuditLog(
                    account_id=committed_account,
                    action="permission_check",
                    resource_type="tool",
                    status="success",
                )
                db.add(row)
                state = audit_chain.get_state(
                    db, account_id=committed_account, for_update=True
                )
                assert state is not None
                assert state.last_seq == 0
                assert state.last_hash == models.GENESIS_HASH
                assert savepoint.is_active
                assert db.scalar(select(1)) == 1
                state_id = state.id
            db.commit()
            return state_id

    state_ids = _race_initializers(db_engine, initialize)
    assert state_ids[0] == state_ids[1]
    with Session(db_engine) as db:
        states = db.scalars(
            select(models.AuditChainState).where(
                models.AuditChainState.account_id == committed_account
            )
        ).all()
        rows = db.scalars(
            select(models.AuditLog).where(
                models.AuditLog.account_id == committed_account
            )
        ).all()
        assert len(states) == 1
        assert len(rows) == 2


def test_concurrent_first_sealers_allocate_one_contiguous_chain(
    db_engine: Engine, committed_account: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two first batches serialize head allocation, including checkpoint writes."""
    monkeypatch.setattr(settings, "audit_chain_checkpoint_interval", 2)
    with Session(db_engine) as db:
        for index in range(4):
            db.add(
                models.AuditLog(
                    account_id=committed_account,
                    action="permission_check",
                    resource_type="tool",
                    resource_id=f"tool-{index}",
                    status="success",
                    timestamp=datetime.now(UTC).replace(tzinfo=None)
                    - timedelta(minutes=10 - index),
                )
            )
        db.commit()

    def seal() -> int:
        with Session(db_engine, autoflush=False) as db:
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            result = audit_chain.seal_account(
                db,
                account_id=committed_account,
                batch_size=2,
                max_batches=1,
                lag=timedelta(0),
            )
            return result.sealed

    assert sorted(_race_initializers(db_engine, seal)) == [2, 2]
    with Session(db_engine) as db:
        report = audit_chain.verify_chain(db, account_id=committed_account)
        assert report["status"] == audit_chain.STATUS_OK
        assert report["head_seq"] == report["checked_rows"] == 4
        checkpoints = db.scalars(
            select(models.AuditChainCheckpoint)
            .where(models.AuditChainCheckpoint.account_id == committed_account)
            .order_by(models.AuditChainCheckpoint.seq)
        ).all()
        assert [checkpoint.seq for checkpoint in checkpoints] == [2, 4]


def test_locked_state_refreshes_previous_identity_map_read(
    db_engine: Engine, committed_account: UUID
) -> None:
    """A read before a competing commit cannot make a locked head go backwards."""
    with Session(db_engine) as db:
        audit_chain.get_state(db, account_id=committed_account)
        db.commit()
    with Session(db_engine, autoflush=False) as reader, Session(db_engine) as writer:
        stale = audit_chain.get_state(reader, account_id=committed_account)
        assert stale.last_seq == 0
        head = audit_chain.get_state(
            writer, account_id=committed_account, for_update=True
        )
        stamp = datetime.now(UTC)
        head.last_seq = 9
        head.last_hash = "a" * 64
        head.last_sealed_at = stamp
        head.pruned_below_seq = 3
        head.pruned_at = stamp
        writer.commit()

        locked = audit_chain.get_state(
            reader, account_id=committed_account, for_update=True
        )
        assert locked is stale
        assert locked.last_seq == 9
        assert locked.last_hash == "a" * 64
        assert locked.last_sealed_at == stamp
        assert locked.pruned_below_seq == 3
        assert locked.pruned_at == stamp
        # A second call in this transaction must preserve pending pruning.
        audit_chain.note_pruned(
            reader, account_id=committed_account, up_to_seq=6, now=stamp
        )
        again = audit_chain.get_state(
            reader, account_id=committed_account, for_update=True
        )
        assert again.last_seq == 9
        assert again.last_hash == "a" * 64
        assert again.pruned_below_seq == 6
        reader.rollback()


def test_initialization_obeys_savepoint_and_outer_rollback(
    db_engine: Engine, committed_account: UUID
) -> None:
    """Creating a head never commits or rolls back work owned by the caller."""
    with Session(db_engine, autoflush=False) as db:
        db.execute(text("SELECT 1"))
        with db.begin_nested() as savepoint:
            assert audit_chain.get_state(db, account_id=committed_account) is not None
            savepoint.rollback()
        assert (
            audit_chain.get_state(db, account_id=committed_account, create=False)
            is None
        )
        assert audit_chain.get_state(db, account_id=committed_account) is not None
        db.rollback()
    with Session(db_engine) as db:
        assert (
            audit_chain.get_state(db, account_id=committed_account, create=False)
            is None
        )


def test_initialization_propagates_unrelated_integrity_errors(
    db_engine: Engine,
) -> None:
    """Conflict handling is limited to a duplicate account head, never bad FKs."""
    with Session(db_engine) as db:
        with pytest.raises(IntegrityError) as error:
            with db.begin_nested():
                audit_chain.get_state(db, account_id=uuid4(), for_update=True)
        assert postgres_sqlstate(error.value) == FOREIGN_KEY_VIOLATION
        assert db.scalar(select(1)) == 1


def test_postgres_sqlstate_reads_pgcode_or_sqlstate() -> None:
    """CI's sync driver exposes pgcode; psycopg3 exposes sqlstate. Neither both."""
    pgcode_only = IntegrityError("INSERT", {}, SimpleNamespace(pgcode="23503"))
    sqlstate_only = IntegrityError("INSERT", {}, SimpleNamespace(sqlstate="23503"))
    neither = IntegrityError("INSERT", {}, object())
    assert postgres_sqlstate(pgcode_only) == FOREIGN_KEY_VIOLATION
    assert postgres_sqlstate(sqlstate_only) == FOREIGN_KEY_VIOLATION
    assert postgres_sqlstate(neither) is None
