"""Tests for embedding session search chunks in bounded, capped batches.

One test per acceptance criterion of issue #654, plus the provider and
pricing behaviour they depend on. No test reaches a provider: every run
passes a fake that records what it was asked to embed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import List, Sequence

import pytest

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_api_usage,
    crud_runtime_session,
    crud_session_embedding_setting,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.session_embedding_setting import (
    DEGRADED_DAILY_CAP,
    DEGRADED_DIMENSION_MISMATCH,
    DEGRADED_PROVIDER_ERROR,
    DEGRADED_UNPRICED_MODEL,
    EMBEDDING_SCOPE_FULL,
    EMBEDDING_SCOPE_SUMMARIES_ONLY,
    PROVIDER_OPENAI_COMPATIBLE,
)
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_STATE_EMBEDDED,
    EMBEDDING_STATE_FAILED,
    EMBEDDING_STATE_IN_PROGRESS,
    EMBEDDING_STATE_PENDING,
    REDACTION_STATE_METADATA_ONLY,
    REDACTION_STATE_REDACTED,
    SOURCE_KIND_SESSION_SUMMARY,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.services import session_embedding
from preloop.services.session_embedding import (
    STATUS_DEGRADED,
    STATUS_DISABLED,
    STATUS_IDLE,
    STATUS_OK,
    EmbeddingProviderError,
    build_provider,
    run_account_batch,
)

OCCURRED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeProvider:
    """Returns deterministic vectors of the right width and counts calls."""

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-small",
        width: int = EMBEDDING_DIMENSIONS,
    ):
        self.model = model
        self.width = width
        self.calls: List[List[str]] = []
        self.last_usage = {"prompt_tokens": 120, "total_tokens": 120}

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        self.calls.append(list(texts))
        return [[0.001 * (index + 1)] * self.width for index in range(len(texts))]


class FailingProvider:
    """Refuses every batch."""

    model = "text-embedding-3-small"
    last_usage = None

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        raise EmbeddingProviderError("upstream refused")


def _account(db_session):
    """A second account, so account scoping can be asserted."""
    account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": f"Other Organization {uuid.uuid4().hex[:8]}",
            "is_active": True,
        },
    )
    db_session.flush()
    return account


def _session(db_session, account_id, *, source_id="session-a"):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=OCCURRED_AT,
        last_activity_at=OCCURRED_AT,
    )


def _chunks(
    db_session,
    *,
    account_id,
    session,
    count=3,
    redaction_state="clear",
    source_id="message-1",
    occurred_at=OCCURRED_AT,
):
    stored = crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[
            SessionSearchChunk(
                content=f"chunk {index} about a database migration",
                chunk_index=index,
                role="user",
                redaction_state=redaction_state,
            )
            for index in range(count)
        ],
    )
    db_session.commit()
    return stored


def _opt_in(db_session, account_id, **overrides):
    """Opt in, embedding every chunk unless the test says otherwise.

    The tests that use this helper are about the worker's mechanics, and
    their fixtures are transcript chunks, so they name ``full`` rather than
    inherit the ``summaries_only`` default. The default is what the scope
    tests further down assert.
    """
    kwargs = {
        "provider": PROVIDER_OPENAI_COMPATIBLE,
        "model_identifier": "text-embedding-3-small",
        "base_url": "https://embeddings.example.com/v1",
        "scope": EMBEDDING_SCOPE_FULL,
    }
    kwargs.update(overrides)
    setting = crud_session_embedding_setting.enable(
        db_session, account_id=account_id, **kwargs
    )
    db_session.commit()
    return setting


def test_an_account_with_the_setting_off_has_zero_vectors(db_session, test_user):
    """The shipped default embeds nothing at all."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session)
    provider = FakeProvider()

    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.status == STATUS_DISABLED
    assert result.reason == "account_opt_out"
    assert provider.calls == []
    assert (
        crud_session_search_document.count_embedded_for_account(
            db_session, account_id=account_id
        )
        == 0
    )


def test_the_kill_switch_stops_embedding_but_not_keyword_indexing(
    db_session, test_user, monkeypatch
):
    """One setting stops vectors; the corpus keeps taking writes."""
    from preloop.services import session_search_index

    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _opt_in(db_session, account_id)
    monkeypatch.setattr(settings, "session_embedding_enabled", False, raising=False)
    provider = FakeProvider()

    stored = session_search_index.index_transcript_message(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_id="message-kill-switch",
        role="user",
        text="a message written while embedding is off",
        occurred_at=OCCURRED_AT,
        commit=True,
    )
    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert stored, "keyword indexing must keep working with embedding off"
    assert result.status == STATUS_DISABLED
    assert result.reason == "kill_switch"
    assert provider.calls == []
    assert all(row.embedding is None for row in stored)


