"""CRUD operations for CLI login sessions (issue #839)."""

import uuid
from datetime import UTC, datetime
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models.cli_session import CliSession

# Columns are String(255); a client-supplied value longer than that is cut.
_MAX_TEXT = 255


def _clip(value: Optional[str]) -> Optional[str]:
    """Return ``value`` stripped and cut to the column width, or None."""
    if value is None:
        return None
    value = value.strip()
    return value[:_MAX_TEXT] or None


class CRUDCliSession:
    """CRUD for ``cli_session`` rows."""

    def create(
        self,
        db: Session,
        *,
        user_id: uuid.UUID,
        refresh_jti: str,
        user_agent: Optional[str] = None,
        hostname: Optional[str] = None,
    ) -> CliSession:
        """Record a new CLI login.

        Args:
            db: Database session.
            user_id: The user who signed in.
            refresh_jti: ``jti`` of the refresh token being issued.
            user_agent: ``User-Agent`` of the code exchange request.
            hostname: Host name reported by the CLI.

        Returns:
            The new session row.
        """
        obj = CliSession(
            id=uuid.uuid4(),
            user_id=user_id,
            refresh_jti=refresh_jti,
            last_seen_at=datetime.now(UTC),
            user_agent=_clip(user_agent),
            hostname=_clip(hostname),
        )
        db.add(obj)
        db.commit()
        return obj

    def get(self, db: Session, *, session_id: uuid.UUID) -> Optional[CliSession]:
        """Return the session row, revoked or not."""
        return db.get(CliSession, session_id)

    def is_active(
        self, db: Session, *, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Return True when the session exists for ``user_id`` and is not revoked."""
        stmt = select(CliSession.id).where(
            CliSession.id == session_id,
            CliSession.user_id == user_id,
            CliSession.revoked_at.is_(None),
        )
        return db.execute(stmt).first() is not None

    def rotate(
        self,
        db: Session,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        old_jti: str,
        new_jti: str,
    ) -> bool:
        """Swap the refresh ``jti`` if ``old_jti`` is still the current one.

        The compare and swap happens in one UPDATE, so two requests that
        present the same refresh token cannot both rotate it.

        Returns:
            True when the row was active and ``old_jti`` matched.
        """
        stmt = (
            update(CliSession)
            .where(
                CliSession.id == session_id,
                CliSession.user_id == user_id,
                CliSession.refresh_jti == old_jti,
                CliSession.revoked_at.is_(None),
            )
            .values(refresh_jti=new_jti, last_seen_at=datetime.now(UTC))
            .returning(CliSession.id)
        )
        rotated = db.execute(stmt).first() is not None
        db.commit()
        return rotated

    def revoke(self, db: Session, *, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Mark the session revoked.

        Returns:
            True when an active session owned by ``user_id`` was revoked.
        """
        stmt = (
            update(CliSession)
            .where(
                CliSession.id == session_id,
                CliSession.user_id == user_id,
                CliSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
            .returning(CliSession.id)
        )
        revoked = db.execute(stmt).first() is not None
        db.commit()
        return revoked

    def revoke_all(
        self, db: Session, *, user_id: uuid.UUID, commit: bool = True
    ) -> int:
        """Mark every active session of ``user_id`` revoked.

        Args:
            db: Database session.
            user_id: Owner of the sessions.
            commit: Commit immediately. Pass False to join the caller's
                transaction (sign out everywhere bumps the generation in the
                same commit).

        Returns:
            The number of sessions revoked.
        """
        stmt = (
            update(CliSession)
            .where(CliSession.user_id == user_id, CliSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
            .returning(CliSession.id)
        )
        revoked = len(db.execute(stmt).all())
        if commit:
            db.commit()
        return revoked

    def list_active(self, db: Session, *, user_id: uuid.UUID) -> List[CliSession]:
        """Return the user's unrevoked sessions, most recently used first."""
        stmt = (
            select(CliSession)
            .where(CliSession.user_id == user_id, CliSession.revoked_at.is_(None))
            .order_by(CliSession.last_seen_at.desc().nulls_last())
        )
        return list(db.execute(stmt).scalars().all())


crud_cli_session = CRUDCliSession()
