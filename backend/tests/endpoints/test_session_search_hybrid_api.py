"""Semantic and hybrid search over session content, through the endpoint.

Every test here is a claim a caller can rely on: which half of the search put
a result in the answer, that the same query twice is the same order, that a
query is never scored against another model's vectors, and that every case
which narrows coverage says so in the degraded block instead of quietly
returning less. No test reaches an embedding provider; a counting fake stands
in for one, which is also how "paging does not re-embed" is asserted.
"""

from datetime import datetime, timedelta, timezone

import pytest

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
    EMBEDDING_SCOPE_FULL,
    EMBEDDING_SCOPE_SUMMARIES_ONLY,
    PROVIDER_LOCAL,
)
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    SOURCE_KIND_SESSION_SUMMARY,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.schemas.session_search import (
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE,
    DEGRADED_SEMANTIC_DAILY_CAP,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SEMANTIC_MODEL_MISMATCH,
    DEGRADED_SEMANTIC_NO_VECTORS,
    DEGRADED_SEMANTIC_NOT_ENABLED,
    DEGRADED_SEMANTIC_PROVIDER_ERROR,
)
from preloop.services import session_search_semantic
from preloop.services.session_embedding import (
    SESSION_EMBEDDING_PURPOSE,
    EmbeddingProviderError,
)

SEARCH_URL = "/api/v1/runtime-sessions/search"
BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
MODEL_IDENTITY = "local:test-embed@1536"
OTHER_MODEL_IDENTITY = "local:other-embed@1536"

#: The words the keyword half matches on, and text that shares none of them.
KEYWORD_TEXT = "the ledger reconciliation finished cleanly"
SEMANTIC_TEXT = "nightly books balanced with no manual steps"
BOTH_TEXT = "ledger reconciliation queued behind the nightly job"
QUERY = "ledger reconciliation"


def _axis(index: int) -> list:
    """A unit vector on one axis, so every similarity here is exact."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


def _blend(first: int, second: int) -> list:
    """Halfway between two axes: cosine 0.7071 with either of them."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[first] = 1.0
    vector[second] = 1.0
    return vector


#: The vector the fake provider returns for any query, so a chunk carrying
#: ``_axis(0)`` is an exact semantic match and an orthogonal one scores zero.
QUERY_VECTOR = _axis(0)


class CountingProvider:
    """An embedding provider that only counts, so paging can be asserted."""

    def __init__(self, *, fails: bool = False):
        self.model = "test-embed"
        self.calls = 0
        self.fails = fails
        self.last_usage = {"prompt_tokens": 5}

    def embed(self, texts):
        self.calls += 1
        if self.fails:
            raise EmbeddingProviderError("the provider refused this batch")
        return [list(QUERY_VECTOR) for _ in texts]


@pytest.fixture(autouse=True)
def clear_query_cache():
    """A cached query vector must not leak from one test into the next."""
    session_search_semantic.reset_query_embedding_cache()
    yield
    session_search_semantic.reset_query_embedding_cache()


@pytest.fixture
def provider(monkeypatch):
    """Stand a counting fake in for whatever the account's setting names."""
    fake = CountingProvider()
    monkeypatch.setattr(
        session_search_semantic, "build_provider", lambda setting: fake, raising=True
    )
    return fake


def _opt_in(db_session, account_id, **kwargs):
    return crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_LOCAL,
        model_identifier="test-embed",
        **kwargs,
    )


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
    text,
    *,
    source_id="message-1",
    occurred_at=BASE_AT,
    vector=None,
    model_identity=MODEL_IDENTITY,
    source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
):
    """Write one chunk, and give it a vector when the test wants one."""
    rows = crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=source_kind,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[SessionSearchChunk(content=text, role="assistant")],
    )
    if vector is not None:
        crud_session_search_document.store_embeddings(
            db_session, vectors=[(rows[0], vector)], model_identity=model_identity
        )
    db_session.flush()
    return rows[0]


def _corpus(db_session, account_id, *, model_identity=MODEL_IDENTITY):
    """Three sessions: one only the words find, one only the vector, one both."""
    keyword_only = _session(db_session, account_id, "keyword-only")
    semantic_only = _session(db_session, account_id, "semantic-only")
    both = _session(db_session, account_id, "both-halves")
    _write(
        db_session,
        account_id,
        keyword_only,
        KEYWORD_TEXT,
        source_id="message-keyword",
        vector=_axis(11),
        model_identity=model_identity,
    )
    _write(
        db_session,
        account_id,
        semantic_only,
        SEMANTIC_TEXT,
        source_id="message-semantic",
        vector=_axis(0),
        model_identity=model_identity,
    )
    _write(
        db_session,
        account_id,
        both,
        BOTH_TEXT,
        source_id="message-both",
        vector=_blend(0, 5),
        model_identity=model_identity,
    )
    return keyword_only, semantic_only, both


