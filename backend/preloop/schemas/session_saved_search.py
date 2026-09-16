"""Request and response shapes for saved session searches.

A saved search stores the question and never the answer: query text, mode,
filters and snippet preferences, with nothing about the sessions a past run
returned. That is what makes re-running it honest a month later, when some of
those sessions have been redacted, retained out or deleted.

Two things a re-run reports that a plain search does not:

* ``unresolved_filters`` names every saved filter that no longer points at
  anything in the account (a deleted flow, a rotated api key). The filter is
  still applied, because widening a search the caller did not widen returns
  more than they asked for with no way to tell; the answer says which filter
  is now empty instead.
* ``ranking_changed`` says the ranking constants moved since the search was
  saved, so this ordering is not the one it was saved under. Nothing is
  pinned: pinning constants that are documented as unvalidated would freeze a
  guess (issue #673 open decision 4).

``extra="forbid"`` throughout, for the same reason the search body forbids it:
a silently dropped key saves a different search than the one the caller
believes they saved.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from preloop.models.crud.session_search_document import normalize_query
from preloop.models.models.session_saved_search import (
    MAX_NAME_CHARS,
    VISIBILITIES,
    VISIBILITY_ACCOUNT,
    VISIBILITY_PRIVATE,
)
from preloop.schemas.session_search import (
    DEFAULT_SESSION_RESULTS,
    DEFAULT_SNIPPETS_PER_SESSION,
    MAX_QUERY_CHARS,
    SessionSearchFilters,
    SessionSearchMode,
    SessionSearchResponse,
)
from preloop.models.crud.session_search_document import (
    MAX_SESSION_RESULTS,
    MAX_SNIPPETS_PER_SESSION,
)

#: Who can see a saved search. ``private`` is the default and what an unshared
#: search stays; ``account`` is reached only by an explicit update.
SessionSavedSearchVisibility = Literal["private", "account"]

# Reason codes for a saved filter that no longer resolves. Each names one
# thing the filter pointed at and what happened to it, because "this filter is
# stale" is not actionable and "the flow it names is gone" is.

#: The saved filter names a flow that no longer exists in this account.
FILTER_UNRESOLVED_FLOW = "flow_not_found"
#: The saved filter names an api key that no longer exists in this account.
FILTER_UNRESOLVED_API_KEY = "api_key_not_found"
#: The saved filter names a corpus source kind this build no longer writes.
FILTER_UNRESOLVED_SOURCE_KIND = "source_kind_unknown"
#: The stored payload holds a key the current filter schema does not define,
#: which happens when a saved search outlives a schema change. The key is
#: reported and left out of the run rather than guessed at.
FILTER_UNRESOLVED_UNKNOWN_FIELD = "field_no_longer_defined"

FILTER_UNRESOLVED_REASONS = (
    FILTER_UNRESOLVED_FLOW,
    FILTER_UNRESOLVED_API_KEY,
    FILTER_UNRESOLVED_SOURCE_KIND,
    FILTER_UNRESOLVED_UNKNOWN_FIELD,
)


class SessionSavedSearchCreate(BaseModel):
    """Save one named search."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=MAX_NAME_CHARS,
        description=(
            "Label shown in the saved list. Unique per author inside an "
            "account, compared with surrounding whitespace stripped."
        ),
    )
    query: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description="Search text, parsed exactly as the search endpoint parses it.",
    )
    mode: SessionSearchMode = Field(
        "keyword",
        description=(
            "Mode to run in. Saved as asked: a semantic search saved on a day "
            "the provider was down is still a semantic search, and a run that "
            "cannot serve the mode answers with the degraded block rather "
            "than an error."
        ),
    )
    filters: SessionSearchFilters = Field(
        default_factory=SessionSearchFilters,
        description=(
            "Filters, validated here and stored as the validated object, so "
            "an unrunnable search cannot be saved."
        ),
    )
    max_snippets_per_session: int = Field(
        DEFAULT_SNIPPETS_PER_SESSION,
        ge=0,
        le=MAX_SNIPPETS_PER_SESSION,
        description="Snippets per session this saved search asks for.",
    )
    include_snippet_text: bool = Field(
        True,
        description="Whether runs of this saved search return snippet text.",
    )
    visibility: SessionSavedSearchVisibility = Field(
        VISIBILITY_PRIVATE,
        description=(
            "Defaults to private. Sharing with the account is a deliberate "
            "step, not something a save does on the author's behalf."
        ),
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Strip the name and refuse one that is only whitespace."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must contain at least one non-whitespace character")
        return stripped

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """Normalise the query the same way the search endpoint does."""
        normalized = normalize_query(value)
        if not normalized:
            raise ValueError("query must contain at least one non-whitespace character")
        return normalized


