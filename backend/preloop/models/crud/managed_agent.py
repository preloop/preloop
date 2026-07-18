"""CRUD operations for durable managed-agent registry entries."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import and_, case, func, or_, tuple_
from sqlalchemy.orm import Session

from ..models.api_usage import ApiUsage
from ..models.managed_agent import ManagedAgent
from ..models.runtime_session import RuntimeSession
from ..models.user import User
from .base import CRUDBase

MANAGED_AGENT_ACTIVE_WINDOW = timedelta(minutes=10)
MANAGED_AGENT_RECENT_WINDOW = timedelta(hours=24)


def normalize_managed_agent_kind(session_source_type: Optional[str]) -> str:
    """Normalize one durable agent kind from a runtime source type."""
    normalized = str(session_source_type or "").strip().lower().replace(" ", "_")
    return normalized or "external_agent"


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
        "estimated_cost": 0.0,
        "latest_model_alias": None,
        "latest_provider_name": None,
        "last_request_at": None,
    }


def _usage_aggregates_for_principals(
    db: Session,
    *,
    account_id: str,
    principals: list[tuple[str, str]],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return principal-scoped usage totals for many agents in few queries.

    Args:
        db: Active database session.
        account_id: Account that owns the usage and sessions.
        principals: ``(runtime_principal_type, runtime_principal_id)`` pairs.
        start_date: Optional inclusive lower bound for sessions and usage.
        end_date: Optional inclusive upper bound for sessions and usage.

    Returns:
        Mapping from principal tuple to the same aggregate shape produced by
        :func:`_usage_aggregate_for_principal`.
    """
    unique_principals = list(dict.fromkeys(principals))
    if not unique_principals:
        return {}

    results = {principal: _empty_usage_aggregate() for principal in unique_principals}
    principal_filter = tuple_(
        RuntimeSession.runtime_principal_type,
        RuntimeSession.runtime_principal_id,
    ).in_(unique_principals)

    session_count_q = (
        db.query(
            RuntimeSession.runtime_principal_type,
            RuntimeSession.runtime_principal_id,
            func.count(RuntimeSession.id).label("session_count"),
        )
        .filter(
            RuntimeSession.account_id == account_id,
            principal_filter,
        )
        .group_by(
            RuntimeSession.runtime_principal_type,
            RuntimeSession.runtime_principal_id,
        )
    )
    if start_date:
        session_count_q = session_count_q.filter(
            RuntimeSession.started_at >= start_date
        )
    if end_date:
        session_count_q = session_count_q.filter(RuntimeSession.started_at <= end_date)
    for row in session_count_q.all():
        key = (row.runtime_principal_type, row.runtime_principal_id)
        if key in results:
            results[key]["session_count"] = int(row.session_count or 0)

    usage_principal_filter = tuple_(
        ApiUsage.runtime_principal_type,
        ApiUsage.runtime_principal_id,
    ).in_(unique_principals)
    usage_q = (
        db.query(
            ApiUsage.runtime_principal_type,
            ApiUsage.runtime_principal_id,
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
        )
        .filter(
            ApiUsage.account_id == account_id,
            ApiUsage.action_type == "model_gateway",
            usage_principal_filter,
        )
        .group_by(
            ApiUsage.runtime_principal_type,
            ApiUsage.runtime_principal_id,
        )
    )
    if start_date:
        usage_q = usage_q.filter(ApiUsage.timestamp >= start_date)
    if end_date:
        usage_q = usage_q.filter(ApiUsage.timestamp <= end_date)
    for row in usage_q.all():
        key = (row.runtime_principal_type, row.runtime_principal_id)
        if key not in results:
            continue
        results[key].update(
            {
                "total_requests": int(row.request_count or 0),
                "successful_requests": int(row.success_count or 0),
                "failed_requests": int(row.error_count or 0),
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "estimated_cost": float(row.estimated_cost or 0.0),
            }
        )

    latest_base = db.query(
        ApiUsage.runtime_principal_type,
        ApiUsage.runtime_principal_id,
        ApiUsage.model_alias.label("latest_model_alias"),
        ApiUsage.provider_name.label("latest_provider_name"),
        ApiUsage.timestamp.label("last_request_at"),
        func.row_number()
        .over(
            partition_by=(
                ApiUsage.runtime_principal_type,
                ApiUsage.runtime_principal_id,
            ),
            order_by=(ApiUsage.timestamp.desc(), ApiUsage.id.desc()),
        )
        .label("rn"),
    ).filter(
        ApiUsage.account_id == account_id,
        ApiUsage.action_type == "model_gateway",
        usage_principal_filter,
    )
    if start_date:
        latest_base = latest_base.filter(ApiUsage.timestamp >= start_date)
    if end_date:
        latest_base = latest_base.filter(ApiUsage.timestamp <= end_date)
    latest_subq = latest_base.subquery()
    latest_rows = db.query(latest_subq).filter(latest_subq.c.rn == 1).all()
    for row in latest_rows:
        key = (row.runtime_principal_type, row.runtime_principal_id)
        if key not in results:
            continue
        results[key]["latest_model_alias"] = row.latest_model_alias
        results[key]["latest_provider_name"] = row.latest_provider_name
        results[key]["last_request_at"] = row.last_request_at

    return results


