"""Every content search leaves a row, and never leaves the query text (#688).

A search over session content greps every captured prompt, response and tool
call the account holds. These tests pin the two properties that make that
defensible: the read is recorded, and the record does not become a second copy
of whatever the searcher typed.
"""

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.models.crud import crud_account, crud_audit_log
from preloop.schemas.session_search import (
    MAX_QUERY_CHARS,
    SessionSearchDegraded,
    SessionSearchRequest,
    SessionSearchResponse,
)
from preloop.services import session_search_audit
from preloop.services.session_search_audit import (
    AUDIT_ACTION,
    AUDIT_RESOURCE_TYPE,
    AUDIT_SCOPE_MAX_CHARS,
    QUERY_TEXT_OPT_IN_KEY,
    SOURCE_API,
    SOURCE_MCP,
    STATUS_DENIED,
    STATUS_FAILURE,
    STATUS_SUCCESS,
    agent_actor,
    applied_filters,
    audited_search,
    build_details,
    query_hash,
    query_text_audit_enabled,
    record_search,
    user_actor,
)

SECRET_QUERY = "acme-deploy-token"


def _response(*, total=0, results=None):
    return SessionSearchResponse(
        query=SECRET_QUERY,
        mode="keyword",
        effective_mode="keyword",
        degraded=SessionSearchDegraded(),
        indexed_through=None,
        total=total,
        limit=20,
        offset=0,
        elapsed_ms=1.0,
        results=results or [],
    )


def _rows(db_session, account_id):
    return crud_audit_log.get_by_account(
        db_session,
        account_id=str(account_id),
        action=AUDIT_ACTION,
        resource_type=AUDIT_RESOURCE_TYPE,
    )


# --- the hash stands in for the query --------------------------------------


def test_the_same_query_hashes_the_same_and_another_query_does_not():
    """The digest is what lets a review group repeats without the text."""
    assert query_hash(SECRET_QUERY) == query_hash(SECRET_QUERY)
    assert query_hash(SECRET_QUERY) != query_hash("acme-deploy-tokens")
    assert query_hash(SECRET_QUERY).startswith("sha256:")
    assert SECRET_QUERY not in query_hash(SECRET_QUERY)


def test_the_hash_ignores_spacing_and_case_the_way_the_query_does():
    """The same search typed twice is the same search, however it was typed."""
    assert query_hash("  rolling   Restart ") == query_hash("rolling restart")


# --- the opt in is off until an account says otherwise ---------------------


def test_the_query_text_opt_in_is_off_by_default():
    assert query_text_audit_enabled(None) is False
    assert query_text_audit_enabled({}) is False
    assert query_text_audit_enabled({"other": True}) is False


def test_only_a_true_boolean_turns_the_query_text_opt_in_on():
    """A truthy string is somebody hoping, not an account deciding."""
    assert query_text_audit_enabled({QUERY_TEXT_OPT_IN_KEY: True}) is True
    assert query_text_audit_enabled({QUERY_TEXT_OPT_IN_KEY: "true"}) is False
    assert query_text_audit_enabled({QUERY_TEXT_OPT_IN_KEY: 1}) is False
    assert query_text_audit_enabled({QUERY_TEXT_OPT_IN_KEY: False}) is False


# --- what a row carries, and what it never carries -------------------------


def test_details_carry_the_mode_the_filters_and_the_count_but_not_the_query():
    details = build_details(
        actor=user_actor(None),
        query=SECRET_QUERY,
        mode="hybrid",
        filters={"source_kind": "transcript_message"},
        result_count=3,
        include_query_text=False,
        effective_mode="keyword",
        total_matched=11,
    )

    assert details["mode"] == "hybrid"
    assert details["effective_mode"] == "keyword"
    assert details["filters"] == {"source_kind": "transcript_message"}
    assert details["result_count"] == 3
    assert details["total_matched"] == 11
    assert details["query_hash"] == query_hash(SECRET_QUERY)
    assert details["query_text_stored"] is False
    assert "query_text" not in details
    assert SECRET_QUERY not in json.dumps(details)


def test_details_carry_the_query_text_only_with_the_opt_in():
    details = build_details(
        actor=user_actor(None),
        query=SECRET_QUERY,
        mode="keyword",
        filters={},
        result_count=0,
        include_query_text=True,
    )

    assert details["query_text"] == SECRET_QUERY
    assert details["query_text_stored"] is True
    assert details["query_hash"] == query_hash(SECRET_QUERY)


def test_opt_in_query_text_is_capped_at_the_search_request_limit():
    """The write path bounds storage even when request validation did not run."""
    oversized = "secret " + ("x" * 2000)
    details = build_details(
        actor=user_actor(None),
        query=oversized,
        mode="keyword",
        filters={},
        result_count=0,
        include_query_text=True,
    )

    assert details["query_text"] == oversized[:MAX_QUERY_CHARS]
    assert len(details["query_text"]) == MAX_QUERY_CHARS
    assert details["query_chars"] == len(oversized)
    assert details["query_hash"] == query_hash(oversized)


def test_an_unknown_scope_echo_is_capped_on_the_row():
    oversized_scope = "everything-" + ("z" * 200)
    details = build_details(
        actor=agent_actor(managed_agent_id="agent-row-1"),
        query=SECRET_QUERY,
        mode="keyword",
        filters={},
        result_count=0,
        include_query_text=False,
        scope=oversized_scope,
        reason="unknown_scope",
    )

    assert details["scope"] == oversized_scope[:AUDIT_SCOPE_MAX_CHARS]
    assert len(details["scope"]) == AUDIT_SCOPE_MAX_CHARS
    assert "query_text" not in details


