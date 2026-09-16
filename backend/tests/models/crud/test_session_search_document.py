"""Tests for the chunked runtime session search corpus CRUD layer."""

from datetime import datetime, timezone

from sqlalchemy import text

from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import (
    SessionSearchChunk,
    content_hash_for,
)
from preloop.models.models.session_search_document import (
    REDACTION_STATE_WITHHELD,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)

OCCURRED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _chunks(*texts, role=None, status=None):
    return [
        SessionSearchChunk(
            content=value,
            chunk_index=index,
            role=role,
            status=status,
        )
        for index, value in enumerate(texts)
    ]


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


def _write(
    db_session,
    account_id,
    session,
    *texts,
    source_id="message-1",
    occurred_at=OCCURRED_AT,
    role=None,
    status=None,
):
    return crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[
            SessionSearchChunk(
                content=value,
                chunk_index=index,
                role=role,
                status=status,
            )
            for index, value in enumerate(texts)
        ],
    )


def test_stored_vector_is_generated_without_an_explicit_write(db_session, test_user):
    """The tsvector column is populated by the database, not by the writer."""
    session = _session(db_session, test_user.account_id)
    stored = _write(db_session, test_user.account_id, session, "deploy the ledger")

    assert len(stored) == 1
    vector = db_session.execute(
        text("SELECT search_vector FROM session_search_document WHERE id = :id"),
        {"id": stored[0].id},
    ).scalar_one()
    assert "ledger" in vector
    matched = crud_session_search_document.search_account_chunks(
        db_session, account_id=test_user.account_id, query="ledger"
    )
    assert [row.id for row in matched] == [stored[0].id]
    assert stored[0].content_hash == content_hash_for("deploy the ledger")


def test_chunks_are_never_visible_to_another_account(db_session, test_user):
    """An account filter bounds the corpus before anything else applies."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    db_session.flush()
    mine = _session(db_session, test_user.account_id)
    theirs = _session(db_session, other_account.id, source_id="session-b")
    _write(db_session, test_user.account_id, mine, "shared vocabulary here")
    _write(
        db_session,
        other_account.id,
        theirs,
        "shared vocabulary here",
        source_id="message-2",
    )

    mine_hits = crud_session_search_document.search_account_chunks(
        db_session, account_id=test_user.account_id, query="vocabulary"
    )
    theirs_hits = crud_session_search_document.search_account_chunks(
        db_session, account_id=other_account.id, query="vocabulary"
    )

    assert [row.runtime_session_id for row in mine_hits] == [mine.id]
    assert [row.runtime_session_id for row in theirs_hits] == [theirs.id]
    # Even naming the other account's session explicitly returns nothing.
    assert (
        crud_session_search_document.search_account_chunks(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=theirs.id,
        )
        == []
    )
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=theirs.id,
        )
        == 0
    )


def test_unchanged_content_hash_is_a_no_op_and_a_change_replaces(db_session, test_user):
    """Re-indexing writes nothing unless the content hash moved."""
    session = _session(db_session, test_user.account_id)
    first = _write(db_session, test_user.account_id, session, "one", "two")
    first_ids = sorted(str(row.id) for row in first)

    repeat = _write(db_session, test_user.account_id, session, "one", "two")

    assert sorted(str(row.id) for row in repeat) == first_ids
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 2
    )

    later = OCCURRED_AT.replace(hour=13)
    metadata_only = _write(
        db_session,
        test_user.account_id,
        session,
        "one",
        "two",
        occurred_at=later,
        status="closed",
    )

    assert sorted(str(row.id) for row in metadata_only) != first_ids
    assert [row.content for row in metadata_only] == ["one", "two"]
    assert metadata_only[0].occurred_at == later
    assert metadata_only[0].status == "closed"

    changed = _write(db_session, test_user.account_id, session, "one", "three")

    assert sorted(str(row.id) for row in changed) != first_ids
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 2
    )
    assert [row.content for row in changed] == ["one", "three"]

    shrunk = _write(db_session, test_user.account_id, session, "one")

    assert len(shrunk) == 1
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 1
    )


def test_deleting_a_runtime_session_deletes_its_chunks(db_session, test_user):
    """The session foreign key cascades, so chunks cannot outlive a session."""
    session = _session(db_session, test_user.account_id)
    _write(db_session, test_user.account_id, session, "content to delete")
    session_id = session.id

    db_session.execute(
        text("DELETE FROM runtime_session WHERE id = :id"), {"id": session_id}
    )
    db_session.flush()

    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session_id,
        )
        == 0
    )


def test_the_guarded_read_applies_the_same_account_bound(db_session, test_user):
    """The safe read may not be a wider query than the raw one (#655)."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    db_session.flush()
    mine = _session(db_session, test_user.account_id)
    theirs = _session(db_session, other_account.id, source_id="session-c")
    _write(db_session, test_user.account_id, mine, "shared vocabulary here")
    _write(
        db_session,
        other_account.id,
        theirs,
        "shared vocabulary here",
        source_id="message-3",
    )

    hits = crud_session_search_document.search_account_hits(
        db_session, account_id=test_user.account_id, query="vocabulary"
    )

    assert [hit.runtime_session_id for hit in hits] == [mine.id]
    assert (
        crud_session_search_document.search_account_hits(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=theirs.id,
        )
        == []
    )


def test_withholding_clears_the_text_and_the_generated_vector(db_session, test_user):
    """The vector is generated from content, so clearing content clears it."""
    session = _session(db_session, test_user.account_id)
    stored = _write(db_session, test_user.account_id, session, "pineapple ledger")

    marked = crud_session_search_document.withhold_source_text(
        db_session,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id="message-1",
    )

    assert marked == 1
    db_session.expire_all()
    row = db_session.get(type(stored[0]), stored[0].id)
    assert row.content == ""
    assert row.redaction_state == REDACTION_STATE_WITHHELD
    assert row.content_hash == content_hash_for("")
    vector = db_session.execute(
        text("SELECT search_vector FROM session_search_document WHERE id = :id"),
        {"id": row.id},
    ).scalar_one()
    assert vector == ""
    assert (
        crud_session_search_document.search_account_chunks(
            db_session, account_id=test_user.account_id, query="pineapple"
        )
        == []
    )


def test_a_withheld_chunk_is_still_a_countable_row(db_session, test_user):
    """A redaction leaves a gap that is legible rather than indistinguishable."""
    session = _session(db_session, test_user.account_id)
    _write(db_session, test_user.account_id, session, "one", "two")

    crud_session_search_document.withhold_source_text(
        db_session,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id="message-1",
    )

    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 2
    )
    hits = crud_session_search_document.search_account_hits(
        db_session, account_id=test_user.account_id
    )
    assert [hit.text_withheld for hit in hits] == [True, True]


