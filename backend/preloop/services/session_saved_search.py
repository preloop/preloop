"""Saving a session search, and re-running it honestly later.

A saved search is a promise that the same question gives a comparable answer
next week. Nothing here can make that promise true, because the corpus changes
and the ranking constants are still tunable and unvalidated; what it can do is
say out loud where the promise stops holding:

* a filter that no longer names anything in the account is reported, and still
  applied. Applying it returns nothing, which is honest; widening the search
  instead would return sessions the caller never asked for and give them no
  way to tell;
* a payload written against an older filter schema is reported key by key, and
  the keys the schema no longer defines are left out rather than guessed at;
* a mode that cannot run today is answered the way the search endpoint answers
  it, with results and a degraded block, not with an error;
* an ordering produced under different ranking constants than the ones the
  search was saved under is flagged. Pinning constants per saved search would
  freeze a guess (issue #673 open decision 4), so the constants move and the
  answer admits it.

The account bound lives in the CRUD queries, here as everywhere else.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_key, crud_flow, crud_session_saved_search
from preloop.models.models.session_saved_search import (
    FILTER_SCHEMA_VERSION,
    VISIBILITY_ACCOUNT,
    SessionSavedSearch,
)
from preloop.models.models.session_search_document import SOURCE_KINDS
from preloop.schemas.session_saved_search import (
    FILTER_UNRESOLVED_API_KEY,
    FILTER_UNRESOLVED_FLOW,
    FILTER_UNRESOLVED_SOURCE_KIND,
    FILTER_UNRESOLVED_UNKNOWN_FIELD,
    SessionSavedSearchRead,
    SessionSavedSearchRunRequest,
    SessionSavedSearchRunResponse,
    SessionSavedSearchUnresolvedFilter,
)
from preloop.schemas.session_search import (
    SessionSearchFilters,
    SessionSearchRequest,
)
from preloop.services import session_search
from preloop.services.session_search_fusion import ranking_identity
from preloop.services.session_search_semantic import EmbeddingProvider

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Timezone aware now, in one place so tests can patch one thing."""
    return datetime.now(UTC)


def filters_payload(filters: SessionSearchFilters) -> Dict[str, Any]:
    """Serialise a validated filter object for storage.

    ``exclude_none`` keeps the stored payload to the filters the caller
    actually set, so a later filter gaining a default does not look like a
    filter this search chose.
    """
    return filters.model_dump(mode="json", exclude_none=True)


def restore_filters(
    payload: Optional[Dict[str, Any]],
) -> Tuple[SessionSearchFilters, List[SessionSavedSearchUnresolvedFilter]]:
    """Rebuild a filter object from a stored payload.

    Args:
        payload: The stored JSON payload, possibly written against an older
            filter schema.

    Returns:
        The filters that can still be applied, and one entry per key the
        current schema cannot accept. Those keys are dropped from the run and
        named in the answer; silently dropping one would run a wider search
        than the saved search describes.
    """
    values = dict(payload or {})
    unresolved: List[SessionSavedSearchUnresolvedFilter] = []
    known = set(SessionSearchFilters.model_fields)
    for key in sorted(set(values) - known):
        unresolved.append(
            SessionSavedSearchUnresolvedFilter(
                field=key,
                value=_as_text(values[key]),
                reason=FILTER_UNRESOLVED_UNKNOWN_FIELD,
                applied=False,
            )
        )
        values.pop(key)
    try:
        filters = SessionSearchFilters.model_validate(values)
    except ValidationError:
        # A stored value the current schema rejects (a renamed source kind, a
        # tightened type) must not make the saved search unrunnable: drop the
        # offending keys one at a time and report each of them.
        filters, dropped = _validate_key_by_key(values)
        unresolved.extend(dropped)
    return filters, unresolved


def _validate_key_by_key(
    values: Dict[str, Any],
) -> Tuple[SessionSearchFilters, List[SessionSavedSearchUnresolvedFilter]]:
    """Keep the filter keys that still validate and report the ones that do not."""
    kept: Dict[str, Any] = {}
    dropped: List[SessionSavedSearchUnresolvedFilter] = []
    for key in sorted(values):
        candidate = dict(kept)
        candidate[key] = values[key]
        try:
            SessionSearchFilters.model_validate(candidate)
        except ValidationError:
            reason = (
                FILTER_UNRESOLVED_SOURCE_KIND
                if key == "source_kind"
                else FILTER_UNRESOLVED_UNKNOWN_FIELD
            )
            dropped.append(
                SessionSavedSearchUnresolvedFilter(
                    field=key,
                    value=_as_text(values[key]),
                    reason=reason,
                    applied=False,
                )
            )
            continue
        kept = candidate
    return SessionSearchFilters.model_validate(kept), dropped


def _as_text(value: Any) -> Optional[str]:
    """Render a stored filter value for display without guessing at its type."""
    if value is None:
        return None
    return str(value)


