"""Ranked search over the runtime session corpus, on words and on meaning.

This is the query half of the corpus written by the indexing service. It
answers "where did an agent do this", which is a relevance question, not a
recency one: the session that matched in five places is almost always the one
the operator wanted, and a timestamp ordering buries it under whatever ran
last night.

Keyword ranking answers that question when the asker knows the words. Vector
ranking answers it when they do not, which is the case keyword search cannot
reach at all: finding a past session without knowing the vocabulary it used.
``hybrid`` runs both and fuses the two candidate lists; see
:mod:`preloop.services.session_search_fusion` for why the fusion throws the
scores away and keeps the ranks.

The honesty rule shapes everything below. A hybrid answer that quietly drops
its semantic half, because the account never opted in, or the day's embedding
cap is spent, or the provider is down, or the corpus was embedded with another
model, is a search interface lying about its own coverage. So every one of
those cases is answered, not refused, and every one of them puts a named
reason in the degraded block:

* the query could not be embedded at all: keyword results, ``effective_mode``
  falls back to ``keyword``, and the reason says which of the five causes it
  was;
* the query was embedded but the corpus has nothing it may be compared with:
  the vector half ran and returned nothing, ``effective_mode`` stays as asked,
  and the reason distinguishes an unembedded corpus from a corpus embedded
  with a different model;
* the corpus is only partly embedded: results are returned and the reason
  says the backfill has not got there yet.

The same rule covers the keyword half, where the gap is not the vectors but
the history. Indexing runs on write, so a deployment that has never run the
backfill has a corpus that starts at the deploy and nothing behind it, and a
search over that corpus answers "no results" to a question whose answer is in
a transcript it simply never read. Every response therefore carries both ends
of the window it actually searched: ``indexed_through`` at the head and
``indexed_from`` at the tail, with the backfill state that says whether the
tail is still moving.

Everything account scoped happens in the CRUD query, not here.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_session_embedding_setting,
    crud_session_search_document,
)
from preloop.models.models.session_embedding_setting import source_kinds_for_scope
from preloop.models.models.session_search_document import SOURCE_KIND_ARTIFACT
from preloop.models.crud.session_search_document import (
    MATCH_REASON_BOTH,
    MATCH_REASON_KEYWORD,
    MATCH_REASON_SEMANTIC,
    MAX_SESSION_RESULTS,
    MAX_VECTOR_SESSIONS,
    MIN_SEMANTIC_SIMILARITY,
    VECTOR_CANDIDATE_CHUNKS,
    EmbeddingCoverage,
    MatchReason,
    RankedSession,
    RankedSnippet,
    SessionIdentity,
    VectorChunkHit,
)
from preloop.models.crud.session_search_document import (
    SessionSearchFilters as CrudSessionSearchFilters,
)
from preloop.schemas.session_search import (
    DEGRADED_FUSION_CANDIDATES_TRUNCATED,
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE,
    DEGRADED_SEMANTIC_DAILY_CAP,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SEMANTIC_MISCONFIGURED,
    DEGRADED_SEMANTIC_MODEL_MISMATCH,
    DEGRADED_SEMANTIC_NO_VECTORS,
    DEGRADED_SEMANTIC_NOT_ENABLED,
    DEGRADED_SEMANTIC_PROVIDER_ERROR,
    SessionSearchArtifactRef,
    SessionSearchDegraded,
    SessionSearchMode,
    SessionSearchRequest,
    SessionSearchResponse,
    SessionSearchResult,
    SessionSearchSnippet,
)
from preloop.services import session_search_backfill, session_search_semantic
from preloop.services.session_search_fusion import (
    FusedSession,
    fuse,
    group_vector_hits,
)
from preloop.services.session_search_semantic import (
    EmbeddingProvider,
    QueryEmbedding,
)

logger = logging.getLogger(__name__)

#: Sessions each half offers to fusion before the page is cut. A fused page
#: cannot be produced by paging the two halves separately, so both are read to
#: this depth and the page is taken from the fused order. It is the same
#: number as the maximum page size, which is what makes a first page in a
#: fused mode always complete.
FUSION_CANDIDATE_DEPTH = MAX_SESSION_RESULTS

#: One sentence per reason code, for a console that would rather not carry a
#: translation table. The order of this mapping is the order the sentences are
#: joined in, so a caller reading ``detail`` reads the same thing every time.
_REASON_DETAILS: Dict[str, str] = {
    DEGRADED_SEMANTIC_DISABLED: (
        "Semantic ranking is switched off on this deployment; these are "
        "keyword results."
    ),
    DEGRADED_SEMANTIC_NOT_ENABLED: (
        "This account has not opted in to embedding its session content, so "
        "these are keyword results."
    ),
    DEGRADED_SEMANTIC_MISCONFIGURED: (
        "The account's embedding provider is not configured usably, so these "
        "are keyword results."
    ),
    DEGRADED_SEMANTIC_DAILY_CAP: (
        "Today's embedding spend has reached this account's daily cap, so "
        "the semantic half of this search did not run."
    ),
    DEGRADED_SEMANTIC_PROVIDER_ERROR: (
        "The embedding provider could not answer, so the semantic half of "
        "this search did not run."
    ),
    DEGRADED_SEMANTIC_MODEL_MISMATCH: (
        "This account's vectors were produced by a different embedding model "
        "than the one that embedded this query, and a query is never scored "
        "across models."
    ),
    DEGRADED_SEMANTIC_NO_VECTORS: (
        "No session content in this account has been embedded yet, so there "
        "is nothing to match semantically."
    ),
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE: (
        "Some session content in this account is still waiting for a vector, "
        "so the semantic half searched less than the keyword half."
    ),
    DEGRADED_FUSION_CANDIDATES_TRUNCATED: (
        "More sessions matched than one fused page can rank; narrow the "
        "query or add a filter."
    ),
}

#: Same reason codes as above, used when the vector half of *this* search
#: did run. The worker can still have stopped filling the corpus (cap or
#: provider), and that is lag behind the query, not a skipped semantic pass.
_REASON_DETAILS_WHEN_SEMANTIC_RAN: Dict[str, str] = {
    DEGRADED_SEMANTIC_DAILY_CAP: (
        "Embedding spend reached the daily cap; the corpus may be behind."
    ),
    DEGRADED_SEMANTIC_PROVIDER_ERROR: (
        "The embedding worker last failed against the provider; the corpus "
        "may be behind."
    ),
    DEGRADED_SEMANTIC_MISCONFIGURED: (
        "The account's embedding provider is not configured usably; the "
        "corpus may be behind."
    ),
}


class SemanticPlan:
    """What the semantic half of one search is able to do.

    Built before either half runs, because the answer to "may this search
    embed its query" decides the mode that actually runs, and the mode
    decides how deep the keyword half has to read.
    """

    def __init__(
        self,
        *,
        embedding: Optional[QueryEmbedding] = None,
        coverage: Optional[EmbeddingCoverage] = None,
        reasons: Optional[List[str]] = None,
    ) -> None:
        self.embedding = embedding
        self.coverage = coverage
        self.reasons: List[str] = list(reasons or [])

    @property
    def ran(self) -> bool:
        """Whether the vector half executed, whatever it found."""
        return self.embedding is not None

    @property
    def searched(self) -> bool:
        """Whether the vector half had vectors of its own model to search."""
        return bool(self.embedding and self.coverage and self.coverage.model_vectors)

    @property
    def embedded_through(self) -> Optional[Any]:
        """How far the corpus is embedded with this query's model."""
        return self.coverage.embedded_through if self.coverage else None


