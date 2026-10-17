"""Read-only projections for the weekly signals service.

Each function is one SQL statement. The service in
``preloop.services.weekly_signals`` turns the rows into dataclasses and
never queries the database itself.

Windows are half-open ``[start, end)``. ``start`` and ``end`` must be
timezone-aware. Naive ``timestamp`` columns are read as UTC.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models.crud.api_usage import REPLAY_VALIDATION_PURPOSE
from preloop.models.crud.runtime_session_activity import TOOL_CALL_SUCCESS_STATUSES

# Same predicate as ``cache_covered_condition``: a reported read or a
# reported write means the provider sent a cache split.
_CACHE_COVERED_SQL = (
    "(cache_read_tokens IS NOT NULL OR cache_creation_tokens IS NOT NULL)"
)
# Non-NULL statuses outside ``TOOL_CALL_SUCCESS_STATUSES`` are failures,
# including ``refused``. NULL stays neither success nor failure.
_TOOL_SUCCESS_SQL = ", ".join(
    "'" + status.replace("'", "") + "'" for status in TOOL_CALL_SUCCESS_STATUSES
)


class ApprovalProjection(NamedTuple):
    """Account-level approval counts and the median decision lag."""

    requested: int
    granted: int
    denied: int
    expired: int
    median_decision_seconds: float
    pending: int = 0
    auto_approved: int = 0
    auto_approved_reasons: tuple[dict[str, Any], ...] = ()


class FirstSeenProjection(NamedTuple):
    """One identity or tool whose first observation falls in the window."""

    kind: str
    item_key: str
    label: str
    first_seen_at: datetime
    user_id: Optional[UUID]
    usage_count: int = 0


class LatencyProjection(NamedTuple):
    """One latency bucket: overall, one model, one error class, or one user."""

    section: str
    bucket: Optional[str]
    user_id: Optional[UUID]
    request_count: int
    error_count: int
    p50_duration: float
    p95_duration: float
    estimated_cost: float


class SessionProjection(NamedTuple):
    """Metadata aggregates for one runtime session in the window."""

    runtime_session_id: UUID
    user_id: Optional[UUID]
    request_count: int
    cache_read_tokens: int
    cache_prompt_tokens: int
    cache_creation_tokens: int
    cache_covered_requests: int
    prefix_churn: int
    retry_count: int
    max_context_tokens: int
    max_tool_call_count: int
    error_classes: dict[str, int]
    tools_attached: tuple[str, ...]
    tools_used: tuple[str, ...]
    failed_tools: dict[str, int]
    activity_tool_loop: int


def _params(account_id: UUID, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "account_id": str(account_id),
        "start": start,
        "end": end,
        "replay_purpose": REPLAY_VALIDATION_PURPOSE,
    }


def _int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def _uuid(value: Any) -> Optional[UUID]:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(count) for key, count in value.items()}


def _names(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(name) for name in value if name)


_NOT_REPLAY = """
    AND (
        meta_data IS NULL
        OR (meta_data ->> 'purpose') IS NULL
        OR (meta_data ->> 'purpose') <> :replay_purpose
    )