def _search(client, **body):
    payload = {"query": QUERY, "mode": "hybrid"}
    payload.update(body)
    return client.post(SEARCH_URL, json=payload)


def _reasons(payload):
    return payload["degraded"]["reasons"]


def _by_session(payload):
    return {row["runtime_session_id"]: row for row in payload["results"]}


def test_hybrid_returns_both_halves_with_the_reason_each_came_from(
    client, db_session, test_user, provider
):
    """The criterion this whole feature exists for, stated as one request.

    A session only the words find, a session only the vector finds and a
    session both find are all in the answer, each carrying which half put it
    there.
    """
    _opt_in(db_session, test_user.account_id)
    keyword_only, semantic_only, both = _corpus(db_session, test_user.account_id)

    response = _search(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_mode"] == "hybrid"
    assert _reasons(payload) == []
    assert payload["degraded"]["keyword"] is True
    assert payload["degraded"]["semantic"] is True
    results = _by_session(payload)
    assert set(results) == {
        str(keyword_only.id),
        str(semantic_only.id),
        str(both.id),
    }
    assert results[str(keyword_only.id)]["match_reason"] == "keyword"
    assert results[str(semantic_only.id)]["match_reason"] == "semantic"
    assert results[str(both.id)]["match_reason"] == "both"
    assert provider.calls == 1


def test_a_semantic_only_hit_carries_its_similarity_and_no_keyword_score(
    client, db_session, test_user, provider
):
    """A number a caller can read, and no number a caller cannot."""
    _opt_in(db_session, test_user.account_id)
    _, semantic_only, _ = _corpus(db_session, test_user.account_id)

    payload = _search(client).json()

    row = _by_session(payload)[str(semantic_only.id)]
    assert row["similarity"] == pytest.approx(1.0, abs=1e-6)
    assert row["keyword_score"] is None
    assert row["matched_chunk_count"] == 0
    assert row["semantic_chunk_count"] == 1
    assert row["snippets"][0]["match_reason"] == "semantic"
    assert row["snippets"][0]["similarity"] == pytest.approx(1.0, abs=1e-6)
    assert row["session_reference"] == "semantic-only"


def test_a_semantic_artifact_hit_carries_its_artifact_ref(
    client, db_session, test_user, provider
):
    """The vector half builds the same artifact identity the keyword half does."""
    _opt_in(db_session, test_user.account_id)
    session = _session(db_session, test_user.account_id, "semantic-artifact")
    rows = crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        source_kind="artifact",
        source_id="artifact-sem-1",
        occurred_at=BASE_AT,
        chunks=[
            SessionSearchChunk(
                content=SEMANTIC_TEXT,
                role="artifact",
                meta_data={
                    "artifact_id": "artifact-sem-1",
                    "activity_id": "activity-sem-1",
                    "kind": "transcript",
                    "name": "books.vtt",
                    "content_type": "text/vtt",
                    "tool_name": "record_call",
                    "labels": {"site": "nord"},
                    "cue_start": 12.0,
                },
            )
        ],
    )
    crud_session_search_document.store_embeddings(
        db_session, vectors=[(rows[0], QUERY_VECTOR)], model_identity=MODEL_IDENTITY
    )
    db_session.flush()

    payload = _search(client, mode="semantic").json()

    [snippet] = _by_session(payload)[str(session.id)]["snippets"]
    assert snippet["source_kind"] == "artifact"
    assert snippet["artifact"] == {
        "artifact_id": "artifact-sem-1",
        "activity_id": "activity-sem-1",
        "kind": "transcript",
        "name": "books.vtt",
        "content_type": "text/vtt",
        "tool_name": "record_call",
        "labels": {"site": "nord"},
        "cue_start": 12.0,
        "text_truncated": False,
    }


def test_a_keyword_only_hit_carries_no_similarity(
    client, db_session, test_user, provider
):
    """The vector half never scored it, so it has no similarity to report."""
    _opt_in(db_session, test_user.account_id)
    keyword_only, _, _ = _corpus(db_session, test_user.account_id)

    payload = _search(client).json()

    row = _by_session(payload)[str(keyword_only.id)]
    assert row["similarity"] is None
    assert row["keyword_score"] is not None
    assert row["snippets"][0]["match_reason"] == "keyword"
    assert row["snippets"][0]["similarity"] is None


