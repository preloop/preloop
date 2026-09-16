"""Tests for the session search corpus chunk writers."""

import logging
from datetime import datetime, timezone
from unittest.mock import patch

from preloop.config import settings
from preloop.models.crud import (
    crud_api_usage,
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_session_search_document,
)
from preloop.models.models.session_search_document import (
    REDACTION_STATE_METADATA_ONLY,
    REDACTION_STATE_REDACTED,
    SOURCE_KIND_GATEWAY_INTERACTION,
    SOURCE_KIND_SESSION_SUMMARY,
    SOURCE_KIND_TOOL_CALL,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.services import session_search_index
from preloop.services.session_search_index import (
    CHUNK_OVERLAP_CHARS,
    CHUNK_SIZE_CHARS,
    chunk_text,
    index_gateway_interaction,
    index_transcript_message,
)

OCCURRED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


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


def _gateway_usage(db_session, test_user, session):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        meta_data={"requested_model": "openai/gpt-5", "endpoint_kind": "responses"},
    )


def _long_fixture() -> str:
    """Return deterministic text that is longer than two chunks."""
    words = [f"word{index:04d}" for index in range(700)]
    return " ".join(words)


def test_chunk_boundaries_are_deterministic_for_the_same_input():
    """The same text always cuts the same way, so hashes stay stable."""
    fixture = _long_fixture()

    first = chunk_text(fixture)
    second = chunk_text(fixture)

    assert len(first) > 2
    assert first == second
    assert all(len(chunk) <= CHUNK_SIZE_CHARS for chunk in first)
    # Consecutive chunks overlap, so a phrase across a cut stays findable.
    assert first[1][:20] in fixture[: CHUNK_SIZE_CHARS + CHUNK_OVERLAP_CHARS]
    assert fixture.startswith(first[0])
    assert fixture.endswith(first[-1])


def test_chunk_boundaries_are_stable_across_reindexing(db_session, test_user):
    """Re-indexing unchanged content rewrites nothing."""
    session = _session(db_session, test_user.account_id)
    fixture = _long_fixture()

    first = index_transcript_message(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_id="message-long",
        text=fixture,
        role="user",
        occurred_at=OCCURRED_AT,
    )
    first_ids = [str(row.id) for row in first]
    repeat = index_transcript_message(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_id="message-long",
        text=fixture,
        role="user",
        occurred_at=OCCURRED_AT,
    )

    assert len(first) > 2
    assert [str(row.id) for row in repeat] == first_ids
    assert [row.chunk_index for row in repeat] == list(range(len(first)))


def test_capture_disabled_writes_a_metadata_only_chunk(db_session, test_user):
    """A chunk written with capture off records the shape, not the content."""
    session = _session(db_session, test_user.account_id)

    with patch.object(settings, "model_gateway_capture_content", False):
        stored = index_transcript_message(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            source_id="message-private",
            text="rotate the staging database password tonight",
            role="user",
            occurred_at=OCCURRED_AT,
        )

    assert len(stored) == 1
    chunk = stored[0]
    assert chunk.redaction_state == REDACTION_STATE_METADATA_ONLY
    assert "rotate the staging database" not in chunk.content
    assert "content_captured: false" in chunk.content
    assert chunk.meta_data["content_captured"] is False
    assert chunk.source_kind == SOURCE_KIND_TRANSCRIPT_MESSAGE


def test_gateway_payload_with_a_credential_is_stored_redacted(db_session, test_user):
    """The shipped gateway sanitiser masks credentials before storage."""
    session = _session(db_session, test_user.account_id)
    usage = _gateway_usage(db_session, test_user, session)

    with patch.object(settings, "model_gateway_capture_content", True):
        stored = index_gateway_interaction(
            db_session,
            usage=usage,
            request_payload={
                "input": "summarise the release",
                "api_key": "sk-not-a-real-key",
                "nested": {"session_token": "tok-not-a-real-token"},
            },
            response_payload={"output_text": "release summarised"},
        )

    assert stored
    content = "\n".join(chunk.content for chunk in stored)
    assert "request.api_key: [redacted]" in content
    assert "request.nested.session_token: [redacted]" in content
    assert "sk-not-a-real-key" not in content
    assert "tok-not-a-real-token" not in content
    assert stored[0].redaction_state == REDACTION_STATE_REDACTED
    assert stored[0].source_kind == SOURCE_KIND_GATEWAY_INTERACTION
    assert stored[0].source_id == str(usage.id)
    assert stored[0].model_alias == "openai/gpt-5"
    assert stored[0].provider_name == "openai"
    assert stored[0].status == "200"


