"""Unified event tracking model for all account activity."""

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import DateTime, Float, String, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from preloop.models.models.base import Base
from preloop.models.models.account import Account
from preloop.models.models.user import User


class Event(Base):
    """Unified event tracking for all account activity - user sessions, actions, and system events.

    This model provides a single source of truth for all events in the system:
    - User session lifecycle (WebSocket connections)
    - User interactions (page views, actions, conversions)
    - System events (flow executions, tracker polls, webhooks)

    Primary Grouping: ACCOUNT
        - All events are grouped by account_id
        - User events have session_id + user_id (or fingerprint for anonymous)
        - System events have account_id only (no session_id)

    Event Categories:
        User Session Events:
          - session_start: WebSocket connection established
          - session_end: WebSocket disconnection
          - session_hello: Rich device/attribution blob sent once per session
          - page_view: User navigated to a page
          - action: User performed an action (click, form submit, etc.)
          - conversion: User completed a conversion event

        Check-in Events (written by the EE growth plugin; ``fingerprint``
        holds the instance uuid / CLI client id as the correlation key):
          - instance_check_in: A tracked OSS/EE instance version check
          - cli_check_in: A tracked CLI install version check

        System Events:
          - flow_execution_started: Flow execution initiated
          - flow_execution_completed: Flow execution finished
          - flow_execution_failed: Flow execution failed
          - tracker_poll_completed: Tracker polling finished
          - webhook_received: External webhook received
          - background_task_completed: Background job finished

    This unified approach enables:
        - Complete account activity timeline (users + system)
        - Real-time monitoring of active connections
        - User journey tracking alongside system events
        - Conversion funnel analysis
        - Geographic user visualization
        - Efficient querying by account, session, or user

    Note: This is kept separate from AuditLog which is specifically for
    security/compliance events (permissions, auth, role changes) with
    tamper-proof logging and longer retention policies.
    """

    __tablename__ = "event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Account identification - REQUIRED for all events
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Account this event belongs to (required for most events)",
    )

    # Session identification - OPTIONAL (null for system events)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="WebSocket session ID (null for system/async events)",
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="User who triggered event (null for system events or anonymous)",
    )
    fingerprint: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        index=True,
        comment="Browser fingerprint for anonymous users",
    )

    # Event classification
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Event type: session_start, page_view, action, conversion, flow_execution_*, etc.",
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
        comment="When the event occurred",
    )

    # Connection metadata (populated for session_start/session_end user events)
    ip_address: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
        index=True,
        comment="IP address (IPv6 max length: 45)",
    )
    user_agent: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True, comment="Browser user agent string"
    )

    # Analytics enrichment (populated by the EE growth plugin; NULL on OSS)
    visitor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="Client-generated visitor id (see Visitor model)",
    )
    client_type: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        index=True,
        comment="web | mobile_web | ios | android | cli | oss_instance | api | system",
    )
    browser: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="Browser family parsed from user agent"
    )
    os: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="Operating system parsed from user agent"
    )

    # Geolocation (populated from IP for session_start)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    region: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Page view data (populated for page_view events)
    path: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True, index=True
    )  # URL path
    referrer: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )  # Previous page

    # Action data (populated for action events)
    action: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )  # Action name (e.g., "click_signup_button")
    element: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )  # HTML element type
    element_text: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )  # Element text content

    # Conversion tracking (populated for conversion events)
    conversion_event: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )  # Conversion event name
    conversion_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )  # Monetary value

    # Additional context - flexible metadata for any event-specific data
    event_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Relationships
    user: Mapped[Optional["User"]] = relationship(
        "User", back_populates="events", foreign_keys=[user_id]
    )
    account: Mapped[Optional["Account"]] = relationship(
        "Account", back_populates="events", foreign_keys=[account_id]
    )

    def __repr__(self) -> str:
        # Determine source: user, anonymous, or system
        if self.user_id:
            source = f"user_id={self.user_id}"
        elif self.fingerprint:
            source = f"fp={self.fingerprint[:8]}"
        else:
            source = "system"

        # Include session if present
        session_info = f" session={str(self.session_id)[:8]}" if self.session_id else ""

        return f"<Event {self.event_type} {source}{session_info} account={str(self.account_id)[:8] if self.account_id else 'none'}>"
