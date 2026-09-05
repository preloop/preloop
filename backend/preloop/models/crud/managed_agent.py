"""CRUD operations for durable managed-agent registry entries."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import and_, case, func, or_, tuple_
from sqlalchemy.orm import Session

from preloop.utils.agent_kind import normalize_agent_kind

from ..models.api_usage import ApiUsage
from ..models.managed_agent import ManagedAgent
from ..models.runtime_session import RuntimeSession
from ..models.user import User
from .base import CRUDBase

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
        # "Active now" means traffic recency, never registration recency:
        # last_seen_at is stamped by CLI enrollment/heartbeats and
        # last_activity_at is stamped at session-token mint, so counting them
        # unconditionally made a freshly onboarded agent with zero requests
        # render as active while the dashboard (which requires requests)
        # showed "No active agents right now". An agent with no gateway
        # requests is idle; once traffic exists, later session activity keeps
        # it fresh.
        has_gateway_traffic = int(row.request_count or 0) > 0
        last_activity = (
            _coerce_utc(row.last_activity_at or row.last_request_at)
            if has_gateway_traffic
            else None
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
