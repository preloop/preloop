"""CRUD helpers for the chunked runtime session search corpus.

The corpus is written per source: every write replaces the chunks of exactly
one source row (one gateway interaction, one transcript message, one tool
call, one operator note, one summary, one log excerpt) and touches nothing
else. Writes are idempotent on the content hash, so re-indexing an unchanged
source is a no op rather than a delete and insert.

Reading has two shapes. :meth:`CRUDSessionSearchDocument.search_account_chunks`
lists chunks newest first, which is what a timeline wants.
:meth:`CRUDSessionSearchDocument.search_sessions_ranked` answers the other
question, "where did an agent do this": it ranks chunks by relevance, fuses the
chunk scores of one session into a single session score, and returns database
generated snippets for the best chunks. Both bind ``account_id`` in the query
itself, never in a serialiser.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

from sqlalchemy import (
    Float,
    String,
    Text,
    and_,
    case,
    cast,
    delete,
    distinct,
    func,
    literal,
    null,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from preloop.config import settings

from ..models.api_usage import ApiUsage
from ..models.flow import Flow
from ..models.flow_execution import FlowExecution
from ..models.runtime_session import RuntimeSession
from ..models.session_search_document import (
    EMBEDDING_STATE_EMBEDDED,
    EMBEDDING_STATE_FAILED,
    EMBEDDING_STATE_IN_PROGRESS,
    EMBEDDING_STATE_PENDING,
    REDACTION_STATE_CLEAR,
    REDACTION_STATE_WITHHELD,
    TEXT_RETURNABLE_REDACTION_STATES,
    SessionSearchDocument,
)
from .base import CRUDBase

#: Text search configuration. This has to be the configuration the corpus is
#: indexed with (``session_search_document.search_vector`` is generated as
#: ``to_tsvector('simple', content)``), otherwise the query would be parsed
#: with one lexeme set and matched against another.
SEARCH_CONFIG = "simple"

#: Documented ceiling on how many sessions one search may return. Fifty is a
#: screenful and a half; past that a caller wants a narrower query, not a
#: longer page.
MAX_SESSION_RESULTS = 50

#: Documented ceiling on snippets returned per session. Snippets are the
#: expensive part of the response (one ``ts_headline`` call each), so the cap
#: is deliberately low.
MAX_SNIPPETS_PER_SESSION = 10

#: How much a matching chunk counts for once it is not the best one in its
#: session. Fusing with the plain sum would let a long, weakly matching session
#: outrank a short, exactly matching one; fusing with the plain maximum would
#: throw away the signal that the session matched repeatedly. Half weight on
#: the rest is the compromise.
CHUNK_FUSION_SECONDARY_WEIGHT = 0.5

#: Additive bonus for a session that matches in several distinct chunks,
#: damped by a logarithm so the tenth match is worth much less than the
#: second. A session that matches in five places is almost always the one the
#: operator wanted.
MULTI_CHUNK_BONUS_WEIGHT = 0.15

#: ``ts_headline`` options. ``MaxFragments`` above zero selects the fragment
#: based headline generator, which picks the densest window rather than simply
#: truncating from the start of the chunk. A chunk that carries none of the
#: query terms (which is every chunk a vector found and keyword did not) gets
#: its opening words instead, which is what a semantic hit has to show.
HEADLINE_OPTIONS = (
    "StartSel=<mark>, StopSel=</mark>, "
    "MaxWords=35, MinWords=10, ShortWord=3, "
    "MaxFragments=2, FragmentDelimiter= ... "
)

#: Chunks the vector pass reads before anything is grouped into sessions.
#: This is the requested nearest-neighbour depth, not a guarantee of the
#: closest N. The HNSW index cannot carry the equality filters
#: (``account_id``, ``embedding_model``, ``redaction_state``), so pgvector
#: post-filters candidates inside the ``hnsw.ef_search`` window. On a
#: multi-tenant corpus, or mid re-embedding sweep, that filter is selective:
#: the scan can return fewer qualifying chunks than this depth while closer
#: matches for this account were never visited. ``search_vector_chunks``
#: raises ``hnsw.ef_search`` to at least this depth for the statement. A
#: full page is still treated as "maybe more" rather than complete coverage.
VECTOR_CANDIDATE_CHUNKS = 200

#: Sessions the vector pass hands to fusion, after grouping.
MAX_VECTOR_SESSIONS = MAX_SESSION_RESULTS

#: Cosine similarity a chunk needs before it counts as a semantic match at
#: all. Without a floor every query returns the whole corpus in nearest
#: neighbour order, which reads as an answer and is not one.
MIN_SEMANTIC_SIMILARITY = 0.20

#: How a result matched: on the words, on the vector, or on both. Published
#: per result and per snippet, because a hybrid answer that does not say which
#: half produced a row is asking the reader to guess.
MatchReason = Literal["keyword", "semantic", "both"]

MATCH_REASON_KEYWORD: MatchReason = "keyword"
MATCH_REASON_SEMANTIC: MatchReason = "semantic"
MATCH_REASON_BOTH: MatchReason = "both"

MATCH_REASONS = (MATCH_REASON_KEYWORD, MATCH_REASON_SEMANTIC, MATCH_REASON_BOTH)

#: The constants above are tunable and unvalidated: they were chosen to make
#: the documented orderings hold on the fixtures in
#: ``backend/tests/models/crud/test_session_search_ranking.py`` and
#: ``backend/tests/models/crud/test_session_search_vector.py``, not from
#: measured relevance on real corpora. Treat a change to them as a product
#: change, not a refactor. The fusion weights live beside them in
#: ``preloop.services.session_search_fusion``.


def _held_session_exists() -> Any:
    """True when the chunk's session is under legal hold.

    Shared by the usage-purge delete, the released-orphan sweep, and the
    usage-orphan count so those paths cannot disagree about which chunks a
    hold is allowed to keep after the usage row they quote is gone.
    """
    return (
        select(RuntimeSession.id)
        .where(RuntimeSession.id == SessionSearchDocument.runtime_session_id)
        .where(RuntimeSession.legal_hold.is_(True))
        .exists()
    )


def _source_row_gone(source_model: Any) -> Any:
    """True when no row of ``source_model`` matches the chunk's source id."""
    return ~(
        select(source_model.id)
        .where(func.cast(source_model.id, String) == SessionSearchDocument.source_id)
        .exists()
    )