def _to_crud_filters(request: SessionSearchRequest) -> CrudSessionSearchFilters:
    """Translate the validated filter block into CRUD filter arguments."""
    filters = request.filters
    return CrudSessionSearchFilters(
        start_date=filters.start_date,
        end_date=filters.end_date,
        model_alias=filters.model_alias,
        provider_name=filters.provider_name,
        runtime_principal_id=filters.runtime_principal_id,
        api_key_id=filters.api_key_id,
        flow_id=filters.flow_id,
        source_kind=filters.source_kind,
        artifact_kind=filters.kind,
        artifact_labels=_label_terms(filters.label or ()),
    )


def _label_terms(values: Sequence[str]) -> Optional[List[Dict[str, Any]]]:
    """One JSONB containment term per ``key:value`` filter, ANDed."""
    terms: List[Dict[str, Any]] = []
    for raw in values:
        key, _sep, value = raw.partition(":")
        key, value = key.strip(), value.strip()
        terms.append({key: [value]} if key == "tags" else {key: value})
    return terms or None


def _artifact_ref(snippet: RankedSnippet) -> Optional[SessionSearchArtifactRef]:
    """The artifact identity an ``artifact`` chunk carries, if any."""
    meta = snippet.meta_data or {}
    if snippet.source_kind != SOURCE_KIND_ARTIFACT or not meta.get("artifact_id"):
        return None
    return SessionSearchArtifactRef(
        artifact_id=str(meta["artifact_id"]),
        activity_id=meta.get("activity_id"),
        kind=meta.get("kind"),
        name=meta.get("name"),
        content_type=meta.get("content_type"),
        tool_name=meta.get("tool_name"),
        labels=dict(meta.get("labels") or {}),
        cue_start=meta.get("cue_start"),
        text_truncated=bool(meta.get("text_truncated")),
    )


