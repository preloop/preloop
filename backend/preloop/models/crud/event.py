"""CRUD operations for analytics events.

Plugins must write events through this module rather than constructing
``Event`` rows inline (CRUD-layer mandate).
"""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models.event import Event
from .base import CRUDBase


# Two-key PostgreSQL advisory namespace reserved for operator digest delivery.
OPERATOR_DIGEST_LOCK_KEY = (190245, 1)


class CRUDEvent(CRUDBase[Event]):
    """CRUD operations for events."""

    def report_operator_events(
        self,
        db: Session,
        *,
        start: datetime,
        end: datetime,
        account_id: UUID | None = None,
        limit: int | None = 1000,
    ) -> list[Event]:
        """Return private assembly failures in a half-open operator window.

        Callers must require platform administrator access. Customer analytics
        must never use this ledger. A timestamp/id order makes results stable.
        """
        query = db.query(Event).filter(
            Event.event_type == "report_operator_failure",
            Event.timestamp >= start,
            Event.timestamp < end,
        )
        if account_id is not None:
            query = query.filter(Event.account_id == account_id)
        query = query.order_by(Event.timestamp.desc(), Event.id)
        if limit is not None:
            if not 1 <= limit <= 10000:
                raise ValueError("Operator event limit must be between 1 and 10000")
            query = query.limit(limit)
        return query.all()

    def operator_digest_sent(self, db: Session, *, day: str) -> bool:
        """Serialize operator delivery globally and check the durable day marker.

        The transaction must remain open through delivery and marker creation.
        This lock is independent of whichever accounts happened to fail today.
        """
        db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
            {
                "namespace": OPERATOR_DIGEST_LOCK_KEY[0],
                "key": OPERATOR_DIGEST_LOCK_KEY[1],
            },
        )
        return (
            db.query(Event.id)
            .filter(
                Event.event_type == "report_operator_email",
                Event.event_data["day"].astext == day,
            )
            .first()
            is not None
        )

    def log_event(
        self,
        db: Session,
        *,
        event_type: str,
        account_id: Optional[UUID] = None,
        user_id: Optional[UUID] = None,
        session_id: Optional[UUID] = None,
        visitor_id: Optional[UUID] = None,
        fingerprint: Optional[str] = None,
        client_type: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        browser: Optional[str] = None,
        os: Optional[str] = None,
        country: Optional[str] = None,
        region: Optional[str] = None,
        city: Optional[str] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        path: Optional[str] = None,
        referrer: Optional[str] = None,
        action: Optional[str] = None,
        conversion_event: Optional[str] = None,
        conversion_value: Optional[float] = None,
        event_data: Optional[dict[str, Any]] = None,
        commit: bool = True,
    ) -> Event:
        """Persist a single analytics event."""
        event = Event(
            event_type=event_type,
            account_id=account_id,
            user_id=user_id,
            session_id=session_id,
            visitor_id=visitor_id,
            fingerprint=fingerprint,
            client_type=client_type,
            timestamp=timestamp or datetime.now(timezone.utc),
            ip_address=ip_address,
            user_agent=user_agent,
            browser=browser,
            os=os,
            country=country,
            region=region,
            city=city,
            latitude=latitude,
            longitude=longitude,
            path=path,
            referrer=referrer,
            action=action,
            conversion_event=conversion_event,
            conversion_value=conversion_value,
            event_data=event_data,
        )
        db.add(event)
        if commit:
            db.commit()
            db.refresh(event)
        else:
            # Visible to same-transaction reads even with autoflush disabled.
            db.flush()
        return event

    def has_conversion_event(
        self, db: Session, *, account_id: UUID, conversion_event: str
    ) -> bool:
        """Whether an account already has a given conversion event recorded.

        Used by plugins to keep once-per-account conversion events (e.g.
        ``first_session_seen``) deduplicated. Same-transaction flushed rows
        are visible, so a flush-then-check within one request stays exact.

        Args:
            db: Database session.
            account_id: Account to check.
            conversion_event: Conversion event name.

        Returns:
            True when a matching ``conversion`` event exists.
        """
        return (
            db.query(Event.id)
            .filter(
                Event.account_id == account_id,
                Event.event_type == "conversion",
                Event.conversion_event == conversion_event,
            )
            .first()
            is not None
        )

    def get_session_start(self, db: Session, *, session_id: UUID) -> Optional[Event]:
        """The session_start row for a WebSocket session, if persisted."""
        return (
            db.query(Event)
            .filter(
                Event.session_id == session_id,
                Event.event_type == "session_start",
            )
            .first()
        )


crud_event = CRUDEvent(Event)