def test_free_text_credential_is_stored_redacted(db_session, test_user):
    """Free text sources are redacted too, not just gateway payloads."""
    session = _session(db_session, test_user.account_id)

    stored = index_transcript_message(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_id="message-secret",
        text="use api_key=sk-not-a-real-key when you call the service",
        role="user",
        occurred_at=OCCURRED_AT,
    )

    assert len(stored) == 1
    assert "sk-not-a-real-key" not in stored[0].content
    assert "[redacted]" in stored[0].content
    assert stored[0].redaction_state == REDACTION_STATE_REDACTED


def test_bare_provider_key_in_free_text_is_stored_redacted(db_session, test_user):
    """A provider key pasted without a label is still masked."""
    session = _session(db_session, test_user.account_id)
    bare_key = "sk-abcdefghijklmnopqrstuvwxyz123456"

    stored = index_transcript_message(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_id="message-bare-key",
        text=f"paste {bare_key} into the client",
        role="user",
        occurred_at=OCCURRED_AT,
    )

    assert len(stored) == 1
    assert bare_key not in stored[0].content
    assert "[redacted]" in stored[0].content
    assert stored[0].redaction_state == REDACTION_STATE_REDACTED


def test_pem_private_key_in_free_text_is_stored_redacted(db_session, test_user):
    """A PEM private key block in free text is not stored verbatim."""
    session = _session(db_session, test_user.account_id)
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0secretpayloadnotreal\n"
        "-----END RSA PRIVATE KEY-----"
    )

    stored = index_transcript_message(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_id="message-pem",
        text=f"rotate this key:\n{pem}",
        role="user",
        occurred_at=OCCURRED_AT,
    )

    assert len(stored) == 1
    assert "MIIEowIBAAKCAQEA0secretpayloadnotreal" not in stored[0].content
    assert "BEGIN RSA PRIVATE KEY" not in stored[0].content
    assert "[redacted]" in stored[0].content
    assert stored[0].redaction_state == REDACTION_STATE_REDACTED


def test_writer_failure_never_fails_the_call_that_triggered_it(
    db_session, test_user, caplog
):
    """A forced failure inside the writer is logged, swallowed and contained."""
    session = _session(db_session, test_user.account_id)
    usage = _gateway_usage(db_session, test_user, session)

    with patch.object(
        crud_session_search_document,
        "replace_source_chunks",
        side_effect=RuntimeError("corpus is unavailable"),
    ):
        with caplog.at_level(logging.WARNING):
            stored = index_gateway_interaction(
                db_session,
                usage=usage,
                request_payload={"input": "a prompt"},
                response_payload={"output_text": "an answer"},
            )

    assert stored == []
    assert any(
        "Session search indexing failed" in record.message for record in caplog.records
    )
    # The transaction that triggered the write is still usable, and the row
    # the gateway call recorded is still there.
    assert crud_api_usage.get(db_session, id=str(usage.id)).id == usage.id
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 0
    )


def test_failed_corpus_commit_rolls_back_before_swallow(db_session, test_user, caplog):
    """A failed outer commit recovers the session, then the writer swallows."""
    session = _session(db_session, test_user.account_id)
    usage = _gateway_usage(db_session, test_user, session)
    rollback_calls: list[bool] = []
    original_rollback = db_session.rollback

    def _rollback() -> None:
        rollback_calls.append(True)
        original_rollback()

    with patch.object(
        db_session, "commit", side_effect=RuntimeError("corpus commit refused")
    ):
        with patch.object(db_session, "rollback", side_effect=_rollback):
            with caplog.at_level(logging.WARNING):
                stored = index_gateway_interaction(
                    db_session,
                    usage=usage,
                    request_payload={"input": "a prompt"},
                    response_payload={"output_text": "an answer"},
                    commit=True,
                )

    assert stored == []
    assert rollback_calls
    assert any(
        "Session search indexing failed" in record.message for record in caplog.records
    )
    # The next query must not raise PendingRollbackError.
    assert crud_api_usage.get(db_session, id=str(usage.id)).id == usage.id