"""


def approval_signals(
    db: Session,
    *,
    account_id: UUID,
    start: datetime,
    end: datetime,
    user_id: UUID | None = None,
) -> ApprovalProjection:
    """Count real requests, separating policy/AI decisions from human work.

    Requests/pending use the requested-in-window cohort. Completed decisions
    use resolved-in-window rows, including requests from the prior window.
    Automated decisions never enter the human decision latency.
    """
    rows = (
        db.execute(
            text("""
        WITH requests AS (
            SELECT *, COALESCE(auto_approved_reason,
                CASE WHEN decided_by_ai THEN 'ai_decision' END) AS automatic_reason
            FROM approval_request
            WHERE account_id = CAST(:account_id AS uuid)
                AND (CAST(:user_id AS uuid) IS NULL
                    OR api_key_id IN (SELECT id FROM api_key
                        WHERE account_id = CAST(:account_id AS uuid)
                        AND user_id = CAST(:user_id AS uuid))
                    OR managed_agent_id IN (SELECT id FROM managed_agent
                        WHERE account_id = CAST(:account_id AS uuid)
                        AND owner_user_id = CAST(:user_id AS uuid))
                    OR responses @> CAST(:response AS jsonb))
        ), counts AS (
            SELECT
                COUNT(*) FILTER (WHERE automatic_reason IS NULL
                    AND requested_at >= :start AND requested_at < :end)::int AS requested,
                COUNT(*) FILTER (WHERE automatic_reason IS NULL AND status = 'approved'
                    AND resolved_at >= :start AND resolved_at < :end)::int AS granted,
                COUNT(*) FILTER (WHERE automatic_reason IS NULL AND status = 'declined'
                    AND resolved_at >= :start AND resolved_at < :end)::int AS denied,
                COUNT(*) FILTER (WHERE automatic_reason IS NULL AND status = 'expired'
                    AND COALESCE(resolved_at, expires_at) >= :start
                    AND COALESCE(resolved_at, expires_at) < :end)::int AS expired,
                COUNT(*) FILTER (WHERE automatic_reason IS NULL AND status = 'pending'
                    AND requested_at >= :start AND requested_at < :end)::int AS pending,
                COALESCE(percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM resolved_at - requested_at)
                ) FILTER (WHERE automatic_reason IS NULL
                    AND status IN ('approved', 'declined')
                    AND resolved_at >= :start AND resolved_at < :end
                    AND resolved_at >= requested_at), 0)::float8 AS median_decision_seconds
            FROM requests
        ), reasons AS (
            SELECT automatic_reason AS reason, COUNT(*)::int AS count,
                COUNT(DISTINCT managed_agent_id)::int AS agent_count
            FROM requests
            WHERE automatic_reason IS NOT NULL AND status = 'approved'
                AND COALESCE(resolved_at, requested_at) >= :start
                AND COALESCE(resolved_at, requested_at) < :end
            GROUP BY automatic_reason
        )
        SELECT counts.*, COALESCE((SELECT SUM(count) FROM reasons), 0)::int AS auto_approved,
            COALESCE((SELECT jsonb_agg(to_jsonb(reasons) ORDER BY count DESC, reason)
                FROM reasons), '[]'::jsonb) AS auto_approved_reasons
        FROM counts
    """),
            {
                **_params(
                    account_id,
                    start.astimezone(timezone.utc).replace(tzinfo=None),
                    end.astimezone(timezone.utc).replace(tzinfo=None),
                ),
                "user_id": str(user_id) if user_id else None,
                "response": json.dumps([{"user_id": str(user_id)}]),
            },
        )
        .mappings()
        .one()
    )
    return ApprovalProjection(
        requested=_int(rows["requested"]),
        granted=_int(rows["granted"]),
        denied=_int(rows["denied"]),
        expired=_int(rows["expired"]),
        median_decision_seconds=_float(rows["median_decision_seconds"]),
        pending=_int(rows["pending"]),
        auto_approved=_int(rows["auto_approved"]),
        auto_approved_reasons=tuple(rows["auto_approved_reasons"]),
    )


def first_seen_signals(
    db: Session, *, account_id: UUID, start: datetime, end: datetime
) -> list[FirstSeenProjection]:
    """List subjects, agents, CLI clients, and tools first seen in the window.

    Tool names come from ``api_usage.tools_meta``. A name is first seen when
    the earliest gateway row that carries it, across the whole account and
    not only the window, falls inside the window. Replay-validation rows are
    ignored, so a tool that exists only on those rows is not reported.

    Args:
        db: Database session.
        account_id: Account to scan.
        start: Inclusive window start, timezone-aware.
        end: Exclusive window end, timezone-aware.

    Returns:
        Rows ordered by first-seen time, kind, then key.
    """
    rows = (
        db.execute(
            text(
                f"""
                SELECT kind, item_key, label, first_seen_at, user_id, usage_count
                FROM (
                    SELECT
                        'gateway_subject'::text AS kind,
                        id::text AS item_key,
                        COALESCE(
                            NULLIF(email, ''), external_subject
                        ) AS label,
                        first_seen_at,
                        linked_user_id AS user_id,
                        (SELECT COUNT(*) FROM api_usage u WHERE u.account_id = gateway_subject.account_id AND u.meta_data ->> 'gateway_subject_id' = gateway_subject.id::text AND u.timestamp >= :start AND u.timestamp < :end AND u.action_type = 'model_gateway')::int AS usage_count
                    FROM gateway_subject
                    WHERE account_id = CAST(:account_id AS uuid)
                      AND first_seen_at >= :start
                      AND first_seen_at < :end

                    UNION ALL

                    SELECT
                        'discovered_agent'::text,
                        id::text,
                        agent_kind,
                        (first_seen_at AT TIME ZONE 'UTC'),
                        NULL::uuid,
                        (SELECT COUNT(*) FROM api_usage u
                         JOIN managed_agent m ON m.id = discovered_agent_candidate.managed_agent_id
                         AND m.account_id = discovered_agent_candidate.account_id
                         WHERE u.account_id = discovered_agent_candidate.account_id
                         AND (u.runtime_session_id = m.runtime_session_id OR
                              (u.runtime_principal_type = 'managed_agent' AND u.runtime_principal_id = m.id::text))
                         AND u.action_type = 'model_gateway'
                         AND u.timestamp >= :start AND u.timestamp < :end)::int
                    FROM discovered_agent_candidate
                    WHERE account_id = CAST(:account_id AS uuid)
                      AND (first_seen_at AT TIME ZONE 'UTC') >= :start
                      AND (first_seen_at AT TIME ZONE 'UTC') < :end

                    UNION ALL

                    SELECT
                        'cli_client'::text,
                        client_id::text,
                        cli_version,
                        first_seen,
                        NULL::uuid,
                        check_count::int
                    FROM cli_clients
                    WHERE account_id = CAST(:account_id AS uuid)
                      AND first_seen >= :start
                      AND first_seen < :end

                    UNION ALL

                    SELECT
                        'mcp_tool'::text,
                        tool_name,
                        tool_name,
                        (first_seen AT TIME ZONE 'UTC'),
                        NULL::uuid,
                        (SELECT COUNT(*) FROM runtime_session_activity a
                         WHERE a.account_id = CAST(:account_id AS uuid)
                         AND a.activity_type = 'tool_call'
                         AND a.timestamp >= :start AND a.timestamp < :end
                         AND (a.tool_name = tools.tool_name OR
                            ('mcp__' || a.server_name || '__' || a.tool_name) = tools.tool_name))::int
                    FROM (
                        SELECT
                            elem ->> 'name' AS tool_name,
                            MIN(timestamp) AS first_seen,
                            COUNT(*) FILTER (WHERE (timestamp AT TIME ZONE 'UTC') >= :start)::int AS usage_count
                        FROM api_usage
                        CROSS JOIN LATERAL jsonb_array_elements(
                            CASE
                                WHEN jsonb_typeof(meta_data -> 'tools_meta')
                                    = 'array'
                                    THEN meta_data -> 'tools_meta'
                                ELSE '[]'::jsonb
                            END
                        ) AS elem
                        WHERE account_id = CAST(:account_id AS uuid)
                          AND action_type = 'model_gateway'
                          AND COALESCE(elem ->> 'name', '') <> ''
                          AND (timestamp AT TIME ZONE 'UTC') < :end
                          {_NOT_REPLAY}
                        GROUP BY elem ->> 'name'
                    ) tools
                    WHERE (first_seen AT TIME ZONE 'UTC') >= :start
                      AND (first_seen AT TIME ZONE 'UTC') < :end
                ) seen
                ORDER BY first_seen_at, kind, item_key
                """
            ),
            _params(account_id, start, end),
        )
        .mappings()
        .all()
    )
    return [
        FirstSeenProjection(
            kind=str(row["kind"]),
            item_key=str(row["item_key"]),
            label=str(row["label"] or ""),
            first_seen_at=row["first_seen_at"],
            user_id=_uuid(row["user_id"]),
            usage_count=_int(row["usage_count"]),
        )
        for row in rows
    ]


def gateway_latency_signals(
    db: Session, *, account_id: UUID, start: datetime, end: datetime
) -> list[LatencyProjection]:
    """Error rate and duration percentiles, overall, per model, and per user.

    Percentiles are ``percentile_cont`` in SQL. Error rate is the share of
    gateway rows with ``status_code >= 400``. The top error classes are the
    three ``error_class`` values with the most such rows. Replay-validation
    rows and non-gateway rows are excluded.

    Args:
        db: Database session.
        account_id: Account to aggregate.
        start: Inclusive window start, timezone-aware.
        end: Exclusive window end, timezone-aware.

    Returns:
        One ``overall`` row (even when the window is empty), then ``model``,
        ``error``, and ``user`` rows. Error rows use ``request_count`` as the
        class count.
    """
    rows = (
        db.execute(
            text(
                f"""
                WITH gateway AS (
                    SELECT
                        COALESCE(model_alias, '') AS model_alias,
                        user_id,
                        status_code,
                        duration,
                        error_class,
                        COALESCE(estimated_cost, 0) AS estimated_cost
                    FROM api_usage
                    WHERE account_id = CAST(:account_id AS uuid)
                      AND action_type = 'model_gateway'
                      AND (timestamp AT TIME ZONE 'UTC') >= :start
                      AND (timestamp AT TIME ZONE 'UTC') < :end
                      {_NOT_REPLAY}
                ),
                overall AS (
                    SELECT
                        'overall'::text AS section,
                        NULL::text AS bucket,
                        NULL::uuid AS user_id,
                        COUNT(*)::int AS request_count,
                        COUNT(*) FILTER (
                            WHERE status_code >= 400
                        )::int AS error_count,
                        COALESCE(
                            percentile_cont(0.5) WITHIN GROUP (
                                ORDER BY duration
                            ),
                            0
                        )::float8 AS p50_duration,
                        COALESCE(
                            percentile_cont(0.95) WITHIN GROUP (
                                ORDER BY duration
                            ),
                            0
                        )::float8 AS p95_duration,
                        0::float8 AS estimated_cost
                    FROM gateway
                ),
                by_model AS (
                    SELECT
                        'model'::text,
                        model_alias,
                        NULL::uuid,
                        COUNT(*)::int,
                        COUNT(*) FILTER (WHERE status_code >= 400)::int,
                        COALESCE(
                            percentile_cont(0.5) WITHIN GROUP (
                                ORDER BY duration
                            ),
                            0
                        )::float8,
                        COALESCE(
                            percentile_cont(0.95) WITHIN GROUP (
                                ORDER BY duration
                            ),
                            0
                        )::float8,
                        0::float8
                    FROM gateway
                    GROUP BY model_alias
                ),
                top_errors AS (
                    SELECT
                        'error'::text,
                        error_class,
                        NULL::uuid,
                        n,
                        n,
                        0::float8,
                        0::float8,
                        0::float8
                    FROM (
                        SELECT error_class, COUNT(*)::int AS n
                        FROM gateway
                        WHERE status_code >= 400
                          AND COALESCE(error_class, '') <> ''
                        GROUP BY error_class
                        ORDER BY n DESC, error_class ASC
                        LIMIT 3
                    ) ranked
                ),
                by_user AS (
                    SELECT
                        'user'::text,
                        ''::text,
                        user_id,
                        COUNT(*)::int,
                        COUNT(*) FILTER (WHERE status_code >= 400)::int,
                        COALESCE(
                            percentile_cont(0.5) WITHIN GROUP (
                                ORDER BY duration
                            ),
                            0
                        )::float8,
                        COALESCE(
                            percentile_cont(0.95) WITHIN GROUP (
                                ORDER BY duration
                            ),
                            0
                        )::float8,
                        COALESCE(SUM(estimated_cost), 0)::float8
                    FROM gateway
                    GROUP BY user_id
                )
                SELECT * FROM overall
                UNION ALL
                SELECT * FROM by_model
                UNION ALL
                SELECT * FROM top_errors
                UNION ALL
                SELECT * FROM by_user
                """
            ),
            _params(account_id, start, end),
        )
        .mappings()
        .all()
    )
    return [
        LatencyProjection(
            section=str(row["section"]),
            bucket=row["bucket"] if row["bucket"] else None,
            user_id=_uuid(row["user_id"]),
            request_count=_int(row["request_count"]),
            error_count=_int(row["error_count"]),
            p50_duration=_float(row["p50_duration"]),
            p95_duration=_float(row["p95_duration"]),
            estimated_cost=_float(row["estimated_cost"]),
        )
        for row in rows
    ]


def session_efficiency_signals(
    db: Session, *, account_id: UUID, start: datetime, end: datetime
) -> list[SessionProjection]:
    """Per-session tier-1 aggregates from gateway metadata and tool activity.

    Cache totals cover only rows whose provider reported ``cache_read_tokens``.
    Prefix churn is the number of times the model alias or the attached tool
    set changes between consecutive gateway requests in the session. The tool
    loop is the longest run of consecutive ``tool_call`` activities with no
    other activity between them. ``max_tool_call_count`` is the largest
    ``tool_call_count`` on a single gateway row, for sessions that never wrote
    activity rows.

    Args:
        db: Database session.
        account_id: Account to aggregate.
        start: Inclusive window start, timezone-aware.
        end: Exclusive window end, timezone-aware.

    Returns:
        One row per runtime session that has gateway traffic in the window,
        ordered by session id. Sessions with no traffic are absent.
    """
    rows = (
        db.execute(
            text(
                f"""
                WITH rows AS (
                    SELECT
                        id,
                        runtime_session_id,
                        user_id,
                        timestamp,
                        COALESCE(model_alias, '') AS model_alias,
                        status_code,
                        NULLIF(error_class, '') AS error_class,
                        COALESCE(is_retry, false) AS is_retry,
                        COALESCE(prompt_tokens, 0) AS prompt_tokens,
                        cache_read_tokens,
                        cache_creation_tokens,
                        COALESCE(tool_call_count, 0) AS tool_call_count,
                        CASE
                            WHEN jsonb_typeof(meta_data -> 'tools_meta')
                                = 'array'
                                THEN meta_data -> 'tools_meta'
                            ELSE '[]'::jsonb
                        END AS tools_meta
                    FROM api_usage
                    WHERE account_id = CAST(:account_id AS uuid)
                      AND action_type = 'model_gateway'
                      AND runtime_session_id IS NOT NULL
                      AND (timestamp AT TIME ZONE 'UTC') >= :start
                      AND (timestamp AT TIME ZONE 'UTC') < :end
                      {_NOT_REPLAY}
                ),
                signed AS (
                    SELECT
                        r.*,
                        COALESCE((
                            SELECT string_agg(
                                DISTINCT elem ->> 'name',
                                chr(31)
                                ORDER BY elem ->> 'name'
                            )
                            FROM jsonb_array_elements(r.tools_meta) AS elem
                            WHERE COALESCE(elem ->> 'name', '') <> ''
                        ), '') AS tool_sig
                    FROM rows r
                ),
                flagged AS (
                    SELECT
                        signed.*,
                        CASE
                            WHEN row_number() OVER (
                                PARTITION BY runtime_session_id
                                ORDER BY timestamp, id
                            ) > 1
                            AND (model_alias || '|' || tool_sig) IS DISTINCT FROM
                                lag(model_alias || '|' || tool_sig) OVER (
                                    PARTITION BY runtime_session_id
                                    ORDER BY timestamp, id
                                )
                            THEN 1
                            ELSE 0
                        END AS churned
                    FROM signed
                ),
                session_stats AS (
                    SELECT
                        runtime_session_id,
                        COUNT(*)::int AS request_count,
                        COALESCE(
                            SUM(cache_read_tokens) FILTER (
                                WHERE {_CACHE_COVERED_SQL}
                            ),
                            0
                        )::int AS cache_read_tokens,
                        COALESCE(
                            SUM(prompt_tokens) FILTER (
                                WHERE {_CACHE_COVERED_SQL}
                            ),
                            0
                        )::int AS cache_prompt_tokens,
                        COALESCE(
                            SUM(cache_creation_tokens) FILTER (
                                WHERE {_CACHE_COVERED_SQL}
                            ),
                            0
                        )::int AS cache_creation_tokens,
                        COUNT(*) FILTER (
                            WHERE {_CACHE_COVERED_SQL}
                        )::int AS cache_covered_requests,
                        SUM(churned)::int AS prefix_churn,
                        COUNT(*) FILTER (WHERE is_retry)::int AS retry_count,
                        MAX(prompt_tokens)::int AS max_context_tokens,
                        MAX(tool_call_count)::int AS max_tool_call_count
                    FROM flagged
                    GROUP BY runtime_session_id
                ),
                user_pick AS (
                    SELECT DISTINCT ON (runtime_session_id)
                        runtime_session_id,
                        user_id
                    FROM (
                        SELECT
                            runtime_session_id,
                            user_id,
                            COUNT(*) AS n
                        FROM rows
                        GROUP BY runtime_session_id, user_id
                    ) counted
                    ORDER BY runtime_session_id, n DESC, user_id NULLS LAST
                ),
                error_counts AS (
                    SELECT
                        runtime_session_id,
                        jsonb_object_agg(error_class, n) AS counts
                    FROM (
                        SELECT
                            runtime_session_id,
                            error_class,
                            COUNT(*)::int AS n
                        FROM rows
                        WHERE status_code >= 400
                          AND error_class IS NOT NULL
                        GROUP BY runtime_session_id, error_class
                    ) per_class
                    GROUP BY runtime_session_id
                ),
                attached AS (
                    SELECT
                        runtime_session_id,
                        array_agg(DISTINCT name ORDER BY name) AS tools_attached
                    FROM (
                        SELECT
                            s.runtime_session_id,
                            elem ->> 'name' AS name
                        FROM signed s
                        CROSS JOIN LATERAL jsonb_array_elements(
                            s.tools_meta
                        ) AS elem
                    ) names
                    WHERE COALESCE(name, '') <> ''
                    GROUP BY runtime_session_id
                ),
                activities AS (
                    SELECT
                        a.runtime_session_id,
                        a.activity_type,
                        a.tool_name,
                        a.status,
                        a.timestamp,
                        a.id
                    FROM runtime_session_activity a
                    WHERE a.account_id = CAST(:account_id AS uuid)
                      AND a.timestamp >= :start
                      AND a.timestamp < :end
                      AND a.runtime_session_id IN (
                          SELECT DISTINCT runtime_session_id FROM rows
                      )
                ),
                marked AS (
                    SELECT
                        runtime_session_id,
                        activity_type,
                        tool_name,
                        status,
                        SUM(
                            CASE
                                WHEN activity_type = 'tool_call' THEN 0
                                ELSE 1
                            END
                        ) OVER (
                            PARTITION BY runtime_session_id
                            ORDER BY timestamp, id
                        ) AS grp
                    FROM activities
                ),
                loops AS (
                    SELECT runtime_session_id, MAX(streak)::int AS activity_tool_loop
                    FROM (
                        SELECT
                            runtime_session_id,
                            grp,
                            COUNT(*)::int AS streak
                        FROM marked
                        WHERE activity_type = 'tool_call'
                        GROUP BY runtime_session_id, grp
                    ) streaks
                    GROUP BY runtime_session_id
                ),
                used_tools AS (
                    SELECT
                        runtime_session_id,
                        array_agg(
                            DISTINCT tool_name ORDER BY tool_name
                        ) AS tools_used
                    FROM marked
                    WHERE activity_type = 'tool_call'
                      AND COALESCE(tool_name, '') <> ''
                    GROUP BY runtime_session_id
                ),
                failed AS (
                    SELECT
                        runtime_session_id,
                        jsonb_object_agg(tool_name, n) AS failed_tools
                    FROM (
                        SELECT
                            runtime_session_id,
                            tool_name,
                            COUNT(*)::int AS n
                        FROM marked
                        WHERE activity_type = 'tool_call'
                          AND status IS NOT NULL
                          AND status NOT IN ({_TOOL_SUCCESS_SQL})
                          AND COALESCE(tool_name, '') <> ''
                        GROUP BY runtime_session_id, tool_name
                    ) per_tool
                    GROUP BY runtime_session_id
                )
                SELECT
                    st.runtime_session_id,
                    up.user_id,
                    st.request_count,
                    st.cache_read_tokens,
                    st.cache_prompt_tokens,
                    st.cache_creation_tokens,
                    st.cache_covered_requests,
                    st.prefix_churn,
                    st.retry_count,
                    st.max_context_tokens,
                    st.max_tool_call_count,
                    COALESCE(ec.counts, '{{}}'::jsonb) AS error_classes,
                    COALESCE(
                        at.tools_attached, ARRAY[]::text[]
                    ) AS tools_attached,
                    COALESCE(ut.tools_used, ARRAY[]::text[]) AS tools_used,
                    COALESCE(fl.failed_tools, '{{}}'::jsonb) AS failed_tools,
                    COALESCE(lp.activity_tool_loop, 0)::int AS activity_tool_loop
                FROM session_stats st
                LEFT JOIN user_pick up
                    ON up.runtime_session_id = st.runtime_session_id
                LEFT JOIN error_counts ec
                    ON ec.runtime_session_id = st.runtime_session_id
                LEFT JOIN attached at
                    ON at.runtime_session_id = st.runtime_session_id
                LEFT JOIN used_tools ut
                    ON ut.runtime_session_id = st.runtime_session_id
                LEFT JOIN failed fl
                    ON fl.runtime_session_id = st.runtime_session_id
                LEFT JOIN loops lp
                    ON lp.runtime_session_id = st.runtime_session_id
                ORDER BY st.runtime_session_id
                """
            ),
            _params(account_id, start, end),
        )
        .mappings()
        .all()
    )
    projections: list[SessionProjection] = []
    for row in rows:
        session_id = _uuid(row["runtime_session_id"])
        if session_id is None:
            continue
        projections.append(
            SessionProjection(
                runtime_session_id=session_id,
                user_id=_uuid(row["user_id"]),
                request_count=_int(row["request_count"]),
                cache_read_tokens=_int(row["cache_read_tokens"]),
                cache_prompt_tokens=_int(row["cache_prompt_tokens"]),
                cache_creation_tokens=_int(row["cache_creation_tokens"]),
                cache_covered_requests=_int(row["cache_covered_requests"]),
                prefix_churn=_int(row["prefix_churn"]),
                retry_count=_int(row["retry_count"]),
                max_context_tokens=_int(row["max_context_tokens"]),
                max_tool_call_count=_int(row["max_tool_call_count"]),
                error_classes=_mapping(row["error_classes"]),
                tools_attached=_names(row["tools_attached"]),
                tools_used=_names(row["tools_used"]),
                failed_tools=_mapping(row["failed_tools"]),
                activity_tool_loop=_int(row["activity_tool_loop"]),
            )
        )
    return projections


__all__ = [
    "ApprovalProjection",
    "FirstSeenProjection",
    "LatencyProjection",
    "SessionProjection",
    "approval_signals",
    "first_seen_signals",
    "gateway_latency_signals",
    "session_efficiency_signals",
]
