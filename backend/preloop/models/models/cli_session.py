"""Server-side record for one CLI login (issue #839).

``preloop auth login`` exchanges its code at ``/oauth/token`` without PKCE and
receives JWTs. Each such login creates one ``cli_session`` row. The access
and refresh tokens carry the row id as ``sid``; the refresh token also carries
a ``jti`` that must match ``refresh_jti``. Rotation writes a new ``jti`` and
``last_seen_at``, so a refresh token that was already rotated away is
rejected. Setting ``revoked_at`` (``POST /oauth/revoke``,
``preloop auth logout``, ``preloop auth sessions revoke``) rejects both the
access and the refresh token of that login on their next use.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CliSession(Base):
    """One CLI JWT login, revocable on its own.

    Attributes:
        user_id: The user who signed in.
        refresh_jti: ``jti`` of the only refresh token that may rotate next.
        last_seen_at: When the session last minted tokens (login or refresh).
        revoked_at: When the session was revoked; null while active.
        user_agent: ``User-Agent`` sent with the code exchange.
        hostname: Host name the CLI reported at login.
    """

    __tablename__ = "cli_session"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    refresh_jti: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="jti of the current refresh token; older ones are rejected",
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    hostname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