def test_kill_switch_writes_nothing_and_logs_no_failure(db_session, test_user, caplog):
    """With indexing disabled nothing is written and nothing is logged."""
    session = _session(db_session, test_user.account_id)
    usage = _gateway_usage(db_session, test_user, session)

    with patch.object(settings, "session_search_index_enabled", False):
        assert session_search_index.indexing_enabled() is False
        with caplog.at_level(logging.WARNING):
            assert (
                index_gateway_interaction(
                    db_session,
                    usage=usage,
                    request_payload={"input": "a prompt"},
                    response_payload={"output_text": "an answer"},
                )
                == []
            )
            assert (
                index_transcript_message(
                    db_session,
                    account_id=test_user.account_id,
                    runtime_session_id=session.id,
                    source_id="message-off",
                    text="nothing to see",
                    role="user",
                    occurred_at=OCCURRED_AT,
                )
                == []
            )

    assert [record.message for record in caplog.records] == []
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 0
    )


def test_tool_call_activity_is_indexed_where_it_is_written(db_session, test_user):
    """Logging a tool call writes its chunk on the same path."""
    session = _session(db_session, test_user.account_id)

    activity = crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        server_name="filesystem",
        tool_name="read_file",
        status="success",
        summary="read the deployment checklist",
        commit=False,
    )

    chunks = crud_session_search_document.list_for_source(
        db_session, source_kind=SOURCE_KIND_TOOL_CALL, source_id=str(activity.id)
    )
    assert len(chunks) == 1
    assert "tool_name: read_file" in chunks[0].content
    assert "read the deployment checklist" in chunks[0].content
    assert chunks[0].status == "success"
    assert chunks[0].role == "tool"


def test_session_summary_is_indexed_where_it_is_written(db_session, test_user):
    """Persisting a session title and summary writes its chunk."""
    session = _session(db_session, test_user.account_id)

    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Release checklist review",
        summary="The agent reviewed the release checklist and fixed two items.",
        commit=False,
    )

    chunks = crud_session_search_document.list_for_source(
        db_session,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=str(session.id),
    )
    assert len(chunks) == 1
    assert "title: Release checklist review" in chunks[0].content
    assert "fixed two items" in chunks[0].content
    hits = crud_session_search_document.search_account_chunks(
        db_session,
        account_id=test_user.account_id,
        query="checklist",
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
    )
    assert [row.id for row in hits] == [chunks[0].id]
    assert chunks[0].account_id == test_user.account_id
    assert str(chunks[0].runtime_session_id) == str(session.id)
    assert chunks[0].occurred_at is not None


def test_regenerated_title_replaces_the_single_summary_chunk(db_session, test_user):
    """A second title for the same session rewrites its one chunk."""
    session = _session(db_session, test_user.account_id, source_id="session-regen")
    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="First attempt",
        summary="The agent drafted the migration plan.",
        commit=False,
    )

    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Second attempt",
        summary="The agent rewrote the migration plan and ran it.",
        commit=False,
    )

    chunks = crud_session_search_document.list_for_source(
        db_session,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=str(session.id),
    )
    assert len(chunks) == 1
    assert "title: Second attempt" in chunks[0].content
    assert "First attempt" not in chunks[0].content
    assert "rewrote the migration plan" in chunks[0].content


def test_regenerating_identical_text_writes_nothing(db_session, test_user):
    """An unchanged regeneration leaves the stored chunk exactly as it was."""
    session = _session(db_session, test_user.account_id, source_id="session-same")
    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Stable title",
        summary="The agent reconciled the ledger.",
        commit=False,
    )
    before = crud_session_search_document.list_for_source(
        db_session,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=str(session.id),
    )
    assert len(before) == 1
    before_id = before[0].id
    before_hash = before[0].content_hash
    before_occurred_at = before[0].occurred_at

    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Stable title",
        summary="The agent reconciled the ledger.",
        commit=False,
    )

    after = crud_session_search_document.list_for_source(
        db_session,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=str(session.id),
    )
    assert len(after) == 1
    assert after[0].id == before_id
    assert after[0].content_hash == before_hash
    assert after[0].occurred_at == before_occurred_at


