"""Real PostgreSQL coverage for bounded, isolated triage lock connections."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models.crud import crud_issue_lifecycle
from preloop.models.db.triage_lock import _lock_pool, triage_lock_connection
from preloop.services.issue_triage_controller import TriageControllerError, _serialized


def test_lock_capacity_isolated_from_saturated_data_pool(db_engine: Engine) -> None:
    source = create_engine(
        db_engine.url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=0,
        connect_args={"options": "-c application_name=triage-lock-test"},
    )
    checkouts = []
    event.listen(source.pool, "checkout", lambda *args: checkouts.append(True))
    try:
        # Real caller sessions already occupy every data-pool slot.
        with source.connect() as first, source.connect() as second:
            with triage_lock_connection(source) as lock1:
                with triage_lock_connection(source) as lock2:
                    assert source.pool.checkedout() == 2
                    assert (
                        lock1.scalar(text("SHOW application_name"))
                        == "triage-lock-test"
                    )
                    assert lock2.scalar(text("SELECT 1")) == 1
                    with pytest.raises(
                        ValueError, match="triage_operation_in_progress"
                    ):
                        with triage_lock_connection(source):
                            pytest.fail("A third lock exceeded admission capacity")
                    assert first.scalar(text("SELECT 1")) == 1
                    assert second.scalar(text("SELECT 1")) == 1
            # Checkout events were retained on the recreated pool as well.
            assert len(checkouts) == 4
        with pytest.raises(RuntimeError):
            with triage_lock_connection(source):
                raise RuntimeError("release capacity on failure")
        with triage_lock_connection(source) as recovered:
            assert recovered.scalar(text("SELECT 1")) == 1
        state = _lock_pool(source)
        assert state.engine.pool.checkedin() > 0
        source.dispose()
        assert state.engine.pool.checkedin() == 0
    finally:
        source.dispose()


def test_connection_failure_releases_lock_admission(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = create_engine(db_engine.url, pool_size=1, max_overflow=0, pool_timeout=0)
    try:
        state = _lock_pool(source)

        def unavailable() -> None:
            raise RuntimeError("connection unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(state.engine, "connect", unavailable)
            with pytest.raises(RuntimeError, match="connection unavailable"):
                with triage_lock_connection(source):
                    pytest.fail("Connection acquisition must fail")
        with triage_lock_connection(source) as recovered:
            assert recovered.scalar(text("SELECT 1")) == 1
    finally:
        source.dispose()


@pytest.mark.asyncio
async def test_capacity_error_is_retryable_and_rolls_back_caller(
    db_engine: Engine,
) -> None:
    source = create_engine(db_engine.url, pool_size=1, max_overflow=0, pool_timeout=0)
    try:
        with Session(source) as caller:
            caller.execute(text("SELECT 1"))
            with triage_lock_connection(source):
                with pytest.raises(
                    TriageControllerError, match="triage_operation_in_progress"
                ):
                    async with _serialized(caller, uuid4(), uuid4()):
                        pytest.fail(
                            "Saturated lock capacity must reject before checkout"
                        )
                assert not caller.in_transaction()
                assert source.pool.checkedout() == 0
            async with crud_issue_lifecycle.triage_locked(caller, uuid4(), uuid4()):
                caller.execute(text("SELECT 1"))
                caller.commit()
                assert _lock_pool(source).engine.pool.checkedout() == 1
            assert _lock_pool(source).engine.pool.checkedout() == 0
    finally:
        source.dispose()
