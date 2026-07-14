"""CRUD operations for analytics visitors."""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from ..models.visitor import Visitor
from .base import CRUDBase

# Attribution columns are first-touch: written once, never overwritten.
_ATTRIBUTION_FIELDS = (
    "first_landing_path",
    "first_referrer",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
)

# Device-profile columns reflect the latest observation.
_PROFILE_FIELDS = (
    "fingerprint",
    "user_agent",
    "browser",
    "browser_version",
    "os",
    "os_version",
    "device_type",
    "screen_width",
    "screen_height",
    "languages",
    "timezone",
    "last_country",
)


class CRUDVisitor(CRUDBase[Visitor]):
    """CRUD operations for visitors (first-party web analytics)."""

    def get_by_id(self, db: Session, *, visitor_id: UUID) -> Optional[Visitor]:
        """Get a visitor by its client-generated UUID."""
        return db.query(Visitor).filter(Visitor.id == visitor_id).first()

    def upsert_observation(
        self,
        db: Session,
        *,
        visitor_id: UUID,
        profile: Optional[Dict[str, Any]] = None,
        attribution: Optional[Dict[str, Any]] = None,
        count_session: bool = False,
        seen_at: Optional[datetime] = None,
        commit: bool = True,
    ) -> Visitor:
        """Insert or update a visitor from a session observation.

        Attribution fields are written only when currently unset
        (first-touch); profile fields always reflect the latest observation.

        Args:
            db: Database session.
            visitor_id: Client-generated visitor UUID.
            profile: Latest device profile values (subset of profile fields).
            attribution: First-touch attribution values.
            count_session: Increment ``session_count`` for this observation.
            seen_at: Observation time (defaults to now).
            commit: Commit the transaction (disable inside larger units).

        Returns:
            The persisted visitor row.
        """
        now = seen_at or datetime.now(timezone.utc)
        visitor = self.get_by_id(db, visitor_id=visitor_id)
        if visitor is None:
            visitor = Visitor(
                id=visitor_id, first_seen=now, last_seen=now, session_count=0
            )
            db.add(visitor)
            # Make the row visible to same-transaction reads (sessions may
            # run with autoflush disabled).
            db.flush()

        visitor.last_seen = max(now, visitor.last_seen if visitor.last_seen else now)
        if count_session:
            visitor.session_count = (visitor.session_count or 0) + 1

        if profile:
            for field in _PROFILE_FIELDS:
                value = profile.get(field)
                if value is not None:
                    setattr(visitor, field, value)

        if attribution:
            for field in _ATTRIBUTION_FIELDS:
                value = attribution.get(field)
                if value is not None and getattr(visitor, field) is None:
                    setattr(visitor, field, value)

        if commit:
            db.commit()
            db.refresh(visitor)
        else:
            db.flush()
        return visitor

    def link_user(
        self,
        db: Session,
        *,
        visitor_id: UUID,
        user_id: UUID,
        commit: bool = True,
    ) -> Optional[Visitor]:
        """Record the first identified user for a visitor (write-once)."""
        visitor = self.get_by_id(db, visitor_id=visitor_id)
        if visitor is None:
            return None
        if visitor.user_id is None:
            visitor.user_id = user_id
            if commit:
                db.commit()
        return visitor


crud_visitor = CRUDVisitor(Visitor)
