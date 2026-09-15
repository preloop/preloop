"""Endpoint tests for ranked search over runtime session content.

The account bound, the validation surface and the POST-only route are all
asserted here rather than in the CRUD tests, because they are the properties a
caller depends on and a serialiser refactor could quietly break.
"""

from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import (
    MAX_SESSION_RESULTS,
    MAX_SNIPPETS_PER_SESSION,
    SessionSearchChunk,
)
from preloop.models.models.session_search_document import (
    SOURCE_KIND_SESSION_SUMMARY,
    SOURCE_KIND_TOOL_CALL,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.schemas.session_search import DEGRADED_SEMANTIC_NOT_ENABLED

SEARCH_URL = "/api/v1/runtime-sessions/search"
BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def _session(db_session, account_id, source_id, *, started_at=BASE_AT):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=started_at,
        last_activity_at=started_at,
    )


def _write(
    db_session,
    account_id,
    session,
    *texts,
    source_id="message-1",
    source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
    occurred_at=BASE_AT,
    **chunk_fields,
):
    crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=source_kind,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[
            SessionSearchChunk(
                content=text, chunk_index=index, role="assistant", **chunk_fields
            )
            for index, text in enumerate(texts)
        ],
    )
    db_session.flush()


def test_another_accounts_sessions_are_never_returned(client, db_session, test_user):
    """The account bound holds with no filters at all."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    mine = _session(db_session, test_user.account_id, "mine")
    theirs = _session(db_session, other_account.id, "theirs")
    _write(
        db_session,
        test_user.account_id,
        mine,
        "the ledger reconciled cleanly",
        source_id="mine-message",
    )
    _write(
        db_session,
        other_account.id,
        theirs,
        "the ledger reconciled cleanly",
        source_id="theirs-message",
    )

    response = client.post(SEARCH_URL, json={"query": "ledger"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [row["runtime_session_id"] for row in payload["results"]] == [str(mine.id)]
    assert str(theirs.id) not in response.text


def test_another_accounts_sessions_are_never_returned_with_filters(
    client, db_session, test_user
):
    """A filter cannot widen the account bound, only narrow inside it.

    The filters here match the other account's rows exactly, so a bound
    applied after filtering rather than inside the query would show up.
    """
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    theirs = _session(db_session, other_account.id, "theirs-filtered")
    _write(
        db_session,
        other_account.id,
        theirs,
        "the ledger reconciled cleanly",
        source_id="theirs-filtered-message",
        provider_name="vendor",
        model_alias="vendor/model-a",
        runtime_principal_id="agent-1",
    )

    response = client.post(
        SEARCH_URL,
        json={
            "query": "ledger",
            "filters": {
                "provider_name": "vendor",
                "model_alias": "vendor/model-a",
                "runtime_principal_id": "agent-1",
                "source_kind": SOURCE_KIND_TRANSCRIPT_MESSAGE,
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["results"] == []


def test_results_are_ordered_by_relevance_not_by_time(client, db_session, test_user):
    """The oldest session comes first when it is the better match."""
    oldest = _session(db_session, test_user.account_id, "oldest")
    newest = _session(
        db_session,
        test_user.account_id,
        "newest",
        started_at=BASE_AT + timedelta(days=30),
    )
    _write(
        db_session,
        test_user.account_id,
        oldest,
        "ledger migration retried, ledger migration retried, ledger migration",
        source_id="oldest-message",
        occurred_at=BASE_AT,
    )
    _write(
        db_session,
        test_user.account_id,
        newest,
        "one passing mention of the ledger",
        source_id="newest-message",
        occurred_at=BASE_AT + timedelta(days=30),
    )

    payload = client.post(SEARCH_URL, json={"query": "ledger"}).json()

    assert [row["runtime_session_id"] for row in payload["results"]] == [
        str(oldest.id),
        str(newest.id),
    ]
    assert (
        payload["results"][0]["last_match_at"] < payload["results"][1]["last_match_at"]
    )


def test_a_session_matching_three_times_outranks_one_matching_once(
    client, db_session, test_user
):
    """The multi chunk bonus is visible on the payload, not just in SQL."""
    spread = _session(db_session, test_user.account_id, "spread")
    single = _session(db_session, test_user.account_id, "single")
    _write(
        db_session,
        test_user.account_id,
        spread,
        "the rollout paused",
        "the rollout resumed",
        "the rollout finished",
        source_id="spread-message",
    )
    _write(
        db_session,
        test_user.account_id,
        single,
        "the rollout paused",
        source_id="single-message",
    )

    payload = client.post(SEARCH_URL, json={"query": "rollout"}).json()

    first, second = payload["results"]
    assert first["runtime_session_id"] == str(spread.id)
    assert first["matched_chunk_count"] == 3
    assert second["matched_chunk_count"] == 1
    assert first["score"] > second["score"]


def test_an_unknown_filter_key_is_a_validation_error(client, db_session, test_user):
    """A dropped filter returns a wider answer than the caller believes."""
    response = client.post(
        SEARCH_URL,
        json={"query": "ledger", "filters": {"nonexistent_column": "value"}},
    )

    assert response.status_code == 422
    assert "nonexistent_column" in response.text


def test_an_unknown_body_key_is_a_validation_error(client, db_session, test_user):
    """The same reasoning applies to the request body itself."""
    response = client.post(SEARCH_URL, json={"query": "ledger", "sort_by": "timestamp"})

    assert response.status_code == 422


def test_an_unknown_source_kind_is_a_validation_error(client, db_session, test_user):
    """A typo in the source kind is rejected, not answered with zero hits."""
    response = client.post(
        SEARCH_URL, json={"query": "ledger", "filters": {"source_kind": "not_a_kind"}}
    )

    assert response.status_code == 422


def test_the_limit_is_capped_at_the_documented_maximum(client, db_session, test_user):
    """Above the ceiling is a validation error, at the ceiling is fine."""
    over = client.post(
        SEARCH_URL, json={"query": "ledger", "limit": MAX_SESSION_RESULTS + 1}
    )
    at_cap = client.post(
        SEARCH_URL, json={"query": "ledger", "limit": MAX_SESSION_RESULTS}
    )
    too_many_snippets = client.post(
        SEARCH_URL,
        json={
            "query": "ledger",
            "max_snippets_per_session": MAX_SNIPPETS_PER_SESSION + 1,
        },
    )

    assert over.status_code == 422
    assert at_cap.status_code == 200
    assert at_cap.json()["limit"] == MAX_SESSION_RESULTS
    assert too_many_snippets.status_code == 422


def test_an_empty_query_is_a_validation_error(client, db_session, test_user):
    """Whitespace is not a request for the whole corpus."""
    assert client.post(SEARCH_URL, json={"query": "   "}).status_code == 422
    assert client.post(SEARCH_URL, json={}).status_code == 422


def test_an_empty_string_filter_is_a_validation_error(client, db_session, test_user):
    """An empty filter must 422, not silently widen the result set."""
    for key in ("model_alias", "provider_name", "runtime_principal_id"):
        response = client.post(
            SEARCH_URL, json={"query": "ledger", "filters": {key: ""}}
        )
        assert response.status_code == 422, key


def test_a_naive_date_filter_is_a_validation_error(client, db_session, test_user):
    """A bound without an offset is not an instant."""
    naive = client.post(
        SEARCH_URL,
        json={"query": "ledger", "filters": {"start_date": "2026-09-01T00:00:00"}},
    )
    aware = client.post(
        SEARCH_URL,
        json={
            "query": "ledger",
            "filters": {"start_date": "2026-09-01T00:00:00+00:00"},
        },
    )
    assert naive.status_code == 422
    assert aware.status_code == 200


def test_snippet_text_can_be_withheld(client, db_session, test_user):
    """With snippet text off, no captured content is on the payload."""
    session = _session(db_session, test_user.account_id, "quiet")
    _write(
        db_session,
        test_user.account_id,
        session,
        "an unmistakable phrase about the ledger",
        source_id="quiet-message",
    )

    response = client.post(
        SEARCH_URL, json={"query": "ledger", "include_snippet_text": False}
    )

    assert response.status_code == 200
    payload = response.json()
    snippets = payload["results"][0]["snippets"]
    assert snippets
    assert all(snippet["text"] is None for snippet in snippets)
    assert "unmistakable" not in response.text


def test_a_snippet_identifies_the_turn_it_matched_in(client, db_session, test_user):
    """Session id, source kind, source id and chunk index open the turn."""
    session = _session(db_session, test_user.account_id, "identity")
    _write(
        db_session,
        test_user.account_id,
        session,
        "grep the audit ledger for the failed write",
        source_id="tool-call-7",
        source_kind=SOURCE_KIND_TOOL_CALL,
        occurred_at=BASE_AT + timedelta(minutes=5),
    )

    payload = client.post(SEARCH_URL, json={"query": "ledger"}).json()

    result = payload["results"][0]
    snippet = result["snippets"][0]
    assert snippet["runtime_session_id"] == str(session.id)
    assert snippet["source_kind"] == SOURCE_KIND_TOOL_CALL
    assert snippet["source_id"] == "tool-call-7"
    assert snippet["chunk_index"] == 0
    assert snippet["occurred_at"].startswith("2026-09-01T09:05")
    assert "<mark>ledger</mark>" in snippet["text"]
    assert result["session_reference"] == "identity"


def test_a_semantic_request_is_answered_with_degraded_keyword_results(
    client, db_session, test_user
):
    """A mode the deployment cannot serve is answered, not refused."""
    session = _session(db_session, test_user.account_id, "degraded")
    _write(
        db_session,
        test_user.account_id,
        session,
        "the ledger reconciled",
        source_id="degraded-message",
    )

    for mode in ("semantic", "hybrid"):
        response = client.post(SEARCH_URL, json={"query": "ledger", "mode": mode})

        assert response.status_code == 200
        payload = response.json()
        assert payload["mode"] == mode
        assert payload["effective_mode"] == "keyword"
        assert payload["degraded"]["semantic"] is False
        assert payload["degraded"]["keyword"] is True
        assert payload["degraded"]["reasons"] == [DEGRADED_SEMANTIC_NOT_ENABLED]
        assert payload["degraded"]["detail"]
        assert [row["runtime_session_id"] for row in payload["results"]] == [
            str(session.id)
        ]


def test_a_keyword_request_is_not_marked_degraded(client, db_session, test_user):
    """Nothing is degraded when the requested mode is the one that ran."""
    payload = client.post(SEARCH_URL, json={"query": "ledger"}).json()

    assert payload["degraded"]["reasons"] == []
    assert payload["degraded"]["detail"] is None
    assert payload["effective_mode"] == "keyword"


def test_an_unknown_mode_is_a_validation_error(client, db_session, test_user):
    """The mode list is closed, so a typo does not silently become keyword."""
    response = client.post(SEARCH_URL, json={"query": "ledger", "mode": "vector"})

    assert response.status_code == 422


def test_the_route_is_post_only_so_the_query_stays_out_of_the_path(app):
    """Query text in a path is query text in every access log on the way.

    Asserted on the published specification rather than on the router
    internals, because the specification is what a client generator reads.
    """
    spec = app.openapi()
    operations = spec["paths"][SEARCH_URL]

    assert set(operations) == {"post"}
    # No path or query parameters at all: there is nowhere for the query text
    # to ride except the body.
    assert "parameters" not in operations["post"]

    body_ref = operations["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    body_schema = spec["components"]["schemas"][body_ref.rsplit("/", 1)[-1]]
    assert "query" in body_schema["properties"]
    assert body_schema["required"] == ["query"]


def test_the_response_carries_the_contract_fields(client, db_session, test_user):
    """Query, mode, degraded block, freshness, total and timing are all there."""
    session = _session(db_session, test_user.account_id, "contract")
    _write(
        db_session,
        test_user.account_id,
        session,
        "the ledger reconciled",
        source_id="contract-message",
        occurred_at=BASE_AT,
    )

    payload = client.post(
        SEARCH_URL, json={"query": "  the   ledger  ", "offset": 0}
    ).json()

    assert payload["query"] == "the ledger"
    assert payload["mode"] == "keyword"
    assert payload["total"] == 1
    assert payload["limit"] == 20
    assert payload["offset"] == 0
    assert payload["elapsed_ms"] >= 0
    assert payload["indexed_through"].startswith("2026-09-01T09:00")
    assert set(payload["degraded"]) == {"keyword", "semantic", "reasons", "detail"}


def test_paging_walks_the_ranked_sessions(client, db_session, test_user):
    """``total`` counts sessions, and the offset moves through them."""
    for index in range(3):
        session = _session(db_session, test_user.account_id, f"page-{index}")
        _write(
            db_session,
            test_user.account_id,
            session,
            "ledger entry",
            source_id=f"page-message-{index}",
        )

    first = client.post(SEARCH_URL, json={"query": "ledger", "limit": 2}).json()
    second = client.post(
        SEARCH_URL, json={"query": "ledger", "limit": 2, "offset": 2}
    ).json()

    assert first["total"] == 3
    assert second["total"] == 3
    assert len(first["results"]) == 2
    assert len(second["results"]) == 1
    seen = {row["runtime_session_id"] for row in first["results"] + second["results"]}
    assert len(seen) == 3


def test_a_time_range_filter_narrows_the_answer(client, db_session, test_user):
    """The time range filters chunks, not sessions, and it is half open."""
    session = _session(db_session, test_user.account_id, "ranged")
    _write(
        db_session,
        test_user.account_id,
        session,
        "early ledger note",
        source_id="ranged-early",
        occurred_at=BASE_AT,
    )
    _write(
        db_session,
        test_user.account_id,
        session,
        "late ledger note",
        source_id="ranged-late",
        occurred_at=BASE_AT + timedelta(days=2),
    )

    payload = client.post(
        SEARCH_URL,
        json={
            "query": "ledger",
            "filters": {"start_date": (BASE_AT + timedelta(days=1)).isoformat()},
        },
    ).json()

    assert payload["total"] == 1
    assert payload["results"][0]["matched_chunk_count"] == 1
    assert payload["results"][0]["snippets"][0]["source_id"] == "ranged-late"


def test_a_session_is_found_by_a_word_only_its_summary_carries(
    client, db_session, test_user
):
    """The generated summary is searchable end to end, transcript aside."""
    session = _session(db_session, test_user.account_id, "summary-only")
    _write(
        db_session,
        test_user.account_id,
        session,
        "step 4128 finished with exit code 0",
        source_id="summary-only-message",
    )
    crud_runtime_session.update_session_title(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        title="Nightly reconciliation",
        summary="The agent reconciled the invoices and retried one payout.",
        commit=False,
    )
    db_session.flush()

    response = client.post(SEARCH_URL, json={"query": "payout"})

    assert response.status_code == 200
    payload = response.json()
    assert [row["runtime_session_id"] for row in payload["results"]] == [
        str(session.id)
    ]
    snippets = payload["results"][0]["snippets"]
    assert [snippet["source_kind"] for snippet in snippets] == [
        SOURCE_KIND_SESSION_SUMMARY
    ]
    assert snippets[0]["source_id"] == str(session.id)
