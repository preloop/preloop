"""Request and response shapes for ranked search over session content.

The request is a POST body rather than a query string on purpose. The text an
operator types here is whatever they are looking for in their own agent
transcripts, and a proxy access log is the last place that belongs. The two
existing GET search surfaces predate that reasoning; this one does not repeat
it.

``extra="forbid"`` on both the body and the filter block is the other
deliberate choice: an unrecognised filter key is a validation error, because a
silently dropped filter returns a wider result set than the caller believes
they asked for, and they have no way to tell.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.models.crud.session_search_document import (
    MATCH_REASON_BOTH,
    MATCH_REASON_KEYWORD,
    MATCH_REASON_SEMANTIC,
    MAX_SESSION_RESULTS,
    MAX_SNIPPETS_PER_SESSION,
    normalize_query,
)
from preloop.models.models.session_search_document import SOURCE_KINDS

#: Search modes the contract accepts. ``keyword`` ranks on the words,
#: ``semantic`` on the query vector, ``hybrid`` fuses both. A mode whose half
#: cannot run is answered with what can run plus a degraded marker naming what
#: is missing, never with an error.
SessionSearchMode = Literal["keyword", "semantic", "hybrid"]

#: Why one result is in the answer: the words, the vector, or both.
SessionSearchMatchReason = Literal["keyword", "semantic", "both"]

# Degraded reason codes. Each one names a specific thing this answer could not
# do, because a hybrid result set that silently drops its semantic half is a
# search interface lying about its own coverage. They are grouped by what a
# reader can act on: the first five are a semantic half that never ran, the
# next three a corpus that cannot answer, and the last a page bounded by the
# fusion depth.

#: The account has not opted in to embedding its session content, so there is
#: nothing to search semantically and nothing was sent to a provider.
DEGRADED_SEMANTIC_NOT_ENABLED = "semantic_not_enabled"
#: The deployment kill switch is off. No account on this deployment embeds.
DEGRADED_SEMANTIC_DISABLED = "semantic_disabled_by_deployment"
#: Today's embedding spend has reached the account's daily cap. Keyword
#: results are unaffected; the semantic half resumes tomorrow.
DEGRADED_SEMANTIC_DAILY_CAP = "semantic_daily_cap_reached"
#: The embedding provider could not answer, so the query has no vector.
DEGRADED_SEMANTIC_PROVIDER_ERROR = "semantic_provider_error"
#: The account's embedding setting does not name a usable provider or model.
DEGRADED_SEMANTIC_MISCONFIGURED = "semantic_provider_misconfigured"
#: The corpus holds vectors, but none from the model that embedded this
#: query. A distance between two models' spaces is not a similarity, so the
#: query scores none of them rather than scoring them wrongly.
DEGRADED_SEMANTIC_MODEL_MISMATCH = "semantic_model_mismatch"
#: The corpus holds no vectors at all for this account yet.
DEGRADED_SEMANTIC_NO_VECTORS = "semantic_no_vectors"
#: Some of this account's chunks are still waiting for a vector, so the
#: semantic half searched less than the keyword half did.
DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE = "semantic_backfill_incomplete"
#: A candidate list filled its documented depth, so a fused page cannot see
#: past it. The keyword half of a keyword search is never truncated this way.
DEGRADED_FUSION_CANDIDATES_TRUNCATED = "fusion_candidates_truncated"

DEGRADED_REASONS = (
    DEGRADED_SEMANTIC_NOT_ENABLED,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SEMANTIC_DAILY_CAP,
    DEGRADED_SEMANTIC_PROVIDER_ERROR,
    DEGRADED_SEMANTIC_MISCONFIGURED,
    DEGRADED_SEMANTIC_MODEL_MISMATCH,
    DEGRADED_SEMANTIC_NO_VECTORS,
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE,
    DEGRADED_FUSION_CANDIDATES_TRUNCATED,
)

# How far back this account's corpus reaches, as a state a caller can read
# without knowing the sweeper exists. These live here rather than in the
# sweeper because they are part of the response contract; the sweeper imports
# them back.

#: No backfill has walked this account. On the shipped defaults the sweeper is
#: off, so this is what every account reports and it means the corpus starts
#: at the deploy that switched indexing on.
BACKFILL_STATE_NOT_STARTED = "not_started"
#: The walk is under way and the covered window is still growing backwards.
BACKFILL_STATE_IN_PROGRESS = "in_progress"
#: The walk reached the end of the retained history: nothing older exists to
#: index, so an empty answer is an honest "no session did that".
BACKFILL_STATE_COMPLETE = "complete"

#: The closed set as a type, so the generated schema publishes the three
#: values the way it publishes the search modes, and a client can exhaust it.
#: A constant above that drifts from one of these fails validation the first
#: time a response carries it, which is why the tuple is derived rather than
#: written out a second time.
SessionSearchBackfillState = Literal["not_started", "in_progress", "complete"]

BACKFILL_STATES: tuple[str, ...] = get_args(SessionSearchBackfillState)

#: Longest query accepted. Past this a caller is pasting a document, not
#: searching for one.
MAX_QUERY_CHARS = 512

#: Default page size, and the default number of snippets per session.
DEFAULT_SESSION_RESULTS = 20
DEFAULT_SNIPPETS_PER_SESSION = 3


class SessionSearchFilters(BaseModel):
    """Filters over the denormalised columns the corpus carries.

    Everything here is a column on the corpus row itself, snapshotted by the
    writer, so filtering never joins a source table and never reaches outside
    the account bound applied by the query.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    start_date: Optional[datetime] = Field(
        None,
        description=(
            "Only chunks at or after this instant. Must include a timezone "
            "offset; a naive value is rejected."
        ),
    )
    end_date: Optional[datetime] = Field(
        None,
        description=(
            "Only chunks strictly before this instant. Must include a "
            "timezone offset; a naive value is rejected."
        ),
    )
    model_alias: Optional[str] = Field(
        None,
        min_length=1,
        description="Model alias recorded on the chunk, for example a gpt-5 alias.",
    )
    provider_name: Optional[str] = Field(
        None, min_length=1, description="Provider recorded on the chunk."
    )
    runtime_principal_id: Optional[str] = Field(
        None,
        min_length=1,
        description="Runtime principal, the agent or user the session ran as.",
    )
    api_key_id: Optional[UUID] = Field(
        None, description="API key the traffic was attributed to."
    )
    flow_id: Optional[UUID] = Field(None, description="Flow the session belonged to.")
    source_kind: Optional[str] = Field(
        None,
        description=("One corpus source kind: " + ", ".join(SOURCE_KINDS) + "."),
    )

    kind: Optional[str] = Field(
        None,
        min_length=1,
        max_length=32,
        description=(
            "Artifact kind, for example transcript or document. Restricts the "
            "search to artifact chunks."
        ),
    )
    label: Optional[List[str]] = Field(
        None,
        max_length=16,
        description=(
            "Artifact label filters as key:value, for example site:nord. "
            "Repeatable; every one must match (AND). tags:x matches an "
            "artifact whose tags include x. Restricts the search to artifact "
            "chunks."
        ),
    )

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """Reject a label filter that is not key:value."""
        for raw in value or ():
            key, sep, item = raw.partition(":")
            if not sep or not key.strip() or not item.strip():
                raise ValueError("label filters must be key:value")
        return value

    @field_validator("source_kind")
    @classmethod
    def validate_source_kind(cls, value: Optional[str]) -> Optional[str]:
        """Reject an unknown source kind rather than matching nothing."""
        if value is None:
            return None
        if value not in SOURCE_KINDS:
            raise ValueError("source_kind must be one of: " + ", ".join(SOURCE_KINDS))
        return value

    @field_validator("start_date", "end_date")
    @classmethod
    def require_timezone_aware_bounds(
        cls, value: Optional[datetime]
    ) -> Optional[datetime]:
        """Reject a naive bound so the same body is the same instant everywhere."""
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("start_date and end_date must include a timezone offset")
        return value