class SessionSavedSearchUpdate(BaseModel):
    """Change a saved search. Every field is optional; at least one is required.

    Renaming, re-querying and sharing are the same operation on purpose: they
    are all "this saved question changed", and splitting them into three
    routes would say nothing extra.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=MAX_NAME_CHARS)
    query: Optional[str] = Field(None, min_length=1, max_length=MAX_QUERY_CHARS)
    mode: Optional[SessionSearchMode] = None
    filters: Optional[SessionSearchFilters] = None
    max_snippets_per_session: Optional[int] = Field(
        None, ge=0, le=MAX_SNIPPETS_PER_SESSION
    )
    include_snippet_text: Optional[bool] = None
    visibility: Optional[SessionSavedSearchVisibility] = Field(
        None,
        description=(
            "Set to "
            f"{VISIBILITY_ACCOUNT!r} to share with the account, back to "
            f"{VISIBILITY_PRIVATE!r} to unshare. Only the author may change it."
        ),
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: Optional[str]) -> Optional[str]:
        """Strip the name and refuse one that is only whitespace."""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must contain at least one non-whitespace character")
        return stripped

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: Optional[str]) -> Optional[str]:
        """Normalise the query the same way the search endpoint does."""
        if value is None:
            return None
        normalized = normalize_query(value)
        if not normalized:
            raise ValueError("query must contain at least one non-whitespace character")
        return normalized

    @model_validator(mode="after")
    def require_one_field(self) -> "SessionSavedSearchUpdate":
        """Refuse an empty body rather than reporting a no-op as a change."""
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class SessionSavedSearchRead(BaseModel):
    """One saved search as its account sees it."""

    id: UUID
    name: str
    query: str = Field(..., description="Query as parsed, with whitespace collapsed.")
    mode: SessionSearchMode
    filters: SessionSearchFilters
    max_snippets_per_session: int
    include_snippet_text: bool
    visibility: SessionSavedSearchVisibility
    owner_user_id: UUID = Field(..., description="The author, inside this account.")
    owned_by_caller: bool = Field(
        ...,
        description=(
            "Whether the caller is the author. Only the author may rename, "
            "edit, share or delete it; everyone else in the account may run "
            "a shared one."
        ),
    )
    shared_at: Optional[datetime] = Field(
        None,
        description="When it was last shared with the account; null while private.",
    )
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime] = Field(
        None, description="When it last ran, by anyone in the account."
    )
    run_count: int = Field(
        0, description="How many times it has been run through this endpoint."
    )
    ranking_changed: bool = Field(
        False,
        description=(
            "Whether the ranking constants have changed since this search was "
            "saved or last edited. True means a run's ordering is not the one "
            "it was saved under; nothing about the query itself changed."
        ),
    )
    filter_schema_changed: bool = Field(
        False,
        description=(
            "Whether the stored filters were validated against an older "
            "filter schema than the one running now."
        ),
    )


class SessionSavedSearchList(BaseModel):
    """A page of saved searches visible to the caller."""

    items: List[SessionSavedSearchRead] = Field(default_factory=list)
    total: int = Field(..., description="Saved searches visible to this caller.")
    limit: int
    offset: int


class SessionSavedSearchUnresolvedFilter(BaseModel):
    """One saved filter that no longer points at anything in the account."""

    field: str = Field(..., description="Filter key, as named in the saved payload.")
    value: Optional[str] = Field(
        None, description="Saved value, rendered as a string for display."
    )
    reason: str = Field(
        ...,
        description=(
            "Why it does not resolve. One of: "
            + ", ".join(FILTER_UNRESOLVED_REASONS)
            + "."
        ),
    )
    applied: bool = Field(
        ...,
        description=(
            "Whether the run still applied this filter. A filter naming a "
            "deleted flow is still applied, because corpus rows keep the flow "
            "id they were written with; only a key the schema no longer "
            "defines is left out, and it is reported here rather than dropped "
            "in silence."
        ),
    )


class SessionSavedSearchRunRequest(BaseModel):
    """Paging for one run. The saved search owns everything else."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        DEFAULT_SESSION_RESULTS,
        ge=1,
        le=MAX_SESSION_RESULTS,
        description=f"Sessions per page, at most {MAX_SESSION_RESULTS}.",
    )
    offset: int = Field(0, ge=0, description="Sessions to skip.")


class SessionSavedSearchRunResponse(BaseModel):
    """The saved question, what no longer resolves in it, and the answer."""

    saved_search: SessionSavedSearchRead
    unresolved_filters: List[SessionSavedSearchUnresolvedFilter] = Field(
        default_factory=list,
        description=(
            "Saved filters that no longer name anything in this account. "
            "Empty when everything the search names still exists."
        ),
    )
    ranking_changed: bool = Field(
        False,
        description=(
            "Same flag as on the saved search, repeated here so a client "
            "rendering only the run does not have to read it out of the "
            "saved search block."
        ),
    )
    search: SessionSearchResponse = Field(
        ...,
        description=(
            "The ranked page, exactly as the search endpoint would have "
            "answered the same question, degraded block included."
        ),
    )


__all__ = [
    "FILTER_UNRESOLVED_API_KEY",
    "FILTER_UNRESOLVED_FLOW",
    "FILTER_UNRESOLVED_REASONS",
    "FILTER_UNRESOLVED_SOURCE_KIND",
    "FILTER_UNRESOLVED_UNKNOWN_FIELD",
    "SessionSavedSearchCreate",
    "SessionSavedSearchList",
    "SessionSavedSearchRead",
    "SessionSavedSearchRunRequest",
    "SessionSavedSearchRunResponse",
    "SessionSavedSearchUnresolvedFilter",
    "SessionSavedSearchUpdate",
    "SessionSavedSearchVisibility",
    "VISIBILITIES",
]
