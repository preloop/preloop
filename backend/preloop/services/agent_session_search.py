"""The session corpus as a tool: an agent asking what its own sessions did.

The console search answers a human's question. This module answers the same
question for the agent, which is the point of keeping the corpus at all: an
agent that can ask "did I already run this migration" does not run it twice,
and one that can ask "what did I ask this user last week" does not ask again.

Two things are deliberate here.

The default scope is the calling agent's own sessions. An agent reading its
own history is obviously safe; an agent reading everything the account ever
did is a policy decision, so it is a grant an operator makes rather than a
default an agent inherits. :func:`account_scope_granted` is the single place
that decision is read, so widening it is a configuration change and not a code
change, and a call that asks for the account scope without the grant is
refused by name instead of being narrowed behind the agent's back. A silently
narrowed search is worse than a refused one: an empty answer then reads as
"nobody ever did this" when it means "not you".

The answer is compact and capped. A search that returns a page of transcript
spends the context the agent was trying to save, so one snippet per session,
a short one, and a hard ceiling on the whole document. What does not fit is
reported as a count, not dropped quietly.

Nothing here filters by account: the account bound is applied in SQL by the
CRUD query the search service calls, exactly as it is for the endpoint.

Every call is audited, answered or refused, with the agent as the actor and
``source="mcp"`` on the row (#688), so an operator reading the trail can tell
an agent's grep over the transcripts from a person's.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account
from preloop.models.models.session_search_document import (
    REDACTION_STATE_METADATA_ONLY,
)
from preloop.schemas.session_search import (
    SessionSearchRequest,
    SessionSearchResponse,
    SessionSearchResult,
)
from preloop.services import session_search_audit
from preloop.services.subject_governance import (
    get_account_governance_defaults,
    get_subject_governance,
    subject_scope_chain,
)
from preloop.tools.builtin_defs import (
    SEARCH_SESSIONS_DEFAULT_LIMIT,
    SEARCH_SESSIONS_MAX_LIMIT,
    SEARCH_SESSIONS_SCOPE_ACCOUNT,
    SEARCH_SESSIONS_SCOPE_OWN,
    SEARCH_SESSIONS_SCOPES,
)

#: Tool name, shared with the catalogue entry and the MCP registration.
SEARCH_SESSIONS_TOOL_NAME = "search_sessions"

#: The grant that widens a search past the caller's own sessions. It is read
#: from the account's governance store (see :func:`account_scope_granted`) and
#: nothing in this repository writes it yet: who may hand it out, and through
#: which surface, is the open decision on issue #624. Until that is decided
#: the read path is the whole mechanism, which is the point: when the grant
#: exists it is configuration, not a release.
ACCOUNT_SCOPE_GRANT = "session_search.account_scope"

#: Key the governance store holds tool grants under, beside the enable
#: overrides and the tool rules a subject already carries.
#:
#: Whoever implements the write half has one thing to remember:
#: ``subject_governance.sanitize_subject_governance_config`` keeps only the
#: fields it names, so this key has to be added there or a saved grant is
#: dropped on the next governance write. It is deliberately not added yet,
#: because a store that can carry a grant is already half of the decision
#: #624 has to make.
TOOL_GRANTS_KEY = "tool_grants"

#: Refusal codes. An agent can branch on these; the detail beside them is for
#: the model to read, and says what to do instead.
REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED = "account_scope_not_granted"
REFUSAL_NO_AGENT_IDENTITY = "no_agent_identity"
REFUSAL_INVALID_REQUEST = "invalid_request"
REFUSAL_UNKNOWN_SCOPE = "unknown_scope"

#: Longest snippet returned per session. ``ts_headline`` already returns a
#: fragment rather than a whole chunk; trimming the fragment as well is what
#: makes the size of one result predictable.
MAX_SNIPPET_CHARS = 280

#: Ceiling on the serialised response, in characters. Roughly a thousand
#: tokens: enough for a handful of sessions with their snippets, small enough
#: that a search never costs more context than the work it saves. A result
#: that does not fit is counted in ``results_omitted``, never silently lost.
MAX_RESPONSE_CHARS = 4000

#: Snippets asked of the search service per session. One: the best fragment
#: of the best chunk is what tells an agent whether to open the session, and
#: the second one rarely changes that answer but always costs context.
SNIPPETS_PER_SESSION = 1


def account_scope_granted(
    meta_data: Optional[Dict[str, Any]],
    *,
    subject_context: Dict[str, Optional[str]],
) -> bool:
    """Whether this caller may search the whole account, not only itself.

    The grant is read from the same account governance store the tool enable
    overrides live in, walking the subject scope chain most specific first
    (the api key, then the managed agent), and falling back to the account
    defaults bucket. The most specific statement wins, and an explicit
    ``false`` stops the walk, so revoking the grant for one agent is not
    undone by an account default.

    Args:
        meta_data: The account's ``meta_data`` document, or None.
        subject_context: The calling identity, as ``api_key_id`` and
            ``managed_agent_id``.

    Returns:
        True only when a grant says so. Absence is refusal: this is the
        conservative half of the scope decision and it fails closed.
    """
    for subject_type, subject_id in subject_scope_chain(subject_context):
        config = get_subject_governance(
            meta_data, subject_type=subject_type, subject_id=subject_id
        )
        granted = _grant_value(config)
        if granted is not None:
            return granted
    granted = _grant_value(get_account_governance_defaults(meta_data))
    return bool(granted)


def _grant_value(config: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Read the account scope grant out of one governance config."""
    if not isinstance(config, dict):
        return None
    grants = config.get(TOOL_GRANTS_KEY)
    if not isinstance(grants, dict):
        return None
    value = grants.get(ACCOUNT_SCOPE_GRANT)
    return value if isinstance(value, bool) else None