def test_cleared_title_and_summary_leave_no_stale_chunk(db_session, test_user):
    """Clearing what described a session removes its chunk instead of emptying it."""
    session = _session(db_session, test_user.account_id, source_id="session-cleared")
    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Temporary title",
        summary="The agent inspected the staging queue.",
        commit=False,
    )
    assert (
        len(
            crud_session_search_document.list_for_source(
                db_session,
                source_kind=SOURCE_KIND_SESSION_SUMMARY,
                source_id=str(session.id),
            )
        )
        == 1
    )

    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="",
        summary="",
        commit=False,
    )

    assert (
        crud_session_search_document.list_for_source(
            db_session,
            source_kind=SOURCE_KIND_SESSION_SUMMARY,
            source_id=str(session.id),
        )
        == []
    )


def test_cleared_summary_keeps_the_title_and_drops_the_summary_text(
    db_session, test_user
):
    """A cleared summary stops answering searches for its own words."""
    session = _session(db_session, test_user.account_id, source_id="session-summary")
    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Queue inspection",
        summary="The agent found an orphaned dispatch record.",
        commit=False,
    )

    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Queue inspection",
        summary="",
        commit=False,
    )

    chunks = crud_session_search_document.list_for_source(
        db_session,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=str(session.id),
    )
    assert len(chunks) == 1
    assert "title: Queue inspection" in chunks[0].content
    assert "orphaned dispatch record" not in chunks[0].content


def test_title_written_as_none_for_an_undescribed_session_writes_nothing(
    db_session, test_user
):
    """A title job that produced nothing leaves the corpus untouched."""
    session = _session(db_session, test_user.account_id, source_id="session-none")

    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title=None,
        title_request_count=4,
        commit=False,
    )

    assert (
        crud_session_search_document.list_for_source(
            db_session,
            source_kind=SOURCE_KIND_SESSION_SUMMARY,
            source_id=str(session.id),
        )
        == []
    )


def test_summary_chunk_failure_keeps_the_title_write(db_session, test_user, caplog):
    """A raising corpus writer is logged and the title still persists."""
    session = _session(db_session, test_user.account_id, source_id="session-broken")

    with patch.object(
        crud_session_search_document,
        "replace_source_chunks",
        side_effect=RuntimeError("corpus is unavailable"),
    ):
        with caplog.at_level(logging.WARNING):
            updated = crud_runtime_session.update_session_title(
                db_session,
                account_id=str(test_user.account_id),
                runtime_session_id=str(session.id),
                title="Survives the corpus",
                summary="The agent shipped the change anyway.",
                commit=False,
            )

    assert updated is not None
    assert any(
        "Session search indexing failed" in record.message for record in caplog.records
    )
    stored = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
    )
    assert stored.title == "Survives the corpus"
    assert stored.summary == "The agent shipped the change anyway."
    assert (
        crud_session_search_document.list_for_source(
            db_session,
            source_kind=SOURCE_KIND_SESSION_SUMMARY,
            source_id=str(session.id),
        )
        == []
    )


def test_summary_indexing_error_outside_the_writer_keeps_the_title_write(
    db_session, test_user, caplog
):
    """Even a failure before the writer's own swallow never fails a title."""
    session = _session(db_session, test_user.account_id, source_id="session-raise")

    with patch.object(
        session_search_index,
        "index_session_summary",
        side_effect=RuntimeError("indexing module is broken"),
    ):
        with caplog.at_level(logging.WARNING):
            updated = crud_runtime_session.update_session_title(
                db_session,
                account_id=str(test_user.account_id),
                runtime_session_id=str(session.id),
                title="Still written",
                summary="The agent finished the run.",
                commit=False,
            )

    assert updated is not None
    assert updated.title == "Still written"
    assert any(
        "Session summary indexing failed" in record.message for record in caplog.records
    )