def test_a_chunk_that_is_not_clean_is_never_embedded(db_session, test_user):
    """Redacted and metadata-only chunks are not what they appear to say."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _opt_in(db_session, account_id)
    redacted = _chunks(
        db_session,
        account_id=account_id,
        session=session,
        count=1,
        redaction_state=REDACTION_STATE_REDACTED,
        source_id="message-redacted",
    )
    metadata_only = _chunks(
        db_session,
        account_id=account_id,
        session=session,
        count=1,
        redaction_state=REDACTION_STATE_METADATA_ONLY,
        source_id="message-metadata",
    )
    provider = FakeProvider()

    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.status == STATUS_IDLE
    assert provider.calls == []
    for row in (*redacted, *metadata_only):
        db_session.refresh(row)
        assert row.embedding is None
        assert row.embedding_state == EMBEDDING_STATE_PENDING


def test_a_chunk_under_legal_hold_is_never_embedded(db_session, test_user):
    """A held execution freezes the session's chunks out of the queue."""
    account_id = str(test_user.account_id)
    flow = models.Flow(
        name="held-flow",
        prompt_template="test",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(
        flow_id=flow.id,
        status="COMPLETED",
        legal_hold=True,
    )
    db_session.add(execution)
    db_session.flush()

    held_session = _session(db_session, account_id, source_id="session-held")
    free_session = _session(db_session, account_id, source_id="session-free")
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=account_id,
        runtime_session_id=str(held_session.id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
    )
    held_chunks = _chunks(
        db_session,
        account_id=account_id,
        session=held_session,
        count=2,
        source_id="message-held",
        occurred_at=OCCURRED_AT - timedelta(hours=1),
    )
    free_chunks = _chunks(
        db_session,
        account_id=account_id,
        session=free_session,
        count=2,
        source_id="message-free",
    )
    _opt_in(db_session, account_id)
    provider = FakeProvider()

    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.status == STATUS_OK
    assert result.runtime_session_id == str(free_session.id)
    for row in held_chunks:
        db_session.refresh(row)
        assert row.embedding is None
        assert row.embedding_state == EMBEDDING_STATE_PENDING
    for row in free_chunks:
        db_session.refresh(row)
        assert row.embedding is not None


def test_each_chunk_records_the_model_identity_that_produced_its_vector(
    db_session, test_user
):
    """A vector nobody can attribute to a model is a vector nobody can redo."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=2)
    _opt_in(db_session, account_id)

    result = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider()
    )

    assert result.status == STATUS_OK
    assert result.embedded == 2
    expected = f"openai_compatible:text-embedding-3-small@{EMBEDDING_DIMENSIONS}"
    for row in stored:
        db_session.refresh(row)
        assert row.embedding_model == expected
        assert row.embedding_state == EMBEDDING_STATE_EMBEDDED
        assert row.embedded_at is not None
        assert len(row.embedding) == EMBEDDING_DIMENSIONS


def test_reaching_the_daily_cap_leaves_chunks_pending_and_degrades(
    db_session, test_user
):
    """A cap is a stopping condition, not a failure."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=2)
    _opt_in(db_session, account_id, daily_cap_usd=0.01)
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint=session_embedding.USAGE_ENDPOINT,
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=account_id,
        model_alias="text-embedding-3-small",
        provider_name="openai_compatible",
        estimated_cost=0.02,
        meta_data={"purpose": session_embedding.SESSION_EMBEDDING_PURPOSE},
    )
    provider = FakeProvider()

    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.status == STATUS_DEGRADED
    assert result.degraded is True
    assert result.reason == DEGRADED_DAILY_CAP
    assert result.pending == 2
    assert provider.calls == []
    for row in stored:
        db_session.refresh(row)
        assert row.embedding_state == EMBEDDING_STATE_PENDING
        assert row.embedding is None
    setting = crud_session_embedding_setting.get_for_account(
        db_session, account_id=account_id
    )
    db_session.refresh(setting)
    assert setting.degraded_reason == DEGRADED_DAILY_CAP
    assert setting.degraded_at is not None
    assert setting.enabled is True


