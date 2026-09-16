"""One audit row for every search over session content.

Opening one session somebody linked reads one session. A content search reads
every captured prompt, response and tool call the account holds, ranked, and
hands back the fragments that matched. That is the broadest read the product
offers, and until this module existed nothing recorded that it happened.

Three decisions are worth stating, because they are the ones a compliance
owner asks about.

The query text is not stored by default. A query is frequently the secret the
searcher is hunting for, typed verbatim: an audit trail that keeps every
search string is a second copy of the thing the first copy was supposed to
protect. What is always stored is a hash of the normalised query, which
answers the questions a review actually asks, "was this searched for again",
"who else searched for it", without holding the string. An account that wants
the text turns :data:`QUERY_TEXT_OPT_IN_KEY` on, and the name says what it
does.

Snippet text is never stored, under any setting. The row records that a
search happened and what it was allowed to see, not what it saw.

An audit failure never changes the answer. The row is written through the
same ``crud_audit_log.log_action`` path everything else uses, and a failure
to write it is logged and swallowed, which is the discipline the indexing
path already keeps: a search that fails because its audit row could not be
written trains operators to turn auditing off.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log
from preloop.models.crud.session_search_document import normalize_query
from preloop.schemas.session_search import (
    MAX_QUERY_CHARS,
    SessionSearchRequest,
    SessionSearchResponse,
)

logger = logging.getLogger(__name__)

#: The action and resource type every row carries. Both are indexed columns on
#: ``audit_log``, so "show me every content search this account ran" is one
#: query against the account and action index.
AUDIT_ACTION = "query"
AUDIT_RESOURCE_TYPE = "session_search"

#: Statuses, matching the vocabulary the audit table already uses.
STATUS_SUCCESS = "success"
STATUS_DENIED = "denied"
STATUS_FAILURE = "failure"

#: Where the call came from. An operator reading the trail has to be able to
#: tell an agent searching the corpus from a person searching it, and the two
#: arrive through different surfaces.
SOURCE_API = "api"
SOURCE_MCP = "mcp"

#: What kind of actor ran the search. The audit table has a ``user_id`` column
#: and no agent column, so an agent actor is named in the details and
#: ``user_id`` stays null rather than borrowing a person's identity.
ACTOR_USER = "user"
ACTOR_MANAGED_AGENT = "managed_agent"
ACTOR_UNKNOWN = "unknown"

#: Account level opt in that adds the raw query text to the row. Off unless
#: the account's ``meta_data`` says otherwise, and read in exactly one place
#: (:func:`query_text_audit_enabled`) so "does this deployment keep search
#: text" has one answer. Nothing in this repository writes the key yet: like
#: the account scope grant added in #658, the read path is the mechanism, and
#: which surface sets it is part of the open decision on #624.
QUERY_TEXT_OPT_IN_KEY = "session_search_audit_store_query_text"

#: Prefix on the stored digest. It names the algorithm, so a later change of
#: digest is visible in the data rather than silently comparing unequal.
QUERY_HASH_PREFIX = "sha256"

#: Longest `scope` kept on the row. Known values are `own` and `account`.
#: An unknown-scope refusal still echoes the caller string, but an unbounded
#: MCP argument must not inflate the JSONB column.
AUDIT_SCOPE_MAX_CHARS = 64


def _normalized(query: Optional[str]) -> str:
    """The query as the search itself parsed it, never None.

    ``normalize_query`` answers None for a query that is only whitespace,
    which the request model refuses. The audit path is not a validator, so it
    records the empty string rather than deciding a search did not happen.
    """
    return normalize_query(query) or ""


def _bounded(value: str, max_chars: int) -> str:
    """Cut one stored string at a hard length, never raising."""
    if len(value) <= max_chars:
        return value
    return value[:max_chars]


def query_hash(query: str) -> str:
    """Return the stable digest recorded instead of the query text.

    The query is normalised first, the same collapse of whitespace the
    request model applies, and lowercased, so that the same search typed
    twice hashes the same way and a review can group repeats. Two different
    searches give two different digests, which is the whole property the row
    needs: it identifies a query without holding it.

    The digest is deliberately unsalted. A salt would stop an offline guess
    at a short query, and would also stop the one thing the hash is for,
    recognising the same search across rows, accounts and exports.

    Args:
        query: The raw or already normalised query string.

    Returns:
        ``sha256:<hex digest>``.
    """
    normalized = _normalized(query).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{QUERY_HASH_PREFIX}:{digest}"


def query_text_audit_enabled(meta_data: Optional[Dict[str, Any]]) -> bool:
    """Whether this account asked for search text to be kept on the row.

    Args:
        meta_data: The account's ``meta_data`` document, or None.

    Returns:
        True only when the opt in is explicitly set to a true boolean.
        Anything else, including a missing document, a missing key, or a
        truthy string somebody hoped would work, is off: storing a query by
        accident is exactly the failure this setting exists to prevent.
    """
    if not isinstance(meta_data, dict):
        return False
    return meta_data.get(QUERY_TEXT_OPT_IN_KEY) is True


@dataclass(frozen=True)
class SearchActor:
    """Who ran the search, and through which surface.

    ``user_id`` is the only field the audit row has a column for. An agent
    call carries a null ``user_id`` and names itself in the details, because
    inventing a user for an agent's act would be the wrong record.
    """

    actor_type: str = ACTOR_UNKNOWN
    source: str = SOURCE_API
    user_id: Optional[UUID] = None
    managed_agent_id: Optional[str] = None
    api_key_id: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None

    def as_details(self) -> Dict[str, Any]:
        """The actor fields that go into the details document."""
        details: Dict[str, Any] = {
            "actor_type": self.actor_type,
            "source": self.source,
        }
        if self.user_id is not None:
            details["actor_user_id"] = str(self.user_id)
        if self.managed_agent_id:
            details["actor_managed_agent_id"] = str(self.managed_agent_id)
        if self.api_key_id:
            details["actor_api_key_id"] = str(self.api_key_id)
        if self.runtime_principal_id:
            details["actor_runtime_principal_id"] = str(self.runtime_principal_id)
        return details


def user_actor(
    user: Any, *, ip_address: Optional[str] = None, user_agent: Optional[str] = None
) -> SearchActor:
    """Build the actor for a console or API search made by a person."""
    return SearchActor(
        actor_type=ACTOR_USER,
        source=SOURCE_API,
        user_id=getattr(user, "id", None),
        ip_address=ip_address,
        user_agent=user_agent,
    )


def agent_actor(
    *,
    managed_agent_id: Optional[Any] = None,
    api_key_id: Optional[Any] = None,
    runtime_principal_id: Optional[str] = None,
    source: str = SOURCE_MCP,
) -> SearchActor:
    """Build the actor for a search an agent made through its tool."""
    return SearchActor(
        actor_type=ACTOR_MANAGED_AGENT,
        source=source,
        user_id=None,
        managed_agent_id=str(managed_agent_id) if managed_agent_id else None,
        api_key_id=str(api_key_id) if api_key_id else None,
        runtime_principal_id=runtime_principal_id,
    )


def _filter_value(value: Any) -> Any:
    """Serialise one filter value for the JSON details column."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def applied_filters(request: Optional[SessionSearchRequest]) -> Dict[str, Any]:
    """The filters that actually narrowed this search, JSON ready.

    Only the filters the caller set are recorded. A document listing every
    filter as null reads as "eight filters were applied" at a glance, and the
    question a reviewer has is which of them narrowed the read.
    """
    if request is None:
        return {}
    filters = request.filters.model_dump(exclude_none=True)
    return {key: _filter_value(value) for key, value in sorted(filters.items())}


