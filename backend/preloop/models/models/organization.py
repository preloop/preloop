"""Organization model."""

# Import at the end to avoid circular imports
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from preloop.models.db.encrypted_types import EncryptedText

from .base import Base
from .tracker import Tracker

if TYPE_CHECKING:
    from .project import Project
    from .account import Account


class Organization(Base):
    """Organization model - a top-level entity that can contain multiple projects.

    An organization is owned by a single account through a tracker.
    """

    __tablename__ = "organization"

    # Organization details
    name: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # Assuming String is imported if still needed
    identifier: Mapped[str] = mapped_column(
        String(100),
        unique=False,
        nullable=False,
        index=True,  # Assuming String is imported
    )
    description: Mapped[Optional[str]] = mapped_column(
        String(1000), nullable=True
    )  # Assuming String is imported
    is_active: Mapped[bool] = mapped_column(default=True)

    # Organization settings stored as JSON
    settings: Mapped[Optional[Dict]] = mapped_column(JSON, nullable=True, default=dict)

    # Generic metadata field for extensibility
    meta_data: Mapped[Dict] = mapped_column(JSON, nullable=True, default=dict)

    # Secret for verifying incoming webhooks (e.g., HMAC signature)
    # Sealed at rest (EncryptedText); ORM reads return plaintext.
    webhook_secret: Mapped[Optional[str]] = mapped_column(
        EncryptedText(), nullable=True
    )

    # Timestamps for sync updates
    last_webhook_update: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_polling_update: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Foreign keys - the tracker determines the owner account
    tracker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracker.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    tracker: Mapped["Tracker"] = relationship("Tracker", back_populates="organizations")
    projects: Mapped[List["Project"]] = relationship(
        "Project", back_populates="organization", cascade="all, delete-orphan"
    )

    @property
    def owner(self) -> "Account":
        """Get the owner account of this organization through the tracker."""
        return self.tracker.account