def test_a_chunk_both_halves_found_is_one_snippet_marked_both(
    client, db_session, test_user, provider
):
    """The same chunk twice under two labels would be a worse answer."""
    _opt_in(db_session, test_user.account_id)
    _, _, both = _corpus(db_session, test_user.account_id)

    payload = _search(client).json()

    snippets = _by_session(payload)[str(both.id)]["snippets"]
    assert len(snippets) == 1
    assert snippets[0]["match_reason"] == "both"
    assert snippets[0]["similarity"] == pytest.approx(0.7071, abs=1e-3)
    assert "<mark>" in (snippets[0]["text"] or "")


def test_the_same_query_twice_returns_the_same_order(
    client, db_session, test_user, provider
):
    """Tied scores are broken by session id, so paging cannot shuffle."""
    _opt_in(db_session, test_user.account_id)
    for index in range(4):
        session = _session(db_session, test_user.account_id, f"tied-{index}")
        _write(
            db_session,
            test_user.account_id,
            session,
            KEYWORD_TEXT,
            source_id=f"message-tied-{index}",
            vector=_axis(0),
        )

    first = _search(client).json()
    second = _search(client).json()

    order = [row["runtime_session_id"] for row in first["results"]]
    assert len(order) == 4
    assert [row["score"] for row in first["results"]] == [
        row["score"] for row in second["results"]
    ]
    assert order == [row["runtime_session_id"] for row in second["results"]]
    assert order == sorted(order)


def test_a_query_is_never_scored_against_another_models_vectors(
    client, db_session, test_user, provider
):
    """A distance between two models' spaces is not a similarity."""
    _opt_in(db_session, test_user.account_id)
    keyword_only, semantic_only, both = _corpus(
        db_session, test_user.account_id, model_identity=OTHER_MODEL_IDENTITY
    )

    payload = _search(client).json()

    assert payload["effective_mode"] == "hybrid"
    assert DEGRADED_SEMANTIC_MODEL_MISMATCH in _reasons(payload)
    assert payload["degraded"]["semantic"] is False
    assert payload["embedded_through"] is None
    results = _by_session(payload)
    # The vector half contributed nothing, so the session only it could have
    # found is absent and every result is a keyword one.
    assert str(semantic_only.id) not in results
    assert set(results) == {str(keyword_only.id), str(both.id)}
    assert {row["match_reason"] for row in payload["results"]} == {"keyword"}
    assert all(row["similarity"] is None for row in payload["results"])


def test_an_account_that_never_opted_in_gets_keyword_results_and_a_marker(
    client, db_session, test_user, provider
):
    """Never an error: the answer is what this account can actually have."""
    keyword_only, _, both = _corpus(db_session, test_user.account_id)

    response = _search(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "hybrid"
    assert payload["effective_mode"] == "keyword"
    assert _reasons(payload) == [DEGRADED_SEMANTIC_NOT_ENABLED]
    assert payload["degraded"]["semantic"] is False
    assert payload["degraded"]["detail"]
    assert set(_by_session(payload)) == {str(keyword_only.id), str(both.id)}
    assert provider.calls == 0


def test_the_deployment_kill_switch_has_its_own_marker(
    client, db_session, test_user, provider, monkeypatch
):
    """ "This account said no" and "this deployment said no" are not one case."""
    _opt_in(db_session, test_user.account_id)
    _corpus(db_session, test_user.account_id)
    monkeypatch.setattr(
        session_search_semantic, "embedding_enabled", lambda: False, raising=True
    )

    payload = _search(client).json()

    assert _reasons(payload) == [DEGRADED_SEMANTIC_DISABLED]
    assert payload["effective_mode"] == "keyword"
    assert payload["results"]
    assert provider.calls == 0


def test_a_reached_daily_cap_still_returns_keyword_results(
    client, db_session, test_user, provider
):
    """The cap stops the spending, not the search."""
    _opt_in(db_session, test_user.account_id, daily_cap_usd=0.10)
    _corpus(db_session, test_user.account_id)
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="internal/session-embedding",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(test_user.account_id),
        estimated_cost=0.25,
        meta_data={"purpose": SESSION_EMBEDDING_PURPOSE},
    )
    usage.timestamp = datetime.now(timezone.utc)
    db_session.flush()

    response = _search(client)

    assert response.status_code == 200
    payload = response.json()
    assert _reasons(payload) == [DEGRADED_SEMANTIC_DAILY_CAP]
    assert payload["effective_mode"] == "keyword"
    assert payload["results"]
    assert provider.calls == 0
    assert "did not run" in payload["degraded"]["detail"]


