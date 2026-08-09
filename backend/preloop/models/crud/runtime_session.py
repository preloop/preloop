"""CRUD operations for RuntimeSession."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    func,
    inspect,
    literal,
    literal_column,
    or_,
)
from sqlalchemy.orm import Session

from ..models.api_usage import ApiUsage
from ..models.flow import Flow
from ..models.runtime_session import RuntimeSession
from .base import CRUDBase

logger = logging.getLogger(__name__)

_summary_columns_cache: dict[int, bool] = {}

# Preloop-internal model-gateway calls (session summarization/optimization,
# session-title generation, and replay-validation re-executions) are logged
# tagged via ``ApiUsage.meta_data->>'purpose'``. Real agent traffic has no
# ``purpose`` (NULL meta_data or a NULL ``purpose`` key). These internal calls
# must be excluded from a session's aggregated token/cost/request metrics so the
# session reflects only the agent's own traffic, not Preloop's overhead.
# ``replay_validation`` runs additionally suppress session attribution at the
# gateway; its presence here is defense in depth so a mis-attributed replay row
# can never inflate the session it was validating.
INTERNAL_USAGE_PURPOSES = ("session_optimization", "session_title", "replay_validation")

# Infix marking a runtime session row minted by the gateway's inactivity closer
# rather than by an agent-declared conversation id. A source id shaped
# ``<principal>:idle-<epoch>`` is the Nth generation of a signal-less agent's
# session; ``<principal>:<something-else>`` is keyed by a real session id the
# agent (or the client's X-Preloop-Session-Id) supplied. Keeping the two
# namespaces distinguishable is what lets provenance be reported honestly and
# stops the closer from ever matching a natively-keyed row.
IDLE_GENERATION_INFIX = ":idle-"


def _exclude_internal_usage_condition():
    """Return a NULL-safe filter that excludes Preloop-internal usage rows.

    Rows are INCLUDED (treated as agent traffic) when ``meta_data`` is NULL or
    when its ``purpose`` key is NULL. Only rows whose ``purpose`` is explicitly
    one of :data:`INTERNAL_USAGE_PURPOSES` are EXCLUDED. The JSONB accessor
    ``ApiUsage.meta_data["purpose"].astext`` matches the style used elsewhere in
    the codebase (see ``crud/api_usage.py``).

    Returns:
        A SQLAlchemy boolean expression suitable for use in a WHERE/JOIN clause.
    """
    purpose_expr = ApiUsage.meta_data["purpose"].astext
    return or_(
        purpose_expr.is_(None),
        purpose_expr.notin_(INTERNAL_USAGE_PURPOSES),
    )


def _gateway_usage_base_query(
    db: Session,
    *,
    account_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    ai_model_id: Optional[str] = None,
):
    """Build the shared gateway-usage query used for latest-per-session lookups."""
    query = db.query(ApiUsage).filter(
        ApiUsage.account_id == account_id,
        ApiUsage.action_type == "model_gateway",
        ApiUsage.runtime_session_id.isnot(None),
        # Never surface a Preloop-internal call as the session's latest model.
        _exclude_internal_usage_condition(),
    )
    if start_date is not None:
        query = query.filter(ApiUsage.timestamp >= start_date)
    if end_date is not None:
        query = query.filter(ApiUsage.timestamp < end_date)
    if ai_model_id is not None:
        query = query.filter(ApiUsage.ai_model_id == ai_model_id)
    return query


def _latest_gateway_usage_for_session(
    db: Session,
    *,
    account_id: str,
    runtime_session_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    ai_model_id: Optional[str] = None,
):
    """Return the latest gateway usage row for one runtime session."""
    return (
        _gateway_usage_base_query(
            db,
            account_id=account_id,
            start_date=start_date,
            end_date=end_date,
            ai_model_id=ai_model_id,
        )
        .filter(ApiUsage.runtime_session_id == runtime_session_id)
        .with_entities(
            ApiUsage.model_alias.label("latest_model_alias"),
            ApiUsage.provider_name.label("latest_provider_name"),
            ApiUsage.timestamp.label("last_request_at"),
        )
        .order_by(ApiUsage.timestamp.desc(), ApiUsage.id.desc())
        .first()
    )


def _latest_gateway_usage_for_sessions(
    db: Session,
    *,
    account_id: str,
    runtime_session_ids: list[str],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    ai_model_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return the latest gateway usage row for many runtime sessions."""
    if not runtime_session_ids:
        return {}

    rows = (
        _gateway_usage_base_query(
            db,
            account_id=account_id,
            start_date=start_date,
            end_date=end_date,
            ai_model_id=ai_model_id,
        )
        .filter(ApiUsage.runtime_session_id.in_(runtime_session_ids))
        .with_entities(
            ApiUsage.runtime_session_id,
            ApiUsage.model_alias,
            ApiUsage.provider_name,
            ApiUsage.timestamp,
        )
        .order_by(
            ApiUsage.runtime_session_id.asc(),
            ApiUsage.timestamp.desc(),
            ApiUsage.id.desc(),
        )
        .all()
    )

    latest_by_session: dict[str, Any] = {}
    for row in rows:
        session_id = str(row.runtime_session_id)
        if session_id in latest_by_session:
            continue
        latest_by_session[session_id] = row
    return latest_by_session