def test_only_the_filters_that_were_applied_are_recorded():
    """Eight null filters read as eight filters; the set ones are the answer."""
    flow_id = uuid4()
    request = SessionSearchRequest.model_validate(
        {
            "query": SECRET_QUERY,
            "filters": {
                "start_date": datetime(2026, 9, 1, tzinfo=timezone.utc),
                "flow_id": str(flow_id),
            },
        }
    )

    assert applied_filters(request) == {
        "flow_id": str(flow_id),
        "start_date": "2026-09-01T00:00:00+00:00",
    }


# --- the row is written through the audit path -----------------------------


def test_one_search_writes_exactly_one_row_with_the_search_action(
    db_session, test_user
):
    request = SessionSearchRequest.model_validate({"query": SECRET_QUERY})

    audited_search(
        db_session,
        account_id=test_user.account_id,
        request=request,
        actor=user_actor(test_user),
        run=lambda: _response(total=2),
    )

    rows = _rows(db_session, test_user.account_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION
    assert row.resource_type == AUDIT_RESOURCE_TYPE
    assert row.status == STATUS_SUCCESS
    assert row.user_id == test_user.id
    assert row.resource_id == query_hash(SECRET_QUERY)
    assert row.details["source"] == SOURCE_API
    assert row.details["actor_type"] == "user"
    assert row.details["result_count"] == 0
    assert row.details["total_matched"] == 2
    assert SECRET_QUERY not in json.dumps(row.details)


def test_the_opt_in_puts_the_query_text_on_the_row(db_session, test_user):
    account = crud_account.get(db_session, id=test_user.account_id)
    account.meta_data = {QUERY_TEXT_OPT_IN_KEY: True}
    db_session.flush()
    request = SessionSearchRequest.model_validate({"query": SECRET_QUERY})

    audited_search(
        db_session,
        account_id=test_user.account_id,
        request=request,
        actor=user_actor(test_user),
        run=_response,
    )

    row = _rows(db_session, test_user.account_id)[0]
    assert row.details["query_text"] == SECRET_QUERY
    assert row.details["query_hash"] == query_hash(SECRET_QUERY)


# --- refusals and failures are recorded, and never change the answer -------


def test_a_refused_search_is_recorded_as_denied_and_still_refused(
    db_session, test_user
):
    request = SessionSearchRequest.model_validate({"query": SECRET_QUERY})

    def refuse():
        raise HTTPException(status_code=403, detail="nope")

    with pytest.raises(HTTPException) as raised:
        audited_search(
            db_session,
            account_id=test_user.account_id,
            request=request,
            actor=user_actor(test_user),
            run=refuse,
        )

    assert raised.value.status_code == 403
    row = _rows(db_session, test_user.account_id)[0]
    assert row.status == STATUS_DENIED
    assert row.details["reason"] == "http_403"


def test_a_broken_search_is_recorded_as_failure_and_still_raises(db_session, test_user):
    request = SessionSearchRequest.model_validate({"query": SECRET_QUERY})

    def explode():
        raise RuntimeError("the index is on fire")

    with pytest.raises(RuntimeError):
        audited_search(
            db_session,
            account_id=test_user.account_id,
            request=request,
            actor=user_actor(test_user),
            run=explode,
        )

    row = _rows(db_session, test_user.account_id)[0]
    assert row.status == STATUS_FAILURE
    assert row.details["error_type"] == "RuntimeError"


def test_an_audit_write_failure_is_logged_and_swallowed(
    db_session, test_user, monkeypatch, caplog
):
    """A search whose row will not write still answers, and says so in the log."""

    def boom(*args, **kwargs):
        raise RuntimeError("audit table is read only")

    monkeypatch.setattr(crud_audit_log, "log_action", boom)
    request = SessionSearchRequest.model_validate({"query": SECRET_QUERY})

    with caplog.at_level(logging.WARNING):
        response = audited_search(
            db_session,
            account_id=test_user.account_id,
            request=request,
            actor=user_actor(test_user),
            run=lambda: _response(total=4),
        )

    assert response.total == 4
    assert _rows(db_session, test_user.account_id) == []
    assert "Session search audit row could not be written" in caplog.text
    assert SECRET_QUERY not in caplog.text


def test_record_search_reports_a_failed_write_rather_than_raising(
    db_session, test_user, monkeypatch
):
    monkeypatch.setattr(
        crud_audit_log,
        "log_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )

    written = record_search(
        db_session,
        account_id=test_user.account_id,
        actor=user_actor(test_user),
        query=SECRET_QUERY,
        mode="keyword",
        status=STATUS_SUCCESS,
    )

    assert written is False


# --- an agent actor is a different row than a person -----------------------


def test_an_agent_actor_names_itself_and_leaves_the_user_column_empty(
    db_session, test_user
):
    """The audit table has no agent column; borrowing a person's is worse."""
    actor = agent_actor(
        managed_agent_id="agent-row-1",
        api_key_id="key-1",
        runtime_principal_id="agent-caller",
    )
    request = SessionSearchRequest.model_validate({"query": SECRET_QUERY})

    audited_search(
        db_session,
        account_id=test_user.account_id,
        request=request,
        actor=actor,
        scope="own",
        run=_response,
    )

    row = _rows(db_session, test_user.account_id)[0]
    assert row.user_id is None
    assert row.details["actor_type"] == "managed_agent"
    assert row.details["actor_managed_agent_id"] == "agent-row-1"
    assert row.details["actor_runtime_principal_id"] == "agent-caller"
    assert row.details["source"] == SOURCE_MCP
    assert row.details["scope"] == "own"


def test_the_account_document_is_read_through_one_helper(db_session, test_user):
    """The opt in has one reader, so "do we keep search text" has one answer."""
    meta_data = session_search_audit.account_meta_data(
        db_session, account_id=test_user.account_id
    )
    assert query_text_audit_enabled(meta_data) is False