def _usage_aggregate_for_principal(
    db: Session,
    *,
    account_id: str,
    principal_type: str,
    principal_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return principal-scoped usage totals across all runtime sessions."""
    aggregates = _usage_aggregates_for_principals(
        db,
        account_id=account_id,
        principals=[(principal_type, principal_id)],
        start_date=start_date,
        end_date=end_date,
    )
    return aggregates.get((principal_type, principal_id), _empty_usage_aggregate())


class CRUDManagedAgent(CRUDBase[ManagedAgent]):
    """CRUD helpers for account-scoped managed-agent registry entries."""

    def get_by_source(
        self,
        db: Session,
        *,
        account_id: str,
        session_source_type: str,
        session_source_id: str,
    ) -> Optional[ManagedAgent]:
        """Look up one agent by its durable source identity."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.session_source_type == session_source_type,
                self.model.session_source_id == session_source_id,
            )
            .first()
        )

    def get_for_account(
        self, db: Session, *, account_id: str, agent_id: str
    ) -> Optional[ManagedAgent]:
        """Return one managed agent scoped to the given account."""
        return (
            db.query(self.model)
            .filter(self.model.account_id == account_id, self.model.id == agent_id)
            .first()
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
        commit: bool = False,
    ) -> Optional[ManagedAgent]:
        """Update last-seen timestamp for one durable managed agent."""
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
        commit: bool = True,
    ) -> Optional[ManagedAgent]:
        """Update ownership and lifecycle controls for one managed agent."""
        db_obj = self.get_for_account(db, account_id=account_id, agent_id=agent_id)
        if db_obj is None:
            return None
        now = _utc_now()
        if set_owner:
            db_obj.owner_user_id = owner_user_id
        if set_display_name and display_name is not None:
            db_obj.display_name = display_name.strip()
        if set_tags and tags is not None:
            db_obj.tags = tags
        if lifecycle_state is not None:
            db_obj.lifecycle_state = lifecycle_state
            db_obj.lifecycle_reason = lifecycle_reason
            db_obj.lifecycle_updated_at = now
            if lifecycle_state in {"suspended", "decommissioned"}:
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
    ) -> ManagedAgent:
        """Create or update one registry entry from a runtime-session token flow.

        ``owner_user_id`` (the enrolling user) is set on create and backfilled on
        update only when the agent has no owner yet, so a manually assigned owner
        is never overwritten. This owner drives per-user cost attribution and
        per-user budgets.
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
            db_obj = ManagedAgent(
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                agent_kind=normalize_managed_agent_kind(session_source_type),
                session_source_type=session_source_type,
                session_source_id=session_source_id,
                session_reference=session_reference,
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
        db_obj.agent_kind = normalize_managed_agent_kind(session_source_type)
        db_obj.display_name = display_name
        db_obj.enrolled_via = enrolled_via
        db_obj.last_seen_at = observed_at
        if session_reference is not None:
            db_obj.session_reference = session_reference
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
        commit: bool = True,
    ) -> ManagedAgent:
        """Register a custom managed agent the discovery CLI cannot find.

        Custom agents never connect through a runtime session, so they are
        given a generated ``session_source_id`` (``custom_<token>``) under the
        reserved ``custom`` source type. Because the id is random it can never
        collide with the real local-config source ids that ``preloop agents
        discover`` keys off, so a later discovery run will not be deduped
        against this row.

        Args:
            db: Active database session.
            account_id: Owning account identifier.
            display_name: Operator-facing name for the agent.
            description: Optional free-form description; stored under ``tags``.
            commit: Whether to commit the transaction.

        Returns:
            The newly created ManagedAgent row.
        """
        now = _utc_now()
        normalized_name = display_name.strip()
        session_source_id = f"custom_{secrets.token_urlsafe(16)}"
        tags: dict[str, str] = {}
        if description and description.strip():
            tags["description"] = description.strip()
        db_obj = ManagedAgent(
            account_id=account_id,
            runtime_session_id=None,
            agent_kind=normalize_managed_agent_kind("custom"),
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

        rows = (
            base_query.outerjoin(
                ApiUsage,
                and_(
                    ApiUsage.runtime_session_id == self.model.runtime_session_id,
                    ApiUsage.action_type == "model_gateway",
                ),
            )
            .with_entities(
                self.model.id,
                self.model.runtime_session_id,
                self.model.owner_user_id,
                self.model.agent_kind,
                self.model.display_name,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.enrolled_via,
                self.model.managed_mcp_servers,
                self.model.lifecycle_state,
                self.model.lifecycle_reason,
                self.model.lifecycle_updated_at,
                self.model.last_seen_at,
                self.model.tags,
                User.username.label("owner_username"),
                User.email.label("owner_email"),
                RuntimeSession.started_at,
                RuntimeSession.last_activity_at,
                RuntimeSession.ended_at,
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
                self.model.agent_kind,
                self.model.display_name,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.enrolled_via,
                self.model.managed_mcp_servers,
                self.model.lifecycle_state,
                self.model.lifecycle_reason,
                self.model.lifecycle_updated_at,
                self.model.last_seen_at,
                self.model.tags,
                User.username,
                User.email,
                RuntimeSession.started_at,
                RuntimeSession.last_activity_at,
                RuntimeSession.ended_at,
            )
            .order_by(
                func.coalesce(
                    func.max(ApiUsage.timestamp),
                    RuntimeSession.last_activity_at,
                    self.model.last_seen_at,
                    self.model.created_at,
                ).desc()
            )
            .limit(limit)
            .offset(offset)
            .all()
        )

        principals = [(row.session_source_type, row.session_source_id) for row in rows]
        aggregates = _usage_aggregates_for_principals(
            db, account_id=account_id, principals=principals
        )

        items = []
        for row in rows:
            summary = self._row_to_summary(row)
            aggregate = aggregates.get(
                (row.session_source_type, row.session_source_id),
                _empty_usage_aggregate(),
            )
            summary["total_requests"] = aggregate["total_requests"]
            summary["successful_requests"] = aggregate["successful_requests"]
            summary["failed_requests"] = aggregate["failed_requests"]
            summary["estimated_cost"] = aggregate["estimated_cost"]
            summary["latest_model_alias"] = aggregate["latest_model_alias"]
            summary["latest_provider_name"] = aggregate["latest_provider_name"]
            summary["last_request_at"] = aggregate["last_request_at"]
            items.append(summary)

        return {"total": total, "items": items}

    def get_summary_for_account(
        self, db: Session, *, account_id: str, agent_id: str
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
                self.model.agent_kind,
                self.model.display_name,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.enrolled_via,
                self.model.managed_mcp_servers,
                self.model.lifecycle_state,
                self.model.lifecycle_reason,
                self.model.lifecycle_updated_at,
                self.model.last_seen_at,
                self.model.tags,
                User.username.label("owner_username"),
                User.email.label("owner_email"),
                RuntimeSession.started_at,
                RuntimeSession.last_activity_at,
                RuntimeSession.ended_at,
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
                self.model.agent_kind,
                self.model.display_name,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.enrolled_via,
                self.model.managed_mcp_servers,
                self.model.lifecycle_state,
                self.model.lifecycle_reason,
                self.model.lifecycle_updated_at,
                self.model.last_seen_at,
                self.model.tags,
                User.username,
                User.email,
                RuntimeSession.started_at,
                RuntimeSession.last_activity_at,
                RuntimeSession.ended_at,
            )
            .first()
        )
        if row is None:
            return None
        summary = self._row_to_summary(row)
        aggregate = _usage_aggregate_for_principal(
            db,
            account_id=account_id,
            principal_type=row.session_source_type,
            principal_id=row.session_source_id,
        )
        summary["total_requests"] = aggregate["total_requests"]
        summary["successful_requests"] = aggregate["successful_requests"]
        summary["failed_requests"] = aggregate["failed_requests"]
        summary["estimated_cost"] = aggregate["estimated_cost"]
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
        )

    def get_usage_by_model_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        agent_id: str,
        limit: int = 10,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
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
            ApiUsage.runtime_principal_type == agent.session_source_type,
            ApiUsage.runtime_principal_id == agent.session_source_id,
        )
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
        last_activity = _coerce_utc(
            row.last_activity_at or row.last_request_at or row.last_seen_at
        )
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
            "owner_username": row.owner_username,
            "owner_email": row.owner_email,
            "agent_kind": row.agent_kind,
            "display_name": row.display_name,
            "session_source_type": row.session_source_type,
            "session_source_id": row.session_source_id,
            "session_reference": row.session_reference,
            "enrolled_via": row.enrolled_via,
            "managed_mcp_servers": row.managed_mcp_servers or [],
            "lifecycle_state": row.lifecycle_state,
            "lifecycle_reason": row.lifecycle_reason,
            "lifecycle_updated_at": row.lifecycle_updated_at,
            "tags": row.tags or {},
            "is_active_now": is_active_now,
            "activity_status": activity_status,
            "last_seen_at": row.last_seen_at,
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