def _plan_semantic(
    db: Session,
    *,
    account_id: Any,
    request: SessionSearchRequest,
    provider: Optional[EmbeddingProvider],
    now: Optional[Any],
) -> SemanticPlan:
    """Decide what the vector half of this search can do, and say why not.

    A keyword request never reaches a provider and never reads coverage: the
    cheapest search must stay the cheapest search.
    """
    if request.mode == "keyword":
        return SemanticPlan()

    outcome = session_search_semantic.embed_query(
        db,
        account_id=account_id,
        query=request.query,
        provider=provider,
        now=now,
    )
    if outcome.embedding is None:
        return SemanticPlan(reasons=[outcome.reason] if outcome.reason else [])

    setting = crud_session_embedding_setting.get_for_account(db, account_id=account_id)
    coverage = crud_session_search_document.embedding_coverage(
        db,
        account_id=account_id,
        embedding_model=outcome.embedding.model_identity,
        source_kinds=source_kinds_for_scope(
            setting.scope if setting is not None else None
        ),
    )
    reasons: List[str] = []
    if coverage.model_vectors == 0:
        reasons.append(
            DEGRADED_SEMANTIC_MODEL_MISMATCH
            if coverage.vectors
            else DEGRADED_SEMANTIC_NO_VECTORS
        )
    if coverage.pending:
        reasons.append(DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE)
    worker_reason = session_search_semantic.stored_degraded_reason(
        db, account_id=account_id
    )
    if worker_reason and worker_reason not in reasons:
        # The worker stopped for a reason of its own. The query embedding
        # went through, so the search works, but the corpus behind it stopped
        # filling and a reader deserves to know which.
        reasons.append(worker_reason)
    return SemanticPlan(embedding=outcome.embedding, coverage=coverage, reasons=reasons)


def _effective_mode(
    request: SessionSearchRequest, plan: SemanticPlan
) -> SessionSearchMode:
    """The mode that actually ran, which is not always the one asked for."""
    if request.mode == "keyword":
        return "keyword"
    if not plan.ran:
        return "keyword"
    return request.mode


def _degraded_block(
    *, effective_mode: SessionSearchMode, plan: SemanticPlan, reasons: Sequence[str]
) -> SessionSearchDegraded:
    """State what this answer is, and what it is not."""
    ordered = [reason for reason in _REASON_DETAILS if reason in set(reasons)]
    detail_parts = []
    for reason in ordered:
        if plan.ran and reason in _REASON_DETAILS_WHEN_SEMANTIC_RAN:
            detail_parts.append(_REASON_DETAILS_WHEN_SEMANTIC_RAN[reason])
        else:
            detail_parts.append(_REASON_DETAILS[reason])
    detail = " ".join(detail_parts) or None
    return SessionSearchDegraded(
        keyword=effective_mode != "semantic",
        semantic=plan.searched,
        reasons=ordered,
        detail=detail,
    )


def _snippet_to_schema(snippet: RankedSnippet) -> SessionSearchSnippet:
    """Serialise one matching chunk."""
    return SessionSearchSnippet(
        document_id=snippet.document_id,
        runtime_session_id=snippet.runtime_session_id,
        source_kind=snippet.source_kind,
        source_id=snippet.source_id,
        chunk_index=snippet.chunk_index,
        occurred_at=snippet.occurred_at,
        role=snippet.role,
        rank=snippet.rank,
        redaction_state=snippet.redaction_state,
        text=snippet.text,
        match_reason=snippet.match_reason,
        similarity=snippet.similarity,
        artifact=_artifact_ref(snippet),
    )


