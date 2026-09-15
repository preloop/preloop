"""Tests for the bounded queue that drives the session embedding worker.

The point of the queue is what it refuses to do: it never blocks the request
thread that wrote the chunks, so a saturated queue drops submissions. Dropping
is safe because the backlog is durable in the corpus, and that is asserted
here rather than assumed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import List, Sequence
from unittest.mock import patch

from preloop.config import settings
from preloop.models.crud import (
    crud_runtime_session,
    crud_session_embedding_setting,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.session_embedding_setting import (
    EMBEDDING_SCOPE_FULL,
    PROVIDER_OPENAI_COMPATIBLE,
)
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_STATE_PENDING,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.services.session_embedding import run_account_batch
from preloop.services.session_embedding_queue import (
    SessionEmbeddingQueue,
    get_session_embedding_queue,
    reset_session_embedding_queue,
    submit_account_for_embedding,
)

OCCURRED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeProvider:
    """Returns deterministic vectors of the declared width."""

    model = "text-embedding-3-small"

    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self.last_usage = {"prompt_tokens": 40, "total_tokens": 40}

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        self.calls.append(list(texts))
        return [[0.01] * EMBEDDING_DIMENSIONS for _ in texts]


class _KeepOpen:
    """Hand the worker a session it may not close."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        return None


def _chunks(db_session, account_id, *, count=2, source_id="message-1"):
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id="queue-session",
        session_reference="queue-session",
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=OCCURRED_AT,
        last_activity_at=OCCURRED_AT,
    )
    crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        occurred_at=OCCURRED_AT,
        chunks=[
            SessionSearchChunk(
                content=f"chunk {index} about a deployment rollback",
                chunk_index=index,
                role="user",
                redaction_state="clear",
            )
            for index in range(count)
        ],
    )
    db_session.commit()
    return session


def test_a_saturated_queue_drops_work_rather_than_blocking():
    """The bound is real: the submission after the last slot is refused."""
    queue = SessionEmbeddingQueue(max_pending=1)

    assert queue.submit(uuid.uuid4()) is True
    assert queue.submit(uuid.uuid4()) is False

    assert queue.dropped == 1
    assert queue.submitted == 1
    assert queue.pending == 1


def test_a_dropped_submission_leaves_the_chunks_pending(db_session, test_user):
    """Dropping costs a delay, never a vector: the backlog is in the table."""
    account_id = str(test_user.account_id)
    _chunks(db_session, account_id)
    queue = SessionEmbeddingQueue(max_pending=1)
    queue.submit(uuid.uuid4())

    assert queue.submit(account_id) is False

    pending = crud_session_search_document.count_pending_embeddings(
        db_session, account_id=account_id
    )
    assert pending == 2


def test_one_busy_account_cannot_fill_the_queue_with_its_own_id():
    """Deduplication is what keeps a noisy account from starving the rest."""
    queue = SessionEmbeddingQueue(max_pending=4)
    account = uuid.uuid4()
    other = uuid.uuid4()

    assert queue.submit(account) is True
    assert queue.submit(account) is False
    assert queue.submit(account) is False
    assert queue.submit(other) is True

    assert queue.deduplicated == 2
    assert queue.dropped == 0
    assert queue.pending == 2


def test_a_queued_account_is_embedded_on_the_workers_own_session(db_session, test_user):
    """The write path hands over an id; the worker opens its own session."""
    account_id = str(test_user.account_id)
    _chunks(db_session, account_id)
    crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
        # This test is about the queue, and its fixture is transcript
        # chunks, so it names the scope that embeds them.
        scope=EMBEDDING_SCOPE_FULL,
    )
    db_session.commit()

    queue = SessionEmbeddingQueue(max_pending=4)
    queue.submit(account_id)
    provider = FakeProvider()

    def _worker_session():
        yield _KeepOpen(db_session)

    with (
        patch(
            "preloop.services.session_embedding_queue.get_db_session",
            side_effect=lambda: _worker_session(),
        ),
        patch(
            "preloop.services.session_embedding_queue.run_account_batch",
            side_effect=lambda db, *, account_id: run_account_batch(
                db, account_id=account_id, provider=provider
            ),
        ),
    ):
        assert queue.drain() == 2

    assert queue.embedded == 2
    assert queue.pending == 0
    assert (
        crud_session_search_document.count_embedded_for_account(
            db_session, account_id=account_id
        )
        == 2
    )