def test_a_provider_failure_still_returns_keyword_results(
    client, db_session, test_user, monkeypatch
):
    """A provider that cannot answer is a marker, not a 500."""
    _opt_in(db_session, test_user.account_id)
    _corpus(db_session, test_user.account_id)
    failing = CountingProvider(fails=True)
    monkeypatch.setattr(
        session_search_semantic, "build_provider", lambda setting: failing, raising=True
    )

    response = _search(client)

    assert response.status_code == 200
    payload = response.json()
    assert _reasons(payload) == [DEGRADED_SEMANTIC_PROVIDER_ERROR]
    assert payload["effective_mode"] == "keyword"
    assert payload["results"]
    assert failing.calls == 1
    assert "did not run" in payload["degraded"]["detail"]


def test_semantic_mode_with_no_vectors_is_empty_and_says_so(
    client, db_session, test_user, provider
):
    """An empty semantic answer, not a keyword answer wearing its name."""
    _opt_in(db_session, test_user.account_id)
    session = _session(db_session, test_user.account_id, "unembedded")
    _write(
        db_session,
        test_user.account_id,
        session,
        KEYWORD_TEXT,
        source_id="message-unembedded",
    )

    response = _search(client, mode="semantic")

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_mode"] == "semantic"
    assert payload["results"] == []
    assert payload["total"] == 0
    assert DEGRADED_SEMANTIC_NO_VECTORS in _reasons(payload)
    assert payload["degraded"]["keyword"] is False
    assert payload["degraded"]["semantic"] is False
    assert provider.calls == 1


def test_semantic_mode_returns_only_what_the_vector_half_found(
    client, db_session, test_user, provider
):
    """No keyword half at all: a session only the words match is not here."""
    _opt_in(db_session, test_user.account_id)
    keyword_only, semantic_only, both = _corpus(db_session, test_user.account_id)

    payload = _search(client, mode="semantic").json()

    results = _by_session(payload)
    assert set(results) == {str(semantic_only.id), str(both.id)}
    assert str(keyword_only.id) not in results
    assert payload["degraded"]["keyword"] is False
    assert payload["degraded"]["semantic"] is True
    assert [row["runtime_session_id"] for row in payload["results"]] == [
        str(semantic_only.id),
        str(both.id),
    ]


def test_a_partly_embedded_corpus_is_marked_behind(
    client, db_session, test_user, provider
):
    """Results, plus the fact that the semantic half searched less."""
    _opt_in(db_session, test_user.account_id, scope=EMBEDDING_SCOPE_FULL)
    _corpus(db_session, test_user.account_id)
    waiting = _session(db_session, test_user.account_id, "waiting")
    _write(
        db_session,
        test_user.account_id,
        waiting,
        "ledger reconciliation still queued for a vector",
        source_id="message-waiting",
    )

    payload = _search(client).json()

    assert _reasons(payload) == [DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE]
    assert payload["effective_mode"] == "hybrid"
    assert payload["degraded"]["semantic"] is True
    assert payload["embedded_through"] is not None
    assert payload["indexed_through"] is not None
    assert payload["results"]


def test_summaries_only_does_not_mark_unembedded_transcripts_as_backfill(
    client, db_session, test_user, provider
):
    """Out-of-scope transcript text is a decision, not an unfinished job."""
    _opt_in(db_session, test_user.account_id, scope=EMBEDDING_SCOPE_SUMMARIES_ONLY)
    session = _session(db_session, test_user.account_id, "summaries-scope")
    for index in range(30):
        _write(
            db_session,
            test_user.account_id,
            session,
            f"transcript turn {index} the account chose not to embed",
            source_id=f"transcript-{index}",
        )
    _write(
        db_session,
        test_user.account_id,
        session,
        SEMANTIC_TEXT,
        source_id=str(session.id),
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        vector=_axis(0),
    )

    payload = _search(client).json()

    assert DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE not in _reasons(payload)
    assert payload["effective_mode"] == "hybrid"
    assert payload["degraded"]["semantic"] is True
    assert payload["results"]