def _session_to_schema(
    session: RankedSession,
    *,
    score: Optional[float] = None,
    match_reason: MatchReason = MATCH_REASON_KEYWORD,
    similarity: Optional[float] = None,
    keyword_score: Optional[float] = None,
    semantic_chunk_count: int = 0,
    snippets: Optional[Sequence[RankedSnippet]] = None,
) -> SessionSearchResult:
    """Serialise one ranked session and its snippets."""
    rows = list(snippets if snippets is not None else session.snippets)
    return SessionSearchResult(
        runtime_session_id=session.runtime_session_id,
        session_source_type=session.session_source_type,
        session_source_id=session.session_source_id,
        session_reference=session.session_reference,
        title=session.title,
        started_at=session.started_at,
        last_activity_at=session.last_activity_at,
        score=session.score if score is None else score,
        match_reason=match_reason,
        similarity=similarity,
        keyword_score=keyword_score,
        best_chunk_rank=session.best_chunk_rank,
        matched_chunk_count=session.matched_chunk_count,
        semantic_chunk_count=semantic_chunk_count,
        first_match_at=session.first_match_at,
        last_match_at=session.last_match_at,
        snippets=[_snippet_to_schema(row) for row in rows],
    )


def _keyword_only(
    db: Session,
    *,
    account_id: Any,
    request: SessionSearchRequest,
) -> Tuple[List[SessionSearchResult], int]:
    """The keyword page, exactly as it was before fusion existed."""
    sessions, total = crud_session_search_document.search_sessions_ranked(
        db,
        account_id=account_id,
        query=request.query,
        filters=_to_crud_filters(request),
        limit=request.limit,
        offset=request.offset,
        max_snippets_per_session=request.max_snippets_per_session,
        include_snippet_text=request.include_snippet_text,
    )
    return [
        _session_to_schema(session, keyword_score=session.score) for session in sessions
    ], total


def _semantic_snippets(
    hits: Sequence[VectorChunkHit],
    *,
    texts: Dict[str, Optional[str]],
) -> List[RankedSnippet]:
    """Turn the vector half's chosen chunks into snippets."""
    return [
        RankedSnippet(
            document_id=hit.document_id,
            runtime_session_id=hit.runtime_session_id,
            source_kind=hit.source_kind,
            source_id=hit.source_id,
            chunk_index=hit.chunk_index,
            occurred_at=hit.occurred_at,
            role=hit.role,
            # A chunk found by vector alone has no keyword relevance, and
            # reporting one would be inventing a number.
            rank=0.0,
            redaction_state=hit.redaction_state,
            text=texts.get(str(hit.document_id)),
            match_reason=MATCH_REASON_SEMANTIC,
            similarity=hit.similarity,
            meta_data=hit.meta_data,
        )
        for hit in hits
    ]


def _merge_snippets(
    *,
    keyword_rows: Sequence[RankedSnippet],
    semantic_rows: Sequence[RankedSnippet],
    budget: int,
) -> List[RankedSnippet]:
    """Combine the two halves' snippets for one session, within the budget.

    Keyword snippets come first because they carry marked terms, which is the
    more useful thing to show when both halves found the same session. A
    chunk both halves found is one snippet marked ``both``, not two rows
    saying the same thing twice.
    """
    merged: List[RankedSnippet] = []
    semantic_by_id = {str(row.document_id): row for row in semantic_rows}
    for row in keyword_rows:
        twin = semantic_by_id.pop(str(row.document_id), None)
        if twin is not None:
            row.match_reason = MATCH_REASON_BOTH
            row.similarity = twin.similarity
        merged.append(row)
    for row in semantic_rows:
        if str(row.document_id) in semantic_by_id:
            merged.append(row)
    return merged[:budget]


