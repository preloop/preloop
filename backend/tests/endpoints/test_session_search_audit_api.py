"""The search endpoint writes one audit row per call (#688).

The service tests pin what a row carries. These pin that a real request
produces one, through the real endpoint, with the real query running: one row,
the caller on it, no transcript text in it, and an answer that does not change
when the row cannot be written.
"""

import json
import logging
from datetime import datetime, timezone

from preloop.models.crud import crud_account, crud_audit_log, crud_runtime_session
from preloop.models.crud import crud_session_search_document
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.session_search_document import (
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.services.session_search_audit import (
    AUDIT_ACTION,
    AUDIT_RESOURCE_TYPE,
    QUERY_TEXT_OPT_IN_KEY,
    SOURCE_API,
    STATUS_SUCCESS,
    query_hash,
)

SEARCH_URL = "/api/v1/runtime-sessions/search"
BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
#: A query written the way the worst case is written: the thing somebody is
#: hunting for, typed verbatim into the search box.
SECRET_QUERY = "ledger"


def _corpus(db_session, account_id, text="the ledger reconciled cleanly"):
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id="audited",
        session_reference="audited",
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=BASE_AT,
        last_activity_at=BASE_AT,
    )
    crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id="message-1",
        occurred_at=BASE_AT,
        chunks=[SessionSearchChunk(content=text, chunk_index=0, role="assistant")],
    )
    db_session.flush()
    return session


def _rows(db_session, account_id):
    return crud_audit_log.get_by_account(
        db_session,
        account_id=str(account_id),
        action=AUDIT_ACTION,
        resource_type=AUDIT_RESOURCE_TYPE,
    )


def test_one_search_writes_exactly_one_audit_row(client, db_session, test_user):
    _corpus(db_session, test_user.account_id)

    response = client.post(SEARCH_URL, json={"query": SECRET_QUERY})

    assert response.status_code == 200
    rows = _rows(db_session, test_user.account_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION
    assert row.resource_type == AUDIT_RESOURCE_TYPE
    assert row.status == STATUS_SUCCESS
    assert row.user_id == test_user.id
    assert row.details["source"] == SOURCE_API
    assert row.details["actor_type"] == "user"


def test_the_row_carries_the_mode_the_filters_and_the_count(
    client, db_session, test_user
):
    _corpus(db_session, test_user.account_id)

    response = client.post(
        SEARCH_URL,
        json={
            "query": SECRET_QUERY,
            "mode": "hybrid",
            "filters": {"source_kind": SOURCE_KIND_TRANSCRIPT_MESSAGE},
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    details = _rows(db_session, test_user.account_id)[0].details
    assert details["mode"] == "hybrid"
    assert details["effective_mode"] == "keyword"
    assert details["filters"] == {"source_kind": SOURCE_KIND_TRANSCRIPT_MESSAGE}
    assert details["result_count"] == 1
    assert details["total_matched"] == 1


def test_the_row_carries_a_hash_and_not_the_query_with_the_opt_in_off(
    client, db_session, test_user
):
    """Nothing anywhere in the row is the query, which is the whole default."""
    _corpus(db_session, test_user.account_id)

    assert client.post(SEARCH_URL, json={"query": SECRET_QUERY}).status_code == 200

    row = _rows(db_session, test_user.account_id)[0]
    assert row.details["query_hash"] == query_hash(SECRET_QUERY)
    assert row.details["query_text_stored"] is False
    serialised = json.dumps(
        {
            "resource_id": row.resource_id,
            "user_agent": row.user_agent,
            "details": row.details,
        }
    )
    assert SECRET_QUERY not in serialised


def test_the_row_carries_the_query_when_the_account_opted_in(
    client, db_session, test_user
):
    _corpus(db_session, test_user.account_id)
    account = crud_account.get(db_session, id=test_user.account_id)
    account.meta_data = {QUERY_TEXT_OPT_IN_KEY: True}
    db_session.flush()

    assert client.post(SEARCH_URL, json={"query": SECRET_QUERY}).status_code == 200

    details = _rows(db_session, test_user.account_id)[0].details
    assert details["query_text"] == SECRET_QUERY
    assert details["query_text_stored"] is True


def test_the_row_never_carries_snippet_text(client, db_session, test_user):
    """The row says a search happened, not what it saw."""
    _corpus(db_session, test_user.account_id, text="the ledger reconciled cleanly")

    response = client.post(SEARCH_URL, json={"query": SECRET_QUERY})

    assert response.json()["results"][0]["snippets"][0]["text"]
    details = _rows(db_session, test_user.account_id)[0].details
    assert "reconciled cleanly" not in json.dumps(details)


def test_a_search_whose_audit_row_cannot_be_written_still_answers(
    client, db_session, test_user, monkeypatch, caplog
):
    """Auditing observes the search; it is never allowed to break it."""
    _corpus(db_session, test_user.account_id)

    def boom(*args, **kwargs):
        raise RuntimeError("audit table is read only")

    monkeypatch.setattr(crud_audit_log, "log_action", boom)

    with caplog.at_level(logging.WARNING):
        response = client.post(SEARCH_URL, json={"query": SECRET_QUERY})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["results"]) == 1
    assert "Session search audit row could not be written" in caplog.text
    assert _rows(db_session, test_user.account_id) == []


def test_two_searches_write_two_rows(client, db_session, test_user):
    """One row per call, so a count of rows is a count of searches."""
    _corpus(db_session, test_user.account_id)

    client.post(SEARCH_URL, json={"query": SECRET_QUERY})
    client.post(SEARCH_URL, json={"query": "reconciled"})

    rows = _rows(db_session, test_user.account_id)
    assert len(rows) == 2
    assert {row.details["query_hash"] for row in rows} == {
        query_hash(SECRET_QUERY),
        query_hash("reconciled"),
    }


def test_a_rejected_request_body_writes_no_row(client, db_session, test_user):
    """Validation refuses before any content is read, so nothing was searched."""
    response = client.post(SEARCH_URL, json={"query": "   "})

    assert response.status_code == 422
    assert _rows(db_session, test_user.account_id) == []
