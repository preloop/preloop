"""CRUD operations for ApiUsage model."""

from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
import uuid
from typing import Any, Dict, List, Optional, Sequence, Union
from sqlalchemy import Float, String, and_, case, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from ..models.api_usage import ApiUsage
from ..models.flow import Flow
from ..models.flow_execution import FlowExecution
from ..models.managed_agent import ManagedAgent
from ..models.runtime_session import RuntimeSession
from ..models.user import User
from ...services.cache_accounting import uncached_input_tokens
from ...utils.jsonb_sanitize import sanitize_for_jsonb
from .base import CRUDBase

logger = logging.getLogger(__name__)

# Known ``meta_data.purpose`` tags for internal model-gateway usage rows.
GATEWAY_USAGE_PURPOSES = frozenset(
    {"session_title", "session_optimization", "replay_validation"}
)

# Replay-validation re-executions are Preloop-driven measurement traffic: real
# spend, but not the account's agent traffic. They are excluded from the
# user-facing usage aggregations below and from budget-bucket accumulation
# (the replay path carries its own hard spend cap; budget POLICIES are still
# checked per request at the gateway, so an account already at its limit
# cannot replay).
REPLAY_VALIDATION_PURPOSE = "replay_validation"


def exclude_replay_usage_condition():
    """Return a NULL-safe filter excluding replay-validation usage rows.

    Rows are INCLUDED when ``meta_data`` is NULL or its ``purpose`` key is
    NULL; only rows explicitly tagged ``replay_validation`` are excluded.
    Mirrors ``crud/runtime_session.py``'s internal-usage exclusion style.

    Returns:
        A SQLAlchemy boolean expression suitable for a WHERE clause.
    """
    purpose_expr = ApiUsage.meta_data["purpose"].astext
    return or_(
        purpose_expr.is_(None),
        purpose_expr != REPLAY_VALIDATION_PURPOSE,
    )


def cache_covered_condition():
    """Return a filter for rows whose provider reported a cache split.

    A NULL cache column means "the provider said nothing about caching", which
    is not the same claim as zero cached tokens. Aggregates therefore compute
    the hit ratio over covered rows only, exactly as the per-session rollup in
    ``preloop.services.cache_accounting`` does, instead of folding blind rows
    in as misses.

    Returns:
        A SQLAlchemy boolean expression suitable for a FILTER clause.
    """
    return or_(
        ApiUsage.cache_read_tokens.isnot(None),
        ApiUsage.cache_creation_tokens.isnot(None),
    )


def cache_split_columns() -> list:
    """Return the aggregate columns backing the cache half of token figures.

    Returns:
        Labelled sums for cache reads, cache writes, and the input tokens of
        the cache-covered rows (the base the uncached remainder is derived
        from).
    """
    return [
        func.coalesce(func.sum(ApiUsage.cache_read_tokens), 0).label(
            "cache_read_tokens"
        ),
        func.coalesce(func.sum(ApiUsage.cache_creation_tokens), 0).label(
            "cache_write_tokens"
        ),
        func.coalesce(
            func.sum(ApiUsage.prompt_tokens).filter(cache_covered_condition()), 0
        ).label("covered_prompt_tokens"),
    ]


def cache_split_from_row(row: Any) -> Dict[str, int]:
    """Turn the :func:`cache_split_columns` sums into response keys.

    Args:
        row: A result row carrying the labels from :func:`cache_split_columns`.

    Returns:
        ``cache_read_tokens``, ``cache_write_tokens`` and
        ``uncached_input_tokens`` for one aggregate.
    """
    read = int(getattr(row, "cache_read_tokens", 0) or 0)
    write = int(getattr(row, "cache_write_tokens", 0) or 0)
    covered = int(getattr(row, "covered_prompt_tokens", 0) or 0)
    return {
        "cache_read_tokens": read,
        "cache_write_tokens": write,
        "uncached_input_tokens": uncached_input_tokens(
            prompt_tokens=covered,
            cache_read_tokens=read,
            cache_write_tokens=write,
        ),
    }


#: The cache half of a token figure when there was no traffic at all.
EMPTY_CACHE_SPLIT: Dict[str, int] = {
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "uncached_input_tokens": 0,
}


# Cap for :meth:`CRUDApiUsage.get_by_ids` so a pathological caller cannot
# build an unbounded ``IN`` clause. Matches the gateway-activity payload
# load cap used by context analysis.
_MAX_GET_BY_IDS = 100