def test_a_failing_batch_is_isolated_from_the_queue():
    """One bad account must not stop the worker for every other account."""
    queue = SessionEmbeddingQueue(max_pending=2)
    queue.submit(uuid.uuid4())

    class _Session:
        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    def _worker_session():
        yield _Session()

    with (
        patch(
            "preloop.services.session_embedding_queue.get_db_session",
            side_effect=lambda: _worker_session(),
        ),
        patch(
            "preloop.services.session_embedding_queue.run_account_batch",
            side_effect=RuntimeError("provider exploded"),
        ),
    ):
        assert queue.drain() == 0

    assert queue.failed == 1
    assert queue.pending == 0


def test_the_kill_switch_stops_submissions_at_the_door(monkeypatch):
    """Nothing is queued while embedding is off, so nothing is dropped."""
    monkeypatch.setattr(settings, "session_embedding_enabled", False)
    reset_session_embedding_queue()

    assert submit_account_for_embedding(uuid.uuid4()) is False
    assert get_session_embedding_queue().pending == 0


def test_the_process_queue_depth_follows_settings(monkeypatch):
    """The bound is a deployment knob, not a number buried in the class."""
    monkeypatch.setattr(settings, "session_embedding_queue_max_pending", 1)
    reset_session_embedding_queue()
    queue = get_session_embedding_queue()

    assert queue.submit(uuid.uuid4()) is True
    assert queue.submit(uuid.uuid4()) is False
    assert queue.dropped == 1
    reset_session_embedding_queue()


def test_indexing_a_source_hands_the_account_to_the_queue(db_session, test_user):
    """The corpus write nudges the worker instead of embedding inline."""
    from preloop.services import session_search_index

    account_id = str(test_user.account_id)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id="handoff-session",
        session_reference="handoff-session",
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=OCCURRED_AT,
        last_activity_at=OCCURRED_AT,
    )
    db_session.commit()

    with patch(
        "preloop.services.session_embedding_queue.submit_account_for_embedding"
    ) as submit:
        stored = session_search_index.index_transcript_message(
            db_session,
            account_id=account_id,
            runtime_session_id=session.id,
            source_id="message-handoff",
            text="a message long enough to be worth a vector",
            role="user",
            occurred_at=OCCURRED_AT,
            commit=True,
        )

    assert stored
    assert all(row.embedding_state == EMBEDDING_STATE_PENDING for row in stored)
    submit.assert_called_once_with(account_id)


def test_a_commit_false_write_does_not_nudge_before_the_host_commits(
    db_session, test_user
):
    """A nudge while the writer still holds the transaction is wasted work."""
    from preloop.services import session_search_index

    account_id = str(test_user.account_id)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id="early-nudge-session",
        session_reference="early-nudge-session",
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=OCCURRED_AT,
        last_activity_at=OCCURRED_AT,
    )
    db_session.commit()

    with patch(
        "preloop.services.session_embedding_queue.submit_account_for_embedding"
    ) as submit:
        stored = session_search_index.index_transcript_message(
            db_session,
            account_id=account_id,
            runtime_session_id=session.id,
            source_id="message-early-nudge",
            text="indexed inside the caller's open transaction",
            role="user",
            occurred_at=OCCURRED_AT,
            commit=False,
        )

    assert stored
    submit.assert_not_called()


