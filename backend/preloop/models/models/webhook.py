"""Webhook model."""

import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import ForeignKey, String, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from preloop.models.db.encrypted_types import EncryptedText

from .base import Base

if TYPE_CHECKING:
    from .project import Project
    from .organization import Organization


class Webhook(Base):
    """Webhook model - represents a registered webhook."""

    __tablename__ = "webhook"

    # Webhook details
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Sealed at rest (EncryptedText); ORM reads return plaintext.
    secret: Mapped[str] = mapped_column(EncryptedText(), nullable=False)
    events: Mapped[List[str]] = mapped_column(JSON, nullable=False)

    # Verified namespace retained when the callback's original grant disconnects.
    jira_cloud_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    jira_provider_subject: Mapped[Optional[str]] = mapped_column(
        String(1000), nullable=True
    )

    # Foreign keys
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Relationships
    project: Mapped["Project"] = relationship("Project")
    organization: Mapped["Organization"] = relationship("Organization")