class CRUDApiUsage(CRUDBase[ApiUsage]):
    """CRUD operations for API usage tracking."""

    def get_by_ids(
        self,
        db: Session,
        *,
        ids: Sequence[Union[uuid.UUID, str]],
        account_id: Optional[Union[uuid.UUID, str]] = None,
    ) -> List[ApiUsage]:
        """Fetch ApiUsage rows by id, optionally scoped to an account.

        Invalid UUID strings are skipped. At most ``_MAX_GET_BY_IDS`` unique
        ids are queried.

        Args:
            db: Database session.
            ids: ApiUsage primary keys to load.
            account_id: When set, restrict to this account's rows.

        Returns:
            Matching rows (order not guaranteed).
        """
        if not ids:
            return []
        parsed: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        for raw in ids:
            try:
                value = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
            except (TypeError, ValueError, AttributeError):
                continue
            if value in seen:
                continue
            seen.add(value)
            parsed.append(value)
            if len(parsed) >= _MAX_GET_BY_IDS:
                break
        if not parsed:
            return []
        query = db.query(self.model).filter(self.model.id.in_(parsed))
        if account_id is not None:
            query = query.filter(self.model.account_id == account_id)
        return query.all()

    def log_request(
        self,
        db: Session,
        *,
        username: Optional[str] = None,
        endpoint: str,
        method: str,
        status_code: int,
        duration: float,
        action_type: Optional[str] = None,
        create_user_if_missing: bool = False,
    ) -> Optional[ApiUsage]:
        """Log an API request.

        Args:
            db: Database session
            username: Username of the user making the request
            endpoint: API endpoint being accessed
            method: HTTP method used (GET, POST, etc.)
            status_code: HTTP status code of the response
            duration: Time taken to process the request in seconds
            action_type: Type of action (create_issue, update_issue, etc.)
            create_user_if_missing: Whether to create a user account if it doesn't exist (not supported)

        Returns:
            Created API usage record, or None if the user doesn't exist and create_user_if_missing is False
        """
        user_id = None

        # Only check for user existence if a username is provided
        if username:
            user = db.query(User).filter(User.username == username).first()

            if user:
                user_id = user.id
            elif not create_user_if_missing:
                # Set user_id to None for non-existent users
                user_id = None

        try:
            # Create the API usage record
            db_obj = ApiUsage(
                user_id=user_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                duration=duration,
                action_type=action_type,
                timestamp=datetime.now(timezone.utc),
            )

            db.add(db_obj)
            db.commit()
            db.refresh(db_obj)
            return db_obj
        except IntegrityError:
            # If there's still an integrity error, roll back and return None
            db.rollback()
            return None

    def log_gateway_request(
        self,
        db: Session,
        *,
        endpoint: str,
        method: str,
        status_code: int,
        duration: float,
        user_id: Optional[str] = None,
        account_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        auth_subject_type: Optional[str] = None,
        ai_model_id: Optional[str] = None,
        flow_id: Optional[str] = None,
        flow_execution_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        model_alias: Optional[str] = None,
        provider_name: Optional[str] = None,
        upstream_request_id: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        cache_creation_tokens: Optional[int] = None,
        reasoning_tokens: Optional[int] = None,
        estimated_cost: Optional[float] = None,
        currency: Optional[str] = None,
        cost_source: Optional[str] = None,
        usage_source: Optional[str] = None,
        is_retry: Optional[bool] = None,
        error_class: Optional[str] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        managed_agent_id: Optional[str] = None,
        rate_limit_retry_after_ms: Optional[int] = None,
        meta_data: Optional[Dict[str, Any]] = None,
    ) -> ApiUsage:
        """Log a model gateway request with usage and attribution fields."""
        db_obj = ApiUsage(
            user_id=user_id,
            account_id=account_id,
            api_key_id=api_key_id,
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            duration=duration,
            action_type="model_gateway",
            auth_subject_type=auth_subject_type,
            ai_model_id=ai_model_id,
            flow_id=flow_id,
            flow_execution_id=flow_execution_id,
            runtime_session_id=runtime_session_id,
            model_alias=model_alias,
            provider_name=provider_name,
            upstream_request_id=upstream_request_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            reasoning_tokens=reasoning_tokens,
            estimated_cost=estimated_cost,
            currency=currency or ("USD" if estimated_cost is not None else None),
            cost_source=cost_source,
            usage_source=usage_source,
            is_retry=is_retry,
            error_class=error_class,
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            rate_limit_retry_after_ms=rate_limit_retry_after_ms,
            # error_detail here can carry raw upstream body text, which for a
            # binary response contains NUL and would be rejected by JSONB.
            meta_data=sanitize_for_jsonb(meta_data),
            timestamp=datetime.now(timezone.utc),
        )

        db.add(db_obj)
        db.flush()  # assign an ID

        # Atomically record spend if estimated_cost is present and > 0.
        # Replay-validation runs are excluded: their spend is bounded by the
        # replay path's own hard cap, and letting them consume budget-bucket
        # headroom would abort the user's REAL agent traffic (budget policies
        # are still checked per request at the gateway, so an account already
        # at its limit cannot replay either).
        usage_purpose = (meta_data or {}).get("purpose")
        if estimated_cost and account_id and usage_purpose != REPLAY_VALIDATION_PURPOSE:
            from .budget import record_spend_for_request

            try:
                parsed_account_id = (
                    account_id
                    if isinstance(account_id, uuid.UUID)
                    else uuid.UUID(str(account_id))
                )
            except (ValueError, TypeError, AttributeError):
                # account_id is not a valid UUID — skip budget recording only.
                parsed_account_id = None

            if parsed_account_id is not None:
                subject_scopes: list[tuple[str, Optional[str]]] = []
                if api_key_id:
                    subject_scopes.append(("api_key", str(api_key_id)))
                if managed_agent_id:
                    subject_scopes.append(("managed_agent", str(managed_agent_id)))
                    # A per-user budget counts spend from every agent the user
                    # owns, so record this spend against the owning user too.
                    owner_user_id = (
                        db.query(ManagedAgent.owner_user_id)
                        .filter(ManagedAgent.id == managed_agent_id)
                        .scalar()
                    )
                    if owner_user_id:
                        subject_scopes.append(("user", str(owner_user_id)))
                elif auth_subject_type == "managed_agents" and runtime_principal_id:
                    subject_scopes.append(("managed_agents", str(runtime_principal_id)))
                elif auth_subject_type == "flows" and flow_id:
                    subject_scopes.append(("flows", str(flow_id)))

                record_spend_for_request(
                    db=db,
                    account_id=parsed_account_id,
                    subject_type=auth_subject_type,
                    subject_id=None,
                    model_alias=model_alias,
                    estimated_cost=estimated_cost,
                    timestamp=db_obj.timestamp,
                    subject_scopes=subject_scopes,
                )

        db.commit()
        db.refresh(db_obj)
        return db_obj

    def get_gateway_cost_by_provider_day(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        provider_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Aggregate estimated cost and tokens per provider and day.

        Used by billing reconciliation to compare against provider-reported
        actuals. Replay-validation traffic is excluded like the other
        user-facing aggregations.
        """
        query = db.query(
            func.lower(ApiUsage.provider_name).label("provider"),
            func.date_trunc("day", ApiUsage.timestamp).label("day"),
            func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                "estimated_cost"
            ),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
        ).filter(
            ApiUsage.action_type == "model_gateway",
            ApiUsage.account_id == account_id,
            ApiUsage.provider_name.isnot(None),
            exclude_replay_usage_condition(),
            ApiUsage.timestamp >= start_date,
            ApiUsage.timestamp < end_date,
        )
        if provider_name:
            query = query.filter(
                func.lower(ApiUsage.provider_name) == provider_name.lower()
            )
        rows = query.group_by("provider", "day").order_by("provider", "day").all()
        return [
            {
                "provider": row.provider,
                "day": row.day,
                "estimated_cost": float(row.estimated_cost or 0.0),
                "total_tokens": int(row.total_tokens or 0),
            }
            for row in rows
        ]

    def iter_gateway_rows_for_repricing(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        start: datetime,
        end: datetime,
        only_unpriced: bool = True,
        batch_size: int = 500,
    ):
        """Yield gateway usage rows in a window, keyset-paginated by id.

        Args:
            db: Database session.
            account_id: Account scope.
            start: Window start (inclusive).
            end: Window end (exclusive).
            only_unpriced: Restrict to rows without a resolved cost: NULL
                ``estimated_cost`` (however tagged) plus rows explicitly
                tagged ``cost_source='unpriced'`` that carry a stray numeric
                cost (legacy $0 writes), so those anomalies heal too.
            batch_size: Rows fetched per query.

        Yields:
            ApiUsage rows ordered by id.
        """
        last_id: Optional[uuid.UUID] = None
        while True:
            query = db.query(ApiUsage).filter(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == "model_gateway",
                ApiUsage.timestamp >= start,
                ApiUsage.timestamp < end,
            )
            if only_unpriced:
                query = query.filter(
                    or_(
                        ApiUsage.estimated_cost.is_(None),
                        ApiUsage.cost_source == "unpriced",
                    )
                )
            if last_id is not None:
                query = query.filter(ApiUsage.id > last_id)
            batch = query.order_by(ApiUsage.id).limit(batch_size).all()
            if not batch:
                return
            yield from batch
            last_id = batch[-1].id

    def iter_unpriced_provider_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        provider_name: str,
        start: datetime,
        end: datetime,
        batch_size: int = 500,
    ):
        """Yield unpriced gateway rows for one provider, keyset-paginated.

        Deliberately narrower than :meth:`iter_gateway_rows_for_repricing`:
        only rows explicitly tagged ``cost_source='unpriced'`` — plus legacy
        rows recorded before provenance existed (``cost_source IS NULL`` with
        a NULL cost, i.e. before the 20260712 accuracy-columns migration) —
        are eligible for the given provider, so a ledger backfill can never
        touch rows the catalog (or the provider itself) already priced.

        Args:
            db: Database session.
            account_id: Account scope (required; no default).
            provider_name: Recorded provider name (e.g. ``openrouter``).
            start: Window start (inclusive).
            end: Window end (exclusive).
            batch_size: Rows fetched per query.

        Yields:
            ApiUsage rows ordered by id.
        """
        last_id: Optional[uuid.UUID] = None
        while True:
            query = db.query(ApiUsage).filter(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == "model_gateway",
                ApiUsage.provider_name == provider_name,
                or_(
                    ApiUsage.cost_source == "unpriced",
                    and_(
                        ApiUsage.cost_source.is_(None),
                        ApiUsage.estimated_cost.is_(None),
                    ),
                ),
                ApiUsage.timestamp >= start,
                ApiUsage.timestamp < end,
            )
            if last_id is not None:
                query = query.filter(ApiUsage.id > last_id)
            batch = query.order_by(ApiUsage.id).limit(batch_size).all()
            if not batch:
                return
            yield from batch
            last_id = batch[-1].id

    def list_execution_ids_with_gateway_usage(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        start: datetime,
        end: datetime,
    ) -> List[uuid.UUID]:
        """List distinct flow execution ids with gateway usage in a window.

        Used after a repricing pass to know which stored per-execution cost
        rollups may have gone stale (issue #209). The window is half-open:
        ``start <= timestamp < end``.

        Args:
            db: Database session.
            account_id: Account scope.
            start: Window start (inclusive).
            end: Window end (exclusive).

        Returns:
            Distinct ``flow_execution_id`` values (order not guaranteed).
        """
        rows = (
            db.query(ApiUsage.flow_execution_id)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                ApiUsage.flow_execution_id.isnot(None),
                ApiUsage.timestamp >= start,
                ApiUsage.timestamp < end,
            )
            .distinct()
            .all()
        )
        return [row.flow_execution_id for row in rows]

    def update_cost_fields(
        self,
        db: Session,
        *,
        api_usage_id: Union[uuid.UUID, str],
        estimated_cost: Optional[float],
        cost_source: Optional[str],
        currency: Optional[str] = "USD",
        meta_data_patch: Optional[Dict[str, Any]] = None,
        commit: bool = True,
    ) -> Optional[ApiUsage]:
        """Update cost columns on an existing usage row without budget effects.

        Used by repricing: budget spend was charged at request time and is
        deliberately NOT rewritten here — repricing is analytics-only.

        Args:
            db: Database session.
            api_usage_id: Target ``ApiUsage`` row id.
            estimated_cost: New estimated cost (may be None to mark unpriced).
            cost_source: New cost provenance marker.
            currency: ISO-4217 code of the new cost (defaults to USD).
            meta_data_patch: Keys merged into ``meta_data`` (existing keys win
                only when absent from the patch).
            commit: When False, flush only so callers can batch commits.

        Returns:
            The updated row, or None when the row does not exist.
        """
        db_obj = db.query(ApiUsage).filter(ApiUsage.id == api_usage_id).first()
        if db_obj is None:
            return None

        db_obj.estimated_cost = estimated_cost
        db_obj.cost_source = cost_source
        db_obj.currency = currency if estimated_cost is not None else db_obj.currency
        if meta_data_patch:
            merged = dict(db_obj.meta_data or {})
            merged.update(meta_data_patch)
            db_obj.meta_data = merged

        if commit:
            try:
                db.commit()
                db.refresh(db_obj)
            except Exception:
                db.rollback()
                raise
        else:
            db.flush()
        return db_obj

    def get_gateway_attempt_summary(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
        request_fingerprint: str,
    ) -> Dict[str, Any]:
        """Return prior attempts for the same logical gateway request."""
        if not account_id or not runtime_session_id or not request_fingerprint:
            return {"count": 0, "first_api_usage_id": None}

        rows = (
            db.query(ApiUsage.id)
            .filter(
                ApiUsage.account_id == account_id,
                ApiUsage.runtime_session_id == runtime_session_id,
                ApiUsage.action_type == "model_gateway",
                ApiUsage.meta_data["request_fingerprint"].astext == request_fingerprint,
            )
            .order_by(ApiUsage.timestamp.asc())
            .all()
        )
        return {
            "count": len(rows),
            "first_api_usage_id": str(rows[0].id) if rows else None,
        }

    def get_user_usage(
        self,
        db: Session,
        *,
        username: str,
        days: int = 30,
        account_id: Optional[str] = None,
    ) -> List[ApiUsage]:
        """Get API usage for a specific user within a time period."""
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        query = (
            db.query(ApiUsage)
            .join(User, ApiUsage.user_id == User.id)
            .filter(User.username == username, ApiUsage.timestamp >= start_date)
        )
        if account_id:
            query = query.filter(User.account_id == account_id)
        return query.order_by(ApiUsage.timestamp.desc()).all()

    def get_endpoint_stats(
        self, db: Session, *, days: int = 30, account_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get statistics for API endpoints."""
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        query = db.query(
            ApiUsage.endpoint,
            ApiUsage.method,
            func.count().label("request_count"),
            func.avg(ApiUsage.duration).label("avg_duration"),
            func.min(ApiUsage.duration).label("min_duration"),
            func.max(ApiUsage.duration).label("max_duration"),
        ).filter(ApiUsage.timestamp >= start_date)

        if account_id:
            query = query.join(User, ApiUsage.user_id == User.id).filter(
                User.account_id == account_id
            )

        result = (
            query.group_by(ApiUsage.endpoint, ApiUsage.method)
            .order_by(func.count().desc())
            .all()
        )

        return [
            {
                "endpoint": row.endpoint,
                "method": row.method,
                "request_count": row.request_count,
                "avg_duration": float(row.avg_duration),
                "min_duration": float(row.min_duration),
                "max_duration": float(row.max_duration),
            }
            for row in result
        ]

    def get_user_stats(
        self,
        db: Session,
        *,
        days: int = 30,
        limit: int = 10,
        account_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get statistics for API users."""
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        query = (
            db.query(
                User.username,
                func.count().label("request_count"),
                func.avg(ApiUsage.duration).label("avg_duration"),
            )
            .join(User, ApiUsage.user_id == User.id)
            .filter(ApiUsage.timestamp >= start_date)
        )

        if account_id:
            query = query.filter(User.account_id == account_id)

        result = (
            query.group_by(User.username)
            .order_by(func.count().desc())
            .limit(limit)
            .all()
        )

        return [
            {
                "username": row.username,
                "request_count": row.request_count,
                "avg_duration": float(row.avg_duration),
            }
            for row in result
        ]

    def get_for_user_filtered(
        self,
        db: Session,
        *,
        username: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        account_id: Optional[str] = None,
        limit: int = 100_000,
    ) -> List[ApiUsage]:
        """Get API usage for a user with optional date filters.

        Caps the result set at ``limit`` to avoid unbounded memory use for
        accounts with very large histories. Callers that need full aggregates
        over huge windows should use ``get_statistics_for_user`` instead.
        """
        query = (
            db.query(ApiUsage)
            .join(User, ApiUsage.user_id == User.id)
            .filter(User.username == username)
        )

        if start_date:
            query = query.filter(ApiUsage.timestamp >= start_date)
        if end_date:
            query = query.filter(ApiUsage.timestamp <= end_date)

        if account_id:
            query = query.filter(User.account_id == account_id)

        return (
            query.order_by(ApiUsage.timestamp.desc(), ApiUsage.id.desc())
            .limit(limit)
            .all()
        )

    def get_statistics_for_user(
        self,
        db: Session,
        *,
        username: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        account_id: Optional[str] = None,
        endpoint_limit: int = 50,
    ) -> Dict[str, Any]:
        """Aggregate API usage statistics for a user via SQL GROUP BY.

        When neither ``start_date`` nor ``end_date`` is provided, defaults to
        the last 30 days (inclusive of ``now - 30 days``). Prefer this over
        ``get_for_user_filtered`` for stats endpoints — it never loads raw
        rows into Python.

        Args:
            db: Database session.
            username: Username whose usage to aggregate.
            start_date: Inclusive lower bound on ``timestamp``.
            end_date: Inclusive upper bound on ``timestamp``.
            account_id: Optional account scope via the user's account.
            endpoint_limit: Max endpoints in ``requests_by_endpoint``
                (top N by count DESC). Defaults to 50.

        Returns:
            Dict with ``total_requests``, ``requests_by_date``,
            ``issues_created``, ``issues_updated``, ``issues_closed``,
            and ``requests_by_endpoint``.
        """
        if start_date is None and end_date is None:
            start_date = datetime.now(timezone.utc) - timedelta(days=30)

        base_filter = [
            User.username == username,
        ]
        if start_date is not None:
            base_filter.append(ApiUsage.timestamp >= start_date)
        if end_date is not None:
            base_filter.append(ApiUsage.timestamp <= end_date)
        if account_id is not None:
            base_filter.append(User.account_id == account_id)

        totals_row = (
            db.query(
                func.count(ApiUsage.id).label("total_requests"),
                func.coalesce(
                    func.sum(
                        case((ApiUsage.action_type == "create_issue", 1), else_=0)
                    ),
                    0,
                ).label("issues_created"),
                func.coalesce(
                    func.sum(
                        case((ApiUsage.action_type == "update_issue", 1), else_=0)
                    ),
                    0,
                ).label("issues_updated"),
                func.coalesce(
                    func.sum(case((ApiUsage.action_type == "close_issue", 1), else_=0)),
                    0,
                ).label("issues_closed"),
            )
            .join(User, ApiUsage.user_id == User.id)
            .filter(*base_filter)
            .one()
        )

        day_bucket = func.date_trunc("day", ApiUsage.timestamp)
        by_date_rows = (
            db.query(
                day_bucket.label("day"),
                func.count(ApiUsage.id).label("request_count"),
            )
            .join(User, ApiUsage.user_id == User.id)
            .filter(*base_filter)
            .group_by(day_bucket)
            .order_by(day_bucket.asc())
            .all()
        )
        requests_by_date: Dict[str, int] = {
            row.day.date().isoformat(): int(row.request_count or 0)
            for row in by_date_rows
            if row.day is not None
        }

        by_endpoint_rows = (
            db.query(
                ApiUsage.endpoint,
                func.count(ApiUsage.id).label("request_count"),
            )
            .join(User, ApiUsage.user_id == User.id)
            .filter(*base_filter)
            .group_by(ApiUsage.endpoint)
            .order_by(func.count(ApiUsage.id).desc())
            .limit(endpoint_limit)
            .all()
        )
        requests_by_endpoint: Dict[str, int] = {
            row.endpoint: int(row.request_count or 0) for row in by_endpoint_rows
        }

        return {
            "total_requests": int(totals_row.total_requests or 0),
            "requests_by_date": requests_by_date,
            "issues_created": int(totals_row.issues_created or 0),
            "issues_updated": int(totals_row.issues_updated or 0),
            "issues_closed": int(totals_row.issues_closed or 0),
            "requests_by_endpoint": requests_by_endpoint,
        }

    def get_gateway_usage_summary(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        flow_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        ai_model_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        exclude_retries: bool = False,
    ) -> Dict[str, Any]:
        """Get aggregated gateway usage totals for an account or flow.

        Args:
            exclude_retries: When True, rows marked as retries of an earlier
                identical request are excluded. Default False — retried calls
                consume real provider tokens, so they count as spend unless
                the caller explicitly asks to collapse them.
        """
        query = db.query(
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
            func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                "estimated_cost"
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                ApiUsage.estimated_cost.is_(None),
                                ApiUsage.total_tokens > 0,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("unpriced_requests"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                ApiUsage.estimated_cost.is_(None),
                                ApiUsage.total_tokens > 0,
                            ),
                            ApiUsage.total_tokens,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("unpriced_tokens"),
            *cache_split_columns(),
        ).filter(
            ApiUsage.action_type == "model_gateway",
            ApiUsage.account_id == account_id,
            exclude_replay_usage_condition(),
            ApiUsage.timestamp >= start_date,
            ApiUsage.timestamp < end_date,
        )

        if flow_id:
            query = query.filter(ApiUsage.flow_id == flow_id)
        if runtime_session_id:
            query = query.filter(ApiUsage.runtime_session_id == runtime_session_id)
        if runtime_principal_id:
            query = query.filter(ApiUsage.runtime_principal_id == runtime_principal_id)
        if ai_model_id:
            query = query.filter(ApiUsage.ai_model_id == ai_model_id)
        if api_key_id:
            query = query.filter(ApiUsage.api_key_id == api_key_id)
        if exclude_retries:
            query = query.filter(
                or_(ApiUsage.is_retry.is_(None), ApiUsage.is_retry.is_(False))
            )

        row = query.one_or_none()
        if row is None:
            return {
                "request_count": 0,
                "success_count": 0,
                "error_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost": 0.0,
                "unpriced_requests": 0,
                "unpriced_tokens": 0,
                **EMPTY_CACHE_SPLIT,
            }
        return {
            "request_count": int(row.request_count or 0),
            "success_count": int(row.success_count or 0),
            "error_count": int(row.error_count or 0),
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
            "estimated_cost": float(row.estimated_cost or 0.0),
            "unpriced_requests": int(row.unpriced_requests or 0),
            "unpriced_tokens": int(row.unpriced_tokens or 0),
            **cache_split_from_row(row),
        }

    def get_unpriced_model_breakdown(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        runtime_principal_id: Optional[str] = None,
        exclude_retries: bool = False,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Group still-unpriced gateway rows by model for the cost banner.

        Uses the exact ``unpriced`` predicate of
        :meth:`get_gateway_usage_summary` (``estimated_cost IS NULL AND
        total_tokens > 0``) so the per-model rows explain the aggregate
        ``unpriced_requests``/``unpriced_tokens`` counts shown next to them.

        Args:
            db: Database session.
            account_id: Account whose gateway usage is aggregated.
            start_date: Inclusive lower bound on usage timestamp.
            end_date: Exclusive upper bound on usage timestamp.
            runtime_principal_id: Restrict to a single runtime principal.
            exclude_retries: When True, rows marked as retries of an earlier
                identical request are excluded. Mirrors the predicate in
                :meth:`get_gateway_usage_summary` exactly, so with
                ``exclude_retries=True`` the per-model rows cannot sum above
                the headline counts.
            limit: Maximum number of models returned, ordered by unpriced
                tokens descending so the biggest offenders name themselves
                first.

        Returns:
            One dict per model with ``model``, ``requests`` and ``tokens``,
            ordered by tokens descending.
        """
        model_label = func.coalesce(ApiUsage.model_alias, "unknown")
        query = db.query(
            model_label.label("model"),
            func.count(ApiUsage.id).label("requests"),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("tokens"),
        ).filter(
            ApiUsage.action_type == "model_gateway",
            ApiUsage.account_id == account_id,
            exclude_replay_usage_condition(),
            ApiUsage.timestamp >= start_date,
            ApiUsage.timestamp < end_date,
            ApiUsage.estimated_cost.is_(None),
            ApiUsage.total_tokens > 0,
        )
        if runtime_principal_id:
            query = query.filter(ApiUsage.runtime_principal_id == runtime_principal_id)
        if exclude_retries:
            query = query.filter(
                or_(ApiUsage.is_retry.is_(None), ApiUsage.is_retry.is_(False))
            )
        query = (
            query.group_by(model_label)
            .order_by(func.coalesce(func.sum(ApiUsage.total_tokens), 0).desc())
            .limit(limit)
        )
        return [
            {
                "model": str(row.model),
                "requests": int(row.requests or 0),
                "tokens": int(row.tokens or 0),
            }
            for row in query.all()
        ]

    def get_rate_limit_summary(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        runtime_principal_id: Optional[str] = None,
        breakdown_limit: int = 25,
    ) -> Dict[str, Any]:
        """Aggregate upstream 429 telemetry for an account window (#136).

        "Blocked" time is the sum of provider-advised ``Retry-After`` values
        observed on 429 responses (``rate_limit_retry_after_ms``): a lower
        bound on real wall-clock stall, never an estimate. Subtype counts
        come from ``meta_data["rate_limit"]["subtype"]`` recorded at capture
        time.

        Args:
            db: Database session.
            account_id: Account whose gateway traffic is aggregated.
            start_date: Inclusive lower bound on usage timestamp.
            end_date: Exclusive upper bound on usage timestamp.
            runtime_principal_id: Restrict to a single runtime principal.
            breakdown_limit: Max rows per breakdown, ordered by 429 count.

        Returns:
            Dict with ``totals`` (429 count, blocked ms, last-hit timestamp,
            subtype counts) plus ``by_model`` and ``by_session`` breakdowns.
        """
        base_filters = [
            ApiUsage.action_type == "model_gateway",
            ApiUsage.account_id == account_id,
            ApiUsage.status_code == 429,
            exclude_replay_usage_condition(),
            ApiUsage.timestamp >= start_date,
            ApiUsage.timestamp < end_date,
        ]
        if runtime_principal_id:
            base_filters.append(ApiUsage.runtime_principal_id == runtime_principal_id)

        subtype_expr = ApiUsage.meta_data["rate_limit"]["subtype"].astext
        totals_row = (
            db.query(
                func.count(ApiUsage.id).label("rate_limited_requests"),
                func.coalesce(func.sum(ApiUsage.rate_limit_retry_after_ms), 0).label(
                    "blocked_ms"
                ),
                func.max(ApiUsage.timestamp).label("last_rate_limited_at"),
                func.coalesce(
                    func.sum(case((subtype_expr == "quota_exhausted", 1), else_=0)),
                    0,
                ).label("quota_exhausted_count"),
                func.coalesce(
                    func.sum(case((subtype_expr == "transient", 1), else_=0)), 0
                ).label("transient_count"),
            )
            .filter(*base_filters)
            .one()
        )

        by_model_rows = (
            db.query(
                ApiUsage.model_alias,
                ApiUsage.provider_name,
                func.count(ApiUsage.id).label("rate_limited_requests"),
                func.coalesce(func.sum(ApiUsage.rate_limit_retry_after_ms), 0).label(
                    "blocked_ms"
                ),
                func.max(ApiUsage.timestamp).label("last_rate_limited_at"),
            )
            .filter(*base_filters)
            .group_by(ApiUsage.model_alias, ApiUsage.provider_name)
            .order_by(func.count(ApiUsage.id).desc())
            .limit(breakdown_limit)
            .all()
        )

        by_session_rows = (
            db.query(
                ApiUsage.runtime_session_id,
                func.max(ApiUsage.runtime_principal_name).label(
                    "runtime_principal_name"
                ),
                func.count(ApiUsage.id).label("rate_limited_requests"),
                func.coalesce(func.sum(ApiUsage.rate_limit_retry_after_ms), 0).label(
                    "blocked_ms"
                ),
                func.max(ApiUsage.timestamp).label("last_rate_limited_at"),
            )
            .filter(*base_filters)
            .group_by(ApiUsage.runtime_session_id)
            .order_by(func.count(ApiUsage.id).desc())
            .limit(breakdown_limit)
            .all()
        )

        return {
            "totals": {
                "rate_limited_requests": int(totals_row.rate_limited_requests or 0),
                "blocked_ms": int(totals_row.blocked_ms or 0),
                "last_rate_limited_at": totals_row.last_rate_limited_at,
                "quota_exhausted_count": int(totals_row.quota_exhausted_count or 0),
                "transient_count": int(totals_row.transient_count or 0),
            },
            "by_model": [
                {
                    "model_alias": row.model_alias,
                    "provider_name": row.provider_name,
                    "rate_limited_requests": int(row.rate_limited_requests or 0),
                    "blocked_ms": int(row.blocked_ms or 0),
                    "last_rate_limited_at": row.last_rate_limited_at,
                }
                for row in by_model_rows
            ],
            "by_session": [
                {
                    "runtime_session_id": (
                        str(row.runtime_session_id) if row.runtime_session_id else None
                    ),
                    "runtime_principal_name": row.runtime_principal_name,
                    "rate_limited_requests": int(row.rate_limited_requests or 0),
                    "blocked_ms": int(row.blocked_ms or 0),
                    "last_rate_limited_at": row.last_rate_limited_at,
                }
                for row in by_session_rows
            ],
        }

    def get_latest_rate_limit_snapshots(
        self,
        db: Session,
        *,
        account_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Latest observed rate-limit header snapshot per provider/model.

        Returns the most recent usage row carrying a
        ``meta_data["rate_limit"]`` snapshot for each (provider, model alias)
        pair: the headroom signal as last observed from real provider
        responses, with its observation timestamp so callers can label
        staleness honestly.

        Args:
            db: Database session.
            account_id: Account whose snapshots are returned.
            limit: Maximum number of (provider, model) groups.

        Returns:
            One dict per group with the snapshot, observation timestamp,
            status code, and upstream credential type.
        """
        rows = (
            db.query(ApiUsage)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                # JSON null (the common "no snapshot" case) must not match,
                # so test the value rather than key presence.
                ApiUsage.meta_data["rate_limit"].astext.isnot(None),
                exclude_replay_usage_condition(),
            )
            .distinct(ApiUsage.provider_name, ApiUsage.model_alias)
            .order_by(
                ApiUsage.provider_name,
                ApiUsage.model_alias,
                ApiUsage.timestamp.desc(),
            )
            .limit(limit)
            .all()
        )
        snapshots: List[Dict[str, Any]] = []
        for row in rows:
            meta = row.meta_data or {}
            snapshots.append(
                {
                    "provider_name": row.provider_name,
                    "model_alias": row.model_alias,
                    "observed_at": row.timestamp,
                    "status_code": row.status_code,
                    "upstream_credential_type": meta.get("upstream_credential_type"),
                    "rate_limit": meta.get("rate_limit") or {},
                }
            )
        return snapshots

    def get_gateway_usage_by_model(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        flow_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        flow_execution_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        ai_model_ids: Optional[Sequence[str]] = None,
        limit: Optional[int] = 20,
    ) -> List[Dict[str, Any]]:
        """Group gateway usage by model.

        Args:
            db: Database session.
            account_id: Account whose gateway usage is aggregated.
            start_date: Inclusive lower bound on usage timestamp.
            end_date: Exclusive upper bound on usage timestamp.
            flow_id: Restrict to a single flow.
            runtime_session_id: Restrict to a single runtime session.
            runtime_principal_id: Restrict to a single runtime principal.
            flow_execution_id: Restrict to a single flow execution.
            api_key_id: Restrict to a single API key.
            ai_model_ids: Restrict aggregation to these model ids. Applied in
                SQL, before the row limit, so callers that need a total for a
                known set of models cannot lose rows to the limit. An empty
                sequence yields no rows.
            limit: Maximum number of grouped rows, ordered by request count
                descending. Pass ``None`` to return every group — required by
                callers that SUM the result (e.g. spend caps), since a
                request-count-ordered truncation silently drops low-volume,
                high-cost models from the sum.

        Returns:
            One dict per (model, alias, provider) group with request counts,
            token totals, estimated cost, how many of those requests carried no
            price, how many were priced at exactly zero, how many failed, and
            when the model was last called.
        """
        # A request with no price and a request priced at zero look identical
        # in a cost total and mean opposite things: the first is a hole in the
        # price list, the second is a deliberate (or mistaken) $0. They are
        # counted apart so the console can say which one it is.
        #
        # ``unpriced`` matches ``get_gateway_usage_summary`` exactly, so the
        # per-model counts sum to the account total the same page shows.
        unpriced_condition = and_(
            ApiUsage.estimated_cost.is_(None),
            ApiUsage.total_tokens > 0,
        )
        zero_priced_condition = and_(
            ApiUsage.estimated_cost.isnot(None),
            ApiUsage.estimated_cost == 0,
            ApiUsage.total_tokens > 0,
        )
        query = db.query(
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
            func.coalesce(func.sum(case((unpriced_condition, 1), else_=0)), 0).label(
                "unpriced_request_count"
            ),
            func.coalesce(func.sum(case((zero_priced_condition, 1), else_=0)), 0).label(
                "zero_priced_request_count"
            ),
            func.coalesce(
                func.sum(case((ApiUsage.status_code >= 400, 1), else_=0)), 0
            ).label("failed_request_count"),
            func.max(ApiUsage.timestamp).label("last_request_at"),
            *cache_split_columns(),
        ).filter(
            # Aggregate by model identity; aliases that share ai_model_id still
            # appear as separate groups when model_alias differs (intentional —
            # callers often filter/sort by the client-visible alias).
            ApiUsage.action_type == "model_gateway",
            ApiUsage.account_id == account_id,
            exclude_replay_usage_condition(),
        )
        if start_date:
            query = query.filter(ApiUsage.timestamp >= start_date)
        if end_date:
            query = query.filter(ApiUsage.timestamp < end_date)
        if flow_id:
            query = query.filter(ApiUsage.flow_id == flow_id)
        if runtime_session_id and flow_execution_id:
            query = query.filter(
                or_(
                    ApiUsage.runtime_session_id == runtime_session_id,
                    and_(
                        ApiUsage.runtime_session_id.is_(None),
                        ApiUsage.flow_execution_id == flow_execution_id,
                    ),
                )
            )
        elif runtime_session_id:
            query = query.filter(ApiUsage.runtime_session_id == runtime_session_id)
        elif flow_execution_id:
            query = query.filter(ApiUsage.flow_execution_id == flow_execution_id)
        if runtime_principal_id:
            query = query.filter(ApiUsage.runtime_principal_id == runtime_principal_id)
        if api_key_id:
            query = query.filter(ApiUsage.api_key_id == api_key_id)
        if ai_model_ids is not None:
            query = query.filter(ApiUsage.ai_model_id.in_(list(ai_model_ids)))

        query = query.group_by(
            ApiUsage.ai_model_id, ApiUsage.model_alias, ApiUsage.provider_name
        ).order_by(func.count(ApiUsage.id).desc())
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
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
                "unpriced_request_count": int(row.unpriced_request_count or 0),
                "zero_priced_request_count": int(row.zero_priced_request_count or 0),
                "failed_request_count": int(row.failed_request_count or 0),
                "last_request_at": row.last_request_at,
                **cache_split_from_row(row),
            }
            for row in rows
        ]

    def get_gateway_usage_by_flow(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        runtime_principal_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Group gateway usage by flow."""
        from ..models.flow import Flow

        base_query = (
            db.query(
                ApiUsage.flow_id,
                Flow.name.label("flow_name"),
                func.count(ApiUsage.id).label("request_count"),
                func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                    "estimated_cost"
                ),
                *cache_split_columns(),
            )
            .outerjoin(Flow, ApiUsage.flow_id == Flow.id)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                exclude_replay_usage_condition(),
            )
        )
        if start_date:
            base_query = base_query.filter(ApiUsage.timestamp >= start_date)
        if end_date:
            base_query = base_query.filter(ApiUsage.timestamp < end_date)
        if runtime_principal_id:
            base_query = base_query.filter(
                ApiUsage.runtime_principal_id == runtime_principal_id
            )

        rows = (
            base_query.group_by(ApiUsage.flow_id, Flow.name)
            .order_by(func.count(ApiUsage.id).desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "flow_id": str(row.flow_id) if row.flow_id else None,
                "flow_name": row.flow_name,
                "request_count": int(row.request_count or 0),
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "estimated_cost": float(row.estimated_cost or 0.0),
                **cache_split_from_row(row),
            }
            for row in rows
        ]

    def get_gateway_usage_by_execution(
        self,
        db: Session,
        *,
        account_id: str,
        flow_id: str,
        start_date: datetime,
        end_date: datetime,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Group gateway usage by flow execution."""
        rows = (
            db.query(
                ApiUsage.flow_execution_id,
                func.count(ApiUsage.id).label("request_count"),
                func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                    "estimated_cost"
                ),
                func.max(ApiUsage.timestamp).label("last_request_at"),
                *cache_split_columns(),
            )
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                exclude_replay_usage_condition(),
                ApiUsage.flow_id == flow_id,
                ApiUsage.timestamp >= start_date,
                ApiUsage.timestamp < end_date,
            )
            .group_by(ApiUsage.flow_execution_id)
            .order_by(func.max(ApiUsage.timestamp).desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "flow_execution_id": (
                    str(row.flow_execution_id) if row.flow_execution_id else None
                ),
                "request_count": int(row.request_count or 0),
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "estimated_cost": float(row.estimated_cost or 0.0),
                "last_request_at": row.last_request_at,
                **cache_split_from_row(row),
            }
            for row in rows
        ]

    def get_gateway_usage_by_session(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        ai_model_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Group recent execution-backed gateway usage into session slices."""
        legacy_session_source_type = case(
            (ApiUsage.flow_execution_id.isnot(None), "flow_execution"),
            else_=None,
        )
        legacy_session_source_id = cast(ApiUsage.flow_execution_id, String)
        session_source_type = func.coalesce(
            RuntimeSession.session_source_type, legacy_session_source_type
        )
        session_source_id = func.coalesce(
            RuntimeSession.session_source_id, legacy_session_source_id
        )
        session_reference = func.coalesce(
            RuntimeSession.session_reference, FlowExecution.agent_session_reference
        )
        resolved_runtime_principal_type = func.coalesce(
            RuntimeSession.runtime_principal_type, ApiUsage.runtime_principal_type
        )
        resolved_runtime_principal_id = func.coalesce(
            RuntimeSession.runtime_principal_id, ApiUsage.runtime_principal_id
        )
        resolved_runtime_principal_name = func.coalesce(
            RuntimeSession.runtime_principal_name, ApiUsage.runtime_principal_name
        )
        rows = (
            db.query(
                ApiUsage.ai_model_id,
                RuntimeSession.id.label("runtime_session_id"),
                session_source_type.label("session_source_type"),
                session_source_id.label("session_source_id"),
                RuntimeSession.title.label("session_title"),
                RuntimeSession.summary.label("session_summary"),
                resolved_runtime_principal_type.label("runtime_principal_type"),
                resolved_runtime_principal_id.label("runtime_principal_id"),
                resolved_runtime_principal_name.label("runtime_principal_name"),
                ManagedAgent.id.label("agent_id"),
                ManagedAgent.display_name.label("agent_name"),
                ApiUsage.flow_execution_id,
                ApiUsage.flow_id,
                Flow.name.label("flow_name"),
                session_reference.label("session_reference"),
                ApiUsage.model_alias,
                ApiUsage.provider_name,
                func.count(ApiUsage.id).label("request_count"),
                func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                    "estimated_cost"
                ),
                func.max(ApiUsage.timestamp).label("last_request_at"),
                *cache_split_columns(),
            )
            .outerjoin(Flow, ApiUsage.flow_id == Flow.id)
            .outerjoin(FlowExecution, ApiUsage.flow_execution_id == FlowExecution.id)
            .outerjoin(RuntimeSession, ApiUsage.runtime_session_id == RuntimeSession.id)
            .outerjoin(
                ManagedAgent, ManagedAgent.runtime_session_id == RuntimeSession.id
            )
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                exclude_replay_usage_condition(),
                or_(
                    ApiUsage.runtime_session_id.isnot(None),
                    ApiUsage.flow_execution_id.isnot(None),
                ),
                ApiUsage.timestamp >= start_date,
                ApiUsage.timestamp < end_date,
            )
        )
        if ai_model_id:
            rows = rows.filter(ApiUsage.ai_model_id == ai_model_id)
        if api_key_id:
            rows = rows.filter(ApiUsage.api_key_id == api_key_id)
        if runtime_principal_id:
            rows = rows.filter(ApiUsage.runtime_principal_id == runtime_principal_id)
        rows = (
            rows.group_by(
                ApiUsage.ai_model_id,
                RuntimeSession.id,
                session_source_type,
                session_source_id,
                RuntimeSession.title,
                RuntimeSession.summary,
                resolved_runtime_principal_type,
                resolved_runtime_principal_id,
                resolved_runtime_principal_name,
                ManagedAgent.id,
                ManagedAgent.display_name,
                ApiUsage.flow_execution_id,
                ApiUsage.flow_id,
                Flow.name,
                session_reference,
                ApiUsage.model_alias,
                ApiUsage.provider_name,
            )
            .order_by(
                func.max(ApiUsage.timestamp).desc(), func.count(ApiUsage.id).desc()
            )
            .limit(limit)
            .all()
        )
        result: list[dict[str, Any]] = [
            {
                "ai_model_id": str(row.ai_model_id) if row.ai_model_id else None,
                "runtime_session_id": (
                    str(row.runtime_session_id) if row.runtime_session_id else None
                ),
                "session_source_type": row.session_source_type,
                "session_source_id": row.session_source_id,
                "title": row.session_title,
                "session_summary": row.session_summary,
                "runtime_principal_type": row.runtime_principal_type,
                "runtime_principal_id": row.runtime_principal_id,
                "runtime_principal_name": row.runtime_principal_name,
                "agent_id": str(row.agent_id) if row.agent_id else None,
                "agent_name": row.agent_name,
                "flow_execution_id": (
                    str(row.flow_execution_id) if row.flow_execution_id else None
                ),
                "flow_id": str(row.flow_id) if row.flow_id else None,
                "flow_name": row.flow_name,
                "session_reference": row.session_reference,
                "model_alias": row.model_alias,
                "provider_name": row.provider_name,
                "request_count": int(row.request_count or 0),
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "estimated_cost": float(row.estimated_cost or 0.0),
                "last_request_at": row.last_request_at,
                **cache_split_from_row(row),
            }
            for row in rows
        ]
        self._resolve_missing_session_agents(db, account_id=account_id, rows=result)
        return result

    @staticmethod
    def _resolve_missing_session_agents(
        db: Session,
        *,
        account_id: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Backfill agent_id/agent_name for per-run sessions in place.

        The SQL join links a managed agent by its single bound
        ``runtime_session_id``, but an agent has many per-run sessions whose
        ``session_source_id`` is the agent's base source id plus a run suffix
        (``<base>:<id>`` or ``<base>-<timestamp|uuid>``). This matches each
        still-unresolved row to the managed agent whose base source id the
        session's source id starts with (longest match wins), so grouping-by-
        agent surfaces every session under its agent rather than under "Other".

        Args:
            db: Database session.
            account_id: Owning account id.
            rows: Session usage rows to enrich in place.
        """
        pending = [
            row
            for row in rows
            if not row.get("agent_id") and row.get("session_source_id")
        ]
        if not pending:
            return
        agents = (
            db.query(
                ManagedAgent.id,
                ManagedAgent.display_name,
                ManagedAgent.session_source_id,
            )
            .filter(
                ManagedAgent.account_id == account_id,
                ManagedAgent.session_source_id.isnot(None),
            )
            .all()
        )
        # Longest base source id first so a more specific agent wins.
        agents = sorted(
            agents, key=lambda a: len(a.session_source_id or ""), reverse=True
        )
        for row in pending:
            source_id = row["session_source_id"]
            for agent in agents:
                base = agent.session_source_id
                if (
                    source_id == base
                    or source_id.startswith(f"{base}:")
                    or source_id.startswith(f"{base}-")
                ):
                    row["agent_id"] = str(agent.id)
                    row["agent_name"] = agent.display_name
                    break

    def get_gateway_usage_timeseries(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        flow_id: Optional[str] = None,
        ai_model_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Group gateway usage by day."""
        bucket = func.date_trunc("day", ApiUsage.timestamp)
        query = db.query(
            bucket.label("bucket"),
            func.count(ApiUsage.id).label("request_count"),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                "output_tokens"
            ),
            func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                "estimated_cost"
            ),
            *cache_split_columns(),
        ).filter(
            ApiUsage.action_type == "model_gateway",
            ApiUsage.account_id == account_id,
            exclude_replay_usage_condition(),
            ApiUsage.timestamp >= start_date,
            ApiUsage.timestamp < end_date,
        )
        if flow_id:
            query = query.filter(ApiUsage.flow_id == flow_id)
        if ai_model_id:
            query = query.filter(ApiUsage.ai_model_id == ai_model_id)
        if api_key_id:
            query = query.filter(ApiUsage.api_key_id == api_key_id)
        if runtime_principal_id:
            query = query.filter(ApiUsage.runtime_principal_id == runtime_principal_id)

        rows = query.group_by(bucket).order_by(bucket.asc()).all()
        return [
            {
                "date": row.bucket.date().isoformat(),
                "request_count": int(row.request_count or 0),
                "total_tokens": int(row.total_tokens or 0),
                "input_tokens": int(row.input_tokens or 0),
                "output_tokens": int(row.output_tokens or 0),
                "cache_read_tokens": int(row.cache_read_tokens or 0),
                "cache_write_tokens": int(row.cache_write_tokens or 0),
                "estimated_cost": float(row.estimated_cost or 0.0),
            }
            for row in rows
        ]

    def get_models_used_for_executions(
        self,
        db: Session,
        execution_ids: Sequence[Any],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return the model aliases that served each execution, most used first.

        The console shows "which model ran this" on the executions list and on
        the execution page. The answer lives in the gateway usage rows, which
        attach to an execution two ways, both of which must be counted:

        * ``api_usage.flow_execution_id`` — the direct attribution the metrics
          endpoint and the cost rollup already use.
        * ``api_usage.runtime_session_id`` pointing at the runtime session the
          execution owns (``session_source_type='flow_execution'`` and
          ``session_source_id`` = the execution id). Agents that call the
          gateway through a session reference that way, and those rows carry
          no ``flow_execution_id``.

        One query for the whole page (never one per row): callers pass every
        execution id they are about to render and index the result by id.
        Replay-validation traffic is excluded exactly as everywhere else, and
        rows with no ``model_alias`` are skipped rather than reported as an
        unnamed model.

        Args:
            db: Database session.
            execution_ids: Execution ids to aggregate (str or UUID).

        Returns:
            ``{execution_id: [{"model_alias", "provider_name",
            "request_count"}, ...]}`` ordered by request count descending then
            alias, for the executions that have any named gateway usage.
            Executions with none are absent from the mapping.
        """
        ids = [str(execution_id) for execution_id in execution_ids if execution_id]
        if not ids:
            return {}

        # The execution an attributed row belongs to: its own
        # flow_execution_id when set, else the execution id its runtime
        # session was created for.
        session_execution_id = case(
            (
                and_(
                    RuntimeSession.session_source_type == "flow_execution",
                    RuntimeSession.session_source_id.isnot(None),
                ),
                RuntimeSession.session_source_id,
            ),
            else_=None,
        )
        execution_key = func.coalesce(
            cast(ApiUsage.flow_execution_id, String), session_execution_id
        )

        rows = (
            db.query(
                execution_key.label("execution_id"),
                ApiUsage.model_alias.label("model_alias"),
                func.max(ApiUsage.provider_name).label("provider_name"),
                func.count(ApiUsage.id).label("request_count"),
            )
            .outerjoin(RuntimeSession, ApiUsage.runtime_session_id == RuntimeSession.id)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.model_alias.isnot(None),
                execution_key.in_(ids),
                exclude_replay_usage_condition(),
            )
            .group_by(execution_key, ApiUsage.model_alias)
            .order_by(
                execution_key,
                func.count(ApiUsage.id).desc(),
                ApiUsage.model_alias.asc(),
            )
            .all()
        )

        models_by_execution: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            models_by_execution.setdefault(str(row.execution_id), []).append(
                {
                    "model_alias": row.model_alias,
                    "provider_name": row.provider_name,
                    "request_count": int(row.request_count or 0),
                }
            )
        return models_by_execution

    def get_gateway_usage_for_execution(
        self,
        db: Session,
        execution_id: str,
    ) -> Dict[str, Any]:
        """Return explicit gateway usage totals for an execution when available.

        ``estimated_cost`` is deliberately NOT coalesced to ``0.0``: a NULL
        cost means "we could not price this", which is different from "this
        was free". When no request could be priced the cost stays ``None`` and
        callers render the token volume instead (see ``unpriced_tokens``).

        Args:
            db: Database session.
            execution_id: Owning flow execution id.

        Returns:
            Usage totals including ``estimated_cost`` (``None`` when nothing
            was priceable), ``cost_is_partial`` when priced and unpriced rows
            are mixed, and the unpriced request/token volume.
        """
        unpriced_condition = and_(
            ApiUsage.estimated_cost.is_(None),
            func.coalesce(ApiUsage.cost_source, "") != "subscription",
        )
        row = (
            db.query(
                func.count(ApiUsage.id).label("api_requests"),
                func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
                # Left un-coalesced on purpose: NULL means "unknown", not zero.
                func.sum(ApiUsage.estimated_cost).label("estimated_cost"),
                func.count(ApiUsage.estimated_cost).label("priced_requests"),
                func.count(ApiUsage.id)
                .filter(unpriced_condition)
                .label("unpriced_requests"),
                func.coalesce(
                    func.sum(ApiUsage.total_tokens).filter(unpriced_condition), 0
                ).label("unpriced_tokens"),
                func.coalesce(
                    func.sum(ApiUsage.prompt_tokens).filter(unpriced_condition), 0
                ).label("unpriced_prompt_tokens"),
                func.coalesce(
                    func.sum(ApiUsage.completion_tokens).filter(unpriced_condition), 0
                ).label("unpriced_completion_tokens"),
                *cache_split_columns(),
            )
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.flow_execution_id == execution_id,
                exclude_replay_usage_condition(),
            )
            .first()
        )
        if row is None:
            return {
                "api_requests": 0,
                "token_usage": {
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    **EMPTY_CACHE_SPLIT,
                },
                "estimated_cost": 0.0,
                "has_pricing": False,
                "cost_is_partial": False,
                "unpriced_requests": 0,
                "unpriced_tokens": 0,
                "unpriced_prompt_tokens": 0,
                "unpriced_completion_tokens": 0,
            }

        priced_requests = int(row.priced_requests or 0)
        unpriced_requests = int(row.unpriced_requests or 0)
        raw_cost = row.estimated_cost
        return {
            "api_requests": int(row.api_requests or 0),
            "token_usage": {
                "total_tokens": int(row.total_tokens or 0),
                "input_tokens": int(row.prompt_tokens or 0),
                "output_tokens": int(row.completion_tokens or 0),
                **cache_split_from_row(row),
            },
            "estimated_cost": (
                round(float(raw_cost), 6) if raw_cost is not None else None
            ),
            "has_pricing": priced_requests > 0,
            # True when a real cost exists but excludes unpriced traffic, so
            # callers can label the number as incomplete rather than final.
            "cost_is_partial": priced_requests > 0 and unpriced_requests > 0,
            "unpriced_requests": unpriced_requests,
            "unpriced_tokens": int(row.unpriced_tokens or 0),
            "unpriced_prompt_tokens": int(row.unpriced_prompt_tokens or 0),
            "unpriced_completion_tokens": int(row.unpriced_completion_tokens or 0),
        }

    def count_by_execution_timeframe(
        self,
        db: Session,
        execution: Any,
    ) -> int:
        """Count API requests made during execution timeframe."""
        from preloop.models import models

        explicit_count = (
            db.query(ApiUsage)
            .filter(ApiUsage.flow_execution_id == execution.id)
            .count()
        )
        if explicit_count:
            return explicit_count

        if not execution.start_time:
            return 0

        # Get the flow and its owner
        flow = db.query(models.Flow).filter(models.Flow.id == execution.flow_id).first()

        if not flow or not flow.account_id:
            return 0

        # Get the first user in the account (the one who owns the API key)
        account = (
            db.query(models.Account)
            .filter(models.Account.id == flow.account_id)
            .first()
        )

        if not account or not account.users:
            return 0

        # Get API usage for the execution timeframe (gateway traffic only).
        query = db.query(ApiUsage).filter(
            ApiUsage.user_id.in_([u.id for u in account.users]),
            ApiUsage.action_type == "model_gateway",
            ApiUsage.timestamp >= execution.start_time,
        )

        if execution.end_time:
            query = query.filter(ApiUsage.timestamp <= execution.end_time)

        return query.count()

    def get_gateway_spend(
        self,
        db: Session,
        *,
        account_id: str,
        start: datetime,
        flow_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        model_alias: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> float:
        """Sum estimated gateway spend for an account since a timestamp.

        Args:
            db: Database session.
            account_id: Owning account id.
            start: Inclusive lower bound on ``timestamp``.
            flow_id: Optional flow id filter.
            api_key_id: Optional API key id filter.
            runtime_principal_id: Optional runtime principal filter.
            model_alias: Optional gateway model alias filter.
            purpose: Optional ``meta_data.purpose`` filter.

        Returns:
            Total estimated gateway spend in USD (``0.0`` when none).
        """
        if purpose is not None and purpose not in GATEWAY_USAGE_PURPOSES:
            raise ValueError(f"Invalid gateway usage purpose: {purpose!r}")
        query = db.query(
            func.coalesce(func.sum(self.model.estimated_cost), 0.0)
        ).filter(
            self.model.action_type == "model_gateway",
            self.model.account_id == account_id,
            self.model.timestamp >= start,
        )
        if flow_id:
            query = query.filter(self.model.flow_id == flow_id)
        if api_key_id:
            query = query.filter(self.model.api_key_id == api_key_id)
        if runtime_principal_id:
            query = query.filter(
                self.model.runtime_principal_id == runtime_principal_id
            )
        if model_alias:
            query = query.filter(self.model.model_alias == model_alias)
        if purpose:
            query = query.filter(self.model.meta_data["purpose"].astext == purpose)
        else:
            # Generic (untargeted) spend reads reflect the account's real
            # agent traffic; replay-validation runs are excluded like in the
            # usage aggregations above. Purpose-targeted reads (e.g. the
            # optimizer's daily cap) are unaffected.
            query = query.filter(exclude_replay_usage_condition())
        return float(query.scalar() or 0.0)

    def list_gateway_rows_in_window(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        start: datetime,
        end: datetime,
        limit: int = 5000,
    ) -> List[ApiUsage]:
        """List model-gateway usage rows for an account within a time window.

        Returns the full ``ApiUsage`` rows (not aggregates) so deterministic
        analyzers can inspect per-row ``meta_data`` (e.g. ``tools_meta``),
        ``prompt_tokens``, ``estimated_cost`` and ``ai_model_id``. The window
        is half-open: ``start <= timestamp < end``.

        Args:
            db: Database session.
            account_id: Owning account id.
            start: Inclusive window start.
            end: Exclusive window end.
            limit: Maximum number of rows to return (newest first).

        Returns:
            Matching gateway usage rows ordered newest-first.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.action_type == "model_gateway",
                self.model.account_id == account_id,
                self.model.timestamp >= start,
                self.model.timestamp < end,
            )
            .order_by(self.model.timestamp.desc())
            .limit(limit)
            .all()
        )

    def list_session_request_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
        limit: int = 100,
        offset: int = 0,
        failed_only: bool = False,
        event_ids: Optional[List[str]] = None,
    ) -> List[ApiUsage]:
        """List per-request gateway usage rows for one runtime session.

        Returns full ``ApiUsage`` rows (one per gateway request) so the
        front-end timeline can be built from real per-request data instead of
        the sparse captured gateway events. Rows are ordered oldest-first so
        callers can present a stable chronological stream.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session whose requests to list.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip (pagination).
            failed_only: When True, restrict to rows with ``status_code >= 400``.
            event_ids: Optional explicit ``ApiUsage`` ids to restrict to.

        Returns:
            Matching gateway usage rows ordered oldest-first.
        """
        query = db.query(self.model).filter(
            self.model.action_type == "model_gateway",
            self.model.account_id == account_id,
            self.model.runtime_session_id == runtime_session_id,
        )
        if failed_only:
            query = query.filter(self.model.status_code >= 400)
        if event_ids is not None:
            query = query.filter(self.model.id.in_(event_ids))
        return (
            query.order_by(self.model.timestamp.asc(), self.model.id.asc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    # Hard cap on cache-rollup rows per session. Even a very long-lived agent
    # session stays well under this; the cap only guards against a pathological
    # session flooding the API pod's memory. Hitting it is logged because the
    # resulting summary silently under-counts the session.
    SESSION_CACHE_ROWS_LIMIT = 50_000

    def list_session_cache_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
    ) -> List[SimpleNamespace]:
        """List the cache-accounting columns for every request in a session.

        Deliberately column-projected rather than a full ORM load: the session
        cache rollup covers ALL requests (it must not change as the UI pages),
        and full rows carry ``meta_data`` with capped-but-still-large request
        and response bodies. Only ``meta_data['usage_details']`` is pulled from
        the JSONB, which is the sole part cache accounting reads.

        Rows are capped at :attr:`SESSION_CACHE_ROWS_LIMIT` as a memory guard;
        exceeding it logs a warning because the rollup then under-counts.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session to summarize.

        Returns:
            Lightweight namespaces shaped like ``ApiUsage`` rows for the fields
            :mod:`preloop.services.cache_accounting` consumes.
        """
        rows = (
            db.query(
                self.model.prompt_tokens,
                self.model.cache_read_tokens,
                self.model.cache_creation_tokens,
                self.model.model_alias,
                self.model.provider_name,
                self.model.usage_source,
                self.model.meta_data["usage_details"].label("usage_details"),
            )
            .filter(
                exclude_replay_usage_condition(),
                self.model.action_type == "model_gateway",
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
            )
            .limit(self.SESSION_CACHE_ROWS_LIMIT + 1)
            .all()
        )
        if len(rows) > self.SESSION_CACHE_ROWS_LIMIT:
            rows = rows[: self.SESSION_CACHE_ROWS_LIMIT]
            logger.warning(
                "Session cache rollup truncated at %d rows for runtime session "
                "%s (account %s); the cache summary under-counts this session.",
                self.SESSION_CACHE_ROWS_LIMIT,
                runtime_session_id,
                account_id,
            )
        return [
            SimpleNamespace(
                prompt_tokens=row.prompt_tokens,
                cache_read_tokens=row.cache_read_tokens,
                cache_creation_tokens=row.cache_creation_tokens,
                model_alias=row.model_alias,
                provider_name=row.provider_name,
                usage_source=row.usage_source,
                meta_data={"usage_details": row.usage_details}
                if row.usage_details is not None
                else None,
            )
            for row in rows
        ]

    def count_session_request_rows(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
        failed_only: bool = False,
        event_ids: Optional[List[str]] = None,
    ) -> int:
        """Count per-request gateway usage rows for one runtime session.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session whose requests to count.
            failed_only: When True, count only rows with ``status_code >= 400``.
            event_ids: Optional explicit ``ApiUsage`` ids to restrict to.

        Returns:
            Number of matching gateway usage rows.
        """
        query = db.query(func.count(self.model.id)).filter(
            self.model.action_type == "model_gateway",
            self.model.account_id == account_id,
            self.model.runtime_session_id == runtime_session_id,
        )
        if failed_only:
            query = query.filter(self.model.status_code >= 400)
        if event_ids is not None:
            query = query.filter(self.model.id.in_(event_ids))
        return int(query.scalar() or 0)

    def get_runtime_principal_gateway_averages(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_principal_id: str,
        start: datetime,
        end: Optional[datetime] = None,
        exclude_runtime_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate per-request gateway usage averages for one principal.

        Used to capture a baseline before an optimization action is applied
        and to measure the realized outcome afterwards.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_principal_id: Runtime principal to aggregate for.
            start: Inclusive window start.
            end: Optional inclusive window end.
            exclude_runtime_session_id: Optional session to exclude (e.g. the
                session the action was applied from).

        Returns:
            Dict with ``requests``, ``avg_total_tokens_per_request``, and
            ``avg_cost_per_request``; zeros when no requests matched.
        """
        query = db.query(
            func.count(ApiUsage.id).label("requests"),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label("total_cost"),
        ).filter(
            ApiUsage.action_type == "model_gateway",
            ApiUsage.account_id == account_id,
            exclude_replay_usage_condition(),
            ApiUsage.runtime_principal_id == runtime_principal_id,
            ApiUsage.timestamp >= start,
        )
        if end is not None:
            query = query.filter(ApiUsage.timestamp <= end)
        if exclude_runtime_session_id:
            query = query.filter(
                or_(
                    ApiUsage.runtime_session_id.is_(None),
                    ApiUsage.runtime_session_id != exclude_runtime_session_id,
                )
            )
        row = query.first()
        requests = int(row.requests or 0) if row else 0
        total_tokens = int(row.total_tokens or 0) if row else 0
        total_cost = float(row.total_cost or 0.0) if row else 0.0
        return {
            "requests": requests,
            "avg_total_tokens_per_request": (
                round(total_tokens / requests, 2) if requests else 0.0
            ),
            "avg_cost_per_request": (
                round(total_cost / requests, 6) if requests else 0.0
            ),
        }

    def get_dashboard_usage_stats(
        self, db: Session, *, account_id: str, since: datetime
    ) -> Dict[str, Any]:
        """Aggregate high-level API usage metrics for dashboard telemetry."""
        row = (
            db.query(
                func.sum(self.model.estimated_cost).label("estimated_cost"),
                func.count(self.model.id).label("total_calls"),
                func.sum(case((self.model.status_code < 400, 1), else_=0)).label(
                    "success_calls"
                ),
            )
            .filter(
                self.model.account_id == account_id,
                self.model.timestamp >= since,
            )
            .first()
        )
        return {
            "estimated_cost": float(row.estimated_cost or 0.0) if row else 0.0,
            "total_calls": int(row.total_calls or 0) if row else 0,
            "success_calls": int(row.success_calls or 0) if row else 0,
        }

    def get_gateway_usage_by_api_key(
        self,
        db: Session,
        *,
        api_key_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get recent gateway usage for an API key."""
        rows = (
            db.query(
                ApiUsage.ai_model_id,
                ApiUsage.model_alias,
                ApiUsage.provider_name,
                ApiUsage.runtime_principal_name,
                ApiUsage.runtime_principal_id,
                ApiUsage.status_code,
                ApiUsage.prompt_tokens,
                ApiUsage.completion_tokens,
                ApiUsage.total_tokens,
                ApiUsage.estimated_cost,
                ApiUsage.timestamp,
            )
            .filter(
                ApiUsage.api_key_id == api_key_id,
                ApiUsage.action_type == "model_gateway",
            )
            .order_by(ApiUsage.timestamp.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "ai_model_id": str(row.ai_model_id) if row.ai_model_id else None,
                "model_alias": row.model_alias,
                "provider_name": row.provider_name,
                "agent_name": row.runtime_principal_name,
                "agent_id": row.runtime_principal_id,
                "status_code": row.status_code,
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "estimated_cost": float(row.estimated_cost or 0.0),
                "timestamp": row.timestamp,
            }
            for row in rows
        ]

    def get_last_model_call_timestamp(
        self, db: Session, api_key_id: str
    ) -> Optional[datetime]:
        """Get the most recent timestamp of a model gateway call by this API key."""
        return (
            db.query(func.max(ApiUsage.timestamp))
            .filter(
                ApiUsage.api_key_id == api_key_id,
                ApiUsage.action_type == "model_gateway",
            )
            .scalar()
        )

    def get_recent_model_calls_count(
        self, db: Session, api_key_id: str, recent_start: datetime
    ) -> int:
        """Get the count of model gateway calls made by this API key since recent_start."""
        return (
            db.query(func.count(ApiUsage.id))
            .filter(
                ApiUsage.api_key_id == api_key_id,
                ApiUsage.action_type == "model_gateway",
                ApiUsage.timestamp >= recent_start,
            )
            .scalar()
            or 0
        )

    def get_accounting_health_counters(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        start: datetime,
    ) -> Dict[str, int]:
        """Return raw counters for the gateway accounting self-check.

        One aggregation query over the account's model-gateway rows since
        ``start`` (replay-validation traffic excluded), using conditional
        sums so the health checks can be computed without extra round-trips.

        Args:
            db: Database session.
            account_id: Owning account id.
            start: Inclusive window start.

        Returns:
            Dict of integer counters:
            - ``total_rows``: all gateway rows in the window.
            - ``success_rows``: rows with ``status_code < 400``.
            - ``streaming_rows``: successful rows whose
              ``meta_data.endpoint_kind`` ends in ``_stream``.
            - ``streaming_rows_with_tokens``: streaming rows with
              ``total_tokens > 0``.
            - ``priceable_rows``: successful rows with ``total_tokens > 0``.
            - ``priced_rows``: priceable rows with a stored cost (or
              subscription-covered, which counts as priced).
            - ``unpriced_source_rows``: rows tagged ``cost_source='unpriced'``.
            - ``usage_source_rows``: successful rows with a ``usage_source``.
            - ``provider_usage_rows``: successful rows with
              ``usage_source='provider'``.
        """
        endpoint_kind = ApiUsage.meta_data["endpoint_kind"].astext
        is_success = ApiUsage.status_code < 400
        is_streaming = and_(is_success, endpoint_kind.like("%\\_stream", escape="\\"))
        is_priceable = and_(is_success, ApiUsage.total_tokens > 0)
        is_priced = or_(
            ApiUsage.estimated_cost.isnot(None),
            ApiUsage.cost_source == "subscription",
        )

        def _count_when(condition: Any) -> Any:
            return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

        row = (
            db.query(
                func.count(ApiUsage.id).label("total_rows"),
                _count_when(is_success).label("success_rows"),
                _count_when(is_streaming).label("streaming_rows"),
                _count_when(and_(is_streaming, ApiUsage.total_tokens > 0)).label(
                    "streaming_rows_with_tokens"
                ),
                _count_when(is_priceable).label("priceable_rows"),
                _count_when(and_(is_priceable, is_priced)).label("priced_rows"),
                _count_when(ApiUsage.cost_source == "unpriced").label(
                    "unpriced_source_rows"
                ),
                _count_when(and_(is_success, ApiUsage.usage_source.isnot(None))).label(
                    "usage_source_rows"
                ),
                _count_when(
                    and_(is_success, ApiUsage.usage_source == "provider")
                ).label("provider_usage_rows"),
            )
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                exclude_replay_usage_condition(),
                ApiUsage.timestamp >= start,
            )
            .one_or_none()
        )
        if row is None:
            return {
                "total_rows": 0,
                "success_rows": 0,
                "streaming_rows": 0,
                "streaming_rows_with_tokens": 0,
                "priceable_rows": 0,
                "priced_rows": 0,
                "unpriced_source_rows": 0,
                "usage_source_rows": 0,
                "provider_usage_rows": 0,
            }
        return {
            "total_rows": int(row.total_rows or 0),
            "success_rows": int(row.success_rows or 0),
            "streaming_rows": int(row.streaming_rows or 0),
            "streaming_rows_with_tokens": int(row.streaming_rows_with_tokens or 0),
            "priceable_rows": int(row.priceable_rows or 0),
            "priced_rows": int(row.priced_rows or 0),
            "unpriced_source_rows": int(row.unpriced_source_rows or 0),
            "usage_source_rows": int(row.usage_source_rows or 0),
            "provider_usage_rows": int(row.provider_usage_rows or 0),
        }

    def get_subscription_absorbed_cost(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        start: datetime,
        end: datetime,
    ) -> float:
        """Sum API-equivalent cost absorbed by subscriptions in a window.

        Subscription-covered gateway calls are logged with
        ``cost_source='subscription'`` and carry the cost the same call would
        have incurred on pay-per-use API pricing in
        ``meta_data.api_equivalent_cost``. Summing that field measures the
        spend the account's subscriptions absorbed. Replay-validation traffic
        is excluded like the other user-facing aggregations.

        Args:
            db: Database session.
            account_id: Owning account id.
            start: Inclusive window start.
            end: Exclusive window end.

        Returns:
            Total absorbed cost in USD (``0.0`` when none).
        """
        absorbed = func.coalesce(
            func.sum(cast(ApiUsage.meta_data["api_equivalent_cost"].astext, Float)),
            0.0,
        )
        value = (
            db.query(absorbed)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                ApiUsage.cost_source == "subscription",
                ApiUsage.meta_data["api_equivalent_cost"].astext.isnot(None),
                exclude_replay_usage_condition(),
                ApiUsage.timestamp >= start,
                ApiUsage.timestamp < end,
            )
            .scalar()
        )
        return float(value or 0.0)

    # ------------------------------------------------------------------
    # Imported (observed) usage — spend the gateway cannot see (issue #123)
    # ------------------------------------------------------------------
    #
    # Imported rows use ``action_type='imported_usage'`` so every gateway
    # aggregation above (summaries, budgets, spend caps, accounting health),
    # which filters on ``action_type == 'model_gateway'``, is structurally
    # unable to mix imported spend into gateway-metered spend. Budget-bucket
    # accumulation is deliberately NOT performed for imported rows.

    IMPORTED_USAGE_ACTION_TYPE = "imported_usage"

    #: Unique partial index enforcing per-account fingerprint dedupe in the DB.
    IMPORTED_FINGERPRINT_INDEX = "ix_api_usage_imported_fingerprint_uniq"

    def _imported_fingerprint_exists(
        self, db: Session, *, account_id: str, import_fingerprint: str
    ) -> bool:
        """Return True when the account already has a row with this fingerprint.

        This is a fast-path check only; the authoritative guard is the
        unique partial index ``ix_api_usage_imported_fingerprint_uniq``,
        which closes the check-then-insert race under concurrent imports.
        """
        exists = (
            db.query(ApiUsage.id)
            .filter(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == self.IMPORTED_USAGE_ACTION_TYPE,
                ApiUsage.meta_data["import_fingerprint"].astext == import_fingerprint,
            )
            .first()
        )
        return exists is not None

    def get_imported_row_by_fingerprint(
        self, db: Session, *, account_id: str, import_fingerprint: str
    ) -> Optional[ApiUsage]:
        """Return the account's imported row with this fingerprint, if any.

        Used by the push-ingest path to flag conflicting replays: the
        stored row's ``meta_data.ingest_content_hash`` is compared with the
        incoming record's payload hash (first write wins either way).
        """
        return (
            db.query(ApiUsage)
            .filter(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == self.IMPORTED_USAGE_ACTION_TYPE,
                ApiUsage.meta_data["import_fingerprint"].astext == import_fingerprint,
            )
            .first()
        )

    def get_imported_rows_by_fingerprints(
        self,
        db: Session,
        *,
        account_id: str,
        fingerprints: Sequence[str],
    ) -> Dict[str, ApiUsage]:
        """Return imported rows keyed by fingerprint for a batch lookup.

        Used by push-ingest to replace per-record existence SELECTs with one
        ``IN`` query. Missing fingerprints are omitted from the result.
        """
        unique = list(dict.fromkeys(fp for fp in fingerprints if fp))
        if not unique:
            return {}
        rows = (
            db.query(ApiUsage)
            .filter(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == self.IMPORTED_USAGE_ACTION_TYPE,
                ApiUsage.meta_data["import_fingerprint"].astext.in_(unique),
            )
            .all()
        )
        found: Dict[str, ApiUsage] = {}
        for row in rows:
            fingerprint = (row.meta_data or {}).get("import_fingerprint")
            if isinstance(fingerprint, str):
                found[fingerprint] = row
        return found

    def log_imported_usage_event(
        self,
        db: Session,
        *,
        account_id: str,
        user_id: Optional[str] = None,
        timestamp: datetime,
        model_alias: Optional[str],
        source: str,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        cache_creation_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        cost_source: Optional[str] = None,
        cost_basis: Optional[str] = None,
        conversation_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        message_count: Optional[int] = None,
        tool_call_count: Optional[int] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        runtime_session_id: Optional[Any] = None,
        import_fingerprint: Optional[str] = None,
        meta_data: Optional[Dict[str, Any]] = None,
        endpoint: Optional[str] = None,
        skip_fingerprint_lookup: bool = False,
        commit: bool = True,
    ) -> Optional[ApiUsage]:
        """Record one imported usage event in the cost ledger.

        Args:
            db: Database session.
            account_id: Owning account id.
            user_id: Importing user's id, for audit.
            timestamp: When the usage occurred at the source vendor.
            model_alias: Source-reported model name (e.g. ``composer``);
                NULL for lifecycle events pushed without one.
            source: Origin label (e.g. ``cursor``); stored in
                ``meta_data.import_source``.
            prompt_tokens: Input tokens reported by the source.
            completion_tokens: Output tokens reported by the source.
            total_tokens: Total tokens; derived from prompt+completion when
                absent.
            cache_read_tokens: Cache-read tokens reported by the source.
            cache_creation_tokens: Cache-write tokens reported by the source.
            cost_usd: Amount stored in ``estimated_cost``: the source
                vendor's charge, or a catalog estimate for hook-derived
                records that carry tokens but no billed amount.
            cost_source: Provenance of ``cost_usd``. Defaults to
                ``imported`` when an amount is present (the vendor charged
                it); usage ingest passes the pricing service's source
                (``catalog``) for estimated amounts.
            cost_basis: ``estimated`` or ``reconciled``; reconciled rows
                supersede estimated rows with the same (account, source,
                conversation_id) in imported-cost sums. NULL rows never
                participate in supersession.
            conversation_id: Source-side conversation the record belongs to.
            parent_conversation_id: Conversation it was spawned from, for
                worker->parent rollup.
            message_count: Conversation message count (growth tripwire).
            tool_call_count: Conversation tool-call count (growth tripwire).
            runtime_principal_type: Managed-agent principal type attribution.
            runtime_principal_id: Managed-agent principal id attribution.
            runtime_principal_name: Managed-agent display name attribution.
            runtime_session_id: Runtime session the record's conversation
                was registered as, so the row shows up in that session's
                usage like gateway rows do.
            import_fingerprint: Stable dedupe key; when a row with the same
                fingerprint already exists for the account, the event is
                skipped and ``None`` is returned (re-importing the same CSV
                must not double-count spend). Enforced by the unique partial
                index ``ix_api_usage_imported_fingerprint_uniq``, so two
                concurrent imports of the same event cannot both land.
            meta_data: Extra keys merged into the stored ``meta_data``.
            endpoint: Ledger endpoint label. Defaults to
                ``/usage/import/{source}`` (CSV/JSON import). Push-ingest
                passes ``/usage/ingest/{source}``.
            skip_fingerprint_lookup: When True, skip the fast-path SELECT
                (the caller already bulk-loaded existing fingerprints). The
                unique index remains the concurrent-insert guard.
            commit: When False, flush only so callers can batch commits.

        Returns:
            The created row, or ``None`` when skipped as a duplicate.
        """
        if (
            import_fingerprint
            and not skip_fingerprint_lookup
            and self._imported_fingerprint_exists(
                db, account_id=account_id, import_fingerprint=import_fingerprint
            )
        ):
            return None

        if total_tokens is None and (
            prompt_tokens is not None or completion_tokens is not None
        ):
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)

        merged_meta: Dict[str, Any] = dict(meta_data or {})
        merged_meta["import_source"] = source
        if import_fingerprint:
            merged_meta["import_fingerprint"] = import_fingerprint

        db_obj = ApiUsage(
            user_id=user_id,
            account_id=account_id,
            endpoint=endpoint or f"/usage/import/{source}",
            method="POST",
            status_code=200,
            duration=0.0,
            action_type=self.IMPORTED_USAGE_ACTION_TYPE,
            model_alias=model_alias,
            provider_name=source,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            estimated_cost=cost_usd,
            currency="USD" if cost_usd is not None else None,
            cost_source=(cost_source or ("imported" if cost_usd is not None else None)),
            cost_basis=cost_basis,
            conversation_id=conversation_id,
            parent_conversation_id=parent_conversation_id,
            message_count=message_count,
            tool_call_count=tool_call_count,
            usage_source="imported",
            runtime_principal_type=runtime_principal_type,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=runtime_principal_name,
            runtime_session_id=runtime_session_id,
            meta_data=merged_meta,
            timestamp=timestamp,
        )
        # Insert inside a savepoint so a unique-index violation (a concurrent
        # import of the same event committed between the fast-path check and
        # this insert) skips just this row and leaves the batch usable.
        try:
            with db.begin_nested():
                db.add(db_obj)
                db.flush()
        except IntegrityError as exc:
            if self.IMPORTED_FINGERPRINT_INDEX not in str(exc.orig):
                raise
            return None
        if commit:
            db.commit()
            db.refresh(db_obj)
        return db_obj

    def _imported_cost_sum(self, *, start_date: datetime, end_date: datetime):
        """SUM of imported cost with reconciled-over-estimated precedence.

        A ``cost_basis='reconciled'`` row (billing export) supersedes ALL
        ``cost_basis='estimated'`` rows (hook/transcript-derived) with the
        same (account, provider_name, conversation_id) **in the queried
        window**: superseded rows contribute $0, so the two bases are never
        summed for one scope. The EXISTS is bounded to
        ``start_date <= timestamp < end_date`` so a reconciled row outside
        the window cannot zero in-window estimates while contributing $0
        itself. Rows without a conversation_id or with a NULL cost_basis
        (legacy/CSV imports) never participate.
        """
        reconciled = aliased(ApiUsage)
        superseded = and_(
            ApiUsage.cost_basis == "estimated",
            ApiUsage.conversation_id.isnot(None),
            select(reconciled.id)
            .where(
                reconciled.account_id == ApiUsage.account_id,
                reconciled.action_type == self.IMPORTED_USAGE_ACTION_TYPE,
                reconciled.cost_basis == "reconciled",
                reconciled.provider_name == ApiUsage.provider_name,
                reconciled.conversation_id == ApiUsage.conversation_id,
                reconciled.timestamp >= start_date,
                reconciled.timestamp < end_date,
            )
            .exists(),
        )
        return func.coalesce(
            func.sum(
                case(
                    (superseded, 0.0),
                    else_=func.coalesce(ApiUsage.estimated_cost, 0.0),
                )
            ),
            0.0,
        )

    def get_imported_usage_summary(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        runtime_principal_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate imported usage totals for an account in a window.

        ``imported_cost`` applies reconciled-over-estimated precedence (see
        :meth:`_imported_cost_sum`); event/token totals count all rows.
        Supersession is bounded to this same window.

        Args:
            db: Database session.
            account_id: Account whose imported usage is aggregated.
            start_date: Inclusive lower bound on event timestamp.
            end_date: Exclusive upper bound on event timestamp.
            runtime_principal_id: Restrict to one managed-agent principal.
            source: Restrict to one import source label (e.g. ``cursor``).

        Returns:
            Dict with ``event_count``, ``total_tokens``, ``imported_cost``.
        """
        query = db.query(
            func.count(ApiUsage.id).label("event_count"),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
            self._imported_cost_sum(start_date=start_date, end_date=end_date).label(
                "imported_cost"
            ),
        ).filter(
            ApiUsage.action_type == self.IMPORTED_USAGE_ACTION_TYPE,
            ApiUsage.account_id == account_id,
            ApiUsage.timestamp >= start_date,
            ApiUsage.timestamp < end_date,
        )
        if runtime_principal_id:
            query = query.filter(ApiUsage.runtime_principal_id == runtime_principal_id)
        if source:
            query = query.filter(ApiUsage.meta_data["import_source"].astext == source)
        row = query.one()
        return {
            "event_count": int(row.event_count or 0),
            "total_tokens": int(row.total_tokens or 0),
            "imported_cost": float(row.imported_cost or 0.0),
        }

    def get_imported_usage_by_model(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        runtime_principal_id: Optional[str] = None,
        source: Optional[str] = None,
        limit: Optional[int] = 20,
    ) -> List[Dict[str, Any]]:
        """Group imported usage by source-reported model.

        ``imported_cost`` applies reconciled-over-estimated precedence (see
        :meth:`_imported_cost_sum`); event/token totals count all rows.
        Supersession is bounded to this same window.

        Args:
            db: Database session.
            account_id: Account whose imported usage is aggregated.
            start_date: Inclusive lower bound on event timestamp.
            end_date: Exclusive upper bound on event timestamp.
            runtime_principal_id: Restrict to one managed-agent principal.
            source: Restrict to one import source label.
            limit: Maximum grouped rows, ordered by event count descending.

        Returns:
            One dict per (model, source) group with counts, tokens, cost.
        """
        import_source = ApiUsage.meta_data["import_source"].astext
        query = db.query(
            ApiUsage.model_alias,
            import_source.label("source"),
            func.count(ApiUsage.id).label("request_count"),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
            self._imported_cost_sum(start_date=start_date, end_date=end_date).label(
                "imported_cost"
            ),
            func.max(ApiUsage.timestamp).label("last_event_at"),
        ).filter(
            ApiUsage.action_type == self.IMPORTED_USAGE_ACTION_TYPE,
            ApiUsage.account_id == account_id,
            ApiUsage.timestamp >= start_date,
            ApiUsage.timestamp < end_date,
        )
        if runtime_principal_id:
            query = query.filter(ApiUsage.runtime_principal_id == runtime_principal_id)
        if source:
            query = query.filter(import_source == source)
        query = query.group_by(ApiUsage.model_alias, import_source).order_by(
            func.count(ApiUsage.id).desc()
        )
        if limit is not None:
            query = query.limit(limit)
        return [
            {
                "model_alias": row.model_alias,
                "source": row.source,
                "request_count": int(row.request_count or 0),
                "total_tokens": int(row.total_tokens or 0),
                "imported_cost": float(row.imported_cost or 0.0),
                "last_event_at": row.last_event_at,
            }
            for row in query.all()
        ]

    def get_imported_usage_by_conversation(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: datetime,
        end_date: datetime,
        runtime_principal_id: Optional[str] = None,
        source: Optional[str] = None,
        limit: Optional[int] = 200,
    ) -> List[Dict[str, Any]]:
        """Group imported usage by source-side conversation for thread rollup.

        Powers the console's per-conversation rollup: rows sharing a
        ``conversation_id`` collapse to one entry, and the caller nests
        entries whose ``parent_conversation_id`` matches another entry
        (subagent workers billed on separate conversations under their
        parent thread).

        Honesty contract (design-partner rail):
          * ``estimated_cost`` sums ONLY ``cost_basis='estimated'`` rows and
            ``reconciled_cost`` sums ONLY ``cost_basis='reconciled'`` rows.
            The two bases are never combined into one number here; display
            layers must keep them separate too. No supersession is applied:
            both bases are surfaced side by side per conversation.
          * A sum with no contributing rows is ``None`` ("not reported"),
            never coerced to 0/0.0. Same for ``total_tokens``.
          * Rows with a NULL ``cost_basis`` cannot occur with a
            conversation_id today (only push-ingest writes conversation ids
            and it always sets a basis); defensively, such cost would be
            excluded from both sums rather than silently classified.

        Rows without a ``conversation_id`` (CSV/JSON batch imports) are not
        part of any conversation and are excluded.

        Args:
            db: Database session.
            account_id: Account whose imported usage is aggregated.
            start_date: Inclusive lower bound on event timestamp.
            end_date: Exclusive upper bound on event timestamp.
            runtime_principal_id: Restrict to one managed-agent principal.
            source: Restrict to one import source label.
            limit: Maximum grouped rows, newest activity first.

        Returns:
            One dict per (conversation_id, source) group, ordered by last
            event descending. ``parent_conversation_id`` is the group's
            maximum non-null value (records of one conversation are expected
            to agree on their parent; MAX is a deterministic tie-break).
        """
        import_source = ApiUsage.meta_data["import_source"].astext
        estimated_sum = func.sum(
            case(
                (ApiUsage.cost_basis == "estimated", ApiUsage.estimated_cost),
                else_=None,
            )
        )
        reconciled_sum = func.sum(
            case(
                (ApiUsage.cost_basis == "reconciled", ApiUsage.estimated_cost),
                else_=None,
            )
        )
        query = db.query(
            ApiUsage.conversation_id,
            func.max(ApiUsage.parent_conversation_id).label("parent_conversation_id"),
            import_source.label("source"),
            func.count(ApiUsage.id).label("event_count"),
            # No COALESCE on purpose: NULL means "not reported", not zero.
            func.sum(ApiUsage.total_tokens).label("total_tokens"),
            estimated_sum.label("estimated_cost"),
            reconciled_sum.label("reconciled_cost"),
            func.max(ApiUsage.timestamp).label("last_event_at"),
        ).filter(
            ApiUsage.action_type == self.IMPORTED_USAGE_ACTION_TYPE,
            ApiUsage.account_id == account_id,
            ApiUsage.conversation_id.isnot(None),
            ApiUsage.timestamp >= start_date,
            ApiUsage.timestamp < end_date,
        )
        if runtime_principal_id:
            query = query.filter(ApiUsage.runtime_principal_id == runtime_principal_id)
        if source:
            query = query.filter(import_source == source)
        query = query.group_by(ApiUsage.conversation_id, import_source).order_by(
            func.max(ApiUsage.timestamp).desc()
        )
        if limit is not None:
            query = query.limit(limit)
        return [
            {
                "conversation_id": row.conversation_id,
                "parent_conversation_id": row.parent_conversation_id,
                "source": row.source,
                "event_count": int(row.event_count or 0),
                "total_tokens": (
                    int(row.total_tokens) if row.total_tokens is not None else None
                ),
                "estimated_cost": (
                    float(row.estimated_cost)
                    if row.estimated_cost is not None
                    else None
                ),
                "reconciled_cost": (
                    float(row.reconciled_cost)
                    if row.reconciled_cost is not None
                    else None
                ),
                "last_event_at": row.last_event_at,
            }
            for row in query.all()
        ]


crud_api_usage = CRUDApiUsage(ApiUsage)