def _fused_page(
    db: Session,
    *,
    account_id: Any,
    request: SessionSearchRequest,
    plan: SemanticPlan,
) -> Tuple[List[SessionSearchResult], int, bool]:
    """Run both halves, fuse them, and build exactly the page asked for.

    Neither half fetches snippets while it ranks: which sessions make the
    page is only known after fusion, and generating headlines for candidates
    nobody will read is the most expensive way to throw work away.
    """
    filters = _to_crud_filters(request)
    keyword_sessions: List[RankedSession] = []
    keyword_total = 0
    if request.mode != "semantic":
        keyword_sessions, keyword_total = (
            crud_session_search_document.search_sessions_ranked(
                db,
                account_id=account_id,
                query=request.query,
                filters=filters,
                limit=FUSION_CANDIDATE_DEPTH,
                offset=0,
                max_snippets_per_session=0,
            )
        )

    hits: List[VectorChunkHit] = []
    if plan.searched and plan.embedding is not None:
        hits = crud_session_search_document.search_vector_chunks(
            db,
            account_id=account_id,
            embedding=plan.embedding.vector,
            embedding_model=plan.embedding.model_identity,
            filters=filters,
            limit=VECTOR_CANDIDATE_CHUNKS,
            min_similarity=MIN_SEMANTIC_SIMILARITY,
        )
    semantic_sessions = group_vector_hits(hits)[:MAX_VECTOR_SESSIONS]
    # The keyword half knows its exact total, so it can say whether anything
    # was actually left behind. The vector half cannot: a filtered HNSW scan
    # that filled its requested depth may still have missed closer matches,
    # and one that did not fill it may still have more outside the ef_search
    # window. Saying "maybe" when the requested depth came back full is the
    # honest answer.
    truncated = keyword_total > FUSION_CANDIDATE_DEPTH or len(hits) >= (
        VECTOR_CANDIDATE_CHUNKS
    )

    keyword_by_id = {
        str(session.runtime_session_id): session for session in keyword_sessions
    }
    fused: List[FusedSession] = fuse(
        keyword_order=[str(row.runtime_session_id) for row in keyword_sessions],
        semantic_order=[row.runtime_session_id for row in semantic_sessions],
        keyword_scores={
            str(row.runtime_session_id): row.score for row in keyword_sessions
        },
        similarities={
            row.runtime_session_id: row.best_similarity for row in semantic_sessions
        },
    )
    total = len(fused)
    page = fused[request.offset : request.offset + request.limit]
    if not page:
        return [], total, truncated

    return (
        _build_results(
            db,
            account_id=account_id,
            request=request,
            page=page,
            keyword_by_id=keyword_by_id,
            hits=hits,
            semantic_counts={
                row.runtime_session_id: row.chunk_count for row in semantic_sessions
            },
        ),
        total,
        truncated,
    )


def _build_results(
    db: Session,
    *,
    account_id: Any,
    request: SessionSearchRequest,
    page: Sequence[FusedSession],
    keyword_by_id: Dict[str, RankedSession],
    hits: Sequence[VectorChunkHit],
    semantic_counts: Dict[str, int],
) -> List[SessionSearchResult]:
    """Build the response rows for one fused page, snippets included."""
    budget = request.max_snippets_per_session
    page_ids = [row.runtime_session_id for row in page]

    keyword_snippets: Dict[str, List[RankedSnippet]] = {}
    if budget and any(row.keyword_rank is not None for row in page):
        keyword_snippets = crud_session_search_document.snippets_for_sessions(
            db,
            account_id=account_id,
            query=request.query,
            session_ids=[
                row.runtime_session_id for row in page if row.keyword_rank is not None
            ],
            filters=_to_crud_filters(request),
            max_per_session=budget,
            include_text=request.include_snippet_text,
        )

    # The vector half's chunks, cut to the snippet budget per session before
    # any text is asked for, so one headline is generated per snippet that
    # will actually be returned and none for the rest of the candidates.
    on_page = set(page_ids)
    chosen_hits: Dict[str, List[VectorChunkHit]] = {}
    for hit in hits:
        session_id = str(hit.runtime_session_id)
        if session_id not in on_page:
            continue
        rows = chosen_hits.setdefault(session_id, [])
        if len(rows) < budget:
            rows.append(hit)
    semantic_texts: Dict[str, Optional[str]] = {}
    if chosen_hits and request.include_snippet_text:
        semantic_texts = crud_session_search_document.snippet_text_for_documents(
            db,
            account_id=account_id,
            document_ids=[
                hit.document_id for rows in chosen_hits.values() for hit in rows
            ],
            query=request.query,
        )

    missing = [session_id for session_id in page_ids if session_id not in keyword_by_id]
    identities: Dict[str, SessionIdentity] = (
        crud_session_search_document.session_identities(
            db, account_id=account_id, session_ids=missing
        )
        if missing
        else {}
    )

    results: List[SessionSearchResult] = []
    for row in page:
        session = keyword_by_id.get(row.runtime_session_id)
        if session is None:
            identity = identities.get(row.runtime_session_id)
            if identity is None:
                # The session went away between the two queries. Dropping it
                # is the honest answer: there is nothing left to open.
                continue
            session = RankedSession(
                runtime_session_id=identity.runtime_session_id,
                session_source_type=identity.session_source_type,
                session_source_id=identity.session_source_id,
                session_reference=identity.session_reference,
                title=identity.title,
                started_at=identity.started_at,
                last_activity_at=identity.last_activity_at,
                score=0.0,
                best_chunk_rank=0.0,
                matched_chunk_count=0,
                first_match_at=None,
                last_match_at=None,
            )
        semantic_rows = _semantic_snippets(
            chosen_hits.get(row.runtime_session_id, []), texts=semantic_texts
        )
        results.append(
            _session_to_schema(
                session,
                score=row.score,
                match_reason=row.match_reason,
                similarity=row.similarity,
                keyword_score=row.keyword_score,
                semantic_chunk_count=semantic_counts.get(row.runtime_session_id, 0),
                snippets=_merge_snippets(
                    keyword_rows=keyword_snippets.get(row.runtime_session_id, []),
                    semantic_rows=semantic_rows,
                    budget=budget,
                ),
            )
        )
    return results