class CRUDRuntimeSession(CRUDBase[RuntimeSession]):
    """CRUD helpers for shared runtime session identities."""

    ACTIVE_WINDOW = timedelta(minutes=10)

    def get_by_source(
        self,
        db: Session,
        *,
        account_id: Any,
        session_source_type: str,
        session_source_id: str,
    ) -> Optional[RuntimeSession]:
        """Look up a runtime session by its source identity."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.session_source_type == session_source_type,
                self.model.session_source_id == session_source_id,
            )
            .first()
        )

    def get_latest_idle_generation(
        self,
        db: Session,
        *,
        account_id: Any,
        session_source_type: str,
        session_source_id: str,
    ) -> Optional[RuntimeSession]:
        """Return the newest generation of one source-keyed runtime session.

        Sources that put no session id on the wire (Gemini CLI, Hermes,
        OpenClaw's Anthropic transport) can only be bounded by an idle window.
        When one goes idle, the gateway closes the row and rolls to a fresh
        generation whose source id is ``<base>:idle-<timestamp>`` — the row
        itself is preserved, so the session's history is never rewritten. This
        returns the generation that traffic should currently land on: the
        newest of the base row and its ``:idle-`` descendants.

        The ``:idle-`` infix is what keeps this unambiguous. Rows suffixed with
        an agent's *native* conversation id (``<base>:<uuid>``) are resolved by
        exact source key and are deliberately NOT matched here.

        Args:
            db: Database session.
            account_id: Owning account.
            session_source_type: Runtime principal type (e.g. ``gemini_cli``).
            session_source_id: The BASE source id, without any generation
                suffix.

        Returns:
            The newest matching runtime session, or ``None`` when the principal
            has no session row yet.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.session_source_type == session_source_type,
                or_(
                    self.model.session_source_id == session_source_id,
                    self.model.session_source_id.startswith(
                        f"{session_source_id}{IDLE_GENERATION_INFIX}"
                    ),
                ),
            )
            .order_by(
                self.model.started_at.desc(),
                self.model.id.desc(),
            )
            .first()
        )

    def touch_activity(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        observed_at: datetime,
        min_update_interval: Optional[timedelta] = None,
        commit: bool = False,
    ) -> Optional[RuntimeSession]:
        """Update last activity for one runtime session."""
        db_obj = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.id == runtime_session_id,
            )
            .first()
        )
        if db_obj is None:
            return None
        if min_update_interval is not None and db_obj.last_activity_at is not None:
            last_activity_at = db_obj.last_activity_at
            normalized_observed_at = observed_at
            if last_activity_at.tzinfo is None and normalized_observed_at.tzinfo:
                normalized_observed_at = normalized_observed_at.replace(tzinfo=None)
            elif last_activity_at.tzinfo and normalized_observed_at.tzinfo is None:
                last_activity_at = last_activity_at.replace(tzinfo=None)

            elapsed = normalized_observed_at - last_activity_at
            if elapsed <= timedelta(0) or elapsed < min_update_interval:
                return db_obj
        db_obj.last_activity_at = observed_at
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
        runtime_session_id: str,
        ended_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> Optional[RuntimeSession]:
        """Update operator-managed lifecycle state for one runtime session."""
        db_obj = self.get_account_session(
            db, account_id=account_id, runtime_session_id=runtime_session_id
        )
        if db_obj is None:
            return None
        if ended_at is not None:
            db_obj.ended_at = ended_at
            db_obj.last_activity_at = ended_at
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def reopen_for_managed_agent(
        self,
        db: Session,
        *,
        account_id: str,
        session_source_type: str,
        session_source_id: str,
        commit: bool = True,
    ) -> Optional[RuntimeSession]:
        """Reopen the identity session of a resumed managed agent.

        A previous release stamped ``ended_at`` when an agent was suspended
        and never cleared it, so session-bound runtime keys kept failing after
        resume. Reopening is a no-op when no session exists or the session is
        already open.

        Args:
            db: Database session.
            account_id: Account that owns the session.
            session_source_type: Durable principal type of the agent.
            session_source_id: Durable principal id of the agent.
            commit: Commit the transaction when True.

        Returns:
            The reopened session, or None when there is nothing to reopen.
        """
        db_obj = self.get_by_source(
            db,
            account_id=account_id,
            session_source_type=session_source_type,
            session_source_id=session_source_id,
        )
        if db_obj is None:
            logger.info(
                "No runtime session to reopen for %s/%s (account %s)",
                session_source_type,
                session_source_id,
                account_id,
            )
            return None
        if db_obj.ended_at is None:
            # Already open: resuming an agent that was never session-ended.
            return db_obj
        now = datetime.now(UTC)
        db_obj.ended_at = None
        # started_at is NOT NULL, so it is left alone: reopening continues the
        # original session rather than pretending it began now.
        db_obj.last_activity_at = now
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def upsert_by_source(
        self,
        db: Session,
        *,
        account_id: Any,
        session_source_type: str,
        session_source_id: str,
        session_reference: Optional[str] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        runtime_principal_name: Optional[str] = None,
        started_at: Optional[datetime] = None,
        last_activity_at: Optional[datetime] = None,
        ended_at: Optional[datetime] = None,
        reopen_if_ended: bool = False,
    ) -> RuntimeSession:
        """Create or update a runtime session keyed by source identity."""
        db_obj = self.get_by_source(
            db,
            account_id=account_id,
            session_source_type=session_source_type,
            session_source_id=session_source_id,
        )
        if db_obj is None:
            is_account_first_session = (
                db.query(self.model.id)
                .filter(self.model.account_id == account_id)
                .first()
                is None
            )
            db_obj = RuntimeSession(
                account_id=account_id,
                session_source_type=session_source_type,
                session_source_id=session_source_id,
                session_reference=session_reference,
                runtime_principal_type=runtime_principal_type,
                runtime_principal_id=runtime_principal_id,
                runtime_principal_name=runtime_principal_name,
                started_at=started_at or last_activity_at,
                last_activity_at=last_activity_at,
                ended_at=ended_at,
            )
            db.add(db_obj)
            db.flush()
            if is_account_first_session:
                # Every session-recording path funnels through this creation
                # branch, so it is the single chokepoint for the account's
                # first-session signal. The notification is an inert no-op
                # unless an EE plugin registered a hook (see
                # preloop.services.session_events); it never raises. Lazy
                # import keeps the models package importable standalone.
                try:
                    from preloop.services.session_events import (
                        notify_first_session_recorded,
                    )
                except ImportError:  # pragma: no cover - models-only contexts
                    pass
                else:
                    notify_first_session_recorded(
                        db,
                        account_id=account_id,
                        occurred_at=db_obj.started_at
                        or db_obj.last_activity_at
                        or datetime.now(UTC),
                    )
            return db_obj

        if session_reference is not None:
            db_obj.session_reference = session_reference
        if runtime_principal_type is not None:
            db_obj.runtime_principal_type = runtime_principal_type
        if runtime_principal_id is not None:
            db_obj.runtime_principal_id = runtime_principal_id
        if runtime_principal_name is not None:
            db_obj.runtime_principal_name = runtime_principal_name
        if reopen_if_ended and db_obj.ended_at is not None and ended_at is None:
            db_obj.ended_at = None
            db_obj.started_at = started_at or last_activity_at
        elif started_at is not None and db_obj.started_at is None:
            db_obj.started_at = started_at
        if last_activity_at is not None:
            db_obj.last_activity_at = last_activity_at
        if ended_at is not None:
            db_obj.ended_at = ended_at

        db.add(db_obj)
        db.flush()
        return db_obj

    def list_account_sessions(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        query: Optional[str] = None,
        ai_model_id: Optional[str] = None,
        session_source_type: Optional[str] = None,
        runtime_principal_type: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        min_requests: Optional[int] = None,
        status: str = "all",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List runtime sessions with aggregated gateway usage."""
        session_query = db.query(self.model).filter(self.model.account_id == account_id)

        if query:
            normalized_query = f"%{' '.join(query.strip().split())}%"
            session_query = session_query.filter(
                or_(
                    self.model.session_source_type.ilike(normalized_query),
                    self.model.session_source_id.ilike(normalized_query),
                    self.model.session_reference.ilike(normalized_query),
                    self.model.runtime_principal_name.ilike(normalized_query),
                )
            )
        if ai_model_id:
            matching_session_ids = db.query(ApiUsage.runtime_session_id).filter(
                ApiUsage.runtime_session_id.isnot(None),
                ApiUsage.action_type == "model_gateway",
                ApiUsage.ai_model_id == ai_model_id,
            )
            if start_date is not None:
                matching_session_ids = matching_session_ids.filter(
                    ApiUsage.timestamp >= start_date
                )
            if end_date is not None:
                matching_session_ids = matching_session_ids.filter(
                    ApiUsage.timestamp < end_date
                )
            session_query = session_query.filter(
                self.model.id.in_(matching_session_ids.distinct())
            )
        else:
            if start_date is not None:
                start_date_utc = (
                    start_date.astimezone(UTC).replace(tzinfo=None)
                    if start_date.tzinfo
                    else start_date
                )
                session_query = session_query.filter(
                    or_(
                        self.model.last_activity_at >= start_date_utc,
                        self.model.started_at >= start_date_utc,
                    )
                )
            if end_date is not None:
                end_date_utc = (
                    end_date.astimezone(UTC).replace(tzinfo=None)
                    if end_date.tzinfo
                    else end_date
                )
                session_query = session_query.filter(
                    self.model.started_at < end_date_utc
                )

        if min_requests is not None:
            usage_count_subq = (
                db.query(ApiUsage.runtime_session_id)
                .filter(
                    ApiUsage.runtime_session_id.isnot(None),
                    ApiUsage.action_type == "model_gateway",
                )
                .group_by(ApiUsage.runtime_session_id)
                .having(func.count(ApiUsage.id) >= min_requests)
                .subquery()
            )
            # Keep historical empty "shell" sessions hidden, but a freshly
            # created session (e.g. a Hermes "/new") has zero requests for the
            # first few seconds before its first gateway call lands. Surface it
            # immediately when it is recent and still open, so a new session is
            # visible the moment it is created instead of only after its request
            # count catches up.
            recent_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                minutes=15
            )
            session_query = session_query.filter(
                or_(
                    self.model.id.in_(usage_count_subq.select()),
                    and_(
                        self.model.started_at >= recent_cutoff,
                        self.model.ended_at.is_(None),
                    ),
                )
            )
        if session_source_type:
            session_query = session_query.filter(
                self.model.session_source_type == session_source_type
            )
        if runtime_principal_type:
            session_query = session_query.filter(
                self.model.runtime_principal_type == runtime_principal_type
            )
        if runtime_principal_id:
            # A managed agent's principal id is the *base* id (e.g.
            # ``custom_ABC``). Per-run sessions key off a derived id that appends
            # the X-Preloop-Session-Id as ``<base>:<run-id>``. Match the base
            # exactly OR any per-run variant so agent-scoped views surface every
            # run, not just sessions whose principal id equals the base verbatim.
            escaped = (
                runtime_principal_id.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            session_query = session_query.filter(
                or_(
                    self.model.runtime_principal_id == runtime_principal_id,
                    self.model.runtime_principal_id.like(f"{escaped}:%", escape="\\"),
                )
            )
        if status == "active":
            session_query = session_query.filter(self.model.ended_at.is_(None))
        elif status == "ended":
            session_query = session_query.filter(self.model.ended_at.isnot(None))

        total = session_query.count()
        usage_join = self._usage_join_conditions(
            start_date=start_date, end_date=end_date, ai_model_id=ai_model_id
        )

        rows = (
            self._account_sessions_query(
                session_query=session_query,
                usage_join=usage_join,
                summary_columns_available=self._summary_columns_available(db),
            )
            .order_by(
                func.coalesce(
                    func.max(ApiUsage.timestamp),
                    self.model.last_activity_at,
                    self.model.started_at,
                ).desc()
            )
            .limit(limit)
            .offset(offset)
            .all()
        )

        session_ids = [str(row.id) for row in rows]
        latest_usage_by_session = _latest_gateway_usage_for_sessions(
            db,
            account_id=account_id,
            runtime_session_ids=session_ids,
            start_date=start_date,
            end_date=end_date,
            ai_model_id=ai_model_id,
        )

        items = []
        for row in rows:
            summary = self._row_to_summary(row)
            latest_usage = latest_usage_by_session.get(str(row.id))
            if latest_usage is not None:
                summary["latest_model_alias"] = latest_usage.model_alias
                summary["latest_provider_name"] = latest_usage.provider_name
                summary["last_request_at"] = latest_usage.timestamp
            items.append(summary)

        return {"total": total, "items": items}

    def _account_sessions_query(
        self,
        *,
        session_query: Any,
        usage_join: Any,
        summary_columns_available: bool,
    ) -> Any:
        """Build the runtime session aggregate query."""
        summary_column = (
            literal_column("runtime_session.summary")
            if summary_columns_available
            else literal(None)
        )
        summary_updated_at_column = (
            literal_column("runtime_session.summary_updated_at")
            if summary_columns_available
            else literal(None)
        )
        title_column = (
            literal_column("runtime_session.title")
            if summary_columns_available
            else literal(None)
        )
        title_request_count_column = (
            literal_column("runtime_session.title_request_count")
            if summary_columns_available
            else literal(None)
        )
        return (
            session_query.outerjoin(ApiUsage, usage_join)
            .outerjoin(Flow, ApiUsage.flow_id == Flow.id)
            .with_entities(
                self.model.account_id,
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.runtime_principal_type,
                self.model.runtime_principal_id,
                self.model.runtime_principal_name,
                summary_column.label("summary"),
                summary_updated_at_column.label("summary_updated_at"),
                title_column.label("title"),
                title_request_count_column.label("title_request_count"),
                self.model.started_at,
                self.model.last_activity_at,
                self.model.ended_at,
                func.max(cast(ApiUsage.flow_id, String)).label("flow_id"),
                func.max(Flow.name).label("flow_name"),
                func.max(cast(ApiUsage.flow_execution_id, String)).label(
                    "flow_execution_id"
                ),
                func.max(ApiUsage.model_alias).label("latest_model_alias"),
                func.max(ApiUsage.provider_name).label("latest_provider_name"),
                func.count(ApiUsage.id).label("request_count"),
                func.coalesce(
                    func.sum(case((ApiUsage.status_code < 400, 1), else_=0)), 0
                ).label("success_count"),
                func.coalesce(
                    func.sum(case((ApiUsage.status_code >= 400, 1), else_=0)), 0
                ).label("error_count"),
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
            )
            .group_by(
                self.model.account_id,
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.runtime_principal_type,
                self.model.runtime_principal_id,
                self.model.runtime_principal_name,
                summary_column,
                summary_updated_at_column,
                title_column,
                title_request_count_column,
                self.model.started_at,
                self.model.last_activity_at,
                self.model.ended_at,
            )
        )

    def update_session_title(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_session_id: str,
        title: Optional[str],
        summary: Optional[str] = None,
        title_request_count: Optional[int] = None,
        commit: bool = True,
    ) -> Optional[RuntimeSession]:
        """Persist a generated title (and optional summary) for one session.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session to update.
            title: Short human-readable title, or ``None`` to leave unchanged.
            summary: Optional longer summary; updates ``summary_updated_at``
                when provided.
            title_request_count: Session request count captured when the title
                was generated, used to drive the periodic refresh watermark.
            commit: Whether to commit the change.

        Returns:
            The updated runtime session, or ``None`` when it was not found.
        """
        db_obj = self.get_account_session(
            db, account_id=account_id, runtime_session_id=runtime_session_id
        )
        if db_obj is None:
            return None
        if title is not None:
            db_obj.title = title
        if summary is not None:
            db_obj.summary = summary
            db_obj.summary_updated_at = datetime.now(UTC)
        if title_request_count is not None:
            db_obj.title_request_count = title_request_count
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def get_account_session(
        self, db: Session, *, account_id: str, runtime_session_id: str
    ) -> Optional[RuntimeSession]:
        """Return one runtime session for an account."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id, self.model.id == runtime_session_id
            )
            .first()
        )

    def count_active_sessions(self, db: Session, *, account_id: str) -> int:
        """Count active (non-ended) runtime sessions for an account."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.ended_at.is_(None),
            )
            .count()
        )

    def get_latest_by_principal(
        self,
        db: Session,
        *,
        account_id: str,
        principal_type: str,
        principal_id: str,
    ) -> Optional[RuntimeSession]:
        """Return the most recent runtime session for a given principal."""
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.session_source_type == principal_type,
                self.model.session_source_id.startswith(f"{principal_id}-")
                | (self.model.session_source_id == principal_id),
            )
            .order_by(self.model.created_at.desc())
            .first()
        )

    def get_account_session_summary(
        self,
        db: Session,
        *,
        account_id: str,
        runtime_session_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        ai_model_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Return one runtime session with aggregated gateway usage."""
        usage_join = self._usage_join_conditions(
            start_date=start_date, end_date=end_date, ai_model_id=ai_model_id
        )
        summary_columns_available = self._summary_columns_available(db)
        summary_column = (
            literal_column("runtime_session.summary")
            if summary_columns_available
            else literal(None)
        )
        summary_updated_at_column = (
            literal_column("runtime_session.summary_updated_at")
            if summary_columns_available
            else literal(None)
        )
        title_column = (
            literal_column("runtime_session.title")
            if summary_columns_available
            else literal(None)
        )
        title_request_count_column = (
            literal_column("runtime_session.title_request_count")
            if summary_columns_available
            else literal(None)
        )
        row = (
            db.query(
                self.model.account_id,
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.runtime_principal_type,
                self.model.runtime_principal_id,
                self.model.runtime_principal_name,
                summary_column.label("summary"),
                summary_updated_at_column.label("summary_updated_at"),
                title_column.label("title"),
                title_request_count_column.label("title_request_count"),
                self.model.started_at,
                self.model.last_activity_at,
                self.model.ended_at,
                func.max(cast(ApiUsage.flow_id, String)).label("flow_id"),
                func.max(Flow.name).label("flow_name"),
                func.max(cast(ApiUsage.flow_execution_id, String)).label(
                    "flow_execution_id"
                ),
                func.max(ApiUsage.model_alias).label("latest_model_alias"),
                func.max(ApiUsage.provider_name).label("latest_provider_name"),
                func.count(ApiUsage.id).label("request_count"),
                func.coalesce(
                    func.sum(case((ApiUsage.status_code < 400, 1), else_=0)), 0
                ).label("success_count"),
                func.coalesce(
                    func.sum(case((ApiUsage.status_code >= 400, 1), else_=0)), 0
                ).label("error_count"),
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
            )
            .outerjoin(ApiUsage, usage_join)
            .outerjoin(Flow, ApiUsage.flow_id == Flow.id)
            .filter(
                self.model.account_id == account_id, self.model.id == runtime_session_id
            )
            .group_by(
                self.model.account_id,
                self.model.id,
                self.model.session_source_type,
                self.model.session_source_id,
                self.model.session_reference,
                self.model.runtime_principal_type,
                self.model.runtime_principal_id,
                self.model.runtime_principal_name,
                summary_column,
                summary_updated_at_column,
                title_column,
                title_request_count_column,
                self.model.started_at,
                self.model.last_activity_at,
                self.model.ended_at,
            )
            .first()
        )
        if row is None:
            return None
        summary = self._row_to_summary(row)
        latest_usage = _latest_gateway_usage_for_session(
            db,
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            start_date=start_date,
            end_date=end_date,
            ai_model_id=ai_model_id,
        )
        if latest_usage is not None:
            summary["latest_model_alias"] = latest_usage.latest_model_alias
            summary["latest_provider_name"] = latest_usage.latest_provider_name
            summary["last_request_at"] = latest_usage.last_request_at
        return summary

    @staticmethod
    def _summary_columns_available(db: Session) -> bool:
        """Return whether the runtime session summary migration has been applied."""
        bind = db.get_bind()
        if bind is None:
            bind = db.bind
        if bind is None:
            return False
        cache_key = id(bind)
        if cache_key in _summary_columns_cache:
            return _summary_columns_cache[cache_key]
        try:
            columns = {
                column["name"]
                for column in inspect(bind).get_columns("runtime_session")
            }
            available = {
                "summary",
                "summary_updated_at",
                "title",
                "title_request_count",
            }.issubset(columns)
        except Exception:
            available = False
        _summary_columns_cache[cache_key] = available
        return available

    @staticmethod
    def _usage_join_conditions(
        *,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        ai_model_id: Optional[str] = None,
    ):
        legacy_flow_execution_match = and_(
            RuntimeSession.session_source_type == "flow_execution",
            ApiUsage.flow_execution_id.isnot(None),
            cast(ApiUsage.flow_execution_id, String)
            == RuntimeSession.session_source_id,
        )
        conditions = [
            ApiUsage.action_type == "model_gateway",
            or_(
                ApiUsage.runtime_session_id == RuntimeSession.id,
                legacy_flow_execution_match,
            ),
            # Exclude Preloop-internal usage (session optimization + title
            # generation) from the summed metrics so a session's token/cost/
            # request totals reflect only real agent traffic. This join is the
            # shared chokepoint for both ``get_account_session_summary`` and
            # ``list_account_sessions`` (via ``_account_sessions_query``), so a
            # single filter here fixes both aggregations. NULL-safe: rows with
            # NULL meta_data or NULL purpose are agent traffic and stay counted.
            _exclude_internal_usage_condition(),
        ]
        if start_date is not None:
            conditions.append(ApiUsage.timestamp >= start_date)
        if end_date is not None:
            conditions.append(ApiUsage.timestamp < end_date)
        if ai_model_id is not None:
            conditions.append(ApiUsage.ai_model_id == ai_model_id)
        return and_(*conditions)

    @staticmethod
    def _row_to_summary(row) -> dict[str, Any]:
        now = datetime.now(UTC)
        last_observed_at = row.last_request_at or row.last_activity_at or row.started_at
        if last_observed_at is not None and last_observed_at.tzinfo is None:
            last_observed_at = last_observed_at.replace(tzinfo=UTC)
        elif last_observed_at is not None:
            last_observed_at = last_observed_at.astimezone(UTC)

        if row.ended_at is not None:
            activity_status = "ended"
            is_active_now = False
        elif (
            last_observed_at is not None
            and (now - last_observed_at) <= CRUDRuntimeSession.ACTIVE_WINDOW
        ):
            activity_status = "active_now"
            is_active_now = True
        else:
            activity_status = "idle"
            is_active_now = False

        return {
            "account_id": str(row.account_id),
            "id": str(row.id),
            "session_source_type": row.session_source_type,
            "session_source_id": row.session_source_id,
            "session_reference": row.session_reference,
            "runtime_principal_type": row.runtime_principal_type,
            "runtime_principal_id": row.runtime_principal_id,
            "runtime_principal_name": row.runtime_principal_name,
            "summary": row.summary,
            "summary_updated_at": row.summary_updated_at,
            "title": row.title,
            "title_request_count": (
                int(row.title_request_count)
                if row.title_request_count is not None
                else None
            ),
            "started_at": row.started_at,
            "last_activity_at": row.last_activity_at,
            "ended_at": row.ended_at,
            "flow_id": row.flow_id,
            "flow_name": row.flow_name,
            "flow_execution_id": row.flow_execution_id,
            "latest_model_alias": row.latest_model_alias,
            "latest_provider_name": row.latest_provider_name,
            "is_active_now": is_active_now,
            "activity_status": activity_status,
            "total_requests": int(row.request_count or 0),
            "successful_requests": int(row.success_count or 0),
            "failed_requests": int(row.error_count or 0),
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
            "estimated_cost": float(row.estimated_cost or 0.0),
            "last_request_at": row.last_request_at,
        }


crud_runtime_session = CRUDRuntimeSession(RuntimeSession)