class SessionSearchRequest(BaseModel):
    """One ranked search over the caller's own session corpus."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description=(
            "Search text, parsed the way a web search box is: a quoted "
            'phrase ("rolling restart") stays a phrase, `or` alternates and a '
            "leading `-` excludes."
        ),
    )
    mode: SessionSearchMode = Field(
        "keyword",
        description=(
            "Requested ranking mode. A semantic or hybrid request whose "
            "semantic half cannot run (no opt in, cap reached, provider "
            "down) is answered with keyword results and a degraded marker "
            "naming the reason, never with an error."
        ),
    )
    filters: SessionSearchFilters = Field(
        default_factory=SessionSearchFilters,
        description="Filters over the corpus columns. Unknown keys are rejected.",
    )
    limit: int = Field(
        DEFAULT_SESSION_RESULTS,
        ge=1,
        le=MAX_SESSION_RESULTS,
        description=f"Sessions per page, at most {MAX_SESSION_RESULTS}.",
    )
    offset: int = Field(0, ge=0, description="Sessions to skip.")
    max_snippets_per_session: int = Field(
        DEFAULT_SNIPPETS_PER_SESSION,
        ge=0,
        le=MAX_SNIPPETS_PER_SESSION,
        description=(
            "Snippets to return per session, at most "
            f"{MAX_SNIPPETS_PER_SESSION}. Zero returns scored sessions with "
            "no snippet rows at all."
        ),
    )
    include_snippet_text: bool = Field(
        True,
        description=(
            "When false the snippet text is never generated, so no captured "
            "content leaves the database; the snippets still carry the "
            "identity needed to open the session at that turn."
        ),
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """Collapse whitespace and reject a query that is only whitespace."""
        normalized = normalize_query(value)
        if not normalized:
            raise ValueError("query must contain at least one non-whitespace character")
        return normalized


class SessionSearchArtifactRef(BaseModel):
    """The artifact an ``artifact`` chunk came from, enough to open it.

    ``activity_id`` is the timeline row the deposit wrote, so a console can
    land on that row; ``cue_start`` is the start in seconds of the transcript
    cue the chunk begins in, when the transcript was timed.
    """

    artifact_id: str
    activity_id: Optional[str] = None
    kind: Optional[str] = None
    name: Optional[str] = None
    content_type: Optional[str] = None
    labels: Dict[str, Any] = Field(default_factory=dict)
    cue_start: Optional[float] = None
    text_truncated: bool = False


class SessionSearchSnippet(BaseModel):
    """One matching chunk of one session.

    The identity fields are the point: ``runtime_session_id`` with
    ``source_kind`` and ``source_id`` names the turn the match came from, and
    ``chunk_index`` names the piece of it, so a console or a CLI can open the
    session at that turn rather than at the top.
    """

    document_id: UUID = Field(..., description="Corpus chunk row id.")
    runtime_session_id: UUID = Field(..., description="Session the chunk belongs to.")
    source_kind: str = Field(..., description="Which kind of turn this came from.")
    source_id: str = Field(..., description="Identifier of the turn in its own table.")
    chunk_index: int = Field(..., description="Position of the chunk inside the turn.")
    occurred_at: datetime = Field(..., description="When the turn happened.")
    role: Optional[str] = Field(None, description="Role recorded for the turn.")
    rank: float = Field(..., description="Relevance of this chunk on its own.")
    redaction_state: str = Field(
        ..., description="Whether the stored chunk was masked or metadata only."
    )
    text: Optional[str] = Field(
        None,
        description=(
            "Database generated headline with the matching terms marked, or "
            "null when the request disabled snippet text. A chunk the vector "
            "half found and the keyword half did not carries no marked term, "
            "so the headline is that chunk's opening words."
        ),
    )
    match_reason: SessionSearchMatchReason = Field(
        MATCH_REASON_KEYWORD,
        description=(
            "Which half of the search produced this chunk: the words, the "
            "vector, or both."
        ),
    )
    similarity: Optional[float] = Field(
        None,
        description=(
            "Cosine similarity between the query vector and this chunk. Null "
            "for a chunk the vector half never scored, because a keyword "
            "match has no similarity to report."
        ),
    )
    artifact: Optional[SessionSearchArtifactRef] = Field(
        None,
        description=(
            "Set when source_kind is artifact: the artifact id, its timeline "
            "activity id, kind, name, labels and the transcript cue start."
        ),
    )


class SessionSearchResult(BaseModel):
    """One session that matched, with its score and why it is here."""

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
            "The number this result was ordered by. In keyword mode it is "
            "the fused chunk relevance of the session; in a mode that ran "
            "the vector half it is the rank fusion score, which is on a "
            "different scale and is not comparable across modes."
        ),
    )
    match_reason: SessionSearchMatchReason = Field(
        MATCH_REASON_KEYWORD,
        description=(
            "Which half of the search produced this session: the words "
            f"({MATCH_REASON_KEYWORD}), the vector ({MATCH_REASON_SEMANTIC}) "
            f"or both ({MATCH_REASON_BOTH})."
        ),
    )
    similarity: Optional[float] = Field(
        None,
        description=(
            "Cosine similarity of this session's closest chunk to the query "
            "vector. Null for a session the vector half did not return."
        ),
    )
    keyword_score: Optional[float] = Field(
        None,
        description=(
            "The session's keyword score, kept alongside the fused score so "
            "a fused ordering can still be read against the keyword one. "
            "Null for a session only the vector half returned."
        ),
    )
    best_chunk_rank: float = Field(
        ...,
        description=(
            "Relevance of the single best keyword matched chunk. Zero for a "
            "session no keyword term matched."
        ),
    )
    matched_chunk_count: int = Field(
        ..., description="How many distinct chunks of this session matched the words."
    )
    semantic_chunk_count: int = Field(
        0,
        description=(
            "How many distinct chunks of this session the vector half "
            "returned above the similarity floor."
        ),
    )
    first_match_at: Optional[datetime] = None
    last_match_at: Optional[datetime] = None
    snippets: List[SessionSearchSnippet] = Field(default_factory=list)


class SessionSearchDegraded(BaseModel):
    """What the answer could not do, stated rather than implied.

    The two booleans are coverage, not health: they say which halves of the
    search are actually behind these results. ``reasons`` names every case
    that reduced coverage, and there can be more than one (a provider that
    failed while a backfill was also behind).
    """

    keyword: bool = Field(
        True, description="Whether keyword ranking contributed to this answer."
    )
    semantic: bool = Field(
        False,
        description=(
            "Whether vector ranking contributed to this answer. False when "
            "the query could not be embedded and false when it could but the "
            "corpus holds no vector the query may be compared with."
        ),
    )
    reasons: List[str] = Field(
        default_factory=list,
        description=(
            "Machine readable reason codes, empty when nothing degraded. One "
            "of: " + ", ".join(DEGRADED_REASONS) + "."
        ),
    )
    detail: Optional[str] = Field(
        None, description="One sentence a console can show without decoding a code."
    )


class SessionSearchResponse(BaseModel):
    """Ranked sessions and everything needed to read them honestly."""

    query: str = Field(..., description="Query as parsed, with whitespace collapsed.")
    mode: SessionSearchMode = Field(..., description="Mode the caller asked for.")
    effective_mode: SessionSearchMode = Field(
        ...,
        description=(
            "Mode that actually ran. A semantic or hybrid request falls back "
            "to keyword when the query could not be embedded at all; it stays "
            "semantic or hybrid when the vector half ran, even if the corpus "
            "had nothing for it."
        ),
    )
    degraded: SessionSearchDegraded
    indexed_through: Optional[datetime] = Field(
        None,
        description=(
            "Newest content this account has in the corpus. The corpus fills "
            "forward, so an empty answer older than this marker means no "
            "match, and one newer means not indexed yet."
        ),
    )
    indexed_from: Optional[datetime] = Field(
        None,
        description=(
            "Oldest point in time this account's corpus covers without gaps. "
            "Indexing on write only covers sessions written since search "
            "shipped, so on a deployment whose backfill has not run this is "
            "the deploy date and nothing before it is searchable. Read it "
            "with indexed_through: together they are the window these "
            "results actually come from. Null when the account has nothing "
            "indexed at all."
        ),
    )
    backfill_complete: bool = Field(
        False,
        description=(
            "Whether the backfill has walked this account's retained history "
            "to the end. False means the window above is still growing "
            "backwards, or that no backfill has been run."
        ),
    )
    backfill_state: SessionSearchBackfillState = Field(
        BACKFILL_STATE_NOT_STARTED,
        description=(
            "One of: " + ", ".join(BACKFILL_STATES) + ". ``not_started`` on a "
            "deployment where the operator has not switched the backfill on, "
            "which is the shipped default."
        ),
    )
    embedded_through: Optional[datetime] = Field(
        None,
        description=(
            "Newest content this account has a vector for, from the model "
            "that embedded this query. Null in keyword mode and whenever the "
            "vector half did not run. Read against indexed_through it says "
            "how far behind the corpus the semantic half is."
        ),
    )
    total: int = Field(
        ...,
        description=(
            "Sessions a caller can page through. In keyword mode it is the "
            "exact number of distinct sessions matching. In a fused mode it "
            "is the size of the fused candidate set, which the documented "
            "fusion depth bounds; the degraded block says so when that "
            "bound was reached."
        ),
    )
    limit: int
    offset: int
    elapsed_ms: float = Field(..., description="Server side time spent on the search.")
    results: List[SessionSearchResult] = Field(default_factory=list)