def content_hash_for(text: str) -> str:
    """Return the stable content hash used to detect an unchanged chunk."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_query(query: Optional[str]) -> Optional[str]:
    """Collapse a raw query to the string handed to ``websearch_to_tsquery``."""
    if not query:
        return None
    collapsed = " ".join(query.strip().split())
    return collapsed or None


@dataclass
class SessionSearchFilters:
    """Filters over the denormalised columns the corpus carries.

    Every field here is a snapshot the writer took from the source row, so a
    filtered search never joins the source table and never widens past the
    account bound.
    """

    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    api_key_id: Optional[Any] = None
    flow_id: Optional[Any] = None
    source_kind: Optional[str] = None


@dataclass
class RankedSnippet:
    """One matching chunk, with the identity needed to reopen that turn.

    ``match_reason`` and ``similarity`` say which half of a hybrid search
    produced the chunk. A keyword snippet carries no similarity, because it
    was never compared with a vector, and reporting one would be inventing a
    number.
    """

    document_id: Any
    runtime_session_id: Any
    source_kind: str
    source_id: str
    chunk_index: int
    occurred_at: datetime
    role: Optional[str]
    rank: float
    redaction_state: str
    text: Optional[str] = None
    match_reason: MatchReason = MATCH_REASON_KEYWORD
    similarity: Optional[float] = None


@dataclass
class VectorChunkHit:
    """One chunk a vector query found, with its cosine similarity.

    Deliberately not a session: the vector pass answers in chunks, and the
    grouping into sessions is a ranking decision that belongs to the service
    that also owns fusion.
    """

    document_id: Any
    runtime_session_id: Any
    source_kind: str
    source_id: str
    chunk_index: int
    occurred_at: datetime
    role: Optional[str]
    redaction_state: str
    similarity: float


@dataclass
class SessionIdentity:
    """The session columns a search result names, without its content."""

    runtime_session_id: Any
    session_source_type: Optional[str] = None
    session_source_id: Optional[str] = None
    session_reference: Optional[str] = None
    title: Optional[str] = None
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None


@dataclass
class EmbeddingCoverage:
    """What the corpus can answer semantically, for one account and model.

    Every field exists to make a degraded marker specific rather than vague.
    ``vectors`` with no ``model_vectors`` is a model mismatch, no vectors at
    all is a corpus that was never embedded, and ``pending`` is a backfill
    that has not reached this far yet.
    """

    vectors: int = 0
    model_vectors: int = 0
    pending: int = 0
    embedded_through: Optional[datetime] = None


@dataclass
class RankedSession:
    """One session, its fused score and its best snippets."""

    runtime_session_id: Any
    session_source_type: Optional[str]
    session_source_id: Optional[str]
    session_reference: Optional[str]
    title: Optional[str]
    started_at: Optional[datetime]
    last_activity_at: Optional[datetime]
    score: float
    best_chunk_rank: float
    matched_chunk_count: int
    first_match_at: Optional[datetime]
    last_match_at: Optional[datetime]
    snippets: List[RankedSnippet] = field(default_factory=list)


@dataclass
class SessionSearchChunk:
    """One chunk offered to the corpus by a writer."""

    content: str
    chunk_index: int = 0
    role: Optional[str] = None
    redaction_state: str = REDACTION_STATE_CLEAR
    embedding_state: str = EMBEDDING_STATE_PENDING
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    api_key_id: Optional[Any] = None
    flow_id: Optional[Any] = None
    status: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = field(default=None)


@dataclass
class SessionSearchHit:
    """One chunk as a search response may see it.

    The difference between this and the row is ``content``: a hit built by
    :meth:`CRUDSessionSearchDocument.search_account_hits` carries text only
    when the chunk's redaction state allows it, and the decision is made by
    the database in the projection rather than by the caller after the fact.
    A caller that forgets to check ``text_withheld`` still cannot leak, which
    is the only property that makes this safe to hand to an endpoint.
    """

    id: Any
    runtime_session_id: Any
    source_kind: str
    source_id: str
    chunk_index: int
    occurred_at: datetime
    role: Optional[str]
    content: str
    redaction_state: str
    text_withheld: bool
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    flow_id: Optional[Any] = None
    status: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None


class CRUDSessionSearchDocument(CRUDBase[SessionSearchDocument]):
    """CRUD operations for `SessionSearchDocument`."""

    def list_for_source(
        self, db: Session, *, source_kind: str, source_id: str
    ) -> List[SessionSearchDocument]:
        """Return every stored chunk of one source, in chunk order."""
        return (
            db.query(SessionSearchDocument)
            .filter(
                SessionSearchDocument.source_kind == source_kind,
                SessionSearchDocument.source_id == str(source_id),
            )
            .order_by(SessionSearchDocument.chunk_index.asc())
            .all()
        )

    def delete_for_source(
        self, db: Session, *, source_kind: str, source_id: str
    ) -> int:
        """Delete every chunk of one source and return how many went."""
        deleted = (
            db.query(SessionSearchDocument)
            .filter(
                SessionSearchDocument.source_kind == source_kind,
                SessionSearchDocument.source_id == str(source_id),
            )
            .delete(synchronize_session=False)
        )
        db.flush()
        return int(deleted or 0)

    def delete_for_sources(
        self,
        db: Session,
        *,
        source_kind: str,
        source_ids: Sequence[Any],
        excluding_held_sessions: bool = False,
    ) -> int:
        """Delete every chunk of many sources of one kind in one statement.

        Used by the purge, which removes its rows in id batches and has to
        take the chunks quoting them in the same pass. An empty batch is a no
        op rather than an unbounded ``IN ()``.

        ``excluding_held_sessions`` keeps chunks whose session is under legal
        hold. The usage purge sets this so a hold outranks a cutoff on a
        different class; the same predicate is used by
        :meth:`count_orphans_for_sources`.
        """
        wanted = [str(value) for value in source_ids]
        if not wanted:
            return 0
        stmt = delete(SessionSearchDocument).where(
            SessionSearchDocument.source_kind == source_kind,
            SessionSearchDocument.source_id.in_(wanted),
        )
        if excluding_held_sessions:
            stmt = stmt.where(~_held_session_exists())
        result = db.execute(stmt.execution_options(synchronize_session=False))
        return int(result.rowcount or 0)

    def delete_orphans_for_sources(
        self,
        db: Session,
        *,
        source_kind: str,
        source_model: Any,
        excluding_held_sessions: bool = False,
        account_id: Optional[Any] = None,
    ) -> int:
        """Delete chunks of one kind whose source row is gone.

        The usage pass calls this after its batch delete. A held session's
        gateway chunks survive the pass that removed the usage row they
        quote; once the hold is released those usage ids never appear in a
        later batch, so only this sweep can reclaim them. The hold
        exclusion is the same EXISTS predicate as
        :meth:`delete_for_sources` and :meth:`count_orphans_for_sources`.
        """
        clauses: List[Any] = [
            SessionSearchDocument.source_kind == source_kind,
            _source_row_gone(source_model),
        ]
        if excluding_held_sessions:
            clauses.append(~_held_session_exists())
        if account_id is not None:
            clauses.append(SessionSearchDocument.account_id == account_id)
        result = db.execute(
            delete(SessionSearchDocument)
            .where(*clauses)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def delete_for_sessions(
        self, db: Session, *, runtime_session_ids: Sequence[Any]
    ) -> int:
        """Delete every chunk of many sessions in one statement.

        The foreign key cascades, so the purge's ``DELETE FROM
        runtime_session`` would take these rows anyway. This runs first and
        returns the count, which turns the cascade from something the schema
        happens to do into something the purge states and audits. It also
        keeps the guarantee true on a database whose constraint was created
        before the cascade existed.
        """
        wanted = [value for value in runtime_session_ids if value is not None]
        if not wanted:
            return 0
        result = db.execute(
            delete(SessionSearchDocument)
            .where(SessionSearchDocument.runtime_session_id.in_(wanted))
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def withhold_source_text(
        self, db: Session, *, source_kind: str, source_id: str
    ) -> int:
        """Clear the stored text of one source's chunks and mark them withheld.

        The rows stay so that a search still knows the content existed, when
        it happened and in which session, which is what makes a redaction
        legible rather than indistinguishable from a gap. The text itself is
        overwritten, because a redaction that only hides a column from one
        query is not a redaction.
        """
        result = db.execute(
            update(SessionSearchDocument)
            .where(
                SessionSearchDocument.source_kind == source_kind,
                SessionSearchDocument.source_id == str(source_id),
            )
            .values(
                content="",
                content_hash=content_hash_for(""),
                redaction_state=REDACTION_STATE_WITHHELD,
            )
            .execution_options(synchronize_session=False)
        )
        db.flush()
        return int(result.rowcount or 0)

    def count_orphans_for_sessions(self, db: Session) -> int:
        """Chunks whose runtime session is gone. Always zero, by construction."""
        return int(
            db.execute(
                select(func.count(SessionSearchDocument.id)).where(
                    ~select(RuntimeSession.id)
                    .where(
                        RuntimeSession.id == SessionSearchDocument.runtime_session_id
                    )
                    .exists()
                )
            ).scalar_one()
        )

    def count_orphans_for_sources(
        self,
        db: Session,
        *,
        source_kind: str,
        source_model: Any,
        excluding_held_sessions: bool = False,
    ) -> int:
        """Chunks of one kind whose source row is gone.

        ``source_id`` is text because the corpus indexes sources with
        different key types, so the join casts the source id to text rather
        than the chunk's id to a uuid: a malformed id then fails to match
        instead of failing the statement.

        ``excluding_held_sessions`` matches :meth:`delete_for_sources`: a
        chunk whose session is under legal hold is not an orphan of a
        purged usage row. The operator check after a usage pass would
        otherwise fail on the state the hold is designed to produce.
        """
        clauses: List[Any] = [
            SessionSearchDocument.source_kind == source_kind,
            _source_row_gone(source_model),
        ]
        if excluding_held_sessions:
            clauses.append(~_held_session_exists())
        return int(
            db.execute(
                select(func.count(SessionSearchDocument.id)).where(*clauses)
            ).scalar_one()
        )

    def replace_source_chunks(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        source_kind: str,
        source_id: str,
        occurred_at: datetime,
        chunks: Sequence[SessionSearchChunk],
        commit: bool = False,
        existing: Optional[Sequence[SessionSearchDocument]] = None,
    ) -> List[SessionSearchDocument]:
        """Store the chunks of one source, skipping an unchanged rewrite.

        The stored chunks are compared with the offered ones by content hash,
        position, ``occurred_at``, ``role`` and ``status``. An identical set
        is left alone (no delete, no insert, no changed row count). A
        metadata-only change (same text, new timestamp or status) still
        replaces the rows so filter columns and the session-timeline index
        stay current. Anything else replaces the source's chunks wholesale,
        which is what keeps a shrinking source from leaving orphans behind.

        ``existing`` is the already-fetched row set for this source, used by
        the backfill walk so it does not pay ``list_for_source`` twice.
        ``None`` means look them up here. An empty sequence means they were
        looked up and there were none.
        """
        if existing is None:
            existing_rows = self.list_for_source(
                db, source_kind=source_kind, source_id=str(source_id)
            )
        else:
            existing_rows = list(existing)
        new_hashes = [content_hash_for(chunk.content) for chunk in chunks]
        if [row.content_hash for row in existing_rows] == new_hashes and all(
            row.occurred_at == occurred_at
            and row.role == chunk.role
            and row.status == chunk.status
            for row, chunk in zip(existing_rows, chunks, strict=False)
        ):
            return existing_rows

        if existing_rows:
            self.delete_for_source(
                db, source_kind=source_kind, source_id=str(source_id)
            )

        stored: List[SessionSearchDocument] = []
        for chunk, chunk_hash in zip(chunks, new_hashes, strict=False):
            db_obj = SessionSearchDocument(
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                source_kind=source_kind,
                source_id=str(source_id),
                chunk_index=chunk.chunk_index,
                occurred_at=occurred_at,
                role=chunk.role,
                content=chunk.content,
                content_hash=chunk_hash,
                redaction_state=chunk.redaction_state,
                embedding_state=chunk.embedding_state,
                model_alias=chunk.model_alias,
                provider_name=chunk.provider_name,
                runtime_principal_id=chunk.runtime_principal_id,
                api_key_id=chunk.api_key_id,
                flow_id=chunk.flow_id,
                status=chunk.status,
                meta_data=chunk.meta_data,
            )
            db.add(db_obj)
            stored.append(db_obj)

        db.flush()
        if commit:
            db.commit()
        return stored

    @staticmethod
    def _search_filters(
        *,
        account_id: Any,
        query: Optional[str] = None,
        runtime_session_id: Optional[Any] = None,
        source_kind: Optional[str] = None,
        role: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Any]:
        """Predicates shared by the row query and the guarded hit query.

        One builder, so the guarded read cannot drift away from the raw one
        and start matching a wider set of rows than the query it is meant to
        be the safe version of.
        """
        clauses: List[Any] = [SessionSearchDocument.account_id == account_id]
        if runtime_session_id is not None:
            clauses.append(
                SessionSearchDocument.runtime_session_id == runtime_session_id
            )
        if source_kind:
            clauses.append(SessionSearchDocument.source_kind == source_kind)
        if role:
            clauses.append(SessionSearchDocument.role == role)
        if start_date:
            clauses.append(SessionSearchDocument.occurred_at >= start_date)
        if end_date:
            clauses.append(SessionSearchDocument.occurred_at < end_date)

        normalized_query = " ".join(query.strip().split()) if query else None
        if normalized_query:
            clauses.append(
                SessionSearchDocument.search_vector.op("@@")(
                    func.websearch_to_tsquery("simple", normalized_query)
                )
            )
        return clauses

    def search_account_chunks(
        self,
        db: Session,
        *,
        account_id: Any,
        query: Optional[str] = None,
        runtime_session_id: Optional[Any] = None,
        source_kind: Optional[str] = None,
        role: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[SessionSearchDocument]:
        """Return account scoped chunk rows, newest first, optionally matched.

        The account filter is applied to the corpus table itself rather than
        to a joined source row, so a session id belonging to another account
        matches nothing whatever else is passed.

        This returns whole rows, withheld text included, and is therefore an
        internal accessor: a search response is built from
        :meth:`search_account_hits`.
        """
        return (
            db.query(SessionSearchDocument)
            .filter(
                *self._search_filters(
                    account_id=account_id,
                    query=query,
                    runtime_session_id=runtime_session_id,
                    source_kind=source_kind,
                    role=role,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            .order_by(
                SessionSearchDocument.occurred_at.desc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(limit)
            .offset(offset)
            .all()
        )

    def search_account_hits(
        self,
        db: Session,
        *,
        account_id: Any,
        query: Optional[str] = None,
        runtime_session_id: Optional[Any] = None,
        source_kind: Optional[str] = None,
        role: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[SessionSearchHit]:
        """The read seam a search response is built from.

        Same filters as :meth:`search_account_chunks`, but the projection
        replaces ``content`` with the empty string for any chunk whose
        redaction state is not in
        :data:`~preloop.models.models.session_search_document.TEXT_RETURNABLE_REDACTION_STATES`.
        The withheld text is therefore not merely unread: it never leaves the
        database, so nothing downstream, including a log line or an exception
        rendering the row, can spill it.

        Every surface that answers a user with corpus content is expected to
        call this. :meth:`search_account_chunks` returns rows and stays for
        internal callers that need the whole record.
        """
        returnable = SessionSearchDocument.redaction_state.in_(
            TEXT_RETURNABLE_REDACTION_STATES
        )
        guarded_content = case(
            (returnable, SessionSearchDocument.content),
            else_="",
        ).label("content")
        stmt = (
            select(
                SessionSearchDocument.id,
                SessionSearchDocument.runtime_session_id,
                SessionSearchDocument.source_kind,
                SessionSearchDocument.source_id,
                SessionSearchDocument.chunk_index,
                SessionSearchDocument.occurred_at,
                SessionSearchDocument.role,
                guarded_content,
                SessionSearchDocument.redaction_state,
                SessionSearchDocument.model_alias,
                SessionSearchDocument.provider_name,
                SessionSearchDocument.flow_id,
                SessionSearchDocument.status,
                SessionSearchDocument.meta_data,
            )
            .where(
                *self._search_filters(
                    account_id=account_id,
                    query=query,
                    runtime_session_id=runtime_session_id,
                    source_kind=source_kind,
                    role=role,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            .order_by(
                SessionSearchDocument.occurred_at.desc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return [
            SessionSearchHit(
                id=row.id,
                runtime_session_id=row.runtime_session_id,
                source_kind=row.source_kind,
                source_id=row.source_id,
                chunk_index=row.chunk_index,
                occurred_at=row.occurred_at,
                role=row.role,
                content=row.content or "",
                redaction_state=row.redaction_state,
                text_withheld=row.redaction_state
                not in TEXT_RETURNABLE_REDACTION_STATES,
                model_alias=row.model_alias,
                provider_name=row.provider_name,
                flow_id=row.flow_id,
                status=row.status,
                meta_data=row.meta_data,
            )
            for row in db.execute(stmt).all()
        ]

    def _scoped_conditions(
        self,
        *,
        account_id: Any,
        filters: Optional[SessionSearchFilters],
    ) -> List[ColumnElement[bool]]:
        """The account bound and the caller's filters, without a match term.

        ``account_id`` is first and unconditional. The account bound lives
        here, in the query, so there is no code path that can produce a row
        from another account for a serialiser to have to remember to drop.
        Shared by the keyword passes and the vector pass, so the two halves of
        a hybrid answer cannot end up searching different sets of rows.
        """
        conditions: List[ColumnElement[bool]] = [
            SessionSearchDocument.account_id == account_id,
        ]
        active = filters or SessionSearchFilters()
        if active.start_date is not None:
            conditions.append(SessionSearchDocument.occurred_at >= active.start_date)
        if active.end_date is not None:
            conditions.append(SessionSearchDocument.occurred_at < active.end_date)
        if active.model_alias:
            conditions.append(SessionSearchDocument.model_alias == active.model_alias)
        if active.provider_name:
            conditions.append(
                SessionSearchDocument.provider_name == active.provider_name
            )
        if active.runtime_principal_id:
            conditions.append(
                SessionSearchDocument.runtime_principal_id
                == active.runtime_principal_id
            )
        if active.api_key_id is not None:
            conditions.append(SessionSearchDocument.api_key_id == active.api_key_id)
        if active.flow_id is not None:
            conditions.append(SessionSearchDocument.flow_id == active.flow_id)
        if active.source_kind:
            conditions.append(SessionSearchDocument.source_kind == active.source_kind)
        return conditions

    def _match_conditions(
        self,
        *,
        account_id: Any,
        tsquery: ColumnElement[Any],
        filters: Optional[SessionSearchFilters],
    ) -> List[ColumnElement[bool]]:
        """The scoped conditions plus the full text match term."""
        conditions = self._scoped_conditions(account_id=account_id, filters=filters)
        conditions.insert(1, SessionSearchDocument.search_vector.op("@@")(tsquery))
        return conditions

    def indexed_through(self, db: Session, *, account_id: Any) -> Optional[datetime]:
        """Return the newest ``occurred_at`` this account has in the corpus.

        A search answer is only as fresh as the corpus behind it, and the
        corpus fills forward from deploy. Publishing the marker lets a caller
        tell "no session did that" apart from "nothing that old is indexed".
        """
        marker = (
            db.query(func.max(SessionSearchDocument.occurred_at))
            .filter(SessionSearchDocument.account_id == account_id)
            .scalar()
        )
        return marker if isinstance(marker, datetime) else None

    def search_sessions_ranked(
        self,
        db: Session,
        *,
        account_id: Any,
        query: str,
        filters: Optional[SessionSearchFilters] = None,
        limit: int = 20,
        offset: int = 0,
        max_snippets_per_session: int = 3,
        include_snippet_text: bool = True,
    ) -> Tuple[List[RankedSession], int]:
        """Rank sessions by relevance to ``query`` and return their snippets.

        The query text goes through ``websearch_to_tsquery``, so a quoted
        phrase stays a phrase, ``or`` alternates and a leading ``-`` excludes,
        parsed with the same text search configuration the corpus is indexed
        with.

        Chunk ranks are fused into one session score as::

            score = best
                  + CHUNK_FUSION_SECONDARY_WEIGHT * (total - best)
                  + MULTI_CHUNK_BONUS_WEIGHT * ln(matching_chunks)

        so a session that matched once scores exactly its chunk rank, and a
        session that matched in several distinct chunks is lifted above an
        equally good single match. The constants are tunable and unvalidated;
        see the module header.

        Returns:
            The page of ranked sessions and the total number of distinct
            sessions matching, which is the number a caller pages through.
        """
        normalized = normalize_query(query)
        if not normalized:
            return [], 0

        limit = max(1, min(int(limit), MAX_SESSION_RESULTS))
        offset = max(0, int(offset))
        snippet_budget = max(
            0, min(int(max_snippets_per_session), MAX_SNIPPETS_PER_SESSION)
        )

        tsquery = func.websearch_to_tsquery(SEARCH_CONFIG, normalized)
        conditions = self._match_conditions(
            account_id=account_id, tsquery=tsquery, filters=filters
        )

        total = int(
            db.query(func.count(distinct(SessionSearchDocument.runtime_session_id)))
            .filter(*conditions)
            .scalar()
            or 0
        )
        if total == 0:
            return [], 0

        chunk_rank = cast(
            func.ts_rank(SessionSearchDocument.search_vector, tsquery), Float
        )
        best_rank = func.max(chunk_rank)
        matched_chunks = func.count(SessionSearchDocument.id)
        score = (
            best_rank
            + CHUNK_FUSION_SECONDARY_WEIGHT * (func.sum(chunk_rank) - best_rank)
            + MULTI_CHUNK_BONUS_WEIGHT * func.ln(cast(matched_chunks, Float))
        )
        last_match_at = func.max(SessionSearchDocument.occurred_at)

        # Grouping by the runtime session primary key lets PostgreSQL resolve
        # the other session columns by functional dependency, so the join can
        # carry session identity without widening the GROUP BY.
        ranked_rows = (
            db.query(
                RuntimeSession.id.label("runtime_session_id"),
                RuntimeSession.session_source_type.label("session_source_type"),
                RuntimeSession.session_source_id.label("session_source_id"),
                RuntimeSession.session_reference.label("session_reference"),
                RuntimeSession.title.label("title"),
                RuntimeSession.started_at.label("started_at"),
                RuntimeSession.last_activity_at.label("last_activity_at"),
                score.label("score"),
                best_rank.label("best_chunk_rank"),
                matched_chunks.label("matched_chunk_count"),
                func.min(SessionSearchDocument.occurred_at).label("first_match_at"),
                last_match_at.label("last_match_at"),
            )
            .join(
                RuntimeSession,
                RuntimeSession.id == SessionSearchDocument.runtime_session_id,
            )
            .filter(*conditions)
            .group_by(RuntimeSession.id)
            # Relevance first, and only then recency: ordering by time is the
            # wrong answer to "where did an agent do this", which is the whole
            # reason this endpoint exists. The session id tail break keeps
            # paging stable when two sessions score identically.
            .order_by(score.desc(), last_match_at.desc(), RuntimeSession.id.asc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        if not ranked_rows:
            return [], total

        sessions = [
            RankedSession(
                runtime_session_id=row.runtime_session_id,
                session_source_type=row.session_source_type,
                session_source_id=row.session_source_id,
                session_reference=row.session_reference,
                title=row.title,
                started_at=row.started_at,
                last_activity_at=row.last_activity_at,
                score=float(row.score or 0.0),
                best_chunk_rank=float(row.best_chunk_rank or 0.0),
                matched_chunk_count=int(row.matched_chunk_count or 0),
                first_match_at=row.first_match_at,
                last_match_at=row.last_match_at,
            )
            for row in ranked_rows
        ]

        if snippet_budget:
            snippets = self._snippets_for_sessions(
                db,
                conditions=conditions,
                tsquery=tsquery,
                session_ids=[row.runtime_session_id for row in ranked_rows],
                max_per_session=snippet_budget,
                include_text=include_snippet_text,
            )
            for session in sessions:
                session.snippets = snippets.get(str(session.runtime_session_id), [])

        return sessions, total

    def _snippets_for_sessions(
        self,
        db: Session,
        *,
        conditions: Sequence[ColumnElement[bool]],
        tsquery: ColumnElement[Any],
        session_ids: Sequence[Any],
        max_per_session: int,
        include_text: bool,
    ) -> Dict[str, List[RankedSnippet]]:
        """Return the best chunks per session, keyed by session id as text.

        The window runs over the same predicate as the ranking pass, so a
        snippet can only ever come from a chunk that was counted. ``ts_headline``
        is applied in the outer query, after the window has cut the candidate
        set down to ``max_per_session`` rows per session, because a headline
        costs a re-parse of the chunk text.
        """
        chunk_rank = cast(
            func.ts_rank(SessionSearchDocument.search_vector, tsquery), Float
        )
        columns: List[Any] = [
            SessionSearchDocument.id.label("document_id"),
            SessionSearchDocument.runtime_session_id.label("runtime_session_id"),
            SessionSearchDocument.source_kind.label("source_kind"),
            SessionSearchDocument.source_id.label("source_id"),
            SessionSearchDocument.chunk_index.label("chunk_index"),
            SessionSearchDocument.occurred_at.label("occurred_at"),
            SessionSearchDocument.role.label("role"),
            SessionSearchDocument.redaction_state.label("redaction_state"),
            chunk_rank.label("rank"),
            func.row_number()
            .over(
                partition_by=SessionSearchDocument.runtime_session_id,
                order_by=(
                    chunk_rank.desc(),
                    SessionSearchDocument.occurred_at.asc(),
                    SessionSearchDocument.chunk_index.asc(),
                ),
            )
            .label("position"),
        ]
        if include_text:
            # Only projected when a headline will actually be built from it, so
            # "no snippet text" means the content column is never read at all,
            # not read and then dropped on the way out. Withheld text is the
            # empty string in this projection, the same rule as
            # search_account_hits: the database never returns the stored body.
            returnable = SessionSearchDocument.redaction_state.in_(
                TEXT_RETURNABLE_REDACTION_STATES
            )
            guarded_content = case(
                (returnable, SessionSearchDocument.content),
                else_="",
            )
            columns.append(guarded_content.label("content"))

        windowed = (
            select(*columns)
            .where(*conditions)
            .where(SessionSearchDocument.runtime_session_id.in_(list(session_ids)))
            .subquery()
        )

        headline: Any
        if include_text:
            headline = func.ts_headline(
                SEARCH_CONFIG, windowed.c.content, tsquery, HEADLINE_OPTIONS
            )
        else:
            headline = cast(null(), Text)

        rows = (
            db.query(
                windowed.c.document_id,
                windowed.c.runtime_session_id,
                windowed.c.source_kind,
                windowed.c.source_id,
                windowed.c.chunk_index,
                windowed.c.occurred_at,
                windowed.c.role,
                windowed.c.redaction_state,
                windowed.c.rank,
                headline.label("snippet"),
            )
            .filter(windowed.c.position <= max_per_session)
            .order_by(
                windowed.c.runtime_session_id.asc(),
                windowed.c.position.asc(),
            )
            .all()
        )

        grouped: Dict[str, List[RankedSnippet]] = {}
        for row in rows:
            grouped.setdefault(str(row.runtime_session_id), []).append(
                RankedSnippet(
                    document_id=row.document_id,
                    runtime_session_id=row.runtime_session_id,
                    source_kind=row.source_kind,
                    source_id=row.source_id,
                    chunk_index=int(row.chunk_index or 0),
                    occurred_at=row.occurred_at,
                    role=row.role,
                    rank=float(row.rank or 0.0),
                    redaction_state=row.redaction_state,
                    text=(
                        row.snippet
                        if include_text
                        and row.redaction_state in TEXT_RETURNABLE_REDACTION_STATES
                        else None
                    ),
                )
            )
        return grouped

    def earliest_occurred_at(
        self, db: Session, *, account_id: Any
    ) -> Optional[datetime]:
        """Return the oldest chunk timestamp an account holds, if any.

        This is how far back the corpus currently reaches for content indexed
        on write; the backfill watermark is what moves it further back.
        """
        return (
            db.query(func.min(SessionSearchDocument.occurred_at))
            .filter(SessionSearchDocument.account_id == account_id)
            .scalar()
        )

    def snippets_for_sessions(
        self,
        db: Session,
        *,
        account_id: Any,
        query: str,
        session_ids: Sequence[Any],
        filters: Optional[SessionSearchFilters] = None,
        max_per_session: int = 3,
        include_text: bool = True,
    ) -> Dict[str, List[RankedSnippet]]:
        """Keyword snippets for a named set of sessions.

        The ranking pass fetches its own snippets, but a fused answer cannot:
        the page it ends up showing is only known after the two candidate
        lists have been merged. This is the same window and the same headline
        as the ranking pass, over the sessions that actually made the page, so
        snippets are never generated for rows nobody will read.
        """
        normalized = normalize_query(query)
        wanted = [value for value in session_ids if value is not None]
        budget = max(0, min(int(max_per_session), MAX_SNIPPETS_PER_SESSION))
        if not normalized or not wanted or not budget:
            return {}
        tsquery = func.websearch_to_tsquery(SEARCH_CONFIG, normalized)
        return self._snippets_for_sessions(
            db,
            conditions=self._match_conditions(
                account_id=account_id, tsquery=tsquery, filters=filters
            ),
            tsquery=tsquery,
            session_ids=wanted,
            max_per_session=budget,
            include_text=include_text,
        )

    def search_vector_chunks(
        self,
        db: Session,
        *,
        account_id: Any,
        embedding: Sequence[float],
        embedding_model: str,
        filters: Optional[SessionSearchFilters] = None,
        limit: int = VECTOR_CANDIDATE_CHUNKS,
        min_similarity: float = MIN_SEMANTIC_SIMILARITY,
    ) -> List[VectorChunkHit]:
        """Return the chunks nearest to ``embedding``, nearest first.

        Two restrictions are not optional and are both in the query:

        ``embedding_model`` pins the candidates to vectors produced by the
        same model as the query vector. A corpus can legitimately hold
        vectors from more than one model (a provider change, a dimension
        change, a half finished re-embedding sweep), and a cosine distance
        between two models' spaces is a number with no meaning. A query
        therefore never scores across models: it sees the part of the corpus
        that speaks its own language and the degraded block says so.

        ``redaction_state`` is pinned to ``clear``, which is the only state
        the worker embeds. It matters after the fact too: a chunk withheld
        *after* it was embedded keeps its vector, and without this term a
        semantic query would still surface the row whose text was taken away.

        Args:
            db: Request scoped session.
            account_id: The caller's account, bound in the query.
            embedding: The query vector.
            embedding_model: Model identity of ``embedding``.
            filters: The same filter block the keyword pass uses.
            limit: Nearest neighbour depth.
            min_similarity: Cosine similarity floor a chunk must clear.

        Returns:
            Candidate chunks ordered by similarity, then by chunk id so the
            ordering is stable when two vectors are equally close.
        """
        if not embedding or not embedding_model:
            return []
        depth = max(1, min(int(limit), VECTOR_CANDIDATE_CHUNKS))
        # Filtered ANN: equality predicates are applied after the HNSW scan.
        # Raise ef_search to the requested depth so the window is at least as
        # large as the page we intend to return. Recall is still approximate.
        ef_search = max(depth, VECTOR_CANDIDATE_CHUNKS)
        db.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
        distance = SessionSearchDocument.embedding.cosine_distance(list(embedding))
        similarity = (literal(1.0) - distance).label("similarity")
        conditions = self._scoped_conditions(account_id=account_id, filters=filters)
        conditions.extend(
            [
                SessionSearchDocument.embedding.isnot(None),
                SessionSearchDocument.embedding_model == embedding_model,
                SessionSearchDocument.redaction_state == REDACTION_STATE_CLEAR,
                distance <= (1.0 - float(min_similarity)),
            ]
        )
        rows = db.execute(
            select(
                SessionSearchDocument.id.label("document_id"),
                SessionSearchDocument.runtime_session_id.label("runtime_session_id"),
                SessionSearchDocument.source_kind.label("source_kind"),
                SessionSearchDocument.source_id.label("source_id"),
                SessionSearchDocument.chunk_index.label("chunk_index"),
                SessionSearchDocument.occurred_at.label("occurred_at"),
                SessionSearchDocument.role.label("role"),
                SessionSearchDocument.redaction_state.label("redaction_state"),
                similarity,
            )
            .where(*conditions)
            .order_by(distance.asc(), SessionSearchDocument.id.asc())
            .limit(depth)
        ).all()
        return [
            VectorChunkHit(
                document_id=row.document_id,
                runtime_session_id=row.runtime_session_id,
                source_kind=row.source_kind,
                source_id=row.source_id,
                chunk_index=int(row.chunk_index or 0),
                occurred_at=row.occurred_at,
                role=row.role,
                redaction_state=row.redaction_state,
                similarity=float(row.similarity or 0.0),
            )
            for row in rows
        ]

    def snippet_text_for_documents(
        self,
        db: Session,
        *,
        account_id: Any,
        document_ids: Sequence[Any],
        query: str,
    ) -> Dict[str, Optional[str]]:
        """Headline text for named chunks, keyed by chunk id as text.

        This is how a semantically matched chunk gets something to show. The
        same ``ts_headline`` call is used as for a keyword snippet, so a chunk
        that happens to carry a query term still gets it marked, and a chunk
        that carries none gets its opening words. Withheld text is the empty
        string in the projection, the same rule as everywhere else in this
        module: the stored body never leaves the database.
        """
        normalized = normalize_query(query)
        wanted = [value for value in document_ids if value is not None]
        if not normalized or not wanted:
            return {}
        tsquery = func.websearch_to_tsquery(SEARCH_CONFIG, normalized)
        returnable = SessionSearchDocument.redaction_state.in_(
            TEXT_RETURNABLE_REDACTION_STATES
        )
        guarded_content = case(
            (returnable, SessionSearchDocument.content),
            else_="",
        )
        rows = db.execute(
            select(
                SessionSearchDocument.id.label("document_id"),
                SessionSearchDocument.redaction_state.label("redaction_state"),
                func.ts_headline(
                    SEARCH_CONFIG, guarded_content, tsquery, HEADLINE_OPTIONS
                ).label("snippet"),
            ).where(
                SessionSearchDocument.account_id == account_id,
                SessionSearchDocument.id.in_(wanted),
            )
        ).all()
        return {
            str(row.document_id): (
                row.snippet
                if row.redaction_state in TEXT_RETURNABLE_REDACTION_STATES
                else None
            )
            for row in rows
        }

    def session_identities(
        self, db: Session, *, account_id: Any, session_ids: Sequence[Any]
    ) -> Dict[str, SessionIdentity]:
        """Identity columns for named sessions, keyed by session id as text.

        The keyword pass joins these columns while it ranks. The vector pass
        answers in chunks, so the sessions it found need them looked up, and
        the lookup is account scoped for the same reason every other read
        here is: a session id from another account must resolve to nothing.
        """
        wanted = [value for value in session_ids if value is not None]
        if not wanted:
            return {}
        rows = db.execute(
            select(
                RuntimeSession.id,
                RuntimeSession.session_source_type,
                RuntimeSession.session_source_id,
                RuntimeSession.session_reference,
                RuntimeSession.title,
                RuntimeSession.started_at,
                RuntimeSession.last_activity_at,
            ).where(
                RuntimeSession.account_id == account_id,
                RuntimeSession.id.in_(wanted),
            )
        ).all()
        return {
            str(row.id): SessionIdentity(
                runtime_session_id=row.id,
                session_source_type=row.session_source_type,
                session_source_id=row.session_source_id,
                session_reference=row.session_reference,
                title=row.title,
                started_at=row.started_at,
                last_activity_at=row.last_activity_at,
            )
            for row in rows
        }

    def embedding_coverage(
        self, db: Session, *, account_id: Any, embedding_model: str
    ) -> EmbeddingCoverage:
        """What one account's corpus can answer with vectors of one model.

        One statement, four conditional aggregates, because a search should
        not pay four round trips to be able to say why its semantic half
        returned little. The counts are what turn "no semantic results" into
        one of "nothing is embedded", "everything is embedded with a
        different model" or "the backfill has not got there yet".
        """
        embedded = SessionSearchDocument.embedding.isnot(None)
        same_model = and_(
            embedded, SessionSearchDocument.embedding_model == embedding_model
        )
        waiting = and_(
            SessionSearchDocument.embedding.is_(None),
            SessionSearchDocument.redaction_state == REDACTION_STATE_CLEAR,
            SessionSearchDocument.content != "",
        )
        row = db.execute(
            select(
                func.count(SessionSearchDocument.id).filter(embedded).label("vectors"),
                func.count(SessionSearchDocument.id)
                .filter(same_model)
                .label("model_vectors"),
                func.count(SessionSearchDocument.id).filter(waiting).label("pending"),
                func.max(SessionSearchDocument.occurred_at)
                .filter(same_model)
                .label("embedded_through"),
            ).where(SessionSearchDocument.account_id == account_id)
        ).one()
        return EmbeddingCoverage(
            vectors=int(row.vectors or 0),
            model_vectors=int(row.model_vectors or 0),
            pending=int(row.pending or 0),
            embedded_through=(
                row.embedded_through
                if isinstance(row.embedded_through, datetime)
                else None
            ),
        )

    def count_for_session(
        self, db: Session, *, account_id: Any, runtime_session_id: Any
    ) -> int:
        """Return how many chunks one session holds inside one account."""
        return int(
            db.query(func.count(SessionSearchDocument.id))
            .filter(
                SessionSearchDocument.account_id == account_id,
                SessionSearchDocument.runtime_session_id == runtime_session_id,
            )
            .scalar()
            or 0
        )

    # ------------------------------------------------------------------
    # Embedding queue
    #
    # The corpus is written by the request path and embedded by a worker.
    # Everything below is the worker's half: which chunks it is allowed to
    # take, how it takes them exactly once, and how a vector gets written
    # back with the identity of whatever produced it.
    # ------------------------------------------------------------------

    def held_runtime_session_ids(self, db: Session, *, account_id: Any) -> set[str]:
        """Sessions frozen by an active legal hold, for this account.

        A session is held when ``runtime_session.legal_hold`` is true, or
        when any of its metered gateway rows belongs to a flow execution
        under hold. The column is the stronger check (issue #650); the
        execution path covers sessions whose hold was recorded only on the
        flow.

        Evaluated as a set rather than a join in the claim query because the
        common case is an empty set, and an empty set costs one cheap index
        lookup instead of a correlated subquery per candidate chunk.
        """
        held: set[str] = {
            str(value)
            for value in db.execute(
                select(RuntimeSession.id).where(
                    RuntimeSession.account_id == account_id,
                    RuntimeSession.legal_hold.is_(True),
                )
            ).scalars()
            if value is not None
        }
        held_executions = select(FlowExecution.id).where(
            FlowExecution.legal_hold.is_(True),
            FlowExecution.flow_id.in_(
                select(Flow.id).where(Flow.account_id == account_id)
            ),
        )
        rows = db.execute(
            select(ApiUsage.runtime_session_id)
            .where(
                ApiUsage.account_id == account_id,
                ApiUsage.runtime_session_id.isnot(None),
                ApiUsage.flow_execution_id.in_(held_executions),
            )
            .distinct()
        ).scalars()
        held.update(str(value) for value in rows if value is not None)
        return held

    def _stale_claim_cutoff(self, now: Optional[datetime] = None) -> datetime:
        """When an ``in_progress`` claim is old enough to reclaim.

        The window is twice the provider timeout plus a 30s margin, so a
        live call cannot be stolen by another worker, but a daemon-thread
        death or unexpected exception cannot strand the batch forever.
        Compared as naive UTC to match ``Base.updated_at``.
        """
        timeout = float(getattr(settings, "session_embedding_timeout_seconds", 30.0))
        window = timedelta(seconds=max(1.0, (2.0 * timeout) + 30.0))
        stamp = now or datetime.now(UTC)
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(UTC).replace(tzinfo=None)
        return stamp - window

    def _embeddable_filters(
        self,
        *,
        account_id: Any,
        now: Optional[datetime] = None,
        source_kinds: Optional[Sequence[str]] = None,
    ) -> list[Any]:
        """Conditions every embeddable chunk must satisfy.

        Only ``clear`` chunks qualify. A redacted chunk has had a credential
        masked and a metadata-only chunk never captured content at all;
        embedding either would put a vector of the mask, or of a descriptor,
        into a corpus that a semantic query then treats as the real thing.

        ``pending`` rows are always claimable. ``in_progress`` rows whose
        ``updated_at`` is older than the reclaim window are claimable too,
        so a worker crash between the claim commit and store cannot hide
        the backlog from the pending count or from the next run.

        ``source_kinds`` narrows the corpus to the kinds the account's
        embedding scope admits; ``None`` means every kind. Narrowing is a
        filter on the claim rather than a state written onto the rows, so
        widening the scope later needs no sweep: the chunks were pending all
        along and the next pass sees them.
        """
        stale_before = self._stale_claim_cutoff(now)
        claimable_state = or_(
            SessionSearchDocument.embedding_state == EMBEDDING_STATE_PENDING,
            and_(
                SessionSearchDocument.embedding_state == EMBEDDING_STATE_IN_PROGRESS,
                SessionSearchDocument.updated_at < stale_before,
            ),
        )
        filters = [
            SessionSearchDocument.account_id == account_id,
            claimable_state,
            SessionSearchDocument.redaction_state == REDACTION_STATE_CLEAR,
            SessionSearchDocument.embedding.is_(None),
            SessionSearchDocument.content != "",
        ]
        if source_kinds is not None:
            filters.append(SessionSearchDocument.source_kind.in_(list(source_kinds)))
        return filters

    def count_pending_embeddings(
        self,
        db: Session,
        *,
        account_id: Any,
        excluded_session_ids: Optional[Iterable[Any]] = None,
        now: Optional[datetime] = None,
        source_kinds: Optional[Sequence[str]] = None,
    ) -> int:
        """How many chunks this account still has waiting for a vector.

        ``source_kinds`` is the account's embedding scope: a transcript chunk
        an account has chosen not to embed is not backlog, so it is not
        counted as pending either.
        """
        stmt = db.query(func.count(SessionSearchDocument.id)).filter(
            *self._embeddable_filters(
                account_id=account_id, now=now, source_kinds=source_kinds
            )
        )
        excluded = [str(value) for value in (excluded_session_ids or [])]
        if excluded:
            stmt = stmt.filter(
                SessionSearchDocument.runtime_session_id.notin_(excluded)
            )
        return int(stmt.scalar() or 0)

    def claim_pending_chunks(
        self,
        db: Session,
        *,
        account_id: Any,
        limit: int,
        excluded_session_ids: Optional[Iterable[Any]] = None,
        now: Optional[datetime] = None,
        source_kinds: Optional[Sequence[str]] = None,
        commit: bool = False,
    ) -> List[SessionSearchDocument]:
        """Claim the oldest waiting chunks of one session, oldest first.

        A batch never spans sessions. One batch produces one purpose tagged
        usage row, and a row that covered several sessions could not be
        attributed to any of them honestly; keeping the batch inside one
        session is what lets the spend be named and then excluded from that
        session's rollup.

        Claiming moves the rows to ``in_progress`` and commits, so a provider
        call that takes seconds does not hold row locks for its duration and
        a second worker skips these rows instead of waiting behind them.
        A claim left ``in_progress`` past the reclaim window is treated as
        pending again, so a restart cannot strand the batch.

        ``source_kinds`` carries the account's embedding scope into the
        claim, which is the only place the scope is enforced: rows outside it
        are never claimed, so they are never sent to a provider and never
        cost anything.
        """
        if limit <= 0:
            return []
        excluded = [str(value) for value in (excluded_session_ids or [])]
        filters = self._embeddable_filters(
            account_id=account_id, now=now, source_kinds=source_kinds
        )

        oldest_stmt = db.query(SessionSearchDocument.runtime_session_id).filter(
            *filters
        )
        if excluded:
            oldest_stmt = oldest_stmt.filter(
                SessionSearchDocument.runtime_session_id.notin_(excluded)
            )
        oldest = (
            oldest_stmt.order_by(
                SessionSearchDocument.occurred_at.asc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(1)
            .first()
        )
        if oldest is None:
            return []
        runtime_session_id = oldest[0]

        claimed = (
            db.query(SessionSearchDocument)
            .filter(
                *filters,
                SessionSearchDocument.runtime_session_id == runtime_session_id,
            )
            .order_by(
                SessionSearchDocument.occurred_at.asc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        for row in claimed:
            row.embedding_state = EMBEDDING_STATE_IN_PROGRESS
            row.embedding_attempts = int(row.embedding_attempts or 0) + 1
        db.flush()
        if commit:
            db.commit()
            for row in claimed:
                db.refresh(row)
        return claimed

    def store_embeddings(
        self,
        db: Session,
        *,
        vectors: Sequence[Tuple[SessionSearchDocument, Sequence[float]]],
        model_identity: str,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> int:
        """Write vectors back with the identity of the model that made them."""
        stamp = now or datetime.now(UTC)
        written = 0
        for row, vector in vectors:
            row.embedding = list(vector)
            row.embedding_model = model_identity
            row.embedded_at = stamp
            row.embedding_state = EMBEDDING_STATE_EMBEDDED
            written += 1
        db.flush()
        if commit:
            db.commit()
        return written

    def release_claim(
        self,
        db: Session,
        *,
        chunks: Sequence[SessionSearchDocument],
        max_attempts: int,
        commit: bool = False,
    ) -> int:
        """Return unembedded chunks to the queue, or retire the hopeless ones.

        A chunk the provider has already refused ``max_attempts`` times goes
        to ``failed`` instead of back to ``pending``: the alternative is one
        poison chunk at the head of the oldest-first queue starving every
        chunk behind it.
        """
        released = 0
        for row in chunks:
            if row.embedding_state != EMBEDDING_STATE_IN_PROGRESS:
                continue
            if int(row.embedding_attempts or 0) >= max_attempts:
                row.embedding_state = EMBEDDING_STATE_FAILED
            else:
                row.embedding_state = EMBEDDING_STATE_PENDING
            released += 1
        db.flush()
        if commit:
            db.commit()
        return released

    def count_embedded_for_account(self, db: Session, *, account_id: Any) -> int:
        """How many chunks of one account carry a vector."""
        return int(
            db.query(func.count(SessionSearchDocument.id))
            .filter(
                SessionSearchDocument.account_id == account_id,
                SessionSearchDocument.embedding.isnot(None),
            )
            .scalar()
            or 0
        )
