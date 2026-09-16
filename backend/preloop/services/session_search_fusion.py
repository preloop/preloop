"""Fuse a keyword candidate list and a vector candidate list into one order.

The two halves of a hybrid search produce scores that cannot be compared.
``ts_rank`` is an unbounded relevance number whose scale depends on the query
and the document; cosine similarity is a bounded number in a vector space.
Adding them, or normalising one into the other, invents a relationship that
does not exist.

Reciprocal rank fusion sidesteps that: it throws the scores away and keeps
only the positions, so a session that both halves put near the top ends up
above a session only one half liked. One session's contribution from one
list is::

    weight / (RRF_K + rank)

with ``rank`` counted from one. ``RRF_K`` damps the difference between
positions deep in a list, so being fourth rather than third matters much less
than being first rather than second.

Every constant here is tunable and unvalidated. ``RRF_K = 60`` is the value
the original rank fusion paper used and the one most implementations copy; the
weights are equal because there is no measurement on this corpus that says one
half deserves more. They were chosen to make the orderings in
``backend/tests/services/test_session_search_fusion.py`` hold, not from
relevance measured on real searches. Treat a change to them as a product
change, not a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence

from preloop.models.crud.session_search_document import (
    MATCH_REASON_BOTH,
    MATCH_REASON_KEYWORD,
    MATCH_REASON_SEMANTIC,
    MIN_SEMANTIC_SIMILARITY,
    MatchReason,
)

#: Rank fusion constant. Larger flattens the difference between positions.
RRF_K = 60.0

#: Weight of the keyword list in the fused score.
KEYWORD_FUSION_WEIGHT = 1.0

#: Weight of the vector list in the fused score.
SEMANTIC_FUSION_WEIGHT = 1.0


def ranking_identity() -> str:
    """Name the ranking constants currently in force.

    Derived from the constants rather than hand versioned, so tuning one of
    them changes the identity whether or not anybody remembers to bump a
    number. A caller that stored an older identity (a saved search, issue
    #673) can then say its ordering is no longer the one it was saved under,
    which is the most that can honestly be claimed while these constants are
    unvalidated.

    Returns:
        A short stable string, safe to store and to compare for equality
        only. It is not ordered and carries no meaning beyond "same" or
        "different".
    """
    return (
        f"rrf-k={RRF_K:g};kw={KEYWORD_FUSION_WEIGHT:g};"
        f"sem={SEMANTIC_FUSION_WEIGHT:g};floor={MIN_SEMANTIC_SIMILARITY:g}"
    )


class VectorHit(Protocol):
    """The three fields grouping needs from a vector chunk hit.

    A structural type rather than the CRUD dataclass: fusion is ranking
    arithmetic, and a ranking module that imports a query result type gains a
    dependency it never uses.
    """

    runtime_session_id: str
    document_id: object
    similarity: float


@dataclass
class FusedSession:
    """One session in the fused order, and what each half said about it."""

    runtime_session_id: str
    score: float
    match_reason: MatchReason
    keyword_rank: Optional[int] = None
    semantic_rank: Optional[int] = None
    keyword_score: Optional[float] = None
    similarity: Optional[float] = None


def _contribution(weight: float, rank: Optional[int]) -> float:
    """One list's share of a fused score, or nothing when it did not match."""
    if rank is None:
        return 0.0
    return weight / (RRF_K + float(rank))


def _match_reason(
    keyword_rank: Optional[int], semantic_rank: Optional[int]
) -> MatchReason:
    """Name which half, or halves, produced this session."""
    if keyword_rank is not None and semantic_rank is not None:
        return MATCH_REASON_BOTH
    if semantic_rank is not None:
        return MATCH_REASON_SEMANTIC
    return MATCH_REASON_KEYWORD


def fuse(
    *,
    keyword_order: Sequence[str],
    semantic_order: Sequence[str],
    keyword_scores: Optional[Dict[str, float]] = None,
    similarities: Optional[Dict[str, float]] = None,
) -> List[FusedSession]:
    """Fuse two ranked lists of session ids into one ranked list.

    Args:
        keyword_order: Session ids in keyword relevance order, best first.
        semantic_order: Session ids in vector similarity order, best first.
        keyword_scores: Keyword session score per id, carried through for the
            response and used as the first tie break.
        similarities: Best cosine similarity per id, carried through for the
            response and used as the second tie break.

    Returns:
        Every session either list held, ordered by fused score. Ties are
        broken by keyword score, then by similarity, then by session id, so
        the same two lists always produce the same order: an ordering that
        depends on dictionary iteration or on which half answered first is an
        ordering that changes under the caller while they page through it.
    """
    keyword_rank = {str(value): index + 1 for index, value in enumerate(keyword_order)}
    semantic_rank = {
        str(value): index + 1 for index, value in enumerate(semantic_order)
    }
    scores = {str(key): value for key, value in (keyword_scores or {}).items()}
    sims = {str(key): value for key, value in (similarities or {}).items()}

    fused: List[FusedSession] = []
    for session_id in list(keyword_rank) + [
        value for value in semantic_rank if value not in keyword_rank
    ]:
        kw = keyword_rank.get(session_id)
        sem = semantic_rank.get(session_id)
        fused.append(
            FusedSession(
                runtime_session_id=session_id,
                score=(
                    _contribution(KEYWORD_FUSION_WEIGHT, kw)
                    + _contribution(SEMANTIC_FUSION_WEIGHT, sem)
                ),
                match_reason=_match_reason(kw, sem),
                keyword_rank=kw,
                semantic_rank=sem,
                keyword_score=scores.get(session_id),
                similarity=sims.get(session_id),
            )
        )

    fused.sort(
        key=lambda row: (
            -row.score,
            -(row.keyword_score if row.keyword_score is not None else 0.0),
            -(row.similarity if row.similarity is not None else 0.0),
            row.runtime_session_id,
        )
    )
    return fused


@dataclass
class SemanticSession:
    """A vector candidate list grouped into one session.

    Grouping is a ranking decision, not a storage one, which is why it
    happens here rather than in the query: the query answers in chunks
    because that is what a vector index ranks.
    """

    runtime_session_id: str
    best_similarity: float
    chunk_count: int = 0
    document_ids: List[str] = field(default_factory=list)


def group_vector_hits(hits: Sequence[VectorHit]) -> List[SemanticSession]:
    """Group chunk hits into sessions, best session first.

    Args:
        hits: Chunk hits ordered by similarity, best first, as returned by
            the vector query.

    Returns:
        One entry per session, ordered by its best chunk's similarity and
        then by session id, so an ordering never depends on how two equally
        close chunks happened to come back.
    """
    grouped: Dict[str, SemanticSession] = {}
    for hit in hits:
        session_id = str(hit.runtime_session_id)
        similarity = float(getattr(hit, "similarity", 0.0) or 0.0)
        entry = grouped.get(session_id)
        if entry is None:
            entry = SemanticSession(
                runtime_session_id=session_id, best_similarity=similarity
            )
            grouped[session_id] = entry
        entry.best_similarity = max(entry.best_similarity, similarity)
        entry.chunk_count += 1
        entry.document_ids.append(str(hit.document_id))
    ordered = sorted(
        grouped.values(),
        key=lambda row: (-row.best_similarity, row.runtime_session_id),
    )
    return ordered


__all__ = [
    "FusedSession",
    "KEYWORD_FUSION_WEIGHT",
    "MATCH_REASON_BOTH",
    "MATCH_REASON_KEYWORD",
    "MATCH_REASON_SEMANTIC",
    "MatchReason",
    "RRF_K",
    "SEMANTIC_FUSION_WEIGHT",
    "SemanticSession",
    "VectorHit",
    "fuse",
    "group_vector_hits",
]