def build_details(
    *,
    actor: SearchActor,
    query: str,
    mode: str,
    filters: Dict[str, Any],
    result_count: int,
    include_query_text: bool,
    effective_mode: Optional[str] = None,
    total_matched: Optional[int] = None,
    scope: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    reason: Optional[str] = None,
    error_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the details document one search writes.

    Everything here is metadata about the read: which mode ran, what narrowed
    it, how much came back, and a digest standing in for the query. No
    snippet, no title, no matched text, and no query text unless the account
    opted in.
    """
    details: Dict[str, Any] = dict(actor.as_details())
    details.update(
        {
            "mode": mode,
            "filters": filters,
            "result_count": result_count,
            "query_hash": query_hash(query),
            "query_chars": len(_normalized(query)),
            "query_text_stored": bool(include_query_text),
        }
    )
    if effective_mode is not None:
        details["effective_mode"] = effective_mode
    if total_matched is not None:
        details["total_matched"] = total_matched
    if scope is not None:
        details["scope"] = _bounded(scope, AUDIT_SCOPE_MAX_CHARS)
    if limit is not None:
        details["limit"] = limit
    if offset is not None:
        details["offset"] = offset
    if reason is not None:
        details["reason"] = reason
    if error_type is not None:
        details["error_type"] = error_type
    if include_query_text:
        details["query_text"] = _bounded(_normalized(query), MAX_QUERY_CHARS)
    return details


def record_search(
    db: Session,
    *,
    account_id: Any,
    actor: SearchActor,
    query: str,
    mode: str,
    status: str,
    result_count: int = 0,
    filters: Optional[Dict[str, Any]] = None,
    include_query_text: bool = False,
    effective_mode: Optional[str] = None,
    total_matched: Optional[int] = None,
    scope: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    reason: Optional[str] = None,
    error_type: Optional[str] = None,
    commit: bool = True,
) -> bool:
    """Write one audit row for one search, and never raise.

    Args:
        db: Request scoped session.
        account_id: Account the search read.
        actor: Who searched, and through which surface.
        query: The query as submitted; only its digest is stored unless
            ``include_query_text`` says the account opted in.
        mode: Ranking mode the caller asked for.
        status: ``success``, ``denied`` or ``failure``.
        result_count: Sessions handed back.
        filters: Filters that narrowed the search, already JSON ready.
        include_query_text: The account level opt in, resolved by the caller.
        effective_mode: Ranking mode that actually ran, when one did.
        total_matched: Sessions that matched before paging.
        scope: Scope the call asked for, for the agent tool.
        limit: Page size the search used.
        offset: Page offset the search used.
        reason: Why a denied search was denied.
        error_type: Exception class name for a failed search.
        commit: Whether to commit the row.

    Returns:
        True when the row was written, False when writing it failed. The
        caller is expected to ignore the answer: it exists so tests can
        assert the failure path was taken.
    """
    try:
        details = build_details(
            actor=actor,
            query=query,
            mode=mode,
            filters=filters or {},
            result_count=result_count,
            include_query_text=include_query_text,
            effective_mode=effective_mode,
            total_matched=total_matched,
            scope=scope,
            limit=limit,
            offset=offset,
            reason=reason,
            error_type=error_type,
        )
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=actor.user_id,
            action=AUDIT_ACTION,
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=details["query_hash"],
            status=status,
            ip_address=actor.ip_address,
            user_agent=actor.user_agent,
            details=details,
            commit=commit,
        )
        return True
    except Exception:  # noqa: BLE001 - auditing never fails the search
        # Logged, not raised, and without the query: the answer the operator
        # asked for is not worth less because the row behind it did not land,
        # and a log line is the second place that string should not appear.
        logger.warning(
            "Session search audit row could not be written for account %s",
            account_id,
            exc_info=True,
        )
        return False


def account_meta_data(db: Session, *, account_id: Any) -> Optional[Dict[str, Any]]:
    """Read the account document the opt in lives in, tolerating absence."""
    try:
        from preloop.models.crud import crud_account

        account = crud_account.get(db, id=account_id)
    except Exception:  # noqa: BLE001 - an unreadable account is not an opt in
        logger.warning(
            "Session search audit could not read account %s", account_id, exc_info=True
        )
        return None
    meta_data = getattr(account, "meta_data", None) if account is not None else None
    return meta_data if isinstance(meta_data, dict) else None


def audited_search(
    db: Session,
    *,
    account_id: Any,
    request: SessionSearchRequest,
    actor: SearchActor,
    scope: Optional[str] = None,
    run: Optional[Callable[[], SessionSearchResponse]] = None,
) -> SessionSearchResponse:
    """Run one search and record it, whatever the search does.

    A search that answers writes a ``success`` row. A search refused with a
    403 writes a ``denied`` row and the refusal still reaches the caller. A
    search that breaks writes a ``failure`` row naming the exception type and
    the exception is re-raised unchanged: auditing observes the call, it never
    edits it.

    Args:
        db: Request scoped session.
        account_id: Account the search reads, bound in SQL by the query.
        request: The validated request.
        actor: Who is searching.
        scope: Optional scope label recorded on the row.
        run: The search callable, defaulting to the search service. Injected
            so a caller that already resolved the request can reuse it and so
            tests can make the search itself fail.

    Returns:
        Whatever the search returned.
    """
    include_query_text = query_text_audit_enabled(
        account_meta_data(db, account_id=account_id)
    )
    filters = applied_filters(request)
    if run is None:
        from preloop.services import session_search

        def run_default() -> SessionSearchResponse:
            return session_search.search_sessions(
                db, account_id=account_id, request=request
            )

        run = run_default

    try:
        response = run()
    except HTTPException as exc:
        record_search(
            db,
            account_id=account_id,
            actor=actor,
            query=request.query,
            mode=request.mode,
            status=(STATUS_DENIED if exc.status_code in (401, 403) else STATUS_FAILURE),
            filters=filters,
            include_query_text=include_query_text,
            scope=scope,
            limit=request.limit,
            offset=request.offset,
            reason=f"http_{exc.status_code}",
            error_type=type(exc).__name__,
        )
        raise
    except Exception as exc:
        record_search(
            db,
            account_id=account_id,
            actor=actor,
            query=request.query,
            mode=request.mode,
            status=STATUS_FAILURE,
            filters=filters,
            include_query_text=include_query_text,
            scope=scope,
            limit=request.limit,
            offset=request.offset,
            error_type=type(exc).__name__,
        )
        raise

    record_search(
        db,
        account_id=account_id,
        actor=actor,
        query=request.query,
        mode=request.mode,
        status=STATUS_SUCCESS,
        result_count=len(response.results),
        filters=filters,
        include_query_text=include_query_text,
        effective_mode=response.effective_mode,
        total_matched=response.total,
        scope=scope,
        limit=request.limit,
        offset=request.offset,
    )
    return response


__all__ = [
    "ACTOR_MANAGED_AGENT",
    "ACTOR_UNKNOWN",
    "ACTOR_USER",
    "AUDIT_ACTION",
    "AUDIT_RESOURCE_TYPE",
    "AUDIT_SCOPE_MAX_CHARS",
    "QUERY_HASH_PREFIX",
    "QUERY_TEXT_OPT_IN_KEY",
    "SOURCE_API",
    "SOURCE_MCP",
    "STATUS_DENIED",
    "STATUS_FAILURE",
    "STATUS_SUCCESS",
    "SearchActor",
    "account_meta_data",
    "agent_actor",
    "applied_filters",
    "audited_search",
    "build_details",
    "query_hash",
    "query_text_audit_enabled",
    "record_search",
    "user_actor",
]