def test_embedding_spend_is_purpose_tagged_and_left_out_of_session_rollups(
    db_session, test_user
):
    """A session's reported cost is the agent's spend, not Preloop's indexing."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=account_id,
        runtime_session_id=str(session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        estimated_cost=0.25,
    )
    _chunks(db_session, account_id=account_id, session=session, count=2)
    _opt_in(db_session, account_id)

    def _session_cost() -> float:
        listing = crud_runtime_session.list_account_sessions(
            db_session, account_id=account_id, status="all", limit=50
        )
        rows = [row for row in listing["items"] if row["id"] == str(session.id)]
        assert rows, "the session must be listed"
        return float(rows[0]["estimated_cost"])

    before = _session_cost()
    result = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider()
    )
    after = _session_cost()

    assert result.status == STATUS_OK
    assert before == pytest.approx(0.25)
    assert after == pytest.approx(before)

    rows = (
        db_session.query(crud_api_usage.model)
        .filter(
            crud_api_usage.model.account_id == account_id,
            crud_api_usage.model.endpoint == session_embedding.USAGE_ENDPOINT,
        )
        .all()
    )
    assert len(rows) == 1, "one batch writes exactly one usage row"
    usage_row = rows[0]
    assert usage_row.meta_data["purpose"] == "session_embedding"
    assert usage_row.meta_data["chunks"] == 2
    assert str(usage_row.runtime_session_id) == str(session.id)
    assert usage_row.prompt_tokens == 120
    spend = crud_api_usage.get_gateway_spend(
        db_session,
        account_id=account_id,
        start=OCCURRED_AT - timedelta(days=1),
        purpose="session_embedding",
    )
    assert spend == pytest.approx(float(usage_row.estimated_cost or 0.0))


def test_embedding_spend_is_priced_from_the_catalog(db_session, test_user):
    """#651 put embedding prices in the catalogue so this is not recorded as $0."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session, count=2)
    _opt_in(db_session, account_id)

    result = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider()
    )

    assert result.status == STATUS_OK
    assert result.cost_source == "catalog"
    assert result.cost_usd is not None and result.cost_usd > 0


def test_a_provider_failure_degrades_and_returns_the_chunks_to_the_queue(
    db_session, test_user
):
    """A refused batch costs an attempt, not the chunks."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=2)
    _opt_in(db_session, account_id)

    result = run_account_batch(
        db_session, account_id=account_id, provider=FailingProvider()
    )

    assert result.status == STATUS_DEGRADED
    assert result.reason == DEGRADED_PROVIDER_ERROR
    for row in stored:
        db_session.refresh(row)
        assert row.embedding_state == EMBEDDING_STATE_PENDING
        assert row.embedding_attempts == 1


def test_a_chunk_the_provider_keeps_refusing_is_retired(db_session, test_user):
    """One poison chunk cannot starve the oldest-first queue behind it."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=1)
    _opt_in(db_session, account_id)

    for _ in range(int(settings.session_embedding_max_attempts)):
        run_account_batch(db_session, account_id=account_id, provider=FailingProvider())

    row = stored[0]
    db_session.refresh(row)
    assert row.embedding_state == EMBEDDING_STATE_FAILED
    assert row.embedding_attempts == int(settings.session_embedding_max_attempts)


def test_a_vector_of_the_wrong_width_is_degraded_not_stored(db_session, test_user):
    """A provider that ignores the dimensions parameter must not corrupt the corpus."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=1)
    _opt_in(db_session, account_id)

    result = run_account_batch(
        db_session,
        account_id=account_id,
        provider=FakeProvider(width=512),
    )

    assert result.status == STATUS_DEGRADED
    assert result.reason == DEGRADED_DIMENSION_MISMATCH
    db_session.refresh(stored[0])
    assert stored[0].embedding is None


def test_a_batch_never_spans_two_sessions(db_session, test_user):
    """One usage row must be attributable to the session it indexed."""
    account_id = str(test_user.account_id)
    first = _session(db_session, account_id, source_id="session-first")
    second = _session(db_session, account_id, source_id="session-second")
    _chunks(
        db_session,
        account_id=account_id,
        session=first,
        count=2,
        source_id="message-first",
        occurred_at=OCCURRED_AT - timedelta(hours=2),
    )
    _chunks(
        db_session,
        account_id=account_id,
        session=second,
        count=2,
        source_id="message-second",
    )
    _opt_in(db_session, account_id)
    provider = FakeProvider()

    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.status == STATUS_OK
    assert result.runtime_session_id == str(first.id)
    assert result.embedded == 2
    assert result.pending == 2
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 2


def test_a_batch_is_capped_at_the_configured_size(db_session, test_user, monkeypatch):
    """The batch size is the unit of spend, so it is the unit of the claim."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session, count=5)
    _opt_in(db_session, account_id)
    monkeypatch.setattr(settings, "session_embedding_batch_size", 2, raising=False)
    provider = FakeProvider()

    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.embedded == 2
    assert result.pending == 3
    assert len(provider.calls[0]) == 2


