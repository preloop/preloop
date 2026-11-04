"""CRUD for desktop endpoints and brokered desktop views (#870).

State transitions are conditional UPDATEs so that the API pods holding the two
halves of a relay, the REST handlers and the sweeper can race safely: exactly
one caller wins each transition and only that caller writes the audit row.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from preloop.models import models

DesktopEndpoint = models.DesktopEndpoint
DesktopView = models.DesktopView
OPEN_STATES = models.DESKTOP_VIEW_OPEN_STATES

#: An endpoint that has not heartbeated for this long is treated as offline.
ENDPOINT_STALE_AFTER = timedelta(seconds=60)


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def endpoint_is_online(endpoint: DesktopEndpoint, now: datetime) -> bool:
    """Online means a registered socket that heartbeated recently."""
    return (
        endpoint.state == "online"
        and endpoint.last_seen_at is not None
        and now - endpoint.last_seen_at <= ENDPOINT_STALE_AFTER
    )


class CRUDDesktop:
    """Queries and atomic transitions for endpoints and views."""

    # Endpoints -----------------------------------------------------------

    def get_endpoint(
        self, db: Session, *, account_id: Any, ident: str
    ) -> Optional[DesktopEndpoint]:
        """Resolve an endpoint by row id (uuid) or host endpoint_id."""
        query = db.query(DesktopEndpoint).filter(
            DesktopEndpoint.account_id == account_id
        )
        pk = _as_uuid(ident)
        if pk is not None:
            found = query.filter(DesktopEndpoint.id == pk).first()
            if found is not None:
                return found
        return query.filter(DesktopEndpoint.endpoint_id == str(ident)).first()

    def get_endpoint_by_pk(self, db: Session, pk: Any) -> Optional[DesktopEndpoint]:
        return db.get(DesktopEndpoint, _as_uuid(pk))

    def register_endpoint(
        self,
        db: Session,
        *,
        account_id: Any,
        endpoint_id: str,
        hostname: str,
        display: str,
        managed_agent_id: Optional[uuid.UUID],
        runner_id: Optional[uuid.UUID],
        api_key_id: Optional[uuid.UUID],
        connection_id: str,
        now: datetime,
    ) -> DesktopEndpoint:
        """Create or refresh the endpoint row and mark it online."""
        endpoint = (
            db.query(DesktopEndpoint)
            .filter(
                DesktopEndpoint.account_id == account_id,
                DesktopEndpoint.endpoint_id == endpoint_id,
            )
            .with_for_update()
            .first()
        )
        if endpoint is None:
            endpoint = DesktopEndpoint(
                account_id=account_id,
                endpoint_id=endpoint_id,
            )
            db.add(endpoint)
        endpoint.hostname = hostname
        endpoint.display = display
        endpoint.managed_agent_id = managed_agent_id
        endpoint.runner_id = runner_id
        endpoint.created_by_api_key_id = api_key_id
        endpoint.state = "online"
        endpoint.connection_id = connection_id
        endpoint.last_seen_at = now
        db.commit()
        db.refresh(endpoint)
        return endpoint

    def touch_endpoint(
        self, db: Session, *, pk: Any, connection_id: str, now: datetime
    ) -> bool:
        """Heartbeat. False when a newer socket has taken over the endpoint."""
        result = db.execute(
            update(DesktopEndpoint)
            .where(
                DesktopEndpoint.id == pk,
                DesktopEndpoint.connection_id == connection_id,
            )
            .values(state="online", last_seen_at=now)
        )
        db.commit()
        return bool(result.rowcount)

    def mark_endpoint_offline(
        self, db: Session, *, pk: Any, connection_id: Optional[str] = None
    ) -> None:
        conditions = [DesktopEndpoint.id == pk]
        if connection_id is not None:
            conditions.append(DesktopEndpoint.connection_id == connection_id)
        db.execute(
            update(DesktopEndpoint)
            .where(*conditions)
            .values(state="offline", connection_id=None)
        )
        db.commit()

    def list_endpoints(
        self,
        db: Session,
        *,
        account_id: Any,
        managed_agent_ids: Optional[Iterable[Any]] = None,
        runner_id: Optional[Any] = None,
        api_key_ids: Optional[Iterable[Any]] = None,
        endpoint_idents: Optional[Iterable[str]] = None,
    ) -> List[DesktopEndpoint]:
        """Endpoints in the account, optionally narrowed by linkage.

        The linkage filters are OR-ed: an endpoint matches when any of them
        matches. With no filter, every endpoint in the account is returned.
        """
        query = db.query(DesktopEndpoint).filter(
            DesktopEndpoint.account_id == account_id
        )
        clauses = []
        if managed_agent_ids is not None:
            ids = [_as_uuid(v) for v in managed_agent_ids]
            clauses.append(DesktopEndpoint.managed_agent_id.in_([i for i in ids if i]))
        if runner_id is not None:
            clauses.append(DesktopEndpoint.runner_id == _as_uuid(runner_id))
        if api_key_ids is not None:
            ids = [_as_uuid(v) for v in api_key_ids]
            clauses.append(
                DesktopEndpoint.created_by_api_key_id.in_([i for i in ids if i])
            )
        if endpoint_idents is not None:
            idents = [str(v) for v in endpoint_idents]
            pks = [p for p in (_as_uuid(v) for v in idents) if p]
            clauses.append(
                or_(
                    DesktopEndpoint.endpoint_id.in_(idents),
                    DesktopEndpoint.id.in_(pks),
                )
            )
        if clauses:
            query = query.filter(or_(*clauses))
        return query.order_by(DesktopEndpoint.last_seen_at.desc().nullslast()).all()

    def session_link_filters(
        self, db: Session, *, account_id: Any, runtime_session_id: Any
    ) -> Optional[dict[str, list[Any]]]:
        """Linkage filters that tie a runtime session to desktop endpoints.

        A session is linked to the endpoints of the managed agent that owns it
        (its identity session, or sessions whose principal is the agent), to
        endpoints registered with an API key bound to that session, and to the
        endpoint the session names itself (``desktop_endpoint_id``, from the
        adapter's ``PRELOOP_DESKTOP_ENDPOINT_ID``, #1568). The named endpoint
        is matched inside ``account_id`` only, so a value pointing at another
        account's endpoint links nothing.
        Returns None when the session is not in the account.
        """
        session_pk = _as_uuid(runtime_session_id)
        if session_pk is None:
            return None
        runtime_session = (
            db.query(models.RuntimeSession)
            .filter(
                models.RuntimeSession.id == session_pk,
                models.RuntimeSession.account_id == account_id,
            )
            .first()
        )
        if runtime_session is None:
            return None
        agent_clauses = [models.ManagedAgent.runtime_session_id == session_pk]
        if runtime_session.runtime_principal_id:
            agent_clauses.append(
                models.ManagedAgent.session_source_id
                == runtime_session.runtime_principal_id
            )
        agent_ids = list(
            db.scalars(
                select(models.ManagedAgent.id).where(
                    models.ManagedAgent.account_id == account_id, or_(*agent_clauses)
                )
            )
        )
        key_ids = list(
            db.scalars(
                select(models.ApiKey.id).where(
                    models.ApiKey.account_id == account_id,
                    models.ApiKey.context_data["runtime_session_id"].as_string()
                    == str(session_pk),
                )
            )
        )
        filters: dict[str, list[Any]] = {
            "managed_agent_ids": agent_ids,
            "api_key_ids": key_ids,
        }
        if runtime_session.desktop_endpoint_id:
            filters["endpoint_idents"] = [runtime_session.desktop_endpoint_id]
        return filters

    # Views ---------------------------------------------------------------

    def get_view(
        self, db: Session, *, view_id: Any, account_id: Any = None
    ) -> Optional[DesktopView]:
        pk = _as_uuid(view_id)
        if pk is None:
            return None
        query = db.query(DesktopView).filter(DesktopView.id == pk)
        if account_id is not None:
            query = query.filter(DesktopView.account_id == account_id)
        return query.first()

    def active_view(self, db: Session, *, endpoint_pk: Any) -> Optional[DesktopView]:
        return (
            db.query(DesktopView)
            .filter(
                DesktopView.endpoint_pk == endpoint_pk,
                DesktopView.state.in_(OPEN_STATES),
            )
            .order_by(DesktopView.created_at.desc())
            .first()
        )

    def active_views_with_usernames(
        self, db: Session, *, endpoint_pks: list[Any]
    ) -> List[tuple[DesktopView, Optional[str]]]:
        """Newest open view per endpoint, joined with the holder's username."""
        if not endpoint_pks:
            return []
        rows = (
            db.query(DesktopView, models.User.username)
            .outerjoin(models.User, models.User.id == DesktopView.user_id)
            .filter(
                DesktopView.endpoint_pk.in_(endpoint_pks),
                DesktopView.state.in_(OPEN_STATES),
            )
            .order_by(DesktopView.created_at.desc())
            .all()
        )
        seen: set[Any] = set()
        out: List[tuple[DesktopView, Optional[str]]] = []
        for view, username in rows:
            if view.endpoint_pk not in seen:
                seen.add(view.endpoint_pk)
                out.append((view, username))
        return out

    def lock_endpoint(self, db: Session, *, pk: Any) -> Optional[DesktopEndpoint]:
        """Row lock serializing view creation on one endpoint (busy rule)."""
        return (
            db.query(DesktopEndpoint)
            .filter(DesktopEndpoint.id == pk)
            .with_for_update()
            .first()
        )

    def count_recent_views(self, db: Session, *, user_id: Any, since: datetime) -> int:
        return (
            db.query(DesktopView)
            .filter(DesktopView.user_id == user_id, DesktopView.created_at >= since)
            .count()
        )

    def create_view(self, db: Session, **values: Any) -> DesktopView:
        view = DesktopView(**values)
        db.add(view)
        db.commit()
        db.refresh(view)
        return view

    def consume_token(
        self,
        db: Session,
        *,
        view_id: Any,
        endpoint_pk: Any,
        token_hash: str,
        now: datetime,
    ) -> bool:
        """Single use: pending -> live iff the hash matches and is unexpired."""
        result = db.execute(
            update(DesktopView)
            .where(
                DesktopView.id == _as_uuid(view_id),
                DesktopView.endpoint_pk == endpoint_pk,
                DesktopView.state == "pending",
                DesktopView.view_token_hash == token_hash,
                DesktopView.token_expires_at > now,
            )
            .values(
                state="live",
                live_at=now,
                view_token_hash=None,
                token_expires_at=None,
            )
        )
        db.commit()
        return bool(result.rowcount)

    def attach_viewer(self, db: Session, *, view_id: Any, now: datetime) -> bool:
        """Exactly one viewer socket per view."""
        result = db.execute(
            update(DesktopView)
            .where(
                DesktopView.id == _as_uuid(view_id),
                DesktopView.state.in_(OPEN_STATES),
                DesktopView.viewer_attached_at.is_(None),
            )
            .values(viewer_attached_at=now)
        )
        db.commit()
        return bool(result.rowcount)

    def close_view(
        self,
        db: Session,
        *,
        view_id: Any,
        reason: str,
        now: datetime,
        expected_state: Optional[str] = None,
    ) -> Optional[DesktopView]:
        """Close an open view. Returns the row only for the caller that won."""
        states = (expected_state,) if expected_state else OPEN_STATES
        result = db.execute(
            update(DesktopView)
            .where(
                DesktopView.id == _as_uuid(view_id),
                DesktopView.state.in_(states),
            )
            .values(
                state="closed",
                closed_at=now,
                closed_reason=reason,
                view_token_hash=None,
                token_expires_at=None,
                control_expires_at=None,
            )
        )
        db.commit()
        if not result.rowcount:
            return None
        return self.get_view(db, view_id=view_id)

    def set_mode(
        self,
        db: Session,
        *,
        view_id: Any,
        mode: str,
        control_expires_at: Optional[datetime],
    ) -> bool:
        result = db.execute(
            update(DesktopView)
            .where(
                DesktopView.id == _as_uuid(view_id),
                DesktopView.state.in_(OPEN_STATES),
            )
            .values(mode=mode, control_expires_at=control_expires_at)
        )
        db.commit()
        return bool(result.rowcount)

    def views_to_sweep(self, db: Session, *, now: datetime) -> List[tuple[Any, str]]:
        """(view_id, reason) for views a live relay should already have closed."""
        pending = db.query(DesktopView.id).filter(
            DesktopView.state == "pending",
            DesktopView.created_at < now - timedelta(seconds=30),
        )
        stale_live = db.query(DesktopView.id).filter(
            DesktopView.state.in_(OPEN_STATES),
            DesktopView.expires_at < now - timedelta(seconds=30),
        )
        out: list[tuple[Any, str]] = [
            (row.id, "desktop_agent_unreachable") for row in pending
        ]
        seen = {v for v, _ in out}
        out.extend(
            (row.id, "server_restart") for row in stale_live if row.id not in seen
        )
        return out

    def mark_stale_endpoints_offline(self, db: Session, *, now: datetime) -> int:
        result = db.execute(
            update(DesktopEndpoint)
            .where(
                DesktopEndpoint.state == "online",
                or_(
                    DesktopEndpoint.last_seen_at.is_(None),
                    DesktopEndpoint.last_seen_at < now - ENDPOINT_STALE_AFTER,
                ),
            )
            .values(state="offline", connection_id=None)
        )
        db.commit()
        return int(result.rowcount or 0)


crud_desktop = CRUDDesktop()