def search_sessions(
    db: Session,
    *,
    account_id: Any,
    request: SessionSearchRequest,
    provider: Optional[EmbeddingProvider] = None,
    now: Optional[Any] = None,
) -> SessionSearchResponse:
    """Run one ranked search and build the response.

    Args:
        db: Request scoped session.
        account_id: The caller's account. Passed to every CRUD query, which
            binds it in SQL; nothing downstream filters by account.
        request: The validated request body.
        provider: Injected embedding provider for the query vector. Built
            from the account's setting when omitted; tests pass a fake so no
            suite ever calls out.
        now: Clock override for the daily cap window.

    Returns:
        The ranked page, the count a caller can page through, both ends of
        the window the corpus actually covers, and a degraded block
        describing what did not run.
    """
    started = time.perf_counter()
    plan = _plan_semantic(
        db, account_id=account_id, request=request, provider=provider, now=now
    )
    effective_mode = _effective_mode(request, plan)
    reasons = list(plan.reasons)

    if effective_mode == "keyword":
        results, total = _keyword_only(db, account_id=account_id, request=request)
    else:
        results, total, truncated = _fused_page(
            db, account_id=account_id, request=request, plan=plan
        )
        if truncated:
            reasons.append(DEGRADED_FUSION_CANDIDATES_TRUNCATED)

    indexed_through = crud_session_search_document.indexed_through(
        db, account_id=account_id
    )
    # Both ends of the covered window, not just the head. The head marker
    # answers "is the newest turn in yet"; the floor answers "does this
    # account have any history behind the deploy at all", which is the
    # question an operator searching a corpus filled forward from a deploy is
    # actually asking, and the one an answer of zero results is otherwise
    # silent about.
    corpus_coverage = session_search_backfill.corpus_coverage_for_account(
        db, account_id=account_id
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    # The query text is never logged. It is the operator's search over their
    # own transcripts, which is exactly the string that should not end up in a
    # log line any more than in an access log.
    logger.debug(
        "Session search (%s) returned %d of %d sessions in %.1f ms",
        effective_mode,
        len(results),
        total,
        elapsed_ms,
    )
    return SessionSearchResponse(
        query=request.query,
        mode=request.mode,
        effective_mode=effective_mode,
        degraded=_degraded_block(
            effective_mode=effective_mode, plan=plan, reasons=reasons
        ),
        indexed_through=indexed_through,
        indexed_from=corpus_coverage.indexed_from,
        backfill_complete=corpus_coverage.complete,
        backfill_state=corpus_coverage.state,
        embedded_through=plan.embedded_through,
        total=total,
        limit=request.limit,
        offset=request.offset,
        elapsed_ms=round(elapsed_ms, 3),
        results=results,
    )
