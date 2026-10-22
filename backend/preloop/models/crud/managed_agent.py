"""CRUD operations for durable managed-agent registry entries."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Float,
    String,
    and_,
    case,
    cast,
    column,
    func,
    or_,
    select,
    true,
    tuple_,
    values,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from preloop.utils.agent_kind import normalize_agent_kind

from preloop.models import models
from .api_usage import (
    EMPTY_CACHE_SPLIT,
    cache_split_columns,
    cache_split_from_row,
    gateway_action_condition,
)
from sqlalchemy import literal_column
from .base import CRUDBase

ApiUsage = models.ApiUsage
ManagedAgent = models.ManagedAgent
RuntimeSession = models.RuntimeSession
User = models.User

MANAGED_AGENT_ACTIVE_WINDOW = timedelta(minutes=10)
MANAGED_AGENT_RECENT_WINDOW = timedelta(hours=24)

# The only lifecycle states the system understands. ``get_by_source`` ranks
# these explicitly and the auth paths branch on them, so writing anything else
# would silently change how an agent resolves and authenticates.
MANAGED_AGENT_LIFECYCLE_STATES = frozenset({"active", "suspended", "decommissioned"})


def normalize_managed_agent_kind(
    session_source_type: Optional[str], *, agent_kind: Optional[str] = None
) -> str:
    """Normalize one durable agent kind for a managed agent.

    ``agent_kind`` records *which product* the agent is (``cursor``), while
    ``session_source_type`` records *how it connects* (``desktop_agent``). For
    most agents the two coincide, but several products (Cursor, Windsurf, VS
    Code, Antigravity, Devin) share the generic ``desktop_agent`` transport, so
    an explicit kind wins when supplied.

    The two are deliberately decoupled: the source type is part of the v2
    principal-id fingerprint, so changing it for existing agents would
    invalidate their identity. See ``#123``.

    Args:
        session_source_type: Transport-level source type for the agent.
        agent_kind: Optional explicit product kind supplied by the caller.

    Returns:
        The normalized durable agent kind.
    """
    explicit = normalize_agent_kind(agent_kind)
    if explicit:
        return explicit
    return normalize_agent_kind(session_source_type) or "external_agent"


def should_refine_agent_kind(
    stored_kind: Optional[str],
    *,
    session_source_type: Optional[str],
    agent_kind: Optional[str],
) -> bool:
    """Decide whether a re-enrollment may overwrite the stored agent kind.

    Refine the kind, never regress it. A client that supplies an explicit
    kind always wins: it knows which product it is. A client that supplies
    none (an older CLI, or one that cannot tell Cursor from Windsurf) may
    only fill in a kind that is still empty or still the generic transport
    value, because otherwise every re-enrollment from that client would
    reset a known ``cursor`` back to ``desktop_agent``.

    Args:
        stored_kind: Kind currently recorded on the agent row.
        session_source_type: Transport-level source type for this enrollment.
        agent_kind: Explicit product kind supplied by the client, if any.

    Returns:
        True when the caller should write the refined kind.
    """
    if agent_kind:
        return True
    transport_kind = normalize_managed_agent_kind(session_source_type)
    return (stored_kind or "") in ("", transport_kind)


def _utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware timestamp."""
    return datetime.now(UTC)


def _coerce_utc(timestamp: Optional[datetime]) -> Optional[datetime]:
    """Normalize stored timestamps so freshness checks can compare safely."""
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _later(*moments: Optional[datetime]) -> Optional[datetime]:
    """Return the latest of the given timestamps, ignoring missing ones."""
    present = [_coerce_utc(moment) for moment in moments if moment is not None]
    return max(present) if present else None


def _latest_gateway_usage_for_runtime_session(
    db: Session, *, account_id: str, runtime_session_id: Any
):
    """Return the latest gateway usage row for one runtime session."""
    if runtime_session_id is None:
        return None
    return (
        db.query(
            ApiUsage.model_alias.label("latest_model_alias"),
            ApiUsage.provider_name.label("latest_provider_name"),
            ApiUsage.timestamp.label("last_request_at"),
        )
        .filter(
            ApiUsage.account_id == account_id,
            ApiUsage.action_type == "model_gateway",
            ApiUsage.runtime_session_id == runtime_session_id,
        )
        .order_by(ApiUsage.timestamp.desc(), ApiUsage.id.desc())
        .first()
    )


def _latest_gateway_usage_for_principal(
    db: Session,
    *,
    account_id: str,
    principal_type: str,
    principal_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    """Return the latest gateway usage row for one durable agent principal."""
    q = db.query(
        ApiUsage.model_alias.label("latest_model_alias"),
        ApiUsage.provider_name.label("latest_provider_name"),
        ApiUsage.timestamp.label("last_request_at"),
    ).filter(
        ApiUsage.account_id == account_id,
        ApiUsage.action_type == "model_gateway",
        ApiUsage.runtime_principal_type == principal_type,
        ApiUsage.runtime_principal_id == principal_id,
    )
    if start_date:
        q = q.filter(ApiUsage.timestamp >= start_date)
    if end_date:
        q = q.filter(ApiUsage.timestamp <= end_date)
    return q.order_by(ApiUsage.timestamp.desc(), ApiUsage.id.desc()).first()


def _empty_usage_aggregate() -> dict[str, Any]:
    """Return a zeroed principal usage aggregate."""
    return {
        "session_count": 0,
        "total_requests": 0,
        "successful_requests": 0,
        "failed_requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        **EMPTY_CACHE_SPLIT,
        "estimated_cost": 0.0,
        "latest_model_alias": None,
        "latest_provider_name": None,
        "last_request_at": None,
        "subscription_usage": None,
    }


class _PageRow:
    """One listed agent: the page columns plus its model request count."""

    def __init__(self, **values: Any) -> None:
        """Expose every column as an attribute, like the SQL row it replaces.

        Args:
            **values: Column values keyed by label.
        """
        self.__dict__.update(values)


def _usage_total_columns() -> list[Any]:
    """Return the summed columns of one agent usage aggregate."""
    return [
        func.count(ApiUsage.id).label("request_count"),
        func.coalesce(
            func.sum(case((ApiUsage.status_code < 400, 1), else_=0)), 0
        ).label("success_count"),
        func.coalesce(
            func.sum(case((ApiUsage.status_code >= 400, 1), else_=0)), 0
        ).label("error_count"),
        func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
            "completion_tokens"
        ),
        func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label("estimated_cost"),
        *cache_split_columns(),
    ]


def _add_usage_totals(aggregate: dict[str, Any], row: Any) -> None:
    """Add one row of :func:`_usage_total_columns` sums into an aggregate."""
    aggregate["total_requests"] += int(row.request_count or 0)
    aggregate["successful_requests"] += int(row.success_count or 0)
    aggregate["failed_requests"] += int(row.error_count or 0)
    aggregate["prompt_tokens"] += int(row.prompt_tokens or 0)
    aggregate["completion_tokens"] += int(row.completion_tokens or 0)
    aggregate["total_tokens"] += int(row.total_tokens or 0)
    aggregate["estimated_cost"] += float(row.estimated_cost or 0.0)
    for key, value in cache_split_from_row(row).items():
        aggregate[key] = int(aggregate.get(key) or 0) + value


