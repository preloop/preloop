"""The search corpus must follow retention, holds and redaction (#655).

A search index is a second copy of the data. These tests pin the three ways
that copy can make a compliance statement untrue: outliving a purge, being
taken by a purge that its session was held against, and answering with text
the source no longer has.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_session_search_document
from preloop.models.models.session_search_document import (
    REDACTION_STATE_CLEAR,
    REDACTION_STATE_METADATA_ONLY,
    REDACTION_STATE_REDACTED,
    REDACTION_STATE_WITHHELD,
    REDACTION_STATES,
    SOURCE_KIND_GATEWAY_INTERACTION,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
    TEXT_RETURNABLE_REDACTION_STATES,
    SessionSearchDocument,
)
from preloop.services import retention_purge as purge
from preloop.services import session_search_index as index
from preloop.services import session_search_retention as corpus_retention
from preloop.services.legal_hold import place_hold, release_hold
from preloop.services.retention_policy import (
    CLASS_RUNTIME_SESSIONS,
    CLASS_USAGE,
    SWEEP_EXECUTION_LOGS,
)


@pytest.fixture(autouse=True)
def enabled_purge(monkeypatch):
    """Tests exercise the job itself; the deployment default is off."""
    monkeypatch.setattr(settings, "retention_purge_enabled", True, raising=False)
    monkeypatch.setattr(settings, "retention_purge_dry_run", False, raising=False)
    monkeypatch.setattr(settings, "retention_purge_window_utc", "", raising=False)
    monkeypatch.setattr(settings, "session_search_index_enabled", True, raising=False)


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


def _session(db_session, test_user, *, age_days: int) -> models.RuntimeSession:
    """One ended session dated ``age_days`` ago."""
    stamp = datetime.now(UTC) - timedelta(days=age_days)
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="managed_agent",
        session_source_id=f"agent-{uuid.uuid4().hex[:8]}",
        started_at=stamp,
        last_activity_at=stamp,
        ended_at=stamp,
    )
    db_session.add(session)
    db_session.flush()
    return session


def _usage(db_session, test_user, *, session_id, age_days: int) -> models.ApiUsage:
    """One metered gateway call attributed to ``session_id``."""
    row = models.ApiUsage(
        account_id=test_user.account_id,
        user_id=test_user.id,
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.4,
        model_alias="house-model",
        provider_name="OpenAI",
        runtime_session_id=session_id,
        timestamp=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=age_days),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _chunk(
    db_session,
    test_user,
    *,
    session_id,
    source_kind: str = SOURCE_KIND_TRANSCRIPT_MESSAGE,
    source_id=None,
    text: str = "the quick brown fox",
    age_days: int = 500,
) -> SessionSearchDocument:
    """One corpus row written the way the indexers write it."""
    stored = index.write_source_chunks(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session_id,
        source_kind=source_kind,
        source_id=source_id or uuid.uuid4(),
        text=text,
        occurred_at=datetime.now(UTC) - timedelta(days=age_days),
        role="user",
    )
    assert stored, "the fixture must actually index something"
    return stored[0]


def _chunk_count(db_session, session_id) -> int:
    return len(
        db_session.execute(
            select(SessionSearchDocument.id).where(
                SessionSearchDocument.runtime_session_id == session_id
            )
        )
        .scalars()
        .all()
    )


def _source_chunk_count(db_session, *, source_kind, source_id) -> int:
    return len(
        crud_session_search_document.list_for_source(
            db_session, source_kind=source_kind, source_id=str(source_id)
        )
    )


# --- chunks follow the source they quote -----------------------------------


def test_deleting_a_session_takes_its_chunks_with_it(db_session, test_user):
    """The plain delete, before any purge is involved."""
    session = _session(db_session, test_user, age_days=1)
    _chunk(db_session, test_user, session_id=session.id, age_days=1)
    _chunk(db_session, test_user, session_id=session.id, age_days=1)
    db_session.commit()
    assert _chunk_count(db_session, session.id) == 2

    db_session.delete(db_session.get(models.RuntimeSession, session.id))
    db_session.commit()

    assert _chunk_count(db_session, session.id) == 0


def test_a_session_purge_deletes_the_chunks_of_every_session_it_deletes(
    db_session, test_user, account
):
    old = _session(db_session, test_user, age_days=500).id
    recent = _session(db_session, test_user, age_days=5).id
    for _ in range(3):
        _chunk(db_session, test_user, session_id=old)
    _chunk(db_session, test_user, session_id=recent, age_days=5)
    db_session.commit()
    assert (_chunk_count(db_session, old), _chunk_count(db_session, recent)) == (3, 1)

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _chunk_count(db_session, old) == 0
    assert _chunk_count(db_session, recent) == 1


def test_the_purge_reports_how_many_derived_rows_it_removed(
    db_session, test_user, account
):
    """``derived_deleted`` is separate from ``deleted``, which counts records."""
    session = _session(db_session, test_user, age_days=500)
    for _ in range(4):
        _chunk(db_session, test_user, session_id=session.id)
    db_session.commit()

    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_RUNTIME_SESSIONS,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=5,
        dry_run=False,
    )

    assert result.deleted == 1
    assert result.derived_deleted == 4
    assert result.as_details()["derived_deleted"] == 4


def test_a_usage_purge_removes_the_chunks_derived_from_its_rows(
    db_session, test_user, account
):
    """A chunk may not outlive the usage row it quotes."""
    session = _session(db_session, test_user, age_days=2).id
    old_usage = _usage(db_session, test_user, session_id=session, age_days=500).id
    recent_usage = _usage(db_session, test_user, session_id=session, age_days=2).id
    _chunk(
        db_session,
        test_user,
        session_id=session,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=old_usage,
    )
    _chunk(
        db_session,
        test_user,
        session_id=session,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=recent_usage,
        age_days=2,
    )
    db_session.commit()

    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=5,
        dry_run=False,
    )

    assert result.deleted == 1
    assert result.derived_deleted == 1
    assert (
        _source_chunk_count(
            db_session,
            source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
            source_id=old_usage,
        )
        == 0
    )
    assert (
        _source_chunk_count(
            db_session,
            source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
            source_id=recent_usage,
        )
        == 1
    )


def test_a_usage_purge_leaves_chunks_of_other_source_kinds_alone(
    db_session, test_user, account
):
    """Only the gateway kind quotes a usage row; the rest are session content."""
    session = _session(db_session, test_user, age_days=2)
    usage = _usage(db_session, test_user, session_id=session.id, age_days=500)
    _chunk(
        db_session,
        test_user,
        session_id=session.id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=usage.id,
    )
    transcript = _chunk(db_session, test_user, session_id=session.id)
    db_session.commit()

    purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=5,
        dry_run=False,
    )

    assert (
        _source_chunk_count(
            db_session,
            source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
            source_id=transcript.source_id,
        )
        == 1
    )


# --- a hold preserves the chunks too ---------------------------------------


def test_a_held_session_keeps_its_chunks_through_a_purge(
    db_session, test_user, account
):
    held = _session(db_session, test_user, age_days=500).id
    unheld = _session(db_session, test_user, age_days=500).id
    for _ in range(2):
        _chunk(db_session, test_user, session_id=held)
    _chunk(db_session, test_user, session_id=unheld)
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(held),
        reason="litigation hold, matter 2026-07",
        user_id=test_user.id,
    )

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _chunk_count(db_session, held) == 2
    assert _chunk_count(db_session, unheld) == 0


def test_a_held_sessions_chunks_survive_the_usage_purge_as_well(
    db_session, test_user, account
):
    """The hold outranks the usage cutoff, and the docs say so.

    The chunk then outlives the usage row it quotes for the life of the hold.
    That is the intended direction: a hold is an instruction to preserve the
    record, and a retention cutoff on another class does not overrule it.
    """
    session = _session(db_session, test_user, age_days=2)
    usage = _usage(db_session, test_user, session_id=session.id, age_days=500)
    _chunk(
        db_session,
        test_user,
        session_id=session.id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=usage.id,
    )
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(session.id),
        reason="regulator request 2026-08",
        user_id=test_user.id,
    )

    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=5,
        dry_run=False,
    )

    assert result.derived_deleted == 0
    assert _chunk_count(db_session, session.id) == 1
    report = corpus_retention.orphan_chunk_report(db_session)
    assert report.clean, report.as_dict()
    corpus_retention.assert_no_orphan_chunks(
        db_session, context="a usage purge of a held session"
    )


def test_releasing_a_hold_lets_the_next_usage_pass_take_the_chunk(
    db_session, test_user, account
):
    """Usage ids purged under a hold never come back; the sweep reclaims them.

    After release, and before the next usage pass, the operator check reports
    a usage orphan. The next usage pass, even with no remaining usage rows,
    deletes the released chunk and leaves a still-held sibling alone.
    """
    released = _session(db_session, test_user, age_days=2)
    released_usage = _usage(db_session, test_user, session_id=released.id, age_days=500)
    _chunk(
        db_session,
        test_user,
        session_id=released.id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=released_usage.id,
    )
    still_held = _session(db_session, test_user, age_days=2)
    still_usage = _usage(db_session, test_user, session_id=still_held.id, age_days=500)
    _chunk(
        db_session,
        test_user,
        session_id=still_held.id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=still_usage.id,
    )
    db_session.commit()
    released_hold = place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(released.id),
        reason="regulator request 2026-08",
        user_id=test_user.id,
    )
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(still_held.id),
        reason="matter still open 2026-09",
        user_id=test_user.id,
    )

    first = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=5,
        dry_run=False,
    )

    assert first.derived_deleted == 0
    assert _chunk_count(db_session, released.id) == 1
    assert _chunk_count(db_session, still_held.id) == 1
    assert corpus_retention.orphan_chunk_report(db_session).clean

    release_hold(
        db_session,
        account_id=account.id,
        hold_id=released_hold.hold.id,
        reason="matter closed 2026-09",
        user_id=test_user.id,
    )

    after_release = corpus_retention.orphan_chunk_report(db_session)
    assert after_release.orphaned_usage == 1
    assert after_release.clean is False

    second = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=5,
        dry_run=False,
    )

    assert second.derived_deleted == 1
    assert _chunk_count(db_session, released.id) == 0
    assert _chunk_count(db_session, still_held.id) == 1
    report = corpus_retention.orphan_chunk_report(db_session)
    assert report.clean, report.as_dict()
    corpus_retention.assert_no_orphan_chunks(
        db_session, context="a usage pass after a hold release"
    )


# --- the orphan check ------------------------------------------------------


def test_the_orphan_check_finds_nothing_after_a_purge(db_session, test_user, account):
    """Several sessions, several source kinds, one pass, no leftovers."""
    for age in (500, 400, 300, 10, 2):
        session = _session(db_session, test_user, age_days=age)
        usage = _usage(db_session, test_user, session_id=session.id, age_days=age)
        _chunk(
            db_session,
            test_user,
            session_id=session.id,
            source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
            source_id=usage.id,
            age_days=age,
        )
        _chunk(db_session, test_user, session_id=session.id, age_days=age)
        _chunk(db_session, test_user, session_id=session.id, age_days=age)
    db_session.commit()
    assert corpus_retention.orphan_chunk_report(db_session).total == 0

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    report = corpus_retention.orphan_chunk_report(db_session)
    assert report.clean, report.as_dict()
    corpus_retention.assert_no_orphan_chunks(db_session, context="a full purge pass")


def test_the_orphan_check_reports_a_chunk_whose_usage_row_went(
    db_session, test_user, account
):
    """The check has to be able to fail, or it proves nothing."""
    session = _session(db_session, test_user, age_days=2)
    usage = _usage(db_session, test_user, session_id=session.id, age_days=500)
    _chunk(
        db_session,
        test_user,
        session_id=session.id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=usage.id,
    )
    db_session.commit()

    # Delete the usage row the way a database-level cleanup would: without
    # going through the purge, so nothing removes the chunk.
    db_session.delete(db_session.get(models.ApiUsage, usage.id))
    db_session.commit()

    report = corpus_retention.orphan_chunk_report(db_session)
    assert report.orphaned_usage == 1
    assert report.clean is False
    with pytest.raises(AssertionError, match="orphan chunks"):
        corpus_retention.assert_no_orphan_chunks(db_session)


# --- redaction after indexing ----------------------------------------------


def test_a_source_redacted_after_indexing_returns_no_text_from_search(
    db_session, test_user, account
):
    """End to end: index, search, redact, search again."""
    session = _session(db_session, test_user, age_days=1)
    chunk = _chunk(
        db_session,
        test_user,
        session_id=session.id,
        text="the customer mentioned a diagnosis in the transcript",
        age_days=1,
    )
    db_session.commit()

    before = crud_session_search_document.search_account_hits(
        db_session, account_id=account.id, query="diagnosis"
    )
    assert [hit.content for hit in before] == [chunk.content]
    assert [hit.text_withheld for hit in before] == [False]

    outcome = index.redact_indexed_source(
        db_session,
        source_kind=chunk.source_kind,
        source_id=chunk.source_id,
        commit=True,
    )
    assert outcome.action == "withheld"
    assert outcome.chunks == 1

    after = crud_session_search_document.search_account_hits(
        db_session, account_id=account.id
    )
    assert [hit.content for hit in after] == [""]
    assert [hit.text_withheld for hit in after] == [True]
    assert "diagnosis" not in " ".join(hit.content for hit in after)


def test_withholding_clears_the_stored_text_not_only_the_response(
    db_session, test_user, account
):
    """A redaction that only hides a column from one query is not a redaction."""
    session = _session(db_session, test_user, age_days=1)
    chunk = _chunk(
        db_session, test_user, session_id=session.id, text="secret note", age_days=1
    )
    db_session.commit()

    index.redact_indexed_source(
        db_session,
        source_kind=chunk.source_kind,
        source_id=chunk.source_id,
        commit=True,
    )

    rows = crud_session_search_document.list_for_source(
        db_session, source_kind=chunk.source_kind, source_id=chunk.source_id
    )
    assert [row.content for row in rows] == [""]
    assert [row.redaction_state for row in rows] == [REDACTION_STATE_WITHHELD]


def test_a_withheld_chunk_is_no_longer_matched_by_its_old_text(
    db_session, test_user, account
):
    """The generated vector is recomputed from the cleared content."""
    session = _session(db_session, test_user, age_days=1)
    chunk = _chunk(
        db_session, test_user, session_id=session.id, text="pineapple", age_days=1
    )
    db_session.commit()

    index.redact_indexed_source(
        db_session,
        source_kind=chunk.source_kind,
        source_id=chunk.source_id,
        commit=True,
    )

    assert (
        crud_session_search_document.search_account_hits(
            db_session, account_id=account.id, query="pineapple"
        )
        == []
    )


def test_a_redaction_with_replacement_text_reindexes_the_chunks(
    db_session, test_user, account
):
    session = _session(db_session, test_user, age_days=1)
    chunk = _chunk(
        db_session,
        test_user,
        session_id=session.id,
        text="original wording with pineapple",
        age_days=1,
    )
    db_session.commit()

    outcome = index.redact_indexed_source(
        db_session,
        source_kind=chunk.source_kind,
        source_id=chunk.source_id,
        replacement_text="redacted wording with mango",
        commit=True,
    )

    assert outcome.action == "reindexed"
    hits = crud_session_search_document.search_account_hits(
        db_session, account_id=account.id, query="mango"
    )
    assert [hit.content for hit in hits] == ["redacted wording with mango"]
    assert (
        crud_session_search_document.search_account_hits(
            db_session, account_id=account.id, query="pineapple"
        )
        == []
    )


def test_a_redaction_can_drop_the_chunks_instead(db_session, test_user, account):
    session = _session(db_session, test_user, age_days=1)
    chunk = _chunk(db_session, test_user, session_id=session.id, age_days=1)
    db_session.commit()

    outcome = index.redact_indexed_source(
        db_session,
        source_kind=chunk.source_kind,
        source_id=chunk.source_id,
        drop=True,
        commit=True,
    )

    assert (outcome.action, outcome.chunks) == ("dropped", 1)
    assert _chunk_count(db_session, session.id) == 0


def test_an_empty_replacement_drops_rather_than_stores_an_empty_chunk(
    db_session, test_user, account
):
    session = _session(db_session, test_user, age_days=1)
    chunk = _chunk(db_session, test_user, session_id=session.id, age_days=1)
    db_session.commit()

    outcome = index.redact_indexed_source(
        db_session,
        source_kind=chunk.source_kind,
        source_id=chunk.source_id,
        replacement_text="   ",
        commit=True,
    )

    assert outcome.action == "dropped"
    assert _chunk_count(db_session, session.id) == 0


def test_redacting_an_unindexed_source_is_a_no_op(db_session, test_user):
    outcome = index.redact_indexed_source(
        db_session,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=uuid.uuid4(),
    )
    assert (outcome.action, outcome.chunks) == ("noop", 0)


def test_a_clean_chunk_still_returns_its_text(db_session, test_user, account):
    """The guard withholds the withheld, not everything."""
    session = _session(db_session, test_user, age_days=1)
    _chunk(db_session, test_user, session_id=session.id, text="hello", age_days=1)
    db_session.commit()

    hits = crud_session_search_document.search_account_hits(
        db_session, account_id=account.id
    )
    assert [(hit.content, hit.redaction_state) for hit in hits] == [
        ("hello", REDACTION_STATE_CLEAR)
    ]


def test_masked_and_metadata_only_chunks_still_answer_with_their_stored_text(
    db_session, test_user, account
):
    """Both were sanitised before they were stored, so both are returnable."""
    session = _session(db_session, test_user, age_days=1)
    index.write_source_chunks(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=uuid.uuid4(),
        text="api_key: sk-live-000 and the rest of the message",
        occurred_at=datetime.now(UTC),
    )
    index.write_source_chunks(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=uuid.uuid4(),
        text="not captured",
        occurred_at=datetime.now(UTC),
        content_captured=False,
    )
    db_session.commit()

    hits = crud_session_search_document.search_account_hits(
        db_session, account_id=account.id
    )
    states = {hit.redaction_state for hit in hits}
    assert states == {REDACTION_STATE_REDACTED, REDACTION_STATE_METADATA_ONLY}
    assert all(hit.content for hit in hits)
    assert all(hit.text_withheld is False for hit in hits)
    assert "sk-live-000" not in " ".join(hit.content for hit in hits)


# --- the invariants --------------------------------------------------------


def test_every_redaction_state_is_decided_returnable_or_not():
    """A new state is withheld until somebody says it is safe to return.

    The read guard is a whitelist for exactly this reason: the failure mode of
    a blacklist is a state added in one PR that leaks in another.
    """
    for state in TEXT_RETURNABLE_REDACTION_STATES:
        assert state in REDACTION_STATES
    assert REDACTION_STATE_WITHHELD not in TEXT_RETURNABLE_REDACTION_STATES
    assert set(REDACTION_STATES) - set(TEXT_RETURNABLE_REDACTION_STATES) == {
        REDACTION_STATE_WITHHELD
    }


def test_every_class_with_derived_chunks_deletes_them_in_the_purge():
    """The mapping the purge consults is the mapping the corpus is written by.

    Two source kinds name a row outside the session: the gateway kind names
    an ``api_usage`` row and the flow log kind a ``flow_execution_log`` row.
    Everything else is session content and goes with the session, so the
    classes and sweeps that need an entry are these three.
    """
    assert set(corpus_retention.DERIVED_CHUNK_DELETES) == {
        CLASS_RUNTIME_SESSIONS,
        CLASS_USAGE,
        SWEEP_EXECUTION_LOGS,
    }
    for record_class in corpus_retention.DERIVED_CHUNK_DELETES:
        assert record_class in purge._CLASS_FILTERS


def test_delete_derived_chunks_is_a_no_op_for_a_class_with_no_corpus(db_session):
    assert (
        corpus_retention.delete_derived_chunks(
            db_session, record_class="audit", ids=[uuid.uuid4()]
        )
        == 0
    )
    assert (
        corpus_retention.delete_derived_chunks(
            db_session, record_class=CLASS_USAGE, ids=[]
        )
        == 0
    )
