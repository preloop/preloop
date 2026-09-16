"""Response shapes for "sessions like this one".

A similarity answer is a ranking, and a ranking whose numbers are tunable has
to say what it did. Three things are therefore published rather than implied:
which vector space the comparison ran in, how much of the session was actually
compared, and every reason the answer is narrower than the question.

The degraded block is the same contract as session search
(:mod:`preloop.schemas.session_search`) minus the keyword half, and it reuses
that module's reason codes wherever the cause is the same. Two causes are new,
because they are about one session rather than about a search: the session
being viewed has no vectors of its own, and the corpus has no other session to
compare it with.

There is no cap or provider reason code here, and that is a property rather
than an omission. Similarity reads vectors the indexing worker already wrote;
it embeds nothing, spends nothing and calls no provider, so none of the ways
embedding can stop can stop it.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from preloop.models.crud.session_search_document import (
    MAX_SIMILAR_MATCHES_PER_SESSION,
    MAX_SIMILAR_SESSIONS,
)
from preloop.schemas.session_search import (
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SEMANTIC_MODEL_MISMATCH,
    DEGRADED_SEMANTIC_NOT_ENABLED,
)

#: The session being viewed carries no vector at all, so there is nothing to
#: compare from. Every other reason explains why: never opted in, switched off
#: for the deployment, or still waiting for the worker.
DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED = "similar_session_not_embedded"
#: This session has vectors, but no other session in the account has vectors
#: of the same model, so there is nothing to compare it with.
DEGRADED_SIMILAR_NO_COMPARABLE_SESSIONS = "similar_no_comparable_sessions"
#: The session has more embedded chunks than the comparison used, so it was
#: represented by a sample of itself. ``probe_chunks`` says how large.
DEGRADED_SIMILAR_SESSION_SAMPLED = "similar_session_sampled"
#: A time window was requested, so sessions older than it were not compared.
DEGRADED_SIMILAR_WINDOW_APPLIED = "similar_window_applied"

SIMILAR_DEGRADED_REASONS = (
    DEGRADED_SEMANTIC_NOT_ENABLED,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED,
    DEGRADED_SEMANTIC_MODEL_MISMATCH,
    DEGRADED_SIMILAR_NO_COMPARABLE_SESSIONS,
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE,
    DEGRADED_SIMILAR_SESSION_SAMPLED,
    DEGRADED_SIMILAR_WINDOW_APPLIED,
)

#: How close a match is, in words rather than in a number. The bands exist
#: because the thresholds behind the number are tunable and unvalidated: a
#: reader can act on "close" without being invited to compare 0.62 with 0.58
#: as though the difference meant something. The number is published beside
#: the band for a caller that wants to sort or measure.
SimilarityBand = Literal["close", "related", "loose"]

#: Similarity at or above which a match is called ``close``.
SIMILARITY_BAND_CLOSE = 0.70
#: Similarity at or above which a match is called ``related``.
SIMILARITY_BAND_RELATED = 0.50

#: Longest window a caller may ask for, in days. Past a year the question is
#: an export, not a session detail panel.
MAX_SIMILAR_WINDOW_DAYS = 365

DEFAULT_SIMILAR_SESSIONS = 5
DEFAULT_SIMILAR_MATCHES_PER_SESSION = 2


def band_for(similarity: float) -> SimilarityBand:
    """Name the band one similarity falls in."""
    if similarity >= SIMILARITY_BAND_CLOSE:
        return "close"
    if similarity >= SIMILARITY_BAND_RELATED:
        return "related"
    return "loose"


class SimilarSessionProbe(BaseModel):
    """The passage of the viewed session that a match was found from."""

    document_id: UUID = Field(..., description="Corpus chunk row id.")
    source_kind: str = Field(..., description="Which kind of turn this came from.")
    source_id: str = Field(..., description="Identifier of the turn in its own table.")
    chunk_index: int = Field(..., description="Position of the chunk inside the turn.")
    occurred_at: datetime = Field(..., description="When the turn happened.")
    role: Optional[str] = Field(None, description="Role recorded for the turn.")


class SimilarSessionMatch(BaseModel):
    """One matching chunk of the other session: the reason it is in the list.

    The identity fields name the turn, so a console can open the other session
    at the passage that matched rather than at its top.
    """

    document_id: UUID = Field(..., description="Corpus chunk row id.")
    source_kind: str = Field(..., description="Which kind of turn this came from.")
    source_id: str = Field(..., description="Identifier of the turn in its own table.")
    chunk_index: int = Field(..., description="Position of the chunk inside the turn.")
    occurred_at: datetime = Field(..., description="When the turn happened.")
    role: Optional[str] = Field(None, description="Role recorded for the turn.")
    similarity: float = Field(
        ..., description="Cosine similarity between this chunk and the probe chunk."
    )
    band: SimilarityBand = Field(
        ..., description="The similarity as a word: close, related or loose."
    )
    redaction_state: str = Field(
        ..., description="Whether the stored chunk was masked or metadata only."
    )
    text: Optional[str] = Field(
        None,
        description=(
            "Opening characters of the matching chunk, or null when the "
            "request disabled match text or the chunk's redaction state "
            "forbids returning any."
        ),
    )
    probe: SimilarSessionProbe = Field(
        ...,
        description=(
            "The chunk of the session being viewed that this one is near, so "
            "a reader can see both ends of the match."
        ),
    )


class SimilarSessionResult(BaseModel):
    """One session similar to the one being viewed."""

    runtime_session_id: UUID
    session_source_type: Optional[str] = None
    session_source_id: Optional[str] = None
    session_reference: Optional[str] = None
    title: Optional[str] = None
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    score: float = Field(
        ...,
        description=(
            "The number this result was ordered by: the best matching pair of "
            "chunks plus a damped bonus for matching in several places. It is "
            "not a probability and is not comparable with a search score."
        ),
    )
    similarity: float = Field(
        ..., description="Cosine similarity of the best matching pair of chunks."
    )
    band: SimilarityBand = Field(
        ..., description="The best similarity as a word: close, related or loose."
    )
    matched_chunk_count: int = Field(
        ...,
        description=(
            "Distinct chunks of this session that were near a probe chunk, "
            "which is how broadly the two sessions overlap."
        ),
    )
    matches: List[SimilarSessionMatch] = Field(
        default_factory=list,
        description="The closest matching chunks, strongest first.",
    )


class SimilarSessionsDegraded(BaseModel):
    """What this answer could not compare, stated rather than implied."""

    semantic: bool = Field(
        False,
        description=(
            "Whether a vector comparison actually ran. False when this "
            "session has no vectors to compare from."
        ),
    )
    reasons: List[str] = Field(
        default_factory=list,
        description=(
            "Machine readable reason codes, empty when nothing narrowed this "
            "answer. One of: " + ", ".join(SIMILAR_DEGRADED_REASONS) + "."
        ),
    )
    detail: Optional[str] = Field(
        None, description="One sentence a console can show without decoding a code."
    )


class SimilarSessionsResponse(BaseModel):
    """Sessions like this one, and everything needed to read the list."""

    model_config = ConfigDict(protected_namespaces=())

    runtime_session_id: UUID = Field(..., description="The session the list is about.")
    model_identity: Optional[str] = Field(
        None,
        description=(
            "Vector space the comparison ran in, as "
            "<provider>:<model>@<dimensions>. Null when nothing was compared. "
            "Sessions embedded with another model were not candidates."
        ),
    )
    embedded_chunks: int = Field(
        0, description="Chunks of this session that carry a vector of that model."
    )
    probe_chunks: int = Field(
        0,
        description=(
            "Chunks of this session actually used as the query. Fewer than "
            "embedded_chunks means the session was represented by an evenly "
            "spread sample of itself, and the degraded block says so."
        ),
    )
    pending_chunks: int = Field(
        0, description="Chunks of this session still waiting for a vector."
    )
    window_days: Optional[int] = Field(
        None,
        description=(
            "Time window the comparison was limited to, or null when the "
            "whole corpus was compared. No window applies by default."
        ),
    )
    limit: int = Field(
        DEFAULT_SIMILAR_SESSIONS,
        description=f"Sessions asked for, at most {MAX_SIMILAR_SESSIONS}.",
    )
    max_matches_per_session: int = Field(
        DEFAULT_SIMILAR_MATCHES_PER_SESSION,
        description=(
            "Matching chunks asked for per session, at most "
            f"{MAX_SIMILAR_MATCHES_PER_SESSION}."
        ),
    )
    degraded: SimilarSessionsDegraded
    elapsed_ms: float = Field(
        ..., description="Server side time spent building this list."
    )
    results: List[SimilarSessionResult] = Field(default_factory=list)
