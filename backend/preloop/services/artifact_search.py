"""Account-wide artifact search (#1086), shared by the API and agent tools.

Filters are applied in the query on ``runtime_session_artifact`` (the
``labels`` GIN index and ``(account_id, kind, created_at)``); ``q`` matches
the artifact's search chunks from #1082 or its name. Excerpts come from the
guarded chunk reader, so withheld text is never returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_session_search_document
from preloop.models.crud import runtime_session_artifact as crud_artifact
from preloop.models.crud.session_search_document import (
    EXCERPT_START,
    EXCERPT_STOP,
)
from preloop.schemas.runtime_session_artifact import (
    ArtifactExcerpt,
    ArtifactSearchFacets,
    ArtifactSearchItem,
    ArtifactSearchOut,
)
from preloop.services.artifact_deposit import (
    ERROR_KIND_INVALID,
    LIST_LIMIT_DEFAULT,
    LIST_LIMIT_MAX,
    ArtifactDepositError,
    decode_cursor,
    describe,
    encode_cursor,
    parse_label_filters,
)
from preloop.services.artifact_media import ARTIFACT_KINDS

AVAILABILITIES: frozenset[str] = frozenset({"available", "evicted", "expired"})
Q_MAX_CHARS = 500
ERROR_AVAILABILITY_INVALID = "artifact_availability_invalid"
ERROR_QUERY_TOO_LONG = "artifact_query_too_long"
ERROR_DATE_RANGE_INVALID = "artifact_date_range_invalid"
ERROR_AGENT_ID_INVALID = "artifact_agent_id_invalid"
ERROR_SESSION_ID_INVALID = "artifact_runtime_session_id_invalid"


def split_highlights(headline: str) -> ArtifactExcerpt:
    """Turn a marked headline into plain text plus ``[start, end)`` offsets."""
    text: list[str] = []
    spans: list[tuple[int, int]] = []
    length = 0
    start: int | None = None
    for char in headline:
        if char == EXCERPT_START:
            start = length
        elif char == EXCERPT_STOP:
            if start is not None and length > start:
                spans.append((start, length))
            start = None
        else:
            text.append(char)
            length += 1
    return ArtifactExcerpt(text="".join(text), highlights=spans)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _uuid(value: str | None, code: str) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise ArtifactDepositError(422, code) from None


def search(
    db: Session,
    *,
    account_id: Any,
    q: str | None = None,
    kinds: Iterable[str] = (),
    labels: Iterable[str] = (),
    agent_id: str | None = None,
    tool_name: str | None = None,
    producer: str | None = None,
    runtime_session_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    held: bool | None = None,
    availability: str | None = None,
    limit: int = LIST_LIMIT_DEFAULT,
    cursor: str | None = None,
    session_cutoff: datetime | None = None,
) -> ArtifactSearchOut:
    """Search an account's artifacts across sessions.

    Raises:
        ArtifactDepositError: 422 with a stable code for a bad kind, label
            filter, id, availability, date range, limit or cursor.
    """
    if not 1 <= limit <= LIST_LIMIT_MAX:
        raise ArtifactDepositError(422, "artifact_limit_invalid")
    kind_list = sorted(set(kinds))
    if any(kind not in ARTIFACT_KINDS for kind in kind_list):
        raise ArtifactDepositError(422, ERROR_KIND_INVALID)
    if availability is not None and availability not in AVAILABILITIES:
        raise ArtifactDepositError(422, ERROR_AVAILABILITY_INVALID)
    query = " ".join(q.split()) if q else None
    if query and len(query) > Q_MAX_CHARS:
        raise ArtifactDepositError(422, ERROR_QUERY_TOO_LONG)
    created_from, created_to = _aware(created_from), _aware(created_to)
    if created_from and created_to and created_from >= created_to:
        raise ArtifactDepositError(422, ERROR_DATE_RANGE_INVALID)
    try:
        label_terms = [parse_label_filters([value]) for value in labels]
        before = decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise ArtifactDepositError(422, str(exc)) from None
    filters: dict[str, Any] = {
        "query": query,
        "kinds": kind_list or None,
        "labels": label_terms or None,
        "agent_id": _uuid(agent_id, ERROR_AGENT_ID_INVALID),
        "tool_name": tool_name or None,
        "producer": producer or None,
        "runtime_session_id": _uuid(runtime_session_id, ERROR_SESSION_ID_INVALID),
        "created_from": created_from,
        "created_to": created_to,
        "held": held,
        "availability": availability,
        "session_cutoff": session_cutoff,
    }
    rows = crud_artifact.search_account(
        db, account_id=account_id, limit=limit + 1, before=before, **filters
    )
    page = rows[:limit]
    excerpts = (
        crud_session_search_document.artifact_excerpts(
            db,
            account_id=account_id,
            artifact_ids=[artifact.id for artifact, _t, _a in page],
            query=query,
        )
        if query
        else {}
    )
    items = []
    for artifact, session_title, agent_name in page:
        hit = excerpts.get(str(artifact.id))
        items.append(
            ArtifactSearchItem(
                **describe(artifact).model_dump(),
                session_title=session_title,
                agent_name=agent_name,
                excerpt=split_highlights(hit[0]) if hit else None,
                cue_start=hit[1] if hit and artifact.kind == "transcript" else None,
            )
        )
    by_kind, by_site, truncated = crud_artifact.search_account_facets(
        db, account_id=account_id, **filters
    )
    return ArtifactSearchOut(
        items=items,
        next_cursor=encode_cursor(page[-1][0]) if len(rows) > limit else None,
        facets=ArtifactSearchFacets(kind=by_kind, site=by_site),
        facets_truncated=truncated,
    )
