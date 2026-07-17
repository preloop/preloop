"""Visitor model: one row per browser install for first-party analytics."""

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from preloop.models.models.base import Base


class Visitor(Base):
    """A distinct browser install observed by first-party web analytics.

    The primary key is a client-generated UUID persisted in localStorage and
    mirrored to a first-party cookie, sent alongside the (config-derived)
    fingerprint on every WebSocket session. First-touch attribution columns
    are write-once; the device profile reflects the latest ``session_hello``.

    Rows are written and consumed exclusively by the EE growth plugin — OSS
    deployments have the table but nothing populates it.
    """

    __tablename__ = "visitor"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        comment="Client-generated visitor UUID (localStorage + cookie)",
    )
    fingerprint: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        index=True,
        comment="Most recent browser-config fingerprint seen for this visitor",
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="First identified user for this visitor (convenience link)",
    )

    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # First-touch attribution (write-once: set on first observation only)
    first_landing_path: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    first_referrer: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    utm_source: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    utm_medium: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    utm_campaign: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    utm_term: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    utm_content: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Latest device profile (from session_hello + user-agent parsing)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    browser: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    browser_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    os: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    os_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    device_type: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True, comment="desktop | mobile | tablet | bot | other"
    )
    screen_width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    screen_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    languages: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    def __repr__(self) -> str:
        return f"<Visitor {str(self.id)[:8]} sessions={self.session_count}>"
