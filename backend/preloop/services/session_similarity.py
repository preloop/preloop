"""Sessions like the one being viewed, from vectors that already exist.

The question "has an agent already done something like this" usually arrives
while reading a session, not while typing into a search box. This module
answers it with the session itself as the query: the indexing worker has
already embedded the session's chunks, so a nearest neighbour read over the
account's own corpus is the whole of the work. Nothing is embedded here,
nothing is sent to a provider and nothing is spent, which is why none of the
reasons an embedding run can stop (daily cap, provider failure) can stop this.

Three decisions shape the ranking, and each one has a bias worth stating.

*A session is represented by a sample of its chunks, not by a centroid.* A
centroid is one vector and one index read, but averaging a two hour session
into a single point blurs exactly the distinctive passage that makes it worth
finding. Probing with individual chunks keeps that passage sharp; the cost is
that a request reads a bounded sample, and the response publishes how large
the sample was against how much of the session carries a vector.

*The best matching pair of chunks decides the order.* Matching in several
places adds a small, logarithmically damped bonus that can only break near
ties. The bias: a session containing one strongly matching passage outranks a
session that is broadly, weakly related, and a long session has more chances
to contain one close chunk than a short one does. The damped bonus is
deliberately too small to turn breadth into a ranking of its own.

*A comparison never crosses models.* Every probe is scored only against
vectors carrying its own model identity, for the same reason session search
does it: a cosine distance between two models' spaces is not a similarity.

Everything account scoped happens in the CRUD query, not here.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_session_embedding_setting,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import (
    MAX_SIMILAR_MATCHES_PER_SESSION,
    MAX_SIMILAR_SESSIONS,
    MIN_SIMILAR_SIMILARITY,
    SIMILAR_BREADTH_WEIGHT,
    SIMILAR_NEIGHBOURS_PER_PROBE,
    SIMILAR_PROBE_CHUNKS,
    ProbeChunk,
    SessionEmbeddingState,
    SimilarChunkHit,
)
from preloop.schemas.session_search import (
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SEMANTIC_MODEL_MISMATCH,
    DEGRADED_SEMANTIC_NOT_ENABLED,
)
from preloop.schemas.session_similarity import (
    DEFAULT_SIMILAR_MATCHES_PER_SESSION,
    DEFAULT_SIMILAR_SESSIONS,
    DEGRADED_SIMILAR_NO_COMPARABLE_SESSIONS,
    DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED,
    DEGRADED_SIMILAR_SESSION_SAMPLED,
    DEGRADED_SIMILAR_WINDOW_APPLIED,
    MAX_SIMILAR_WINDOW_DAYS,
    SimilarSessionMatch,
    SimilarSessionProbe,
    SimilarSessionResult,
    SimilarSessionsDegraded,
    SimilarSessionsResponse,
    band_for,
)
from preloop.services.session_embedding import embedding_enabled

logger = logging.getLogger(__name__)

#: One sentence per reason code, in the order they are joined, so a console
#: reading ``detail`` reads the same thing every time.
_REASON_DETAILS: Dict[str, str] = {
    DEGRADED_SEMANTIC_DISABLED: (
        "Embedding is switched off on this deployment, so no session here can "
        "be compared with another."
    ),
    DEGRADED_SEMANTIC_NOT_ENABLED: (
        "This account has not opted in to embedding its session content, so "
        "there is nothing to compare."
    ),
    DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED: (
        "This session has no embedded content, so there is nothing to compare it from."
    ),
    DEGRADED_SEMANTIC_MODEL_MISMATCH: (
        "This session was embedded with a different model than the account "
        "uses now, so it was compared only with sessions embedded by that "
        "same model."
    ),
    DEGRADED_SIMILAR_NO_COMPARABLE_SESSIONS: (
        "No other session in this account has been embedded with the same "
        "model, so there was nothing to compare this one with."
    ),
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE: (
        "Some of this session's content is still waiting for a vector, so "
        "part of it was not compared."
    ),
    DEGRADED_SIMILAR_SESSION_SAMPLED: (
        "This session is longer than one comparison can carry, so it was "
        "represented by an evenly spread sample of its content."
    ),
    DEGRADED_SIMILAR_WINDOW_APPLIED: (
        "Only sessions inside the requested time window were compared; older "
        "ones were not."
    ),
}


@dataclass
class _Candidate:
    """One other session, as the chunk pairs found so far describe it."""

    runtime_session_id: str
    best_similarity: float = 0.0
    matched_documents: set = field(default_factory=set)
    hits: List[SimilarChunkHit] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Best pair, plus a damped bonus for matching in several places.

        ``log1p`` of the matches after the first: one match scores exactly its
        similarity, and the bonus grows slowly enough that breadth can only
        reorder sessions that were already close together.
        """
        breadth = max(0, len(self.matched_documents) - 1)
        return self.best_similarity + SIMILAR_BREADTH_WEIGHT * math.log1p(breadth)


