"""Session management for unified WebSocket connections."""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import WebSocket
from sqlalchemy.orm import Session

from preloop.models.models import User, Event
from preloop.services.db_executor import run_db_async

logger = logging.getLogger(__name__)


@dataclass
class WebSocketSession:
    """Represents an active WebSocket session.

    This tracks the runtime state of a WebSocket connection. The persistent
    session data is stored in Event table in the database.
    """

    id: str
    connection_id: str
    websocket: WebSocket
    user_id: Optional[uuid.UUID]
    account_id: Optional[uuid.UUID]
    username: Optional[str]
    fingerprint: Optional[str]
    ip_address: str
    user_agent: str
    connected_at: datetime
    last_activity: datetime
    metadata: dict = field(default_factory=dict)

    @property
    def is_authenticated(self) -> bool:
        """Check if this is an authenticated session."""
        return self.user_id is not None

    @property
    def display_name(self) -> str:
        """Human-readable identity for logs without ORM lazy loads."""
        if self.username:
            return self.username
        if self.user_id:
            return str(self.user_id)
        if self.fingerprint:
            return f"anonymous {self.fingerprint[:8]}"
        return "anonymous"


class SessionManager:
    """Manages WebSocket sessions and tracks user activity.

    This class:
    - Tracks active WebSocket sessions in memory
    - Persists session events to Event table
    - Manages session lifecycle (connect/disconnect)
    - Tracks last activity for heartbeat monitoring
    """

    def __init__(self):
        """Initialize the session manager."""
        self.sessions: Dict[str, WebSocketSession] = {}
        self.connection_to_session: Dict[str, str] = {}  # connection_id -> session_id

    async def create_session(
        self,
        websocket: WebSocket,
        user: Optional[User],
        fingerprint: Optional[str],
        ip_address: str,
        user_agent: str,
    ) -> WebSocketSession:
        """Create a new WebSocket session."""
        session_id = str(uuid.uuid4())
        connection_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        session = WebSocketSession(
            id=session_id,
            connection_id=connection_id,
            websocket=websocket,
            user_id=user.id if user else None,
            account_id=user.account_id if user else None,
            username=user.username if user else None,
            fingerprint=fingerprint,
            ip_address=ip_address,
            user_agent=user_agent,
            connected_at=now,
            last_activity=now,
            metadata={},
        )

        self.sessions[session_id] = session
        self.connection_to_session[connection_id] = session_id

        activity = Event(
            session_id=uuid.UUID(session_id),
            user_id=session.user_id,
            account_id=session.account_id,
            fingerprint=session.fingerprint,
            event_type="session_start",
            timestamp=now,
            ip_address=ip_address,
            user_agent=user_agent,
            event_data={
                "connection_id": connection_id,
                "authenticated": session.is_authenticated,
            },
        )

        def _persist_event(db: Session) -> None:
            db.add(activity)
            db.commit()

        await run_db_async(_persist_event)

        logger.info(
            f"Created session {session_id} for {session.display_name} from {ip_address}"
        )

        return session

    async def end_session(self, session_id: str) -> None:
        """End a WebSocket session."""
        session = self.sessions.pop(session_id, None)
        if not session:
            logger.warning(f"Attempted to end non-existent session {session_id}")
            return

        if session.connection_id in self.connection_to_session:
            del self.connection_to_session[session.connection_id]

        now = datetime.now(timezone.utc)
        duration_seconds = (now - session.connected_at).total_seconds()

        activity = Event(
            session_id=uuid.UUID(session_id),
            user_id=session.user_id,
            account_id=session.account_id,
            fingerprint=session.fingerprint,
            event_type="session_end",
            timestamp=now,
            ip_address=session.ip_address,
            event_data={
                "connection_id": session.connection_id,
                "duration_seconds": duration_seconds,
            },
        )

        def _persist_event(db: Session) -> None:
            db.add(activity)
            db.commit()

        await run_db_async(_persist_event)

        logger.info(
            f"Ended session {session_id} after {duration_seconds:.1f}s "
            f"for {session.display_name}"
        )

    def get_session(self, session_id: str) -> Optional[WebSocketSession]:
        """Get session by ID."""
        return self.sessions.get(session_id)

    def get_session_by_connection(
        self, connection_id: str
    ) -> Optional[WebSocketSession]:
        """Get session by connection ID."""
        session_id = self.connection_to_session.get(connection_id)
        if session_id:
            return self.sessions.get(session_id)
        return None

    def update_activity(self, session_id: str) -> None:
        """Update last activity timestamp for a session."""
        session = self.sessions.get(session_id)
        if session:
            session.last_activity = datetime.now(timezone.utc)

    def get_active_sessions_count(self) -> int:
        """Get count of active sessions."""
        return len(self.sessions)

    def get_sessions_for_account(self, account_id: uuid.UUID) -> list[WebSocketSession]:
        """Get all active sessions for an account."""
        return [s for s in self.sessions.values() if s.account_id == account_id]

    async def upgrade_session(
        self, session_id: str, user: User
    ) -> Optional[WebSocketSession]:
        """Upgrade an anonymous session to authenticated."""
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"Cannot upgrade non-existent session {session_id}")
            return None

        if session.user_id:
            logger.warning(f"Session {session_id} is already authenticated")
            return session

        session.user_id = user.id
        session.account_id = user.account_id
        session.username = user.username

        now = datetime.now(timezone.utc)
        activity = Event(
            session_id=uuid.UUID(session_id),
            user_id=user.id,
            account_id=user.account_id,
            fingerprint=session.fingerprint,
            event_type="session_authenticated",
            timestamp=now,
            ip_address=session.ip_address,
            user_agent=session.user_agent,
            event_data={
                "connection_id": session.connection_id,
                "username": user.username,
            },
        )

        def _persist_event(db: Session) -> None:
            db.add(activity)
            db.commit()

        await run_db_async(_persist_event)

        logger.info(
            f"Upgraded session {session_id} to authenticated user {user.username}"
        )

        return session


# Global singleton instance
session_manager = SessionManager()
