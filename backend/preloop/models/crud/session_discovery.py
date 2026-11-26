"""Batched identity and family reads for choosing runtime sessions."""

from datetime import UTC, datetime
from typing import Any, Sequence

from sqlalchemy import String, case, cast, false, func, or_, select
from sqlalchemy.orm import Session

from preloop.models import models


def opening_prompt(
    session: Any = models.RuntimeSession, since: datetime | None = None
) -> Any:
    """Select the earliest captured user request without loading its transcript."""
    activity = models.RuntimeSessionActivity
    transcript = (
        select(
            case(
                (activity.timestamp >= since, func.substr(activity.summary, 1, 1000)),
                else_=None,
            )
            if since
            else func.substr(activity.summary, 1, 1000)
        )
        .where(
            activity.account_id == session.account_id,
            activity.runtime_session_id == session.id,
            activity.activity_type == "transcript_message",
            activity.metadata_["role"].astext == "user",
            activity.summary.isnot(None),
        )
        .order_by(activity.timestamp, activity.id)
        .limit(1)
        .correlate(session)
        .scalar_subquery()
    )

    document = models.SessionSearchDocument
    captured = (
        select(
            case(
                (document.occurred_at >= since, func.substr(document.content, 1, 1000)),
                else_=None,
            )
            if since
            else func.substr(document.content, 1, 1000)
        )
        .where(
            document.account_id == session.account_id,
            document.runtime_session_id == session.id,
            document.role == "user",
            document.chunk_index == 0,
            document.redaction_state.in_(("clear", "redacted")),
        )
        .order_by(document.occurred_at, document.id)
        .limit(1)
        .correlate(session)
        .scalar_subquery()
    )
    return func.coalesce(transcript, captured)


def identity_match(
    query: str, session: Any = models.RuntimeSession, since: datetime | None = None
) -> Any:
    """Match literal identity text and either session id by prefix."""
    escaped = " ".join(query.strip().split()).replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%").replace("_", "\\_")
    contains, prefix = f"%{escaped}%", f"{escaped}%"
    return or_(
        *(
            column.ilike(contains, escape="\\")
            for column in (
                session.title,
                session.alias,
                session.cwd,
                session.runtime_principal_name,
                session.session_source_type,
                opening_prompt(session, since),
            )
        ),
        cast(session.id, String).ilike(prefix, escape="\\"),
        session.session_reference.ilike(prefix, escape="\\"),
        session.session_source_id.ilike(prefix, escape="\\"),
        func.split_part(session.session_source_id, ":", 2).ilike(prefix, escape="\\"),
    )


def opening_prompts(
    db: Session,
    *,
    account_id: str,
    session_ids: Sequence[str],
    since: datetime | None = None,
) -> dict[str, str]:
    """Read one opening prompt per session in a single bounded query."""
    if not session_ids:
        return {}
    activity = models.RuntimeSessionActivity
    query = db.query(
        activity.runtime_session_id,
        func.substr(activity.summary, 1, 1000),
        activity.timestamp,
    ).filter(
        activity.account_id == account_id,
        activity.runtime_session_id.in_(session_ids),
        activity.activity_type == "transcript_message",
        activity.metadata_["role"].astext == "user",
        activity.summary.isnot(None),
    )
    rows = (
        query.distinct(activity.runtime_session_id)
        .order_by(activity.runtime_session_id, activity.timestamp, activity.id)
        .all()
    )
    seen = {str(row[0]) for row in rows}
    missing = [session_id for session_id in session_ids if session_id not in seen]
    if missing:
        document = models.SessionSearchDocument
        rows += (
            db.query(
                document.runtime_session_id,
                func.substr(document.content, 1, 1000),
                document.occurred_at,
            )
            .filter(
                document.account_id == account_id,
                document.runtime_session_id.in_(missing),
                document.role == "user",
                document.chunk_index == 0,
                document.redaction_state.in_(("clear", "redacted")),
            )
            .distinct(document.runtime_session_id)
            .order_by(
                document.runtime_session_id,
                document.occurred_at,
                document.id,
            )
            .all()
        )
    cutoff = since.replace(tzinfo=UTC) if since and since.tzinfo is None else since
    return {
        str(session_id): prompt
        for session_id, prompt, timestamp in rows
        if cutoff is None or timestamp.replace(tzinfo=UTC) >= cutoff
    }