def _like_prefix(value: str) -> str:
    """Escape ``value`` for use as a literal LIKE prefix."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_run_of(value: Optional[str], base: str) -> bool:
    """True when ``value`` is ``base`` or a per-run variant of it.

    Per-run sessions key off the agent's base source id plus a run suffix:
    ``<base>:<run>`` (X-Preloop-Session-Id) or ``<base>-<timestamp|uuid>``
    (runtime-token agents). The Overview matches both forms, so this does too.
    """
    if not value:
        return False
    return value == base or value.startswith(f"{base}:") or value.startswith(f"{base}-")


@dataclass
class _AgentSessionOwnership:
    """Which runtime sessions belong to which listed agent principal."""

    sessions_by_principal: dict[tuple[str, str], list[Any]] = field(
        default_factory=dict
    )
    session_started_at: dict[Any, Optional[datetime]] = field(default_factory=dict)
    #: Every agent base principal of the account. A usage row written under
    #: one of these is that agent's wherever it ran, so the session pass
    #: never counts it a second time.
    known_principals: list[tuple[str, str]] = field(default_factory=list)


def _agent_session_ownership(
    db: Session, *, account_id: str, principals: list[tuple[str, str]]
) -> _AgentSessionOwnership:
    """Resolve the runtime sessions each listed agent principal owns.

    A session is an agent's when (in this order, the Overview's order) it is
    the session the agent is bound to, its runtime principal is
    ``("managed_agent", <agent id>)``, or its source id or runtime principal
    id is the agent's base source id or a per-run variant of it. When two
    agents' base ids both prefix a session id, the longest base wins.

    Args:
        db: Database session.
        account_id: Account that owns the agents and sessions.
        principals: ``(session_source_type, session_source_id)`` of the agents
            whose sessions are wanted.

    Returns:
        The owned session ids per listed principal, their start times, and the
        account's agent base principals.
    """
    known = (
        db.query(
            ManagedAgent.id,
            ManagedAgent.session_source_type,
            ManagedAgent.session_source_id,
            ManagedAgent.runtime_session_id,
        )
        .filter(ManagedAgent.account_id == account_id)
        .all()
    )
    ownership = _AgentSessionOwnership(
        known_principals=list(
            dict.fromkeys(
                (row.session_source_type, row.session_source_id)
                for row in known
                if row.session_source_type and row.session_source_id
            )
        )
    )
    wanted = set(principals)
    listed = [
        row
        for row in known
        if (row.session_source_type, row.session_source_id) in wanted
    ]
    if not listed:
        return ownership
    bound_owner = {
        row.runtime_session_id: (row.session_source_type, row.session_source_id)
        for row in known
        if row.runtime_session_id is not None
    }
    agent_owner = {
        str(row.id): (row.session_source_type, row.session_source_id) for row in known
    }
    bases_by_type: dict[str, list[str]] = {}
    for principal_type, principal_id in ownership.known_principals:
        bases_by_type.setdefault(principal_type, []).append(principal_id)
    for bases in bases_by_type.values():
        bases.sort(key=len, reverse=True)

    candidates: list[Any] = [
        RuntimeSession.id.in_(
            [row.runtime_session_id for row in listed if row.runtime_session_id]
        ),
        and_(
            RuntimeSession.runtime_principal_type == "managed_agent",
            RuntimeSession.runtime_principal_id.in_([str(row.id) for row in listed]),
        ),
    ]
    for principal_type, base in wanted:
        if not principal_type or not base:
            continue
        escaped = _like_prefix(base)
        for type_column, id_column in (
            (RuntimeSession.session_source_type, RuntimeSession.session_source_id),
            (
                RuntimeSession.runtime_principal_type,
                RuntimeSession.runtime_principal_id,
            ),
        ):
            candidates.append(
                and_(
                    type_column == principal_type,
                    or_(
                        id_column == base,
                        id_column.like(f"{escaped}:%", escape="\\"),
                        id_column.like(f"{escaped}-%", escape="\\"),
                    ),
                )
            )
    sessions = (
        db.query(
            RuntimeSession.id,
            RuntimeSession.started_at,
            RuntimeSession.session_source_type,
            RuntimeSession.session_source_id,
            RuntimeSession.runtime_principal_type,
            RuntimeSession.runtime_principal_id,
        )
        .filter(RuntimeSession.account_id == account_id, or_(*candidates))
        .all()
    )

    def owner_of(session: Any) -> Optional[tuple[str, str]]:
        if session.id in bound_owner:
            return bound_owner[session.id]
        if session.runtime_principal_type == "managed_agent":
            owner = agent_owner.get(str(session.runtime_principal_id))
            if owner is not None:
                return owner
        for key_type, key_id in (
            (session.session_source_type, session.session_source_id),
            (session.runtime_principal_type, session.runtime_principal_id),
        ):
            for base in bases_by_type.get(key_type or "", []):
                if _is_run_of(key_id, base):
                    return (key_type, base)
        return None

    for session in sessions:
        owner = owner_of(session)
        if owner in wanted:
            ownership.sessions_by_principal.setdefault(owner, []).append(session.id)
            ownership.session_started_at[session.id] = session.started_at
    return ownership


def _session_usage_filter(
    *,
    account_id: str,
    known_principals: list[tuple[str, str]],
    start_date: Optional[datetime],
    end_date: Optional[datetime],
) -> list[Any]:
    """Rows an agent's sessions add on top of its own principal's rows.

    Rows written under any agent's base principal are counted by the
    principal pass (for that agent) and are left out here.
    """
    conditions = [ApiUsage.account_id == account_id, gateway_action_condition()]
    if known_principals:
        conditions.append(
            or_(
                ApiUsage.runtime_principal_type.is_(None),
                ApiUsage.runtime_principal_id.is_(None),
                tuple_(
                    ApiUsage.runtime_principal_type, ApiUsage.runtime_principal_id
                ).notin_(known_principals),
            )
        )
    if start_date:
        conditions.append(ApiUsage.timestamp >= start_date)
    if end_date:
        conditions.append(ApiUsage.timestamp <= end_date)
    return conditions


def _subscription_columns() -> list[Any]:
    """Sums of subscription-routed rows, as the Cost page computes them.

    Subscription rows (``cost_source='subscription'``) record $0 marginal
    spend and carry ``meta_data.api_equivalent_cost``, an ESTIMATE of the
    pay-per-use price (#1401). Rows without a numeric estimate are counted
    but not covered, so coverage says how much of the workload the sum
    describes instead of reading a missing estimate as $0.
    """
    equivalent = ApiUsage.meta_data["api_equivalent_cost"]
    is_covered = func.jsonb_typeof(equivalent) == "number"
    return [
        func.count(ApiUsage.id).label("sub_requests"),
        func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label("sub_prompt"),
        func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label("sub_completion"),
        func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("sub_total"),
        func.coalesce(func.sum(case((is_covered, 1), else_=0)), 0).label("sub_covered"),
        func.coalesce(
            func.sum(case((is_covered, cast(equivalent.astext, Float)), else_=None)),
            0.0,
        ).label("sub_equivalent"),
    ]


def _add_subscription(aggregate: dict[str, Any], row: Any) -> None:
    """Add one row of :func:`_subscription_columns` sums into an aggregate."""
    if not int(row.sub_requests or 0):
        return
    block = aggregate.get("subscription_usage") or {
        "request_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "api_equivalent_cost": 0.0,
        "api_equivalent_cost_is_estimate": True,
        "covered_requests": 0,
        "coverage": None,
        "billed": None,
        "billed_available": False,
    }
    block["request_count"] += int(row.sub_requests or 0)
    block["prompt_tokens"] += int(row.sub_prompt or 0)
    block["completion_tokens"] += int(row.sub_completion or 0)
    block["total_tokens"] += int(row.sub_total or 0)
    block["covered_requests"] += int(row.sub_covered or 0)
    block["api_equivalent_cost"] += float(row.sub_equivalent or 0.0)
    block["coverage"] = block["covered_requests"] / block["request_count"]
    aggregate["subscription_usage"] = block


def _usage_aggregates_for_principals(
    db: Session,
    *,
    account_id: str,
    principals: list[tuple[str, str]],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    ownership: Optional[_AgentSessionOwnership] = None,
    include_subscription_usage: bool = True,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return principal-scoped usage totals for many agents in few queries.

    Args:
        db: Active database session.
        account_id: Account that owns the usage and sessions.
        principals: ``(runtime_principal_type, runtime_principal_id)`` pairs.
        start_date: Optional inclusive lower bound for sessions and usage.
        end_date: Optional inclusive upper bound for sessions and usage.
        ownership: Sessions already resolved for these principals, so one
            request that reads several windows resolves them once.
        include_subscription_usage: Read API-equivalent subscription pricing.
            Disable for list reads to avoid parsing historical request metadata.

    Returns:
        Mapping from principal tuple to the same aggregate shape produced by
        :func:`_usage_aggregate_for_principal`.
    """
    unique_principals = list(dict.fromkeys(principals))
    if not unique_principals:
        return {}

    results = {principal: _empty_usage_aggregate() for principal in unique_principals}
    if ownership is None:
        ownership = _agent_session_ownership(
            db, account_id=account_id, principals=unique_principals
        )
    lower = _coerce_utc(start_date)
    upper = _coerce_utc(end_date)
    for principal, session_ids in ownership.sessions_by_principal.items():
        started = [
            _coerce_utc(ownership.session_started_at.get(session_id))
            for session_id in session_ids
        ]
        results[principal]["session_count"] = sum(
            1
            for started_at in started
            if (lower is None or (started_at is not None and started_at >= lower))
            and (upper is None or (started_at is not None and started_at <= upper))
        )

    # One LATERAL pair per principal: its totals (a plain aggregate over its
    # own index range) and its latest request (the head of
    # ix_api_usage_account_principal_ts). Grouping every principal's rows in
    # one statement sorted all of them first: 215k rows spilled to disk and a
    # window over the same rows, ~0.6 s for the founder account on prod
    # (#1694).
    principal_rows = values(
        column("principal_type", String),
        column("principal_id", String),
        name="listed_principals",
    ).data(unique_principals)

    def _principal_usage_filter() -> list[Any]:
        conditions = [
            ApiUsage.account_id == account_id,
            # Literal so a generic prepared plan can still match the partial
            # ix_api_usage_principal_totals index (#1694).
            gateway_action_condition(),
            ApiUsage.runtime_principal_type == principal_rows.c.principal_type,
            ApiUsage.runtime_principal_id == principal_rows.c.principal_id,
        ]
        if start_date:
            conditions.append(ApiUsage.timestamp >= start_date)
        if end_date:
            conditions.append(ApiUsage.timestamp <= end_date)
        return conditions

    totals = (
        select(*_usage_total_columns())
        .where(*_principal_usage_filter())
        .lateral("principal_usage_totals")
    )
    latest = (
        select(
            ApiUsage.model_alias.label("latest_model_alias"),
            ApiUsage.provider_name.label("latest_provider_name"),
            ApiUsage.timestamp.label("last_request_at"),
        )
        .where(*_principal_usage_filter())
        .order_by(ApiUsage.timestamp.desc(), ApiUsage.id.desc())
        .limit(1)
        .lateral("principal_latest_usage")
    )
    # Usage totals include subscription traffic at its recorded marginal cost.
    # Detailed readers also request the API-equivalent estimate. List readers
    # skip that aggregate: extracting its price detoasts historical JSON even
    # when the subscription index finds the matching rows efficiently.
    usage_query = select(
        principal_rows.c.principal_type,
        principal_rows.c.principal_id,
        totals,
        latest,
    )
    usage_from = principal_rows.join(totals, true()).outerjoin(latest, true())
    if include_subscription_usage:
        subscription = (
            select(*_subscription_columns())
            .where(
                *_principal_usage_filter(),
                ApiUsage.cost_source == literal_column("'subscription'"),
            )
            .lateral("principal_subscription_totals")
        )
        usage_query = usage_query.add_columns(subscription)
        usage_from = usage_from.join(subscription, true())
    usage_rows = db.execute(usage_query.select_from(usage_from)).all()
    for row in usage_rows:
        key = (row.principal_type, row.principal_id)
        if key not in results:
            continue
        if int(row.request_count or 0):
            _add_usage_totals(results[key], row)
        results[key]["latest_model_alias"] = row.latest_model_alias
        results[key]["latest_provider_name"] = row.latest_provider_name
        results[key]["last_request_at"] = row.last_request_at
        if include_subscription_usage:
            _add_subscription(results[key], row)

    # Rows in the agent's own sessions written under another principal (a
    # per-run ``<base>:<run>`` id, or none): the same rows the Overview maps
    # to the agent through the session (B-02).
    session_owner = values(
        column("owned_session_id", PG_UUID(as_uuid=True)),
        column("owner_type", String),
        column("owner_id", String),
        name="agent_owned_sessions",
    )
    owned_pairs = [
        (session_id, principal[0], principal[1])
        for principal, session_ids in ownership.sessions_by_principal.items()
        for session_id in session_ids
    ]
    if owned_pairs:
        owned = session_owner.data(owned_pairs)
        session_rows = db.execute(
            select(
                owned.c.owner_type,
                owned.c.owner_id,
                *_usage_total_columns(),
                func.max(ApiUsage.timestamp).label("last_request_at"),
            )
            .select_from(
                owned.join(
                    ApiUsage, ApiUsage.runtime_session_id == owned.c.owned_session_id
                )
            )
            .where(
                *_session_usage_filter(
                    account_id=account_id,
                    known_principals=ownership.known_principals,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            .group_by(owned.c.owner_type, owned.c.owner_id)
        ).all()
        newer: set[tuple[str, str]] = set()
        for row in session_rows:
            key = (row.owner_type, row.owner_id)
            if key not in results or not int(row.request_count or 0):
                continue
            _add_usage_totals(results[key], row)
            current = results[key]["last_request_at"]
            if current is None or (
                row.last_request_at is not None and row.last_request_at > current
            ):
                results[key]["last_request_at"] = row.last_request_at
                newer.add(key)
        if newer:
            # One statement for every owner whose latest request ran in a
            # session it owns: the newest row per owner (DISTINCT ON).
            for row in db.execute(
                select(
                    owned.c.owner_type,
                    owned.c.owner_id,
                    ApiUsage.model_alias,
                    ApiUsage.provider_name,
                )
                .select_from(
                    owned.join(
                        ApiUsage,
                        ApiUsage.runtime_session_id == owned.c.owned_session_id,
                    )
                )
                .where(
                    *_session_usage_filter(
                        account_id=account_id,
                        known_principals=ownership.known_principals,
                        start_date=start_date,
                        end_date=end_date,
                    )
                )
                .distinct(owned.c.owner_type, owned.c.owner_id)
                .order_by(
                    owned.c.owner_type,
                    owned.c.owner_id,
                    ApiUsage.timestamp.desc(),
                    ApiUsage.id.desc(),
                )
            ).all():
                key = (row.owner_type, row.owner_id)
                if key in newer:
                    results[key]["latest_model_alias"] = row.model_alias
                    results[key]["latest_provider_name"] = row.provider_name
        if include_subscription_usage:
            for row in db.execute(
                select(owned.c.owner_type, owned.c.owner_id, *_subscription_columns())
                .select_from(
                    owned.join(
                        ApiUsage,
                        ApiUsage.runtime_session_id == owned.c.owned_session_id,
                    )
                )
                .where(
                    *_session_usage_filter(
                        account_id=account_id,
                        known_principals=ownership.known_principals,
                        start_date=start_date,
                        end_date=end_date,
                    ),
                    ApiUsage.cost_source == literal_column("'subscription'"),
                )
                .group_by(owned.c.owner_type, owned.c.owner_id)
            ).all():
                key = (row.owner_type, row.owner_id)
                if key in results:
                    _add_subscription(results[key], row)

    return results


def _token_usage_from_aggregate(aggregate: dict[str, Any]) -> dict[str, Any]:
    """Return the token-figure block a managed-agent summary carries.

    The agents list shows tokens before cost, so the registry row needs the
    same in/out/cache split every other aggregate returns rather than the
    total alone.

    Args:
        aggregate: One principal usage aggregate.

    Returns:
        A mapping accepted by ``GatewayTokenUsage``.
    """
    return {
        "prompt_tokens": aggregate.get("prompt_tokens", 0),
        "completion_tokens": aggregate.get("completion_tokens", 0),
        "total_tokens": aggregate.get("total_tokens", 0),
        "cache_read_tokens": aggregate.get("cache_read_tokens", 0),
        "cache_write_tokens": aggregate.get("cache_write_tokens", 0),
        "uncached_input_tokens": aggregate.get("uncached_input_tokens", 0),
    }


def _usage_aggregate_for_principal(
    db: Session,
    *,
    account_id: str,
    principal_type: str,
    principal_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    ownership: Optional[_AgentSessionOwnership] = None,
) -> dict[str, Any]:
    """Return principal-scoped usage totals across all runtime sessions."""
    aggregates = _usage_aggregates_for_principals(
        db,
        account_id=account_id,
        principals=[(principal_type, principal_id)],
        start_date=start_date,
        end_date=end_date,
        ownership=ownership,
    )
    return aggregates.get((principal_type, principal_id), _empty_usage_aggregate())


class CRUDManagedAgent(CRUDBase[ManagedAgent]):
    """CRUD helpers for account-scoped managed-agent registry entries."""

    def remove_registry_entry(
        self, db: Session, *, agent: ManagedAgent, commit: bool = True
    ) -> None:
        """Delete a resolved registry entry, allowing an atomic audited removal."""
        db.delete(agent)
        if commit:
            db.commit()
        else:
            db.flush()

    def get_visible_target(self, db: Session, *, account_id: Any, agent_id: Any) -> Any:
        """Resolve an own or currently shared target for execution only."""
        from preloop.models.crud.resource_share import crud_resource_share

        own = self.get_for_account(
            db, account_id=str(account_id), agent_id=str(agent_id)
        )
        if own is not None:
            return own
        return crud_resource_share.visible_resource(
            db,
            account_id=account_id,
            resource_type="managed_agent",
            resource_id=agent_id,
        )

    def get_for_note_session(
        self, db: Session, *, account_id: str, runtime_session: models.RuntimeSession
    ) -> Optional[ManagedAgent]:
        """Resolve a note session's agent using its durable runtime identity.

        Native runs have their own session rows while the managed agent
        pointer names only its latest run. Resolve older and concurrent runs
        by principal too, always within the target account.
        """
        if str(runtime_session.account_id) != str(account_id):
            return None
        agent = getattr(runtime_session, "managed_agent", None)
        if agent is not None and str(agent.account_id) == str(account_id):
            return agent
        principal_type = runtime_session.runtime_principal_type
        principal_id = runtime_session.runtime_principal_id
        if not principal_type or not principal_id:
            return None
        if principal_type == "managed_agent":
            try:
                UUID(principal_id)
            except (ValueError, TypeError, AttributeError):
                return None
            return self.get_for_account(
                db, account_id=account_id, agent_id=principal_id
            )
        agent = self.get_by_source(
            db,
            account_id=account_id,
            session_source_type=principal_type,
            session_source_id=principal_id,
        )
        if agent is None and ":" in principal_id:
            agent = self.get_by_source(
                db,
                account_id=account_id,
                session_source_type=principal_type,
                session_source_id=principal_id.split(":", 1)[0],
            )
        return agent

    def get_by_source(
        self,
        db: Session,
        *,
        account_id: str,
        session_source_type: str,
        session_source_id: str,
    ) -> Optional[ManagedAgent]:
        """Look up one agent by its durable source identity, deterministically.

        A unique constraint normally keeps one row per
        ``(account, source_type, source_id)``, but sibling rows do exist in
        the field (rekey/merge races, databases restored from older dumps).
        An unordered ``.first()`` let a stale archived sibling shadow the live
        agent, which nondeterministically blocked runtime token issuance and
        broke key-to-agent resolution on the gateway auth path. Resolution is
        therefore explicit: most usable lifecycle first (active, then the
        resumable suspended, then decommissioned, then anything unrecognised),
        then most recent.

        Args:
            db: Database session.
            account_id: Account that owns the agent.
            session_source_type: Durable principal type.
            session_source_id: Durable principal id.

        Returns:
            The best-matching managed agent, or None when there is no match.
        """
        # Unknown states sort last, not between suspended and decommissioned:
        # an unrecognised value is the one case where we know least, so it must
        # never win over a state we do understand.
        lifecycle_rank = case(
            (self.model.lifecycle_state == "active", 0),
            (self.model.lifecycle_state == "suspended", 1),
            (self.model.lifecycle_state == "decommissioned", 2),
            else_=99,
        )
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.session_source_type == session_source_type,
                self.model.session_source_id == session_source_id,
            )
            .order_by(
                lifecycle_rank.asc(),
                self.model.created_at.desc(),
                self.model.id.desc(),
            )
            .first()
        )

    def list_by_kind(
        self,
        db: Session,
        *,
        account_id: str,
        agent_kind: str,
        active_only: bool = True,
    ) -> list[ManagedAgent]:
        """Return the account's managed agents of one kind, newest first.

        Used by usage ingest to resolve the default attribution target when
        the caller does not name an agent explicitly. By default only
        rows with ``lifecycle_state == "active"`` are returned so archived
        duplicates do not trip ambiguity errors.

        Args:
            db: Database session.
            account_id: Account that owns the agents.
            agent_kind: Normalized agent kind (for example ``"cursor"``).
            active_only: When True (default), keep only
                ``lifecycle_state == "active"`` rows.

        Returns:
            Matching managed agents ordered by ``created_at`` descending.
        """
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.agent_kind == agent_kind,
        )
        if active_only:
            query = query.filter(self.model.lifecycle_state == "active")
        return query.order_by(self.model.created_at.desc()).all()

    def get_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        for_update: bool = False,
    ) -> Optional[ManagedAgent]:
        """Return one managed agent scoped to the given account.

        Args:
            db: Database session.
            account_id: Account the agent must belong to.
            agent_id: Identifier of the agent to load.
            for_update: Take a row lock (``SELECT ... FOR UPDATE``) so the
                caller can read ``lifecycle_state``, decide on it, and write
                without a concurrent operator action landing in between. Only
                meaningful inside a transaction that commits afterwards.

        Returns:
            The managed agent, or ``None`` when no such agent exists.
        """
        query = db.query(self.model).filter(
            self.model.account_id == account_id, self.model.id == agent_id
        )
        if for_update:
            query = query.with_for_update()
        return query.first()

    def latest_merge_into_at(
        self, db: Session, *, account_id: str, survivor_id: str
    ) -> Optional[datetime]:
        """Return when the most recent duplicate was merged into an agent.

        A merge tags the duplicate with ``merged_into`` and ``merged_at``.
        ``merged_at`` is the merge time. Duplicates merged before that tag
        existed fall back to ``lifecycle_updated_at``, which the merge also
        stamped but a later lifecycle write can move.

        Args:
            db: Database session.
            account_id: Account the agents belong to.
            survivor_id: Agent that absorbed the duplicates.

        Returns:
            The latest merge time, or ``None`` when nothing was merged in.
        """
        merged_at = func.coalesce(
            cast(self.model.tags["merged_at"].astext, DateTime(timezone=True)),
            func.timezone("UTC", self.model.lifecycle_updated_at),
        )
        return (
            db.query(func.max(merged_at))
            .filter(
                self.model.account_id == account_id,
                self.model.tags["merged_into"].astext == str(survivor_id),
            )
            .scalar()
        )

    def touch_last_seen_for_principal(
        self,
        db: Session,
        *,
        account_id: Any,
        session_source_type: str,
        session_source_id: str,
        runtime_session_id: Optional[Any] = None,
        observed_at: datetime,
        control_session_mode: Optional[str] = None,
        control_heartbeat_at: Optional[datetime] = None,
        commit: bool = False,
    ) -> Optional[ManagedAgent]:
        """Update last-seen timestamp for one durable managed agent.

        ``control_heartbeat_at`` is passed only by the Agent Control
        WebSocket. It is a separate column from ``last_seen_at`` because
        enrollment and gateway traffic stamp that one too, and presence has to
        mean "the plugin is connected", not "this agent did something".
        """
        db_obj = self.get_by_source(
            db,
            account_id=str(account_id),
            session_source_type=session_source_type,
            session_source_id=session_source_id,
        )
        if db_obj is None:
            return None
        if db_obj.lifecycle_state != "active":
            return db_obj
        db_obj.last_seen_at = observed_at
        if control_heartbeat_at is not None:
            db_obj.control_last_heartbeat_at = control_heartbeat_at
        if control_session_mode in {"local", "remote", "queued"}:
            db_obj.control_session_mode = control_session_mode
        if runtime_session_id is not None:
            db_obj.runtime_session_id = runtime_session_id
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def update_operator_state(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        owner_user_id: Any = None,
        set_owner: bool = False,
        display_name: Optional[str] = None,
        set_display_name: bool = False,
        lifecycle_state: Optional[str] = None,
        lifecycle_reason: Optional[str] = None,
        tags: Optional[dict[str, str]] = None,
        set_tags: bool = False,
        others_may_start_sessions: Optional[str] = None,
        commit: bool = True,
    ) -> Optional[ManagedAgent]:
        """Update ownership and lifecycle controls for one managed agent.

        Raises:
            ValueError: If ``lifecycle_state`` is not a recognised state.
        """
        if (
            lifecycle_state is not None
            and lifecycle_state not in MANAGED_AGENT_LIFECYCLE_STATES
        ):
            raise ValueError(
                f"Invalid managed agent lifecycle_state {lifecycle_state!r}; "
                f"expected one of {sorted(MANAGED_AGENT_LIFECYCLE_STATES)}"
            )
        if lifecycle_state is not None:
            from .billing import billing

            with db.no_autoflush:
                billing.lock_account(db, str(account_id))
                db_obj = (
                    db.query(self.model)
                    .filter(
                        self.model.account_id == account_id, self.model.id == agent_id
                    )
                    .with_for_update()
                    .populate_existing()
                    .one_or_none()
                )
        else:
            db_obj = self.get_for_account(db, account_id=account_id, agent_id=agent_id)
        if db_obj is None:
            return None
        if lifecycle_state == "active" and db_obj.lifecycle_state != "active":
            from .capacity import authorize_capacity

            authorize_capacity(db, str(account_id), "agents")
        now = _utc_now()
        if set_owner:
            db_obj.owner_user_id = owner_user_id
        if set_display_name and display_name is not None:
            db_obj.display_name = display_name.strip()
        if others_may_start_sessions is not None:
            if others_may_start_sessions not in {"ask_me", "allow", "deny"}:
                raise ValueError("Invalid session consent mode")
            db_obj.others_may_start_sessions = others_may_start_sessions
        if set_tags and tags is not None:
            db_obj.tags = tags
        if lifecycle_state is not None:
            db_obj.lifecycle_state = lifecycle_state
            # Reset deliberately, including to None. lifecycle_reason explains
            # the CURRENT state, alongside lifecycle_updated_at; carrying a
            # previous transition's reason forward would label a resumed agent
            # with the reason it was paused.
            db_obj.lifecycle_reason = lifecycle_reason
            db_obj.lifecycle_updated_at = now
            # Only the terminal state unbinds the runtime session. Pausing is
            # reversible and every auth path rejects non-active agents on each
            # request, so dropping the binding here would just make resume
            # unable to restore the agent to its previous state.
            if lifecycle_state != "active":
                db_obj.control_connection_id = None
                db_obj.control_last_heartbeat_at = None
                db_obj.control_session_mode = None
            if lifecycle_state == "decommissioned":
                db_obj.runtime_session_id = None
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def clear_runtime_session_binding(
        self,
        db: Session,
        *,
        account_id: str,
        session_source_type: str,
        session_source_id: str,
        runtime_session_id: Optional[Any] = None,
        commit: bool = False,
    ) -> Optional[ManagedAgent]:
        """Clear the active runtime-session binding for one managed agent."""
        db_obj = self.get_by_source(
            db,
            account_id=account_id,
            session_source_type=session_source_type,
            session_source_id=session_source_id,
        )
        if db_obj is None:
            return None
        if runtime_session_id is not None and str(db_obj.runtime_session_id) != str(
            runtime_session_id
        ):
            return db_obj
        db_obj.runtime_session_id = None
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def clear_control_heartbeat(
        self,
        db: Session,
        *,
        account_id: str,
        session_source_type: str,
        session_source_id: str,
        not_newer_than: datetime,
        commit: bool = False,
    ) -> Optional[ManagedAgent]:
        """Drop the presence heartbeat when a control socket closes cleanly.

        Without this the badge would keep saying online for the length of the
        presence window after somebody quits their agent. ``not_newer_than``
        is the last heartbeat this connection wrote: if the stored value has
        moved past it the plugin already reconnected (possibly to another api
        replica), and this late close must not report it offline.
        """
        db_obj = self.get_by_source(
            db,
            account_id=account_id,
            session_source_type=session_source_type,
            session_source_id=session_source_id,
        )
        if db_obj is None:
            return None
        stored = db_obj.control_last_heartbeat_at
        if stored is not None:
            if stored.tzinfo is None:
                stored = stored.replace(tzinfo=UTC)
            if stored > not_newer_than:
                return db_obj
        db_obj.control_last_heartbeat_at = None
        db_obj.control_session_mode = None
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def upsert_from_runtime_session(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        session_source_type: str,
        session_source_id: str,
        display_name: str,
        session_reference: Optional[str] = None,
        managed_mcp_servers: Optional[list[str]] = None,
        enrolled_via: str = "runtime_session_token",
        last_seen_at: Optional[datetime] = None,
        owner_user_id: Any = None,
        enrollment_hostname: Optional[str] = None,
        identity_derivation: Optional[str] = None,
        agent_kind: Optional[str] = None,
    ) -> ManagedAgent:
        """Create or update one registry entry from a runtime-session token flow.

        ``owner_user_id`` (the enrolling user) is set on create and backfilled on
        update only when the agent has no owner yet, so a manually assigned owner
        is never overwritten. This owner drives per-user cost attribution and
        per-user budgets.

        ``agent_kind`` lets a newer CLI declare the product it is enrolling
        (``cursor``) while keeping the transport ``session_source_type``
        (``desktop_agent``) that the v2 principal id is derived from, so the
        agent's identity is preserved. On update the stored kind is only
        refined, never reset to the generic transport value, so an older CLI
        re-enrolling an agent cannot regress a known product kind.
        """
        db_obj = self.get_by_source(
            db,
            account_id=str(account_id),
            session_source_type=session_source_type,
            session_source_id=session_source_id,
        )
        normalized_servers = list(dict.fromkeys(managed_mcp_servers or []))
        observed_at = last_seen_at or _utc_now()

        if db_obj is None:
            from .capacity import authorize_capacity

            authorize_capacity(db, str(account_id), "agents")
            db_obj = ManagedAgent(
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                agent_kind=normalize_managed_agent_kind(
                    session_source_type, agent_kind=agent_kind
                ),
                session_source_type=session_source_type,
                session_source_id=session_source_id,
                session_reference=session_reference,
                enrollment_hostname=enrollment_hostname,
                identity_derivation=identity_derivation,
                display_name=display_name,
                enrolled_via=enrolled_via,
                managed_mcp_servers=normalized_servers,
                owner_user_id=owner_user_id,
                lifecycle_state="active",
                lifecycle_reason=None,
                lifecycle_updated_at=observed_at,
                last_seen_at=observed_at,
            )
            db.add(db_obj)
            db.flush()
            return db_obj

        db_obj.runtime_session_id = runtime_session_id
        if should_refine_agent_kind(
            db_obj.agent_kind,
            session_source_type=session_source_type,
            agent_kind=agent_kind,
        ):
            db_obj.agent_kind = normalize_managed_agent_kind(
                session_source_type, agent_kind=agent_kind
            )
        # Preserve operator renames on reuse; only fill an empty display name.
        if not (db_obj.display_name or "").strip():
            db_obj.display_name = display_name
        db_obj.enrolled_via = enrolled_via
        db_obj.last_seen_at = observed_at
        if session_reference is not None:
            db_obj.session_reference = session_reference
        if enrollment_hostname is not None:
            db_obj.enrollment_hostname = enrollment_hostname
        if identity_derivation is not None:
            db_obj.identity_derivation = identity_derivation
        if owner_user_id is not None and db_obj.owner_user_id is None:
            db_obj.owner_user_id = owner_user_id
        db_obj.managed_mcp_servers = normalized_servers
        db.add(db_obj)
        db.flush()
        return db_obj

    def create_custom_agent(
        self,
        db: Session,
        *,
        account_id: Any,
        display_name: str,
        description: Optional[str] = None,
        owner_user_id: Any = None,
        agent_kind: Optional[str] = None,
        commit: bool = True,
    ) -> ManagedAgent:
        """Register a custom managed agent the discovery CLI cannot find.

        Custom agents never connect through a runtime session, so they are
        given a generated ``session_source_id`` (``custom_<token>``) under the
        reserved ``custom`` source type. Because the id is random it can never
        collide with the real local-config source ids that ``preloop agents
        discover`` keys off, so a later discovery run will not be deduped
        against this row.

        ``agent_kind`` records which product the agent is (``cursor``) so
        API-created agents are no longer indistinguishable from genuinely
        bespoke ones. The ``custom`` ``session_source_type`` is kept regardless:
        it drives the generated-id/dedupe contract above and is part of the v2
        principal-id fingerprint, so it must not vary with the declared kind.

        Args:
            db: Active database session.
            account_id: Owning account identifier.
            display_name: Operator-facing name for the agent.
            description: Optional free-form description; stored under ``tags``.
            owner_user_id: User credited as the agent owner.
            agent_kind: Optional product kind; defaults to ``custom``.
            commit: Whether to commit the transaction.

        Returns:
            The newly created ManagedAgent row.
        """
        from .capacity import authorize_capacity

        authorize_capacity(db, str(account_id), "agents")
        now = _utc_now()
        normalized_name = display_name.strip()
        session_source_id = f"custom_{secrets.token_urlsafe(16)}"
        tags: dict[str, str] = {}
        if description and description.strip():
            tags["description"] = description.strip()
        db_obj = ManagedAgent(
            account_id=account_id,
            runtime_session_id=None,
            agent_kind=normalize_managed_agent_kind("custom", agent_kind=agent_kind),
            session_source_type="custom",
            session_source_id=session_source_id,
            session_reference=None,
            display_name=normalized_name,
            enrolled_via="operator_registration",
            managed_mcp_servers=[],
            owner_user_id=owner_user_id,
            tags=tags,
            lifecycle_state="active",
            lifecycle_reason=None,
            lifecycle_updated_at=now,
            last_seen_at=now,
        )
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        query: Optional[str] = None,
        agent_kind: Optional[str] = None,
        last_seen_after: Optional[datetime] = None,
        status: str = "all",
        tags: Optional[dict[str, str]] = None,
        owner_username: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List managed agents with runtime-session and gateway usage summary."""
        base_query = (
            db.query(self.model)
            .outerjoin(
                RuntimeSession, self.model.runtime_session_id == RuntimeSession.id
            )
            .outerjoin(User, self.model.owner_user_id == User.id)
        )
        from preloop.plugins.account_hooks import (
            VISIBLE_MANAGED_AGENT,
            extra_visible_ids,
        )

        shared_ids = {
            str(shared)
            for shared in extra_visible_ids(db, account_id, VISIBLE_MANAGED_AGENT)
        }
        if shared_ids:
            # Agents another account shares here (account hook H3).
            base_query = base_query.filter(
                or_(
                    self.model.account_id == account_id,
                    self.model.id.in_(list(shared_ids)),
                )
            )
        else:
            base_query = base_query.filter(self.model.account_id == account_id)

        if query:
            normalized_query = f"%{' '.join(query.strip().split())}%"
            base_query = base_query.filter(
                or_(
                    self.model.display_name.ilike(normalized_query),
                    self.model.session_source_type.ilike(normalized_query),
                    self.model.session_source_id.ilike(normalized_query),
                    self.model.session_reference.ilike(normalized_query),
                )
            )
        if agent_kind:
            if "," in agent_kind:
                kinds = [k.strip() for k in agent_kind.split(",") if k.strip()]
                base_query = base_query.filter(self.model.agent_kind.in_(kinds))
            else:
                base_query = base_query.filter(self.model.agent_kind == agent_kind)
        if last_seen_after:
            last_seen_utc = (
                last_seen_after.astimezone(UTC).replace(tzinfo=None)
                if last_seen_after.tzinfo
                else last_seen_after
            )
            base_query = base_query.filter(self.model.last_seen_at >= last_seen_utc)
        if owner_username:
            base_query = base_query.filter(User.username == owner_username)
        if tags:
            base_query = base_query.filter(self.model.tags.contains(tags))
        if status == "active":
            base_query = base_query.filter(
                self.model.lifecycle_state == "active",
                RuntimeSession.id.isnot(None),
                RuntimeSession.ended_at.is_(None),
            )
        elif status == "ended":
            base_query = base_query.filter(
                or_(
                    RuntimeSession.ended_at.isnot(None),
                    self.model.lifecycle_state.in_(["suspended", "decommissioned"]),
                )
            )

        total = base_query.count()

        # Rank with one index-bounded max() per agent, then count the page's
        # requests over the page's sessions only. Joining api_usage for every
        # agent and grouping on every column before the LIMIT made the cost
        # follow the account's whole gateway history (#1694).
        last_request = (
            db.query(func.max(ApiUsage.timestamp))
            .filter(
                ApiUsage.runtime_session_id == self.model.runtime_session_id,
                ApiUsage.action_type == "model_gateway",
            )
            .correlate(self.model)
            .scalar_subquery()
        )
        page = (
            base_query.with_entities(
                self.model.id,
                self.model.runtime_session_id,
                self.model.owner_user_id,
                self.model.others_may_start_sessions,
                self.model.agent_kind,
                self.model.display_name,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.enrollment_hostname,
                self.model.identity_derivation,
                self.model.enrolled_via,
                self.model.managed_mcp_servers,
                self.model.lifecycle_state,
                self.model.lifecycle_reason,
                self.model.lifecycle_updated_at,
                self.model.last_seen_at,
                self.model.control_session_mode,
                self.model.control_last_heartbeat_at,
                self.model.tags,
                User.username.label("owner_username"),
                User.email.label("owner_email"),
                RuntimeSession.started_at,
                RuntimeSession.last_activity_at,
                RuntimeSession.ended_at,
                RuntimeSession.turn_state,
                RuntimeSession.turn_started_at,
                RuntimeSession.turn_ended_at,
                RuntimeSession.turn_interrupted,
                last_request.label("last_request_at"),
            )
            .order_by(
                func.coalesce(
                    last_request,
                    RuntimeSession.last_activity_at,
                    self.model.last_seen_at,
                    self.model.created_at,
                ).desc(),
                self.model.id.desc(),
            )
            .limit(limit)
            .offset(offset)
            .all()
        )
        session_ids = [row.runtime_session_id for row in page if row.runtime_session_id]
        request_counts: dict[Any, int] = {}
        if session_ids:
            request_counts = {
                session_id: int(count or 0)
                for session_id, count in db.query(
                    ApiUsage.runtime_session_id, func.count(ApiUsage.id)
                )
                .filter(
                    ApiUsage.runtime_session_id.in_(session_ids),
                    ApiUsage.action_type == "model_gateway",
                )
                .group_by(ApiUsage.runtime_session_id)
                .all()
            }
        rows = [
            _PageRow(
                **row._asdict(),
                request_count=request_counts.get(row.runtime_session_id, 0),
                # Overwritten below from the principal-scoped aggregates.
                estimated_cost=0.0,
                latest_model_alias=None,
                latest_provider_name=None,
            )
            for row in page
        ]

        principals = [(row.session_source_type, row.session_source_id) for row in rows]
        aggregates = _usage_aggregates_for_principals(
            db,
            account_id=account_id,
            principals=principals,
            # The optional estimate parses wide JSON for every subscription
            # request. Keep list reads on the covering usage index; detailed
            # pricing remains available on the agent and Spend pages.
            include_subscription_usage=False,
        )

        items = []
        for row in rows:
            aggregate = aggregates.get(
                (row.session_source_type, row.session_source_id),
                _empty_usage_aggregate(),
            )
            # Status reads the agent's traffic in every session it owns, not
            # only the bound one the page query counted (B-02).
            row.request_count = max(
                int(row.request_count or 0), aggregate["total_requests"]
            )
            row.last_request_at = _later(
                row.last_request_at, aggregate["last_request_at"]
            )
            summary = self._row_to_summary(row)
            if str(row.id) in shared_ids:
                from preloop.models.crud.resource_share import crud_resource_share

                agent = self.get(db, id=row.id)
                if agent is None:
                    continue
                public = crud_resource_share.public_projection(
                    db, resource_type="managed_agent", row=agent
                )
                items.append(public.model_dump(mode="json"))
                continue
            aggregate = aggregates.get(
                (row.session_source_type, row.session_source_id),
                _empty_usage_aggregate(),
            )
            summary["total_requests"] = aggregate["total_requests"]
            summary["successful_requests"] = aggregate["successful_requests"]
            summary["failed_requests"] = aggregate["failed_requests"]
            summary["token_usage"] = _token_usage_from_aggregate(aggregate)
            summary["estimated_cost"] = aggregate["estimated_cost"]
            summary["subscription_usage"] = aggregate["subscription_usage"]
            summary["latest_model_alias"] = aggregate["latest_model_alias"]
            summary["latest_provider_name"] = aggregate["latest_provider_name"]
            summary["last_request_at"] = aggregate["last_request_at"]
            items.append(summary)

        return {"total": total, "items": items}

    def session_ownership(
        self, db: Session, *, account_id: str, agent: ManagedAgent
    ) -> _AgentSessionOwnership:
        """Resolve the sessions one agent owns, once per request.

        The Agent detail reads its all-time summary, a windowed aggregate and
        the per-model rows; passing this to each keeps the session match (an
        account-scoped scan of runtime sessions) to one statement.
        """
        return _agent_session_ownership(
            db,
            account_id=account_id,
            principals=[(agent.session_source_type, agent.session_source_id)],
        )

    def get_summary_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        ownership: Optional[_AgentSessionOwnership] = None,
    ) -> Optional[dict[str, Any]]:
        """Return one managed agent summary with runtime and usage aggregates."""
        row = (
            db.query(self.model)
            .outerjoin(
                RuntimeSession, self.model.runtime_session_id == RuntimeSession.id
            )
            .outerjoin(User, self.model.owner_user_id == User.id)
            .outerjoin(
                ApiUsage,
                and_(
                    ApiUsage.runtime_session_id == self.model.runtime_session_id,
                    ApiUsage.action_type == "model_gateway",
                ),
            )
            .filter(self.model.account_id == account_id, self.model.id == agent_id)
            .with_entities(
                self.model.id,
                self.model.runtime_session_id,
                self.model.owner_user_id,
                self.model.others_may_start_sessions,
                self.model.agent_kind,
                self.model.display_name,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.enrollment_hostname,
                self.model.identity_derivation,
                self.model.enrolled_via,
                self.model.managed_mcp_servers,
                self.model.lifecycle_state,
                self.model.lifecycle_reason,
                self.model.lifecycle_updated_at,
                self.model.last_seen_at,
                self.model.control_session_mode,
                self.model.control_last_heartbeat_at,
                self.model.tags,
                User.username.label("owner_username"),
                User.email.label("owner_email"),
                RuntimeSession.started_at,
                RuntimeSession.last_activity_at,
                RuntimeSession.ended_at,
                RuntimeSession.turn_state,
                RuntimeSession.turn_started_at,
                RuntimeSession.turn_ended_at,
                RuntimeSession.turn_interrupted,
                func.count(ApiUsage.id).label("request_count"),
                func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                    "estimated_cost"
                ),
                func.max(ApiUsage.model_alias).label("latest_model_alias"),
                func.max(ApiUsage.provider_name).label("latest_provider_name"),
                func.max(ApiUsage.timestamp).label("last_request_at"),
            )
            .group_by(
                self.model.id,
                self.model.runtime_session_id,
                self.model.owner_user_id,
                self.model.others_may_start_sessions,
                self.model.agent_kind,
                self.model.display_name,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.enrollment_hostname,
                self.model.identity_derivation,
                self.model.enrolled_via,
                self.model.managed_mcp_servers,
                self.model.lifecycle_state,
                self.model.lifecycle_reason,
                self.model.lifecycle_updated_at,
                self.model.last_seen_at,
                self.model.control_session_mode,
                self.model.control_last_heartbeat_at,
                self.model.tags,
                User.username,
                User.email,
                RuntimeSession.started_at,
                RuntimeSession.last_activity_at,
                RuntimeSession.ended_at,
                RuntimeSession.turn_state,
                RuntimeSession.turn_started_at,
                RuntimeSession.turn_ended_at,
                RuntimeSession.turn_interrupted,
            )
            .first()
        )
        if row is None:
            return None
        aggregate = _usage_aggregate_for_principal(
            db,
            account_id=account_id,
            principal_type=row.session_source_type,
            principal_id=row.session_source_id,
            ownership=ownership,
        )
        row = _PageRow(
            **{
                **row._asdict(),
                "request_count": max(
                    int(row.request_count or 0), aggregate["total_requests"]
                ),
                "last_request_at": _later(
                    row.last_request_at, aggregate["last_request_at"]
                ),
            }
        )
        summary = self._row_to_summary(row)
        summary["total_requests"] = aggregate["total_requests"]
        summary["successful_requests"] = aggregate["successful_requests"]
        summary["failed_requests"] = aggregate["failed_requests"]
        summary["token_usage"] = _token_usage_from_aggregate(aggregate)
        summary["estimated_cost"] = aggregate["estimated_cost"]
        summary["subscription_usage"] = aggregate["subscription_usage"]
        summary["latest_model_alias"] = aggregate["latest_model_alias"]
        summary["latest_provider_name"] = aggregate["latest_provider_name"]
        summary["last_request_at"] = aggregate["last_request_at"]
        return summary

    def get_usage_aggregate_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        ownership: Optional[_AgentSessionOwnership] = None,
    ) -> Optional[dict[str, Any]]:
        """Return historical usage totals across all sessions for one agent."""
        agent = self.get_for_account(db, account_id=account_id, agent_id=agent_id)
        if agent is None:
            return None

        return _usage_aggregate_for_principal(
            db,
            account_id=account_id,
            principal_type=agent.session_source_type,
            principal_id=agent.session_source_id,
            start_date=start_date,
            end_date=end_date,
            ownership=ownership,
        )

    def get_usage_by_agent_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Return every own agent's usage in a window, by the agent rule.

        This is the Overview Inventory's agent breakdown. It reads the same
        aggregate as the Agents list and Agent detail, so the three surfaces
        agree for the same window (B-02). Agents without traffic in the
        window are left out.

        Args:
            db: Database session.
            account_id: Account whose agents are summed.
            start_date: Inclusive lower bound on the usage timestamp.
            end_date: Inclusive upper bound on the usage timestamp.

        Returns:
            One dict per agent with traffic: ``agent_id`` plus the aggregate.
        """
        agents = (
            db.query(
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
            )
            .filter(self.model.account_id == account_id)
            .all()
        )
        aggregates = _usage_aggregates_for_principals(
            db,
            account_id=account_id,
            principals=[
                (agent.session_source_type, agent.session_source_id) for agent in agents
            ],
            start_date=start_date,
            end_date=end_date,
        )
        rows: list[dict[str, Any]] = []
        for agent in agents:
            aggregate = aggregates.get(
                (agent.session_source_type, agent.session_source_id)
            )
            if not aggregate or not aggregate["total_requests"]:
                continue
            rows.append({"agent_id": str(agent.id), **aggregate})
        return rows

    def get_usage_by_model_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        limit: int = 10,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        ownership: Optional[_AgentSessionOwnership] = None,
    ) -> list[dict[str, Any]]:
        """Return historical model usage grouped across all sessions for one agent."""
        agent = self.get_for_account(db, account_id=account_id, agent_id=agent_id)
        if agent is None:
            return []

        q = db.query(
            ApiUsage.ai_model_id,
            ApiUsage.model_alias,
            ApiUsage.provider_name,
            func.count(ApiUsage.id).label("request_count"),
            func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                "completion_tokens"
            ),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                "estimated_cost"
            ),
            func.max(ApiUsage.timestamp).label("last_request_at"),
        ).filter(
            ApiUsage.account_id == account_id,
            ApiUsage.action_type == "model_gateway",
        )
        # The same rows as the agent's aggregate: its own principal's rows
        # plus the other rows of the sessions it owns (B-02).
        principal = (agent.session_source_type, agent.session_source_id)
        if ownership is None:
            ownership = _agent_session_ownership(
                db, account_id=account_id, principals=[principal]
            )
        own_rows = and_(
            ApiUsage.runtime_principal_type == agent.session_source_type,
            ApiUsage.runtime_principal_id == agent.session_source_id,
        )
        owned_sessions = ownership.sessions_by_principal.get(principal, [])
        if owned_sessions:
            q = q.filter(
                or_(
                    own_rows,
                    and_(
                        ApiUsage.runtime_session_id.in_(owned_sessions),
                        *_session_usage_filter(
                            account_id=account_id,
                            known_principals=ownership.known_principals,
                            start_date=None,
                            end_date=None,
                        ),
                    ),
                )
            )
        else:
            q = q.filter(own_rows)
        if start_date:
            q = q.filter(ApiUsage.timestamp >= start_date)
        if end_date:
            q = q.filter(ApiUsage.timestamp <= end_date)

        rows = (
            q.group_by(
                ApiUsage.ai_model_id, ApiUsage.model_alias, ApiUsage.provider_name
            )
            .order_by(
                func.count(ApiUsage.id).desc(), func.sum(ApiUsage.total_tokens).desc()
            )
            .limit(limit)
            .all()
        )

        return [
            {
                "ai_model_id": str(row.ai_model_id) if row.ai_model_id else None,
                "model_alias": row.model_alias,
                "provider_name": row.provider_name,
                "request_count": int(row.request_count or 0),
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "estimated_cost": float(row.estimated_cost or 0.0),
                "last_request_at": row.last_request_at,
            }
            for row in rows
        ]

    @staticmethod
    def _row_to_summary(row: Any) -> dict[str, Any]:
        """Normalize one list row into API response data."""
        now = _utc_now()
        # "Active now" means traffic recency, never registration recency:
        # last_seen_at is stamped by CLI enrollment/heartbeats and
        # last_activity_at is stamped at session-token mint, so counting them
        # unconditionally made a freshly onboarded agent with zero requests
        # render as active while the dashboard (which requires requests)
        # showed "No active agents right now". An agent with no gateway
        # requests is idle; once traffic exists, later session activity keeps
        # it fresh.
        has_gateway_traffic = int(row.request_count or 0) > 0
        # The later of the bound session's activity and the agent's last
        # request: traffic in a per-run session keeps the agent fresh even
        # when its bound session has been quiet (B-02).
        moments = [
            moment
            for moment in (
                _coerce_utc(row.last_activity_at),
                _coerce_utc(row.last_request_at),
            )
            if moment is not None
        ]
        last_activity = max(moments) if has_gateway_traffic and moments else None
        if row.lifecycle_state == "decommissioned":
            activity_status = "decommissioned"
            is_active_now = False
        elif row.lifecycle_state == "suspended":
            activity_status = "suspended"
            is_active_now = False
        elif row.ended_at is not None:
            activity_status = "ended"
            is_active_now = False
        elif (
            last_activity is not None
            and (now - last_activity) <= MANAGED_AGENT_ACTIVE_WINDOW
        ):
            activity_status = "active_now"
            is_active_now = True
        elif (
            last_activity is not None
            and (now - last_activity) <= MANAGED_AGENT_RECENT_WINDOW
        ):
            activity_status = "recently_active"
            is_active_now = False
        else:
            activity_status = "idle"
            is_active_now = False
        return {
            "id": str(row.id),
            "runtime_session_id": (
                str(row.runtime_session_id) if row.runtime_session_id else None
            ),
            "owner_user_id": str(row.owner_user_id) if row.owner_user_id else None,
            "others_may_start_sessions": getattr(
                row, "others_may_start_sessions", "ask_me"
            ),
            "owner_username": row.owner_username,
            "owner_email": row.owner_email,
            "agent_kind": row.agent_kind,
            "display_name": row.display_name,
            "session_source_type": row.session_source_type,
            "session_source_id": row.session_source_id,
            "session_reference": row.session_reference,
            "enrollment_hostname": getattr(row, "enrollment_hostname", None),
            "identity_derivation": getattr(row, "identity_derivation", None),
            "enrolled_via": row.enrolled_via,
            "managed_mcp_servers": row.managed_mcp_servers or [],
            "lifecycle_state": row.lifecycle_state,
            "lifecycle_reason": row.lifecycle_reason,
            "lifecycle_updated_at": row.lifecycle_updated_at,
            "tags": row.tags or {},
            "is_active_now": is_active_now,
            "activity_status": activity_status,
            "turn_state": getattr(row, "turn_state", None),
            "turn_started_at": getattr(row, "turn_started_at", None),
            "turn_ended_at": getattr(row, "turn_ended_at", None),
            "turn_interrupted": getattr(row, "turn_interrupted", None),
            "last_seen_at": row.last_seen_at,
            "control_session_mode": getattr(row, "control_session_mode", None)
            or "offline",
            "control_last_heartbeat_at": getattr(
                row, "control_last_heartbeat_at", None
            ),
            "started_at": row.started_at,
            "last_activity_at": row.last_activity_at,
            "ended_at": row.ended_at,
            "total_requests": int(row.request_count or 0),
            "successful_requests": 0,
            "failed_requests": 0,
            "estimated_cost": float(row.estimated_cost or 0.0),
            "latest_model_alias": row.latest_model_alias,
            "latest_provider_name": row.latest_provider_name,
            "last_request_at": row.last_request_at,
        }


crud_managed_agent = CRUDManagedAgent(ManagedAgent)
