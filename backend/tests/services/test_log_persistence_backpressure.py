"""Exercise batching, pool saturation and retry safety against a local DB."""

import asyncio
import threading
import uuid
from collections.abc import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from preloop.services import websocket_manager as logs


@pytest.fixture
def log_database(tmp_path, monkeypatch):
    """Use a deliberately tiny local pool, never a configured deployment DB."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'logs.sqlite'}",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.01,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE flow_execution_log (id CHAR(32) PRIMARY KEY, "
                "execution_id CHAR(32), timestamp DATETIME, log_type VARCHAR(50), "
                "message TEXT, metadata JSON, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )

    def sessions() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(logs, "get_db", sessions)
    monkeypatch.setattr(logs, "LOG_PERSIST_BASE_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(logs, "LOG_PERSIST_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(logs, "LOG_BATCH_WAIT_SECONDS", 0.01)
    with patch.object(logs, "notify_admins") as notify:
        yield engine, notify
    engine.dispose()


@pytest.mark.asyncio
async def test_saturated_pool_retains_logs_and_backpressures_producers(
    log_database, monkeypatch
) -> None:
    engine, notify = log_database
    monkeypatch.setattr(logs, "LOG_QUEUE_MAX_SIZE", 32)
    monkeypatch.setattr(logs, "LOG_BATCH_MAX_SIZE", 16)
    queue = logs.get_log_queue()
    exhausted_cycle = threading.Event()
    original_insert = logs._sync_batch_insert_logs
    batch_sizes: list[int] = []

    def insert(batch: list) -> bool:
        batch_sizes.append(len(batch))
        try:
            return original_insert(batch)
        except SQLAlchemyTimeoutError:
            exhausted_cycle.set()
            raise

    monkeypatch.setattr(logs, "_sync_batch_insert_logs", insert)
    connections = [engine.connect()]
    worker = asyncio.create_task(logs._log_writer_worker())
    execution_ids = [str(uuid.uuid4()) for _ in range(40)]

    async def producer(execution_id: str) -> None:
        for line in range(100):
            await logs.persist_execution_log(
                execution_id, {"type": "agent_log_line", "message": str(line)}
            )

    producers = [
        asyncio.create_task(producer(execution_id)) for execution_id in execution_ids
    ]
    try:
        async with asyncio.timeout(5):
            while not exhausted_cycle.is_set():
                await asyncio.sleep(0.005)
        assert queue.qsize() == 32
        assert not all(task.done() for task in producers)
        assert engine.pool.checkedout() == 1
        for connection in connections:
            connection.close()
        connections.clear()
        await asyncio.wait_for(asyncio.gather(*producers), 10)
        await asyncio.wait_for(queue.join(), 10)
        with engine.connect() as connection:
            count = connection.scalar(text("SELECT COUNT(*) FROM flow_execution_log"))
            executions = connection.scalar(
                text("SELECT COUNT(DISTINCT execution_id) FROM flow_execution_log")
            )
        assert count == 4000
        assert executions == 40
        assert max(batch_sizes) <= 16
        assert engine.pool.checkedout() == 0
        notify.assert_not_called()
    finally:
        for connection in connections:
            connection.close()
        for task in producers:
            task.cancel()
        await asyncio.gather(*producers, return_exceptions=True)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        logs._log_queues.pop(asyncio.get_running_loop(), None)


def test_retry_after_ambiguous_commit_does_not_duplicate_logs(
    log_database, monkeypatch
) -> None:
    engine, notify = log_database
    original_commit = Session.commit
    commits = 0

    def commit(session: Session) -> None:
        nonlocal commits
        original_commit(session)
        commits += 1
        if commits == 1:
            raise OperationalError("COMMIT", {}, Exception("connection dropped"))

    monkeypatch.setattr(Session, "commit", commit)
    secret = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
    batch = [
        (str(uuid.uuid4()), {"message": f"https://{secret}@example.com/repository"}),
        (str(uuid.uuid4()), {"type": "agent_log_line", "payload": {"line": "ok"}}),
    ]
    assert logs._sync_batch_insert_logs(batch)
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text("SELECT message FROM flow_execution_log ORDER BY message")
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert "ok" in rows
    assert all(secret not in message for message in rows)
    assert commits == 2
    assert engine.pool.checkedout() == 0
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_coalesces_lines_from_separate_event_loop_ticks(monkeypatch) -> None:
    monkeypatch.setattr(logs, "LOG_BATCH_WAIT_SECONDS", 0.05)
    batches = []
    monkeypatch.setattr(
        logs, "_sync_batch_insert_logs", lambda batch: batches.append(batch)
    )
    worker = asyncio.create_task(logs._log_writer_worker())
    queue = logs.get_log_queue()
    try:
        for index in range(10):
            await logs.persist_execution_log("execution", {"message": str(index)})
            await asyncio.sleep(0.001)
        await asyncio.wait_for(queue.join(), 2)
        assert len(batches) == 1
        assert len(batches[0]) == 10
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        logs._log_queues.pop(asyncio.get_running_loop(), None)


@pytest.mark.asyncio
async def test_worker_cancellation_waits_for_in_flight_transaction(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(logs, "LOG_BATCH_WAIT_SECONDS", 0)

    def insert(batch: list) -> bool:
        started.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(logs, "_sync_batch_insert_logs", insert)
    queue = logs.get_log_queue()
    await logs.persist_execution_log("execution", {"message": "in flight"})
    worker = asyncio.create_task(logs._log_writer_worker())
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        worker.cancel()
        await asyncio.sleep(0.01)
        assert not worker.done()
        worker.cancel()
        await asyncio.sleep(0.01)
        assert not worker.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
        await asyncio.wait_for(queue.join(), 2)
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)
        logs._log_queues.pop(asyncio.get_running_loop(), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["coalescing", "retry_backoff"])
async def test_worker_restart_recovers_dequeued_batch(phase, monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        logs, "LOG_BATCH_WAIT_SECONDS", 60 if phase == "coalescing" else 0
    )
    monkeypatch.setattr(logs, "LOG_PERSIST_RETRY_SECONDS", 60)
    failed = threading.Event()
    written: list[tuple[str, dict]] = []

    def unavailable(batch: list) -> bool:
        failed.set()
        raise SQLAlchemyTimeoutError("pool exhausted")

    monkeypatch.setattr(logs, "_sync_batch_insert_logs", unavailable)
    queue = logs.get_log_queue()
    await logs.persist_execution_log("execution", {"message": "retained"})
    worker = asyncio.create_task(logs._log_writer_worker())
    try:
        async with asyncio.timeout(2):
            while not logs._log_batches.get(loop) or (
                phase == "retry_backoff" and not failed.is_set()
            ):
                await asyncio.sleep(0.001)
        # Allow the worker to enter its async backoff after the thread finishes.
        await asyncio.sleep(0.01)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert len(logs._log_batches[loop]) == 1
        monkeypatch.setattr(logs, "LOG_BATCH_WAIT_SECONDS", 0)
        monkeypatch.setattr(
            logs, "_sync_batch_insert_logs", lambda batch: written.extend(batch)
        )
        worker = asyncio.create_task(logs._log_writer_worker())
        await asyncio.wait_for(queue.join(), 2)
        assert len(written) == 1
        assert written[0][1]["message"] == "retained"
        assert loop not in logs._log_batches
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        logs._log_queues.pop(loop, None)
        logs._log_batches.pop(loop, None)