def test_a_successful_run_clears_an_earlier_degraded_marker(db_session, test_user):
    """Degraded describes the last run, not the account forever."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session, count=1)
    _opt_in(db_session, account_id)
    crud_session_embedding_setting.mark_degraded(
        db_session, account_id=account_id, reason=DEGRADED_PROVIDER_ERROR, commit=True
    )

    result = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider()
    )

    setting = crud_session_embedding_setting.get_for_account(
        db_session, account_id=account_id
    )
    db_session.refresh(setting)
    assert result.status == STATUS_OK
    assert setting.degraded_reason is None
    assert setting.degraded_at is None


def test_another_accounts_chunks_are_never_claimed(db_session, test_user):
    """Every claim is account scoped before it is anything else."""
    account_id = str(test_user.account_id)
    other = _account(db_session)
    other_session = _session(db_session, str(other.id), source_id="session-other")
    other_chunks = _chunks(
        db_session,
        account_id=str(other.id),
        session=other_session,
        count=2,
        source_id="message-other",
        occurred_at=OCCURRED_AT - timedelta(days=1),
    )
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session, count=1)
    _opt_in(db_session, account_id)

    result = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider()
    )

    assert result.status == STATUS_OK
    assert result.runtime_session_id == str(session.id)
    for row in other_chunks:
        db_session.refresh(row)
        assert row.embedding is None


def test_a_held_runtime_session_column_keeps_chunks_pending(db_session, test_user):
    """The #650 column is enough; a flow-execution join is not required."""
    account_id = str(test_user.account_id)
    held_session = _session(db_session, account_id, source_id="session-held-column")
    free_session = _session(db_session, account_id, source_id="session-free-column")
    held_session.legal_hold = True
    db_session.commit()
    held_chunks = _chunks(
        db_session,
        account_id=account_id,
        session=held_session,
        count=2,
        source_id="message-held-column",
        occurred_at=OCCURRED_AT - timedelta(hours=1),
    )
    free_chunks = _chunks(
        db_session,
        account_id=account_id,
        session=free_session,
        count=2,
        source_id="message-free-column",
    )
    _opt_in(db_session, account_id)
    provider = FakeProvider()

    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.status == STATUS_OK
    assert result.runtime_session_id == str(free_session.id)
    for row in held_chunks:
        db_session.refresh(row)
        assert row.embedding is None
        assert row.embedding_state == EMBEDDING_STATE_PENDING
    for row in free_chunks:
        db_session.refresh(row)
        assert row.embedding is not None


def test_a_stale_in_progress_claim_is_reclaimed(db_session, test_user):
    """A worker death between claim and store cannot hide the backlog."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=2)
    _opt_in(db_session, account_id)
    claimed = crud_session_search_document.claim_pending_chunks(
        db_session, account_id=account_id, limit=8, commit=True
    )
    assert len(claimed) == 2
    assert all(row.embedding_state == EMBEDDING_STATE_IN_PROGRESS for row in claimed)
    assert (
        crud_session_search_document.count_pending_embeddings(
            db_session, account_id=account_id
        )
        == 0
    )

    later = datetime.now(UTC) + timedelta(minutes=10)
    assert (
        crud_session_search_document.count_pending_embeddings(
            db_session, account_id=account_id, now=later
        )
        == 2
    )
    provider = FakeProvider()
    result = run_account_batch(
        db_session, account_id=account_id, provider=provider, now=later
    )

    assert result.status == STATUS_OK
    assert result.embedded == 2
    assert provider.calls
    for row in stored:
        db_session.refresh(row)
        assert row.embedding_state == EMBEDDING_STATE_EMBEDDED
        assert row.embedding is not None


def test_a_fresh_in_progress_claim_is_not_stolen(db_session, test_user):
    """A live provider call keeps its lease until the reclaim window."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=1)
    _opt_in(db_session, account_id)
    claimed = crud_session_search_document.claim_pending_chunks(
        db_session, account_id=account_id, limit=8, commit=True
    )
    assert len(claimed) == 1

    provider = FakeProvider()
    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.status == STATUS_IDLE
    assert provider.calls == []
    db_session.refresh(stored[0])
    assert stored[0].embedding_state == EMBEDDING_STATE_IN_PROGRESS
    assert stored[0].embedding is None