def _degraded_block(
    *, compared: bool, reasons: Sequence[str]
) -> SimilarSessionsDegraded:
    """State what this answer is, and what it is not."""
    ordered = [reason for reason in _REASON_DETAILS if reason in set(reasons)]
    detail = " ".join(_REASON_DETAILS[reason] for reason in ordered) or None
    return SimilarSessionsDegraded(semantic=compared, reasons=ordered, detail=detail)


def _empty_response(
    *,
    runtime_session_id: Any,
    state: SessionEmbeddingState,
    limit: int,
    max_matches_per_session: int,
    window_days: Optional[int],
    reasons: Sequence[str],
    started: float,
    compared: bool = False,
    probe_count: int = 0,
) -> SimilarSessionsResponse:
    """An answer with no results, carrying the reason there are none."""
    return SimilarSessionsResponse(
        runtime_session_id=runtime_session_id,
        model_identity=state.model_identity,
        embedded_chunks=state.model_chunks,
        probe_chunks=probe_count,
        pending_chunks=state.pending,
        window_days=window_days,
        limit=limit,
        max_matches_per_session=max_matches_per_session,
        degraded=_degraded_block(compared=compared, reasons=reasons),
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        results=[],
    )


def _group(hits: Sequence[SimilarChunkHit]) -> List[_Candidate]:
    """Group chunk pairs into sessions, strongest session first.

    The hits arrive strongest first, so the first hit seen for a session is
    also its best one. Ordering breaks on the score, then on the best pair,
    then on the session id, so the same corpus produces the same list twice.
    """
    candidates: Dict[str, _Candidate] = {}
    for hit in hits:
        session_id = str(hit.runtime_session_id)
        candidate = candidates.get(session_id)
        if candidate is None:
            candidate = _Candidate(runtime_session_id=session_id)
            candidates[session_id] = candidate
        document_id = str(hit.document_id)
        if document_id in candidate.matched_documents:
            # The same chunk found by a second probe is the same chunk. It
            # counts once, at its closest similarity, so a repetitive session
            # cannot inflate its own breadth bonus.
            continue
        candidate.matched_documents.add(document_id)
        candidate.hits.append(hit)
        candidate.best_similarity = max(candidate.best_similarity, hit.similarity)
    return sorted(
        candidates.values(),
        key=lambda row: (-row.score, -row.best_similarity, row.runtime_session_id),
    )


def _probe_schema(probe: ProbeChunk) -> SimilarSessionProbe:
    """Serialise the end of the match that belongs to the viewed session."""
    return SimilarSessionProbe(
        document_id=probe.document_id,
        source_kind=probe.source_kind,
        source_id=probe.source_id,
        chunk_index=probe.chunk_index,
        occurred_at=probe.occurred_at,
        role=probe.role,
    )


