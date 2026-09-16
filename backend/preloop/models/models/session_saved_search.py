"""A named question about what the agents did, saved for re-running.

A saved search is not a bookmark of results. It stores the question (query
text, mode, filters, snippet preferences) and nothing about the answer, so
re-running it next week searches the corpus as it is next week. Storing rows
instead would be a report, and a report of an agent transcript ages badly: the
sessions it points at may have been redacted, retained out or deleted since.

Three columns exist only so a re-run can be read honestly:

* ``filters_version`` records the filter schema the payload was validated
  against, so a payload written before a schema change can be recognised
  rather than silently reinterpreted;
* ``ranking_identity`` records the ranking constants in force when the search
  was saved. The constants are still described as tunable and unvalidated, so
  a saved search pins nothing; it reports that the ordering it was saved under
  is not the ordering it now runs under;
* ``last_run_at`` and ``run_count`` say whether anyone actually re-runs it,
  which is the only evidence that saving searches was worth shipping.

Visibility starts private. Sharing is an explicit step (issue #673 open
decision 1), never a default, because the query text an operator saves is a
description of what they are hunting for in their own account's transcripts.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Final, Literal, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .user import User

#: Only the author sees it. The default, and what an unshared search stays.
VISIBILITY_PRIVATE: Final[Literal["private"]] = "private"
#: Everyone in the account who may read sessions sees it. Reached only by an
#: explicit share, and reversible.
VISIBILITY_ACCOUNT: Final[Literal["account"]] = "account"

VISIBILITIES = (VISIBILITY_PRIVATE, VISIBILITY_ACCOUNT)

#: Longest saved name. A name is a label in a list, not a description.
MAX_NAME_CHARS = 120

#: Schema version of the stored ``filters`` payload. Bump it when the filter
#: contract changes shape (a renamed key, a changed type), so a run can say
#: "this was saved against an older filter schema" instead of quietly
#: dropping what it no longer understands.
FILTER_SCHEMA_VERSION = 1


class SessionSavedSearch(Base):
    """One named, re-runnable session search owned by one user."""

    __tablename__ = "session_saved_search"
    __table_args__ = (
        # Names are unique per author, not per account: two people may both
        # have a search called "billing", and neither one may rename the
        # other's.
        UniqueConstraint(
            "account_id",
            "owner_user_id",
            "name",
            name="uq_session_saved_search_owner_name",
        ),
        Index(
            "ix_session_saved_search_account_visibility",
            "account_id",
            "visibility",
        ),
        Index(
            "ix_session_saved_search_account_owner",
            "account_id",
            "owner_user_id",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The author. A saved search is deleted with the user who wrote it,
    #: including a shared one: it is that person's question, and an account
    #: that wants to keep it can save its own copy while it is shared.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(MAX_NAME_CHARS), nullable=False)
    #: The query exactly as the search endpoint would parse it.
    query: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``keyword``, ``semantic`` or ``hybrid``. Stored as the caller asked,
    #: never as the mode that happened to run: a semantic search saved on a
    #: day the provider was down is still a semantic search.
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The validated filter object, serialised (issue #673 open decision 3).
    #: An opaque payload would let anything be saved and fail at run time;
    #: validating at save time means an unrunnable search cannot be stored.
    filters: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    filters_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=FILTER_SCHEMA_VERSION,
        server_default=str(FILTER_SCHEMA_VERSION),
    )
    max_snippets_per_session: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    include_snippet_text: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    visibility: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=VISIBILITY_PRIVATE,
        server_default=VISIBILITY_PRIVATE,
    )
    #: When it was last shared with the account. Null while private.
    shared_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Ranking constants in force when this was saved or last edited.
    ranking_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    run_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    account: Mapped["Account"] = relationship("Account")
    owner: Mapped["User"] = relationship("User")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Name the row without quoting the saved query text."""
        return (
            f"<SessionSavedSearch id={self.id} account_id={self.account_id} "
            f"name={self.name!r} mode={self.mode}>"
        )