def test_an_unexpected_error_after_claim_releases_the_chunks(db_session, test_user):
    """A non-provider exception must not leave the batch stranded."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=1)
    _opt_in(db_session, account_id)

    class ExplodingProvider:
        model = "text-embedding-3-small"

        def embed(self, texts: Sequence[str]) -> List[List[float]]:
            raise RuntimeError("store exploded")

    result = run_account_batch(
        db_session, account_id=account_id, provider=ExplodingProvider()
    )

    assert result.status == STATUS_DEGRADED
    assert result.reason == DEGRADED_PROVIDER_ERROR
    db_session.refresh(stored[0])
    assert stored[0].embedding_state == EMBEDDING_STATE_PENDING
    assert stored[0].embedding is None
    assert stored[0].embedding_attempts == 1


def test_an_unpriced_openai_compatible_model_never_reaches_the_provider(
    db_session, test_user
):
    """A catalogue miss must not disable the daily cap by recording $0."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    stored = _chunks(db_session, account_id=account_id, session=session, count=1)
    _opt_in(db_session, account_id, model_identifier="not-in-the-catalogue")
    provider = FakeProvider()

    result = run_account_batch(db_session, account_id=account_id, provider=provider)

    assert result.status == STATUS_DEGRADED
    assert result.reason == DEGRADED_UNPRICED_MODEL
    assert provider.calls == []
    db_session.refresh(stored[0])
    assert stored[0].embedding_state == EMBEDDING_STATE_PENDING
    setting = crud_session_embedding_setting.get_for_account(
        db_session, account_id=account_id
    )
    db_session.refresh(setting)
    assert setting.degraded_reason == DEGRADED_UNPRICED_MODEL


def test_the_shared_api_key_is_omitted_unless_the_url_is_allow_listed(monkeypatch):
    """An account-chosen host must not receive the deployment credential."""
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "session_embedding_api_key", "shared-secret")
    monkeypatch.setattr(settings, "session_embedding_api_key_base_urls", "")
    setting = SimpleNamespace(
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model_identifier="text-embedding-3-small",
        base_url="https://embeddings.example.com/v1",
        dimensions=EMBEDDING_DIMENSIONS,
    )

    omitted = build_provider(setting)
    assert omitted.api_key is None

    monkeypatch.setattr(
        settings,
        "session_embedding_api_key_base_urls",
        "https://embeddings.example.com/v1/",
    )
    allowed = build_provider(setting)
    assert allowed.api_key == "shared-secret"


def test_build_provider_refuses_a_hostname_rebound_to_link_local(
    db_session, test_user, monkeypatch
):
    """Request-time resolve catches a DNS rebind that enable() did not see."""
    import ipaddress

    from preloop.models.crud import session_embedding_setting as setting_mod

    setting = _opt_in(db_session, str(test_user.account_id))
    monkeypatch.setattr(
        setting_mod,
        "_resolved_ip_addresses",
        lambda host: [ipaddress.ip_address("169.254.169.254")],
    )

    with pytest.raises(EmbeddingProviderError, match="loopback, link-local"):
        build_provider(setting)


def _summary_chunk(db_session, *, account_id, session, occurred_at=OCCURRED_AT):
    """Write the one chunk a session's own title and summary produce."""
    stored = crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=str(session.id),
        occurred_at=occurred_at,
        chunks=[
            SessionSearchChunk(
                content="summary: the run that fixed the connection pool",
                chunk_index=0,
                role="system",
            )
        ],
    )
    db_session.commit()
    return stored


def _embedded_ids(db_session, account_id):
    rows = (
        db_session.query(models.SessionSearchDocument)
        .filter(
            models.SessionSearchDocument.account_id == account_id,
            models.SessionSearchDocument.embedding_state == EMBEDDING_STATE_EMBEDDED,
        )
        .all()
    )
    return {str(row.id) for row in rows}


