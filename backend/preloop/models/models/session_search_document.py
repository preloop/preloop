"""Chunked search corpus rows for runtime session content.

One row is one chunk of one source: a gateway interaction, a transcript
message, a tool call, a browser step, an operator note, a session summary
or a flow log line.
The corpus is account scoped and session scoped on purpose, so a query can be
bounded by account before it ever touches the full text index.

The stored ``search_vector`` is a generated column rather than an expression
index: ranking and snippets read the vector back instead of recomputing
``to_tsvector`` per candidate row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db.vector_types import VectorType
from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .runtime_session import RuntimeSession

#: Source kinds a chunk may come from. Kept as a plain tuple (not a database
#: enum) so a new writer does not need a migration to add its kind.
SOURCE_KIND_GATEWAY_INTERACTION = "gateway_interaction"
SOURCE_KIND_TRANSCRIPT_MESSAGE = "transcript_message"
SOURCE_KIND_TOOL_CALL = "tool_call"
SOURCE_KIND_BROWSER_STEP = "browser_step"
SOURCE_KIND_OPERATOR_NOTE = "operator_note"
SOURCE_KIND_SESSION_SUMMARY = "session_summary"
SOURCE_KIND_FLOW_LOG = "flow_log"

SOURCE_KINDS = (
    SOURCE_KIND_GATEWAY_INTERACTION,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
    SOURCE_KIND_TOOL_CALL,
    SOURCE_KIND_BROWSER_STEP,
    SOURCE_KIND_OPERATOR_NOTE,
    SOURCE_KIND_SESSION_SUMMARY,
    SOURCE_KIND_FLOW_LOG,
)

#: Content stored verbatim after sanitising, nothing was masked.
REDACTION_STATE_CLEAR = "clear"
#: Content stored with at least one credential-looking value masked.
REDACTION_STATE_REDACTED = "redacted"
#: Capture was disabled: the chunk carries a descriptor and no captured text.
REDACTION_STATE_METADATA_ONLY = "metadata_only"
#: The source was redacted *after* this chunk was written. The stored text is
#: cleared when the state is set, and the read path refuses to return text for
#: the state as well, so a chunk in this state cannot answer a search with
#: content the source no longer has.
REDACTION_STATE_WITHHELD = "withheld"

REDACTION_STATES = (
    REDACTION_STATE_CLEAR,
    REDACTION_STATE_REDACTED,
    REDACTION_STATE_METADATA_ONLY,
    REDACTION_STATE_WITHHELD,
)

#: The redaction states whose stored text a search response may return.
#:
#: ``clear`` is text that needed no masking and ``redacted`` is text whose
#: credential-looking values were masked before it was ever stored, so both
#: are text the corpus is allowed to have. ``metadata_only`` holds a synthetic
#: descriptor (kind, role, length) and no captured text at all, which is safe
#: for the same reason. ``withheld`` is the one state that exists precisely
#: because the text stopped being allowed, and it is the one state left out.
#:
#: The read path derives its behaviour from this tuple rather than testing for
#: a state inline, so adding a state without deciding whether it is returnable
#: is not possible: a new state is withheld until it is named here.
TEXT_RETURNABLE_REDACTION_STATES = (
    REDACTION_STATE_CLEAR,
    REDACTION_STATE_REDACTED,
    REDACTION_STATE_METADATA_ONLY,
)

#: The chunk is waiting for an embedding worker to pick it up.
EMBEDDING_STATE_PENDING = "pending"
#: The chunk is deliberately excluded from embedding.
EMBEDDING_STATE_SKIPPED = "skipped"
#: A worker has claimed this chunk and is calling the provider for it.
EMBEDDING_STATE_IN_PROGRESS = "in_progress"
#: An embedding exists for this chunk elsewhere.
EMBEDDING_STATE_EMBEDDED = "embedded"
#: Embedding was attempted too many times and is not retried again.
EMBEDDING_STATE_FAILED = "failed"

EMBEDDING_STATES = (
    EMBEDDING_STATE_PENDING,
    EMBEDDING_STATE_SKIPPED,
    EMBEDDING_STATE_IN_PROGRESS,
    EMBEDDING_STATE_EMBEDDED,
    EMBEDDING_STATE_FAILED,
)

#: Width of the stored vector column. The number is a decision, not a fact
#: (issue #624 open decision 1): 1536 is what the OpenAI compatible defaults
#: return, and the storage difference against 512 is roughly threefold. A
#: vector column and an HNSW index both need a fixed width, so a later change
#: is a migration; ``embedding_model`` is what makes that change survivable,
#: because every stored vector says which model and width produced it.
EMBEDDING_DIMENSIONS = 1536


class SessionSearchDocument(Base):
    """One searchable chunk of runtime session content."""

    __tablename__ = "session_search_document"
    __table_args__ = (
        UniqueConstraint(
            "source_kind",
            "source_id",
            "chunk_index",
            name="uq_session_search_document_source_chunk",
        ),
        Index(
            "ix_session_search_document_vector",
            "search_vector",
            postgresql_using="gin",
        ),
        Index(
            "ix_session_search_document_account_time",
            "account_id",
            text("occurred_at DESC"),
        ),
        Index(
            "ix_session_search_document_session_time",
            "runtime_session_id",
            "occurred_at",
        ),
        Index(
            "ix_session_search_document_embedding_pending",
            "occurred_at",
            postgresql_where=text("embedding_state = 'pending'"),
        ),
        # Restricted to rows that actually carry a vector: an HNSW build over
        # a mostly NULL column would index nothing and cost the whole table.
        Index(
            "ix_session_search_document_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    role: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    search_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', content)", persisted=True),
        nullable=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    redaction_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=REDACTION_STATE_CLEAR
    )
    embedding_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EMBEDDING_STATE_PENDING
    )
    embedding: Mapped[Optional[List[float]]] = mapped_column(
        VectorType(EMBEDDING_DIMENSIONS), nullable=True
    )
    #: The model identity that produced ``embedding``, as
    #: ``<provider>:<model>@<dimensions>``. Stored per chunk so a corpus
    #: written across a provider or dimension change stays interpretable and
    #: a re-embedding sweep can find exactly the rows it has to redo.
    embedding_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    embedded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Provider attempts spent on this chunk. Bounds the retry of a chunk the
    #: provider keeps refusing, so one poison row cannot hold a queue forever.
    embedding_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Denormalised filter columns: snapshots of the source row taken at write
    # time, so a filtered search never has to join the source table.
    model_alias: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    runtime_principal_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    api_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    flow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    meta_data: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    account: Mapped["Account"] = relationship("Account")
    runtime_session: Mapped["RuntimeSession"] = relationship("RuntimeSession")

    def __repr__(self) -> str:
        return (
            f"<SessionSearchDocument(id={self.id}, source_kind={self.source_kind}, "
            f"source_id={self.source_id}, chunk_index={self.chunk_index})>"
        )