def test_cleanup_failure_is_logged_and_swallowed(db_session, test_user, caplog):
    """A failing stale-chunk delete never fails the title write either."""
    session = _session(db_session, test_user.account_id, source_id="session-cleanup")

    with patch.object(
        crud_session_search_document,
        "delete_for_source",
        side_effect=RuntimeError("corpus is unavailable"),
    ):
        with caplog.at_level(logging.WARNING):
            updated = crud_runtime_session.update_session_title(
                db_session,
                account_id=str(test_user.account_id),
                runtime_session_id=str(session.id),
                title="",
                summary="",
                commit=False,
            )

    assert updated is not None
    assert any(
        "Session summary chunk cleanup failed" in record.message
        for record in caplog.records
    )


def test_capture_off_summary_only_session_leaves_no_header_chunk(db_session, test_user):
    """With capture off, a summary and no title is nothing searchable."""
    session = _session(db_session, test_user.account_id, source_id="session-capture")
    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Temporary title",
        summary="The agent inspected a private payload.",
        commit=False,
    )
    assert (
        len(
            crud_session_search_document.list_for_source(
                db_session,
                source_kind=SOURCE_KIND_SESSION_SUMMARY,
                source_id=str(session.id),
            )
        )
        == 1
    )

    with patch.object(settings, "model_gateway_capture_content", False):
        crud_runtime_session.update_session_title(
            db_session,
            account_id=str(test_user.account_id),
            runtime_session_id=str(session.id),
            title="",
            summary="The agent inspected a private payload.",
            commit=False,
        )

    assert (
        crud_session_search_document.list_for_source(
            db_session,
            source_kind=SOURCE_KIND_SESSION_SUMMARY,
            source_id=str(session.id),
        )
        == []
    )


def test_capture_off_keeps_the_title_and_omits_the_summary_text(db_session, test_user):
    """The title is metadata; capture off still stores it and not the summary."""
    session = _session(db_session, test_user.account_id, source_id="session-meta")

    with patch.object(settings, "model_gateway_capture_content", False):
        crud_runtime_session.update_session_title(
            db_session,
            account_id=str(test_user.account_id),
            runtime_session_id=str(session.id),
            title="Public title",
            summary="The agent read a private payload.",
            commit=False,
        )

    chunks = crud_session_search_document.list_for_source(
        db_session,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=str(session.id),
    )
    assert len(chunks) == 1
    assert "title: Public title" in chunks[0].content
    assert "private payload" not in chunks[0].content


def test_kill_switch_still_drops_a_cleared_summary_chunk(db_session, test_user):
    """Clearing a session still removes its chunk while indexing is disabled."""
    session = _session(db_session, test_user.account_id, source_id="session-kill")
    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Temporary title",
        summary="The agent inspected the staging queue.",
        commit=False,
    )
    assert (
        len(
            crud_session_search_document.list_for_source(
                db_session,
                source_kind=SOURCE_KIND_SESSION_SUMMARY,
                source_id=str(session.id),
            )
        )
        == 1
    )

    with patch.object(settings, "session_search_index_enabled", False):
        crud_runtime_session.update_session_title(
            db_session,
            account_id=str(test_user.account_id),
            runtime_session_id=str(session.id),
            title="",
            summary="",
            commit=False,
        )

    assert (
        crud_session_search_document.list_for_source(
            db_session,
            source_kind=SOURCE_KIND_SESSION_SUMMARY,
            source_id=str(session.id),
        )
        == []
    )


def test_kill_switch_does_not_write_a_new_summary_chunk(db_session, test_user):
    """The kill switch still parks new writes. Only cleanup stays live."""
    session = _session(db_session, test_user.account_id, source_id="session-off")

    with patch.object(settings, "session_search_index_enabled", False):
        crud_runtime_session.update_session_title(
            db_session,
            account_id=str(test_user.account_id),
            runtime_session_id=str(session.id),
            title="Parked title",
            summary="The agent finished the run.",
            commit=False,
        )

    assert (
        crud_session_search_document.list_for_source(
            db_session,
            source_kind=SOURCE_KIND_SESSION_SUMMARY,
            source_id=str(session.id),
        )
        == []
    )
