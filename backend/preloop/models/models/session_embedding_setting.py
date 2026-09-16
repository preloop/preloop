"""Per account opt in for embedding session content.

Embedding session chunks sends the account's own agent text to a provider.
That is a decision the account makes, not one a deployment makes on its
behalf, so the switch lives here rather than in deployment config: one row
per account, absent or ``enabled = False`` until somebody turns it on, and
enabling requires naming the provider and the model the text is about to be
sent to.

The row also carries how much of a session may be embedded: ``scope`` is
``summaries_only`` by default and ``full`` when an account asks for recall
over whole transcripts. Keyword search reads the whole corpus either way.

The deployment keeps a kill switch of its own
(``SESSION_EMBEDDING_ENABLED``). Both must say yes. The kill switch stops
embedding only: keyword indexing into the corpus is a separate setting and
keeps running.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .session_search_document import (
    EMBEDDING_DIMENSIONS,
    SOURCE_KIND_SESSION_SUMMARY,
)

if TYPE_CHECKING:
    from .account import Account

#: An OpenAI compatible ``/embeddings`` endpoint named by base url. This is
#: the provider a self hosted or air gapped install actually has: the vectors
#: are produced by whatever the operator runs, and the text never leaves it.
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
#: A sentence-transformers model loaded in process, which is the path
#: ``crud/embedding.py`` already supports for issue embeddings.
PROVIDER_LOCAL = "local"

EMBEDDING_PROVIDERS = (PROVIDER_OPENAI_COMPATIBLE, PROVIDER_LOCAL)

#: Embed only the chunks that describe the session as a whole: its generated
#: title and summary. One short chunk per session, which is the text a person
#: searches by ("the session where we debugged the pool"), at a fortieth of
#: the storage and provider spend of the transcript behind it.
EMBEDDING_SCOPE_SUMMARIES_ONLY = "summaries_only"
#: Embed every chunk the corpus holds, transcripts included. The opt in an
#: account makes when it wants recall of what was actually said, and accepts
#: what that costs.
EMBEDDING_SCOPE_FULL = "full"

EMBEDDING_SCOPES = (EMBEDDING_SCOPE_SUMMARIES_ONLY, EMBEDDING_SCOPE_FULL)

#: Source kinds a scope admits, or ``None`` for "every kind". The worker asks
#: this rather than testing the scope inline, so a scope added without saying
#: what it embeds cannot silently mean "everything".
SCOPE_SOURCE_KINDS: dict[str, Optional[tuple[str, ...]]] = {
    EMBEDDING_SCOPE_SUMMARIES_ONLY: (SOURCE_KIND_SESSION_SUMMARY,),
    EMBEDDING_SCOPE_FULL: None,
}


def effective_scope(scope: Optional[str]) -> str:
    """The scope this build will actually apply.

    An unknown stored value is the default, never ``full``. The worker and
    the read API both use this so a row from a newer build cannot silently
    embed more than this build understands, and cannot 500 the console.
    """
    cleaned = (scope or EMBEDDING_SCOPE_SUMMARIES_ONLY).strip()
    if cleaned not in SCOPE_SOURCE_KINDS:
        return EMBEDDING_SCOPE_SUMMARIES_ONLY
    return cleaned


def source_kinds_for_scope(scope: Optional[str]) -> Optional[tuple[str, ...]]:
    """Source kinds a scope embeds, or ``None`` when it embeds all of them.

    An unknown value is read as the default rather than as "everything": a
    row that somehow carries a scope this build does not know embeds less
    than asked, never more.
    """
    return SCOPE_SOURCE_KINDS[effective_scope(scope)]


#: Reason codes recorded on the row when a run could not do its work. These
#: are degraded states, not errors: the chunks stay pending and the next run
#: picks them up.
DEGRADED_DAILY_CAP = "daily_cap_reached"
DEGRADED_PROVIDER_ERROR = "provider_error"
DEGRADED_DIMENSION_MISMATCH = "dimension_mismatch"
DEGRADED_MISCONFIGURED = "misconfigured"
DEGRADED_UNPRICED_MODEL = "unpriced_model"

DEGRADED_REASONS = (
    DEGRADED_DAILY_CAP,
    DEGRADED_PROVIDER_ERROR,
    DEGRADED_DIMENSION_MISMATCH,
    DEGRADED_MISCONFIGURED,
    DEGRADED_UNPRICED_MODEL,
)


class SessionEmbeddingSetting(Base):
    """One account's answer to "may we embed this account's session text"."""

    __tablename__ = "session_embedding_setting"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PROVIDER_OPENAI_COMPATIBLE,
        server_default=PROVIDER_OPENAI_COMPATIBLE,
    )
    #: Required for :data:`PROVIDER_OPENAI_COMPATIBLE`. Enabling names the
    #: endpoint the account's text is posted to. The deployment
    #: ``SESSION_EMBEDDING_API_KEY`` is never sent here unless this URL is
    #: on ``SESSION_EMBEDDING_API_KEY_BASE_URLS``; an account-chosen host
    #: must not harvest the shared credential. ``enable()`` also refuses a
    #: non-https URL and a private, loopback, or link-local IP host.
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    model_identifier: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dimensions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=EMBEDDING_DIMENSIONS,
        server_default=str(EMBEDDING_DIMENSIONS),
    )
    #: What the worker is allowed to embed for this account. Defaults to
    #: :data:`EMBEDDING_SCOPE_SUMMARIES_ONLY`, because embedding every
    #: transcript chunk is bounded by the daily cap but is still the wrong
    #: default: a 1536 wide vector is about 6 KB, so a session of about 40
    #: chunks is about 240 KB of vectors before the index. Keyword search
    #: keeps covering the whole corpus either way.
    scope: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=EMBEDDING_SCOPE_SUMMARIES_ONLY,
        server_default=EMBEDDING_SCOPE_SUMMARIES_ONLY,
    )
    #: Per account daily spend ceiling in USD. NULL falls back to the
    #: deployment default.
    daily_cap_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    degraded_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    degraded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    account: Mapped["Account"] = relationship("Account")

    @property
    def model_identity(self) -> Optional[str]:
        """Stable identity stamped on every vector this setting produces."""
        if not self.model_identifier:
            return None
        return f"{self.provider}:{self.model_identifier}@{self.dimensions}"

    @property
    def embedded_source_kinds(self) -> Optional[tuple[str, ...]]:
        """Source kinds this account's scope admits, ``None`` for all of them."""
        return source_kinds_for_scope(self.scope)

    def __repr__(self) -> str:
        return (
            f"<SessionEmbeddingSetting(account_id={self.account_id}, "
            f"enabled={self.enabled}, provider={self.provider}, "
            f"scope={self.scope})>"
        )