def _refusal(reason: str, detail: str, *, scope: str) -> Dict[str, Any]:
    """Build the one refusal shape the tool ever returns.

    It carries an empty ``results`` list on purpose: a caller that reads
    results first sees nothing found, and a caller that reads ``refused``
    first learns why, and neither reading is wrong.
    """
    return {
        "refused": True,
        "reason": reason,
        "detail": detail,
        "scope": scope,
        "results": [],
    }


def _audit_refusal(
    db: Session,
    *,
    account_id: Any,
    actor: session_search_audit.SearchActor,
    query: str,
    mode: Optional[str],
    scope: str,
    reason: str,
    filters: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a refused search, which is the row a reviewer wants most.

    A refusal is an attempted read of the corpus: the agent asked for
    something it was not allowed to have, and that is worth more to whoever
    reviews the trail than most of the searches that succeeded. The status is
    ``denied`` for every refusal, and the reason names which rule declined it.
    """
    session_search_audit.record_search(
        db,
        account_id=account_id,
        actor=actor,
        query=query,
        mode=mode or "keyword",
        status=session_search_audit.STATUS_DENIED,
        filters=filters or {},
        include_query_text=session_search_audit.query_text_audit_enabled(
            session_search_audit.account_meta_data(db, account_id=account_id)
        ),
        scope=scope,
        reason=reason,
    )


def _match_reason(result: SessionSearchResult) -> str:
    """Say why this session is in the answer, in one short phrase."""
    kinds: List[str] = []
    for snippet in result.snippets:
        if snippet.source_kind not in kinds:
            kinds.append(snippet.source_kind)
    count = result.matched_chunk_count
    turns = "turn" if count == 1 else "turns"
    reason = f"matched in {count} {turns}"
    if kinds:
        reason += " (" + ", ".join(kinds) + ")"
    withheld = [
        snippet
        for snippet in result.snippets
        if snippet.text is None
        and snippet.redaction_state != REDACTION_STATE_METADATA_ONLY
    ]
    if withheld:
        reason += "; snippet withheld by redaction"
    return reason


def _snippet_text(result: SessionSearchResult) -> Optional[str]:
    """Return the one snippet this session is represented by, trimmed."""
    for snippet in result.snippets:
        if snippet.text:
            text = " ".join(snippet.text.split())
            if len(text) > MAX_SNIPPET_CHARS:
                text = text[: MAX_SNIPPET_CHARS - 1].rstrip() + "…"
            return text
    return None


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    """Serialise an instant, or None."""
    return value.isoformat() if value is not None else None


def _compact_result(result: SessionSearchResult) -> Dict[str, Any]:
    """One session as the agent meets it: where, when, what, why."""
    return {
        "runtime_session_id": str(result.runtime_session_id),
        "session_reference": result.session_reference or result.session_source_id,
        "occurred_at": _isoformat(result.last_match_at or result.started_at),
        "snippet": _snippet_text(result),
        "match_reason": _match_reason(result),
    }


def _capped_results(
    results: List[SessionSearchResult],
) -> tuple[List[Dict[str, Any]], int]:
    """Fit as many results as the size cap allows, and count the rest.

    The first result is always kept: an answer of "there is a match but you
    may not see any of it" helps nobody, and one result is bounded by the
    snippet cap anyway.
    """
    kept: List[Dict[str, Any]] = []
    for index, result in enumerate(results):
        candidate = kept + [_compact_result(result)]
        if kept and len(json.dumps(candidate, default=str)) > MAX_RESPONSE_CHARS:
            return kept, len(results) - index
        kept = candidate
    return kept, 0


def search_for_agent(
    db: Session,
    *,
    account_id: Any,
    runtime_principal_id: Optional[str],
    subject_context: Dict[str, Optional[str]],
    query: str,
    scope: Optional[str] = None,
    mode: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one scoped, capped search on behalf of a calling agent.

    Args:
        db: Request scoped session.
        account_id: The caller's account, bound in SQL by the search query.
        runtime_principal_id: The calling agent's runtime principal, taken
            from the authenticated context and never from an argument: an
            agent chooses what to search, never whose sessions.
        subject_context: The calling identity the account scope grant is
            resolved against (``api_key_id``, ``managed_agent_id``).
        query: Search text, in ``websearch_to_tsquery`` syntax.
        scope: ``own`` (default) or ``account``.
        mode: Requested ranking mode; a mode the deployment cannot serve is
            answered with keyword results and a degraded marker.
        start_date: Optional lower bound, ISO 8601 with an offset.
        end_date: Optional upper bound, ISO 8601 with an offset.
        limit: Sessions to return, clamped to the documented maximum.

    Returns:
        The compact answer, or a refusal record naming the rule that declined
        it. Nothing here raises for a bad request: a model corrects a refusal
        on its next turn, and cannot correct a stack trace.
    """
    actor = session_search_audit.agent_actor(
        managed_agent_id=subject_context.get("managed_agent_id"),
        api_key_id=subject_context.get("api_key_id"),
        runtime_principal_id=runtime_principal_id,
        source=session_search_audit.SOURCE_MCP,
    )
    requested_scope = (scope or SEARCH_SESSIONS_SCOPE_OWN).strip().lower()
    if requested_scope not in SEARCH_SESSIONS_SCOPES:
        _audit_refusal(
            db,
            account_id=account_id,
            actor=actor,
            query=query,
            mode=mode,
            scope=requested_scope,
            reason=REFUSAL_UNKNOWN_SCOPE,
        )
        return _refusal(
            REFUSAL_UNKNOWN_SCOPE,
            "scope must be one of: " + ", ".join(SEARCH_SESSIONS_SCOPES) + ".",
            scope=requested_scope,
        )

    if requested_scope == SEARCH_SESSIONS_SCOPE_ACCOUNT:
        account = crud_account.get(db, id=account_id)
        meta_data = getattr(account, "meta_data", None) if account else None
        if not account_scope_granted(meta_data, subject_context=subject_context):
            _audit_refusal(
                db,
                account_id=account_id,
                actor=actor,
                query=query,
                mode=mode,
                scope=requested_scope,
                reason=REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED,
            )
            return _refusal(
                REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED,
                "Searching every session of the account needs the "
                f"'{ACCOUNT_SCOPE_GRANT}' grant, which an operator has not "
                "given this agent. Your own sessions are searchable now: "
                "repeat the call with scope 'own', and ask an operator for "
                "the grant if the answer has to come from another agent's "
                "work.",
                scope=requested_scope,
            )

    principal = (runtime_principal_id or "").strip()
    if requested_scope == SEARCH_SESSIONS_SCOPE_OWN and not principal:
        _audit_refusal(
            db,
            account_id=account_id,
            actor=actor,
            query=query,
            mode=mode,
            scope=requested_scope,
            reason=REFUSAL_NO_AGENT_IDENTITY,
        )
        return _refusal(
            REFUSAL_NO_AGENT_IDENTITY,
            "This session carries no agent identity, so there is no set of "
            "'your own' sessions to search. A call made by an agent has "
            "one; a call made outside an agent runtime does not.",
            scope=requested_scope,
        )

    filters: Dict[str, Any] = {}
    if start_date is not None:
        filters["start_date"] = start_date
    if end_date is not None:
        filters["end_date"] = end_date
    if requested_scope == SEARCH_SESSIONS_SCOPE_OWN:
        filters["runtime_principal_id"] = principal

    payload: Dict[str, Any] = {
        "query": query,
        "filters": filters,
        "limit": _clamped_limit(limit),
        "max_snippets_per_session": SNIPPETS_PER_SESSION,
        "include_snippet_text": True,
    }
    if mode is not None:
        payload["mode"] = mode

    try:
        request = SessionSearchRequest.model_validate(payload)
    except ValidationError as exc:
        _audit_refusal(
            db,
            account_id=account_id,
            actor=actor,
            query=query,
            mode=mode,
            scope=requested_scope,
            reason=REFUSAL_INVALID_REQUEST,
        )
        return _refusal(
            REFUSAL_INVALID_REQUEST,
            _validation_detail(exc),
            scope=requested_scope,
        )

    response = session_search_audit.audited_search(
        db,
        account_id=account_id,
        request=request,
        actor=actor,
        scope=requested_scope,
    )
    return _to_tool_response(response, scope=requested_scope)


def _clamped_limit(limit: Optional[int]) -> int:
    """Clamp the requested page size into the documented range.

    A limit above the maximum is clamped rather than refused: the caller is
    asking for more of the same answer, and the size cap would have trimmed
    it anyway. A limit that is not a number at all falls through to the
    request model, which refuses it.
    """
    if limit is None:
        return SEARCH_SESSIONS_DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return SEARCH_SESSIONS_DEFAULT_LIMIT
    return max(1, min(value, SEARCH_SESSIONS_MAX_LIMIT))


def _validation_detail(exc: ValidationError) -> str:
    """Turn a validation error into one line a model can act on."""
    parts: List[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "")
        message = error.get("msg", "invalid value")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts) or "The request was not valid."