def similar_sessions(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    limit: int = DEFAULT_SIMILAR_SESSIONS,
    max_matches_per_session: int = DEFAULT_SIMILAR_MATCHES_PER_SESSION,
    window_days: Optional[int] = None,
    include_match_text: bool = True,
    now: Optional[datetime] = None,
) -> SimilarSessionsResponse:
    """Rank other sessions of this account by similarity to this one.

    Args:
        db: Request scoped session.
        account_id: The caller's account. Passed to every CRUD query, which
            binds it in SQL; nothing downstream filters by account.
        runtime_session_id: The session being viewed, excluded from its own
            results in the query rather than afterwards.
        limit: Sessions to return, capped at ``MAX_SIMILAR_SESSIONS``.
        max_matches_per_session: Matching chunks per session, capped at
            ``MAX_SIMILAR_MATCHES_PER_SESSION``.
        window_days: Only compare content this recent. No window by default:
            the session worth finding is often the one from six months ago,
            and hiding it silently is the failure this contract exists to
            avoid. A window that is applied is named in the degraded block.
        include_match_text: When false no chunk text is read at all, so a
            caller that only needs the ranking never moves captured content.
        now: Clock override for the window.

    Returns:
        The ranked list and a degraded block naming everything that narrowed
        it. Never raises for a session that cannot be compared: an empty list
        with a reason is the answer.
    """
    started = time.perf_counter()
    limit = max(1, min(int(limit), MAX_SIMILAR_SESSIONS))
    match_budget = max(
        0, min(int(max_matches_per_session), MAX_SIMILAR_MATCHES_PER_SESSION)
    )
    if window_days is not None:
        window_days = max(1, min(int(window_days), MAX_SIMILAR_WINDOW_DAYS))

    setting = crud_session_embedding_setting.get_for_account(db, account_id=account_id)
    preferred_model = (
        setting.model_identity if setting is not None and setting.enabled else None
    )
    state = crud_session_search_document.session_embedding_state(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        preferred_model=preferred_model,
    )

    reasons: List[str] = []
    if state.pending:
        reasons.append(DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE)

    if not state.model_identity:
        # Nothing to compare from. The other reasons say why, in the order a
        # reader can act on them: a deployment switch, then an account opt in,
        # then a worker that has not arrived yet.
        reasons.append(DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED)
        if not embedding_enabled():
            reasons.append(DEGRADED_SEMANTIC_DISABLED)
        elif setting is None or not setting.enabled:
            reasons.append(DEGRADED_SEMANTIC_NOT_ENABLED)
        return _empty_response(
            runtime_session_id=runtime_session_id,
            state=state,
            limit=limit,
            max_matches_per_session=match_budget,
            window_days=window_days,
            reasons=reasons,
            started=started,
        )

    if preferred_model and preferred_model != state.model_identity:
        # The account embeds with one model now and this session was embedded
        # with another. The comparison still runs, in the session's own space,
        # and says which space that was.
        reasons.append(DEGRADED_SEMANTIC_MODEL_MISMATCH)
    if window_days is not None:
        reasons.append(DEGRADED_SIMILAR_WINDOW_APPLIED)

    probes = crud_session_search_document.session_probe_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        embedding_model=state.model_identity,
        limit=SIMILAR_PROBE_CHUNKS,
    )
    if not probes:
        # Vectors exist but every one of them belongs to a chunk whose text
        # was withheld after it was embedded, which is not content this is
        # allowed to compare.
        reasons.append(DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED)
        return _empty_response(
            runtime_session_id=runtime_session_id,
            state=state,
            limit=limit,
            max_matches_per_session=match_budget,
            window_days=window_days,
            reasons=reasons,
            started=started,
        )
    if len(probes) < state.model_chunks:
        reasons.append(DEGRADED_SIMILAR_SESSION_SAMPLED)

    start_date: Optional[datetime] = None
    if window_days is not None:
        start_date = (now or datetime.now(UTC)) - timedelta(days=window_days)

    hits = crud_session_search_document.similar_chunks(
        db,
        account_id=account_id,
        probes=probes,
        exclude_session_id=runtime_session_id,
        neighbours_per_probe=SIMILAR_NEIGHBOURS_PER_PROBE,
        min_similarity=MIN_SIMILAR_SIMILARITY,
        start_date=start_date,
    )
    if not hits:
        # Empty is an answer, but "nothing was close enough" and "there was
        # nothing to compare with" are different answers, and one coverage
        # read tells them apart.
        coverage = crud_session_search_document.embedding_coverage(
            db, account_id=account_id, embedding_model=state.model_identity
        )
        if coverage.model_vectors <= state.model_chunks:
            reasons.append(DEGRADED_SIMILAR_NO_COMPARABLE_SESSIONS)
        return _empty_response(
            runtime_session_id=runtime_session_id,
            state=state,
            limit=limit,
            max_matches_per_session=match_budget,
            window_days=window_days,
            reasons=reasons,
            started=started,
            compared=True,
            probe_count=len(probes),
        )

    page = _group(hits)[:limit]
    chosen: Dict[str, List[SimilarChunkHit]] = {
        candidate.runtime_session_id: sorted(
            candidate.hits,
            key=lambda hit: (-hit.similarity, str(hit.document_id)),
        )[:match_budget]
        for candidate in page
    }

    previews: Dict[str, Optional[str]] = {}
    if include_match_text and match_budget:
        previews = crud_session_search_document.chunk_previews(
            db,
            account_id=account_id,
            document_ids=[hit.document_id for rows in chosen.values() for hit in rows],
        )

    identities = crud_session_search_document.session_identities(
        db,
        account_id=account_id,
        session_ids=[candidate.runtime_session_id for candidate in page],
    )
    probes_by_id = {str(probe.document_id): probe for probe in probes}

    results: List[SimilarSessionResult] = []
    for candidate in page:
        identity = identities.get(candidate.runtime_session_id)
        if identity is None:
            # The session went away between the two reads. Dropping it is the
            # honest answer: there is nothing left to open.
            continue
        matches = [
            SimilarSessionMatch(
                document_id=hit.document_id,
                source_kind=hit.source_kind,
                source_id=hit.source_id,
                chunk_index=hit.chunk_index,
                occurred_at=hit.occurred_at,
                role=hit.role,
                similarity=hit.similarity,
                band=band_for(hit.similarity),
                redaction_state=hit.redaction_state,
                text=previews.get(str(hit.document_id)),
                probe=_probe_schema(probes_by_id[str(hit.probe_document_id)]),
            )
            for hit in chosen.get(candidate.runtime_session_id, [])
            if str(hit.probe_document_id) in probes_by_id
        ]
        results.append(
            SimilarSessionResult(
                runtime_session_id=identity.runtime_session_id,
                session_source_type=identity.session_source_type,
                session_source_id=identity.session_source_id,
                session_reference=identity.session_reference,
                title=identity.title,
                started_at=identity.started_at,
                last_activity_at=identity.last_activity_at,
                score=candidate.score,
                similarity=candidate.best_similarity,
                band=band_for(candidate.best_similarity),
                matched_chunk_count=len(candidate.matched_documents),
                matches=matches,
            )
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.debug(
        "Similar sessions returned %d candidates from %d probes in %.1f ms",
        len(results),
        len(probes),
        elapsed_ms,
    )
    return SimilarSessionsResponse(
        runtime_session_id=runtime_session_id,
        model_identity=state.model_identity,
        embedded_chunks=state.model_chunks,
        probe_chunks=len(probes),
        pending_chunks=state.pending,
        window_days=window_days,
        limit=limit,
        max_matches_per_session=match_budget,
        degraded=_degraded_block(compared=True, reasons=reasons),
        elapsed_ms=round(elapsed_ms, 3),
        results=results,
    )