def family_ids(
    db: Session,
    *,
    account_id: str,
    session_ids: Sequence[str],
    limit: int = 500,
    since: datetime | None = None,
) -> tuple[list[str], bool]:
    """Return ancestors, siblings and children in one account-bounded query.

    Recursive UNION removes duplicates and terminates even for malformed cycles.
    Context does not inherit a text or state filter: a matching child must still
    identify its parent. The caller applies the account's history entitlement.
    """
    if not session_ids:
        return [], False
    session = models.RuntimeSession
    ancestors = (
        select(session.id, session.parent_session_id)
        .where(session.account_id == account_id, session.id.in_(session_ids))
        .cte("session_ancestors", recursive=True)
    )
    ancestors = ancestors.union(
        select(session.id, session.parent_session_id)
        .join(ancestors, session.id == ancestors.c.parent_session_id)
        .where(session.account_id == account_id)
    )
    query = db.query(session.id).filter(
        session.account_id == account_id,
        or_(
            session.id.in_(select(ancestors.c.id)),
            session.parent_session_id.in_(select(ancestors.c.id)),
            session.parent_session_id.in_(select(ancestors.c.parent_session_id)),
        ),
    )
    if since is not None:
        query = query.filter(
            func.coalesce(session.last_activity_at, session.started_at) >= since
        )
    rows = query.order_by(session.started_at, session.id).limit(limit + 1).all()
    return [str(row.id) for row in rows[:limit]], len(rows) > limit


def reserve_initial_titles(
    db: Session,
    *,
    account_id: str,
    session_ids: Sequence[str],
) -> list[str]:
    """Atomically admit at most one automatic title attempt per session.

    The existing watermark also marks an attempted generation. A refresh of
    an unchanged or quiet session cannot spend again or replace its identity.
    """
    from sqlalchemy import update

    session = models.RuntimeSession
    ids = (
        db.execute(
            update(session)
            .where(
                session.account_id == account_id,
                session.id.in_(session_ids),
                or_(session.title.is_(None), session.title == ""),
                session.title_request_count.is_(None),
            )
            .values(title_request_count=0)
            .returning(session.id)
        )
        .scalars()
        .all()
    )
    db.commit()
    return [str(value) for value in ids]


def selection_ids(
    db: Session,
    *,
    account_id: Any,
    session_ids: Sequence[Any] | None = None,
    principals: Sequence[tuple[str, str]] | None = None,
    source_types: Sequence[str] | None = None,
    source_type: str | None = None,
    status: str | None = None,
    has_artifacts: str | None = None,
) -> Any:
    """Build the account-bounded selection subquery before content ranking."""
    from preloop.models.crud.runtime_session import crud_runtime_session
    from preloop.models.crud.runtime_session_artifact import (
        sessions_with_available_artifacts,
    )

    session = models.RuntimeSession
    query = db.query(session.id).filter(session.account_id == account_id)
    if session_ids is not None:
        query = query.filter(session.id.in_(session_ids))
    if principals is not None or source_types is not None:
        alternatives = [
            crud_runtime_session._principal_condition(kind, identifier)
            for kind, identifier in principals or ()
        ]
        if source_types:
            alternatives.append(session.session_source_type.in_(source_types))
        query = query.filter(or_(*alternatives) if alternatives else false())
    if source_type:
        query = query.filter(session.session_source_type == source_type)
    if status == "active":
        query = query.filter(session.ended_at.is_(None))
    elif status == "ended":
        query = query.filter(session.ended_at.isnot(None))
    if has_artifacts:
        holders = sessions_with_available_artifacts(
            db,
            account_id=account_id,
            kind=None if has_artifacts == "any" else has_artifacts,
        )
        query = query.filter(session.id.in_(holders))
    return query