def test_summaries_only_embeds_the_summary_and_leaves_the_transcript(
    db_session, test_user
):
    """The shipped scope: one vector per session, not one per turn."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session, count=30)
    _summary_chunk(db_session, account_id=account_id, session=session)
    _opt_in(db_session, account_id, scope=EMBEDDING_SCOPE_SUMMARIES_ONLY)
    provider = FakeProvider()

    result = run_account_batch(
        db_session, account_id=account_id, provider=provider, batch_size=64
    )

    assert result.status == STATUS_OK
    assert result.embedded == 1
    assert result.pending == 0
    embedded = (
        db_session.query(models.SessionSearchDocument)
        .filter(
            models.SessionSearchDocument.account_id == account_id,
            models.SessionSearchDocument.embedding_state == EMBEDDING_STATE_EMBEDDED,
        )
        .all()
    )
    assert [row.source_kind for row in embedded] == [SOURCE_KIND_SESSION_SUMMARY]
    # The transcript is not embedded and not consumed: it is simply not
    # this account's backlog while the scope says summaries only.
    still_pending = (
        db_session.query(models.SessionSearchDocument)
        .filter(
            models.SessionSearchDocument.account_id == account_id,
            models.SessionSearchDocument.source_kind == SOURCE_KIND_TRANSCRIPT_MESSAGE,
            models.SessionSearchDocument.embedding_state == EMBEDDING_STATE_PENDING,
        )
        .count()
    )
    assert still_pending == 30
    # Nothing outside the scope was ever sent to the provider.
    assert len(provider.calls) == 1
    assert len(provider.calls[0]) == 1


def test_full_embeds_every_chunk_of_the_session(db_session, test_user):
    """The opt in for transcript recall embeds the transcript."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session, count=30)
    _summary_chunk(db_session, account_id=account_id, session=session)
    _opt_in(db_session, account_id, scope=EMBEDDING_SCOPE_FULL)

    result = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider(), batch_size=64
    )

    assert result.status == STATUS_OK
    assert result.embedded == 31
    assert result.pending == 0


def test_widening_the_scope_embeds_the_backlog_and_keeps_existing_vectors(
    db_session, test_user
):
    """full picks up what summaries_only left, without redoing the summary."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session, count=30)
    _summary_chunk(db_session, account_id=account_id, session=session)
    _opt_in(db_session, account_id, scope=EMBEDDING_SCOPE_SUMMARIES_ONLY)

    first = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider(), batch_size=64
    )
    assert first.embedded == 1
    summary_row = (
        db_session.query(models.SessionSearchDocument)
        .filter(
            models.SessionSearchDocument.account_id == account_id,
            models.SessionSearchDocument.source_kind == SOURCE_KIND_SESSION_SUMMARY,
        )
        .one()
    )
    embedded_at = summary_row.embedded_at
    attempts = summary_row.embedding_attempts

    crud_session_embedding_setting.set_scope(
        db_session, account_id=account_id, scope=EMBEDDING_SCOPE_FULL, commit=True
    )
    second = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider(), batch_size=64
    )

    assert second.status == STATUS_OK
    assert second.embedded == 30
    db_session.refresh(summary_row)
    # The chunk that was already embedded is not claimed again: widening the
    # scope is not a re-embed.
    assert summary_row.embedded_at == embedded_at
    assert summary_row.embedding_attempts == attempts


def test_narrowing_the_scope_stops_new_vectors_and_deletes_none(db_session, test_user):
    """summaries_only after full keeps every vector already written."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id)
    _chunks(db_session, account_id=account_id, session=session, count=30)
    # Written last, so the first small batch is transcript chunks only and
    # the summary is still waiting when the scope narrows.
    _summary_chunk(
        db_session,
        account_id=account_id,
        session=session,
        occurred_at=OCCURRED_AT + timedelta(hours=1),
    )
    _opt_in(db_session, account_id, scope=EMBEDDING_SCOPE_FULL)

    first = run_account_batch(
        db_session, account_id=account_id, provider=FakeProvider(), batch_size=10
    )
    assert first.embedded == 10
    before = _embedded_ids(db_session, account_id)

    crud_session_embedding_setting.set_scope(
        db_session,
        account_id=account_id,
        scope=EMBEDDING_SCOPE_SUMMARIES_ONLY,
        commit=True,
    )
    provider = FakeProvider()
    second = run_account_batch(
        db_session, account_id=account_id, provider=provider, batch_size=10
    )

    # The summary is still in scope, the 20 transcript chunks left are not,
    # and no vector written under `full` is removed.
    assert second.status == STATUS_OK
    assert second.embedded == 1
    assert second.pending == 0
    after = _embedded_ids(db_session, account_id)
    assert before < after
    new_rows = (
        db_session.query(models.SessionSearchDocument)
        .filter(models.SessionSearchDocument.id.in_(after - before))
        .all()
    )
    assert [row.source_kind for row in new_rows] == [SOURCE_KIND_SESSION_SUMMARY]
    assert len(provider.calls) == 1
