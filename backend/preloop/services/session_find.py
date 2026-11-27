"""Session selection combines cheap identity reads with ranked content search."""

from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from preloop.models import models
from preloop.schemas.gateway_usage import AccountRuntimeSessionListResponse
from preloop.schemas.session_find import SessionFindRequest
from preloop.schemas.session_search import SessionSearchFilters, SessionSearchRequest
from preloop.services import session_search_audit
from preloop.services.runtime_session_explorer import RuntimeSessionExplorerService


def find_sessions(
    db: Session,
    *,
    account: models.Account,
    payload: SessionFindRequest,
    actor: Any,
) -> AccountRuntimeSessionListResponse:
    """Return matching rows plus bounded family context in one HTTP response.

    Empty queries only read the list. Nonempty queries use the existing audited
    relevance service. Its results are filtered through the same account, agent,
    history, state and source restrictions as identity matches before hydration.
    """
    explorer = RuntimeSessionExplorerService(db)
    query = payload.query.strip()
    filters = payload.model_dump(exclude={"query", "offset", "limit", "session_ids"})
    try:
        with db.begin_nested():
            page = explorer.list_account_sessions(
                account=account,
                query=query or None,
                session_ids=[str(value) for value in payload.session_ids]
                if payload.session_ids is not None
                else None,
                **filters,
                offset=payload.offset,
                limit=payload.limit,
                include_related=True,
            )
    except SQLAlchemyError:
        if not query:
            raise
        # A failed identity predicate must not suppress a healthy content index.
        page = explorer.list_account_sessions(
            account=account,
            session_ids=[],
            **filters,
            offset=payload.offset,
            limit=payload.limit,
        )
        page.search_warning = "Identity search is unavailable. Showing content matches."
    page.next_offset = payload.offset + payload.limit if page.has_more else None
    if not query:
        return page
    if query:
        filters["start_date"] = page.period_start
        filters["end_date"] = page.period_end
        principals, source_types = explorer._resolve_agent_filters(
            account=account, agent=payload.agent, agent_kind=payload.agent_kind
        )
        try:
            with db.begin_nested():
                matches = session_search_audit.audited_search(
                    db,
                    account_id=account.id,
                    actor=actor,
                    request=SessionSearchRequest(
                        query=query,
                        mode="keyword",
                        limit=payload.limit,
                        offset=payload.offset,
                        max_snippets_per_session=3,
                        filters=SessionSearchFilters(
                            start_date=page.period_start,
                            end_date=page.period_end,
                            session_ids=payload.session_ids,
                            session_principals=principals,
                            session_source_types=source_types,
                            session_source_type=payload.session_source_type,
                            session_status=payload.status,
                            session_has_artifacts=payload.has_artifacts,
                            session_workspace_id=payload.workspace_id,
                        ),
                    ),
                )
        except SQLAlchemyError:
            page.search_warning = (
                "Search is unavailable. Try again."
                if page.search_warning
                else "Content search is unavailable. Showing identity matches."
            )
            return page
        scope_ids = (
            {str(value) for value in payload.session_ids}
            if payload.session_ids is not None
            else None
        )
        match_ids = [
            str(result.runtime_session_id)
            for result in matches.results
            if scope_ids is None or str(result.runtime_session_id) in scope_ids
        ]
        if match_ids:
            content = explorer.list_account_sessions(
                account=account,
                **filters,
                session_ids=match_ids,
                limit=100,
            )
            by_id = {item.id: item for item in content.items}
            seen = {item.id for item in page.items}
            page.items.extend(
                by_id[sid] for sid in match_ids if sid in by_id and sid not in seen
            )
            page.matching_snippets = {
                str(result.runtime_session_id): result.snippets[0].text or ""
                for result in matches.results
                if result.snippets and str(result.runtime_session_id) in by_id
            }
        allowed = {item.id for item in page.items}
        visible_results = [
            result
            for result in matches.results
            if str(result.runtime_session_id) in allowed
        ]
        page.content_search = {
            **matches.model_dump(mode="json"),
            "results": [result.model_dump(mode="json") for result in visible_results],
        }
        page.matching_turns = {
            str(result.runtime_session_id): str(result.snippets[0].source_id)
            for result in visible_results
            if result.snippets
            and result.snippets[0].source_kind
            in {
                "transcript_message",
                "gateway_interaction",
                "tool_call",
                "operator_note",
            }
        }
        page.has_more = page.has_more or matches.total > payload.offset + len(
            matches.results
        )
        page.total = None if page.has_more else len(page.items)
    page.next_offset = payload.offset + payload.limit if page.has_more else None
    page.related_sessions, page.related_truncated = explorer.session_family(
        account=account,
        items=page.items,
        start_date=page.period_start,
        end_date=page.period_end,
    )
    return page