def unresolved_references(
    db: Session,
    *,
    account_id: Any,
    filters: SessionSearchFilters,
) -> List[SessionSavedSearchUnresolvedFilter]:
    """Name the saved filters that point at something the account no longer has.

    Only the two filters that name another row are checked: a flow and an api
    key. The rest (a model alias, a provider name, a runtime principal) are
    strings denormalised onto the corpus row itself, so there is no other row
    for them to stop resolving against; a corpus that no longer carries the
    value simply returns nothing for it.

    Both lookups are account scoped, so a flow id belonging to another account
    reads as missing rather than as resolving.
    """
    unresolved: List[SessionSavedSearchUnresolvedFilter] = []
    if filters.flow_id is not None:
        flow = crud_flow.get(db, str(filters.flow_id), str(account_id))
        if flow is None:
            unresolved.append(
                SessionSavedSearchUnresolvedFilter(
                    field="flow_id",
                    value=str(filters.flow_id),
                    reason=FILTER_UNRESOLVED_FLOW,
                    applied=True,
                )
            )
    if filters.api_key_id is not None:
        api_key = crud_api_key.get(
            db, str(filters.api_key_id), account_id=str(account_id)
        )
        if api_key is None:
            unresolved.append(
                SessionSavedSearchUnresolvedFilter(
                    field="api_key_id",
                    value=str(filters.api_key_id),
                    reason=FILTER_UNRESOLVED_API_KEY,
                    applied=True,
                )
            )
    if filters.source_kind is not None and filters.source_kind not in SOURCE_KINDS:
        # Reachable when a build stops writing a source kind a saved search
        # names. The filter is still applied and simply matches nothing.
        unresolved.append(
            SessionSavedSearchUnresolvedFilter(
                field="source_kind",
                value=filters.source_kind,
                reason=FILTER_UNRESOLVED_SOURCE_KIND,
                applied=True,
            )
        )
    return unresolved


def to_read(
    saved: SessionSavedSearch, *, caller_user_id: Any
) -> SessionSavedSearchRead:
    """Present one saved search to the caller who asked for it.

    ``owned_by_caller`` is derived here, but the account and visibility bound
    that decided whether this row could be loaded at all was applied in SQL.
    """
    filters, _ = restore_filters(saved.filters)
    return SessionSavedSearchRead(
        id=saved.id,
        name=saved.name,
        query=saved.query,
        mode=saved.mode,  # type: ignore[arg-type]
        filters=filters,
        max_snippets_per_session=saved.max_snippets_per_session,
        include_snippet_text=saved.include_snippet_text,
        visibility=saved.visibility,  # type: ignore[arg-type]
        owner_user_id=saved.owner_user_id,
        owned_by_caller=str(saved.owner_user_id) == str(caller_user_id),
        shared_at=saved.shared_at,
        created_at=saved.created_at,
        updated_at=saved.updated_at,
        last_run_at=saved.last_run_at,
        run_count=saved.run_count or 0,
        ranking_changed=saved.ranking_identity != ranking_identity(),
        filter_schema_changed=saved.filters_version != FILTER_SCHEMA_VERSION,
    )


def is_shared(saved: SessionSavedSearch) -> bool:
    """Whether this saved search is visible to the whole account."""
    return saved.visibility == VISIBILITY_ACCOUNT


def run_saved_search(
    db: Session,
    *,
    account_id: Any,
    caller_user_id: Any,
    saved: SessionSavedSearch,
    paging: SessionSavedSearchRunRequest,
    provider: Optional[EmbeddingProvider] = None,
    now: Optional[datetime] = None,
) -> SessionSavedSearchRunResponse:
    """Re-run a saved search and report what no longer holds about it.

    Args:
        db: Request scoped session.
        account_id: The caller's account, bound in every CRUD query below.
        caller_user_id: Who is running it, for ``owned_by_caller`` only.
        saved: The saved search, already loaded through a visibility scoped
            read.
        paging: Page size and offset for this run. Everything else comes from
            the saved row.
        provider: Injected embedding provider, passed through to the search.
        now: Clock override for the run counters and the daily cap window.

    Returns:
        The saved question, the filters that no longer resolve, and the
        ranked page the search endpoint would have returned for it.
    """
    filters, schema_unresolved = restore_filters(saved.filters)
    unresolved = schema_unresolved + unresolved_references(
        db, account_id=account_id, filters=filters
    )
    request = SessionSearchRequest(
        query=saved.query,
        mode=saved.mode,  # type: ignore[arg-type]
        filters=filters,
        limit=paging.limit,
        offset=paging.offset,
        max_snippets_per_session=saved.max_snippets_per_session,
        include_snippet_text=saved.include_snippet_text,
    )
    response = session_search.search_sessions(
        db,
        account_id=account_id,
        request=request,
        provider=provider,
        now=now,
    )
    # The run is recorded whatever came back. An empty or degraded answer is
    # still somebody asking the question, which is what these counters are
    # for.
    crud_session_saved_search.record_run(db, saved=saved, now=now)
    read = to_read(saved, caller_user_id=caller_user_id)
    return SessionSavedSearchRunResponse(
        saved_search=read,
        unresolved_filters=unresolved,
        ranking_changed=read.ranking_changed,
        search=response,
    )