def test_paging_a_query_does_not_embed_it_again(
    client, db_session, test_user, provider
):
    """The reason the cache exists, asserted on the call count."""
    _opt_in(db_session, test_user.account_id)
    _corpus(db_session, test_user.account_id)

    first = _search(client, limit=1, offset=0).json()
    second = _search(client, limit=1, offset=1).json()
    third = _search(client, limit=1, offset=2).json()

    assert provider.calls == 1
    assert first["total"] == second["total"] == third["total"] == 3
    paged = [
        page["results"][0]["runtime_session_id"] for page in (first, second, third)
    ]
    assert len(set(paged)) == 3


def test_keyword_mode_never_embeds_anything(client, db_session, test_user, provider):
    """The cheapest mode stays the cheapest mode."""
    _opt_in(db_session, test_user.account_id)
    _corpus(db_session, test_user.account_id)

    payload = _search(client, mode="keyword").json()

    assert provider.calls == 0
    assert payload["effective_mode"] == "keyword"
    assert _reasons(payload) == []
    assert payload["embedded_through"] is None
    assert {row["match_reason"] for row in payload["results"]} == {"keyword"}


def test_another_accounts_vectors_are_never_searched(
    client, db_session, test_user, provider
):
    """The account bound holds on the vector half as well as the words."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    _opt_in(db_session, test_user.account_id)
    _opt_in(db_session, other_account.id)
    theirs = _session(db_session, other_account.id, "theirs")
    _write(
        db_session,
        other_account.id,
        theirs,
        SEMANTIC_TEXT,
        source_id="message-theirs",
        vector=_axis(0),
    )
    mine = _session(db_session, test_user.account_id, "mine")
    _write(
        db_session,
        test_user.account_id,
        mine,
        SEMANTIC_TEXT,
        source_id="message-mine",
        vector=_axis(0),
    )

    response = _search(client, mode="semantic")

    payload = response.json()
    assert [row["runtime_session_id"] for row in payload["results"]] == [str(mine.id)]
    assert str(theirs.id) not in response.text


def test_a_filter_narrows_the_vector_half_too(client, db_session, test_user, provider):
    """A filter a caller set has to bind both halves or it is not a filter."""
    _opt_in(db_session, test_user.account_id)
    recent = _session(db_session, test_user.account_id, "recent")
    older = _session(db_session, test_user.account_id, "older")
    _write(
        db_session,
        test_user.account_id,
        recent,
        SEMANTIC_TEXT,
        source_id="message-recent",
        occurred_at=BASE_AT,
        vector=_axis(0),
    )
    _write(
        db_session,
        test_user.account_id,
        older,
        SEMANTIC_TEXT,
        source_id="message-older",
        occurred_at=BASE_AT - timedelta(days=10),
        vector=_axis(0),
    )

    payload = _search(
        client,
        mode="semantic",
        filters={"start_date": (BASE_AT - timedelta(days=1)).isoformat()},
    ).json()

    assert [row["runtime_session_id"] for row in payload["results"]] == [str(recent.id)]


def test_a_query_embedding_is_recorded_as_spend(
    client, db_session, test_user, provider
):
    """One search, one ledger row, counted against the same daily cap."""
    _opt_in(db_session, test_user.account_id)
    _corpus(db_session, test_user.account_id)

    _search(client)

    rows = [
        row
        for row in crud_api_usage.get_multi(db_session, limit=100)
        if (row.meta_data or {}).get("kind") == "query"
    ]
    assert len(rows) == 1
    assert (rows[0].meta_data or {})["purpose"] == SESSION_EMBEDDING_PURPOSE
    assert QUERY not in str(rows[0].meta_data)


def test_a_worker_cap_does_not_claim_the_semantic_half_skipped(
    client, db_session, test_user, provider
):
    """The worker stopping is corpus lag, not this search skipping vectors."""
    _opt_in(db_session, test_user.account_id)
    _corpus(db_session, test_user.account_id)
    crud_session_embedding_setting.mark_degraded(
        db_session,
        account_id=test_user.account_id,
        reason=DEGRADED_DAILY_CAP,
    )

    payload = _search(client).json()

    assert payload["effective_mode"] == "hybrid"
    assert payload["degraded"]["semantic"] is True
    assert DEGRADED_SEMANTIC_DAILY_CAP in _reasons(payload)
    assert "did not run" not in payload["degraded"]["detail"]
    assert "corpus may be behind" in payload["degraded"]["detail"]
    assert provider.calls == 1