def _to_tool_response(response: SessionSearchResponse, *, scope: str) -> Dict[str, Any]:
    """Shrink one endpoint response into the agent facing answer.

    The degraded block is passed through rather than summarised: an agent
    told that semantic ranking did not run knows that a miss is a keyword
    miss, which is the difference between "this was never done" and "this was
    not phrased the way I searched for it".
    """
    results, omitted = _capped_results(response.results)
    return {
        "query": response.query,
        "scope": scope,
        "mode": response.mode,
        "effective_mode": response.effective_mode,
        "degraded": response.degraded.model_dump(),
        "indexed_through": _isoformat(response.indexed_through),
        "total": response.total,
        "returned": len(results),
        "truncated": bool(omitted),
        "results_omitted": omitted,
        "results": results,
    }


__all__ = [
    "ACCOUNT_SCOPE_GRANT",
    "MAX_RESPONSE_CHARS",
    "MAX_SNIPPET_CHARS",
    "REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED",
    "REFUSAL_INVALID_REQUEST",
    "REFUSAL_NO_AGENT_IDENTITY",
    "REFUSAL_UNKNOWN_SCOPE",
    "SEARCH_SESSIONS_TOOL_NAME",
    "SNIPPETS_PER_SESSION",
    "account_scope_granted",
    "search_for_agent",
]