def test_deleting_many_sources_in_one_statement(db_session, test_user):
    """The purge removes a batch of ids, not one source at a time."""
    session = _session(db_session, test_user.account_id)
    _write(db_session, test_user.account_id, session, "a", "b", source_id="message-a")
    _write(db_session, test_user.account_id, session, "c", source_id="message-b")
    _write(db_session, test_user.account_id, session, "d", source_id="message-c")

    deleted = crud_session_search_document.delete_for_sources(
        db_session,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_ids=["message-a", "message-b"],
    )

    assert deleted == 3
    assert (
        crud_session_search_document.delete_for_sources(
            db_session, source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE, source_ids=[]
        )
        == 0
    )
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 1
    )


def test_deleting_sources_skips_chunks_of_held_sessions(db_session, test_user):
    """The usage purge and the orphan count share this hold exclusion."""
    held = _session(db_session, test_user.account_id, source_id="held-session")
    unheld = _session(db_session, test_user.account_id, source_id="unheld-session")
    _write(db_session, test_user.account_id, held, "kept", source_id="held-src")
    _write(db_session, test_user.account_id, unheld, "gone", source_id="unheld-src")
    held.legal_hold = True
    db_session.flush()

    deleted = crud_session_search_document.delete_for_sources(
        db_session,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_ids=["held-src", "unheld-src"],
        excluding_held_sessions=True,
    )

    assert deleted == 1
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=held.id,
        )
        == 1
    )
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=unheld.id,
        )
        == 0
    )


def test_deleting_source_orphans_skips_held_sessions(db_session, test_user):
    """The released-hold sweep uses the same hold exclusion as the purge."""
    from preloop.models.models.api_usage import ApiUsage
    from preloop.models.models.session_search_document import (
        SOURCE_KIND_GATEWAY_INTERACTION,
    )

    held = _session(db_session, test_user.account_id, source_id="held-orphan")
    unheld = _session(db_session, test_user.account_id, source_id="unheld-orphan")
    crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=held.id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id="missing-held-usage",
        occurred_at=OCCURRED_AT,
        chunks=_chunks("kept"),
    )
    crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=unheld.id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id="missing-unheld-usage",
        occurred_at=OCCURRED_AT,
        chunks=_chunks("gone"),
    )
    held.legal_hold = True
    db_session.flush()

    deleted = crud_session_search_document.delete_orphans_for_sources(
        db_session,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_model=ApiUsage,
        excluding_held_sessions=True,
        account_id=test_user.account_id,
    )

    assert deleted == 1
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=held.id,
        )
        == 1
    )
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=unheld.id,
        )
        == 0
    )


def test_replace_source_chunks_reuses_supplied_existing_rows(db_session, test_user):
    """The backfill walk can skip a second list_for_source on the same source."""
    from unittest.mock import patch

    session = _session(db_session, test_user.account_id)
    stored = _write(db_session, test_user.account_id, session, "one")
    with patch.object(type(crud_session_search_document), "list_for_source") as listed:
        again = crud_session_search_document.replace_source_chunks(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
            source_id="message-1",
            occurred_at=OCCURRED_AT,
            chunks=_chunks("one"),
            existing=stored,
        )

    listed.assert_not_called()
    assert [row.id for row in again] == [row.id for row in stored]
