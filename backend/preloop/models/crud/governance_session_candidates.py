"""Bounded, account-scoped Stage A candidate reads."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models import governance_review as gov
from preloop.models.gateway_usage import AGENT_GATEWAY_PREDICATE


def list_session_digest_candidates(
    db: Session,
    *,
    account_id: UUID,
    after_id: UUID | None = None,
    limit: int = 200,
) -> list[UUID]:
    """Return a keyset page with real traffic beyond the last digest.

    Counts preserve equal-timestamp arrivals; the timestamp comparison also
    handles retained histories whose cumulative count has decreased. Only IDs
    cross the CRUD boundary, so unchanged sessions never load their histories.
    Keyset pagination remains stable while workers append digest segments.
    """
    runtime = models.RuntimeSession
    usage = models.ApiUsage
    digest = models.GovernanceSessionDigest
    traffic = (
        usage.account_id == account_id,
        usage.runtime_session_id == runtime.id,
        text(AGENT_GATEWAY_PREDICATE),
    )
    count = select(func.count()).where(*traffic).correlate(runtime).scalar_subquery()
    segment = (
        select(digest.request_watermark)
        .where(digest.account_id == account_id, digest.runtime_session_id == runtime.id)
        .order_by(digest.segment_index.desc())
        .limit(1)
        .correlate(runtime)
        .scalar_subquery()
    )
    end = (
        select(digest.window_end)
        .where(digest.account_id == account_id, digest.runtime_session_id == runtime.id)
        .order_by(digest.segment_index.desc())
        .limit(1)
        .correlate(runtime)
        .scalar_subquery()
    )
    latest = (
        select(usage.timestamp)
        .where(*traffic)
        .order_by(usage.timestamp.desc())
        .limit(1)
        .correlate(runtime)
        .scalar_subquery()
    )
    statement = select(runtime.id).where(
        runtime.account_id == account_id,
        or_(count > func.coalesce(segment, 0), latest > end),
    )
    if after_id is not None:
        statement = statement.where(runtime.id > after_id)
    return list(
        db.scalars(statement.order_by(runtime.id).limit(min(max(limit, 1), 200))).all()
    )


def list_session_digest_retries(
    db: Session,
    *,
    account_id: UUID,
    stale_before: datetime,
    runtime_session_id: UUID | None = None,
    limit: int = 50,
) -> list[UUID]:
    """Return only the oldest failed or abandoned summaries for an account.

    Filter and limit in SQL using the partial retry queue index, so completed
    history is excluded from both the returned rows and the indexed scan. Stale claims use the typed update timestamp rather
    than parsing optional JSON diagnostics. No traffic watermark is required.
    """
    digest = models.GovernanceSessionDigest
    status = digest.digest["summary_status"].astext
    columns = (
        digest.id,
        digest.window_end,
        digest.runtime_session_id,
        digest.segment_index,
    )
    statement = select(*columns).where(
        digest.account_id == account_id,
        text(gov.SESSION_DIGEST_RETRY_PREDICATE),
        or_(
            status.in_(("budget_limited", "provider_failed")),
            and_(status == "summarizing", digest.updated_at <= stale_before),
        ),
    )
    parse_failures = select(*columns).where(
        digest.account_id == account_id,
        text(gov.SESSION_DIGEST_PARSE_RETRY_PREDICATE),
    )
    incomplete_captures = select(*columns).where(
        digest.account_id == account_id,
        text(gov.SESSION_DIGEST_CAPTURE_RETRY_PREDICATE),
        digest.updated_at <= stale_before,
    )
    if runtime_session_id is not None:
        statement = statement.where(digest.runtime_session_id == runtime_session_id)
        parse_failures = parse_failures.where(
            digest.runtime_session_id == runtime_session_id
        )
        incomplete_captures = incomplete_captures.where(
            digest.runtime_session_id == runtime_session_id
        )
    # Each branch has its own partial index. An OR would defeat the existing
    # queue predicate and scan completed history on every sweep.
    candidates = statement.union_all(parse_failures, incomplete_captures).subquery()
    return list(
        db.scalars(
            select(candidates.c.id)
            .order_by(
                candidates.c.window_end,
                candidates.c.runtime_session_id,
                candidates.c.segment_index,
                candidates.c.id,
            )
            .limit(min(max(limit, 1), 50))
        ).all()
    )