def test_ingest_push_records_nudges_embedding_after_the_host_commits(
    db_session, test_user
):
    """Imported transcripts must not wait for an unrelated later write."""
    from decimal import Decimal

    from preloop.models.crud import crud_managed_agent
    from preloop.schemas.usage_import import (
        UsageIngestRecord,
        UsageIngestTranscriptMessage,
    )
    from preloop.services.usage_import import ingest_push_records

    account_id = str(test_user.account_id)
    agent = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=account_id,
        runtime_session_id=None,
        session_source_type="desktop_agent",
        session_source_id="cursor-embed-ingest",
        display_name="Cursor",
        agent_kind="cursor",
    )
    db_session.commit()
    record = UsageIngestRecord(
        external_id="turn-embed-1",
        timestamp=OCCURRED_AT,
        model="composer",
        charged_cost=Decimal("0.10"),
        input_tokens=12,
        output_tokens=8,
        conversation_id="conv-embed",
        transcript=[
            UsageIngestTranscriptMessage(
                role="user",
                text="a transcript chunk that should be embedded after commit",
            )
        ],
    )

    with patch("preloop.services.usage_import.request_embedding") as nudge:
        ingest_push_records(
            db_session,
            account_id=account_id,
            user_id=str(test_user.id),
            agent=agent,
            records=[record],
            source="cursor",
        )

    nudge.assert_called_once_with(account_id)


def test_ingest_push_records_without_a_transcript_does_not_nudge(db_session, test_user):
    """A ledger-only import has no chunks to embed."""
    from decimal import Decimal

    from preloop.models.crud import crud_managed_agent
    from preloop.schemas.usage_import import UsageIngestRecord
    from preloop.services.usage_import import ingest_push_records

    account_id = str(test_user.account_id)
    agent = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=account_id,
        runtime_session_id=None,
        session_source_type="desktop_agent",
        session_source_id="cursor-embed-ledger",
        display_name="Cursor",
        agent_kind="cursor",
    )
    db_session.commit()
    record = UsageIngestRecord(
        external_id="turn-ledger-1",
        timestamp=OCCURRED_AT,
        model="composer",
        charged_cost=Decimal("0.10"),
        input_tokens=12,
        output_tokens=8,
        conversation_id="conv-ledger",
    )

    with patch("preloop.services.usage_import.request_embedding") as nudge:
        ingest_push_records(
            db_session,
            account_id=account_id,
            user_id=str(test_user.id),
            agent=agent,
            records=[record],
            source="cursor",
        )

    nudge.assert_not_called()


def test_drain_continues_an_account_that_exceeds_one_batch(
    db_session, test_user, monkeypatch
):
    """A quiet account with more than one batch must not wait for the next write."""
    account_id = str(test_user.account_id)
    _chunks(db_session, account_id, count=5)
    crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
        # This test is about the queue, and its fixture is transcript
        # chunks, so it names the scope that embeds them.
        scope=EMBEDDING_SCOPE_FULL,
    )
    db_session.commit()
    monkeypatch.setattr(settings, "session_embedding_batch_size", 2, raising=False)

    queue = SessionEmbeddingQueue(max_pending=4)
    queue.submit(account_id)
    provider = FakeProvider()

    def _worker_session():
        yield _KeepOpen(db_session)

    with (
        patch(
            "preloop.services.session_embedding_queue.get_db_session",
            side_effect=lambda: _worker_session(),
        ),
        patch(
            "preloop.services.session_embedding_queue.run_account_batch",
            side_effect=lambda db, *, account_id: run_account_batch(
                db, account_id=account_id, provider=provider
            ),
        ),
    ):
        embedded = queue.drain()

    assert embedded == 5
    assert queue.embedded == 5
    assert queue.pending == 0
    assert (
        crud_session_search_document.count_embedded_for_account(
            db_session, account_id=account_id
        )
        == 5
    )
    assert len(provider.calls) == 3
