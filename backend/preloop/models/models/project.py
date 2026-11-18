"""Project model."""

import datetime
import uuid

# Use TYPE_CHECKING to avoid circular imports
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sqlalchemy import DateTime, ForeignKey, String, event, inspect
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .base import Base

if TYPE_CHECKING:
    from .issue import Issue
    from .organization import Organization


class Project(Base):
    """Project model - belongs to an organization."""

    # Project details
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    identifier: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)

    # Foreign keys
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Project settings stored as JSON
    settings: Mapped[Optional[Dict]] = mapped_column(JSON, nullable=True, default=dict)

    # Project-specific tracker settings
    # For configuring project-specific keys, filters, etc.
    tracker_settings: Mapped[Optional[Dict]] = mapped_column(
        JSON, nullable=True, default=dict
    )

    # Generic metadata field for extensibility
    meta_data: Mapped[Dict] = mapped_column(JSON, nullable=True, default=dict)

    # Timestamp for the last webhook verification/registration attempt for this project
    # Used by Preloop Sync for GitLab CE project-level webhook handling.
    webhook_last_verified_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="projects"
    )
    issues: Mapped[List["Issue"]] = relationship(
        "Issue", back_populates="project", cascade="all, delete-orphan"
    )


#: ``meta_data`` key listing every slug a project had before its current one.
FORMER_SLUGS_KEY = "former_slugs"

#: ``meta_data`` flag set once the prefixes of a project's existing issue keys
#: have been recorded in ``FORMER_SLUGS_KEY``.
KEY_PREFIXES_RECORDED_KEY = "issue_key_prefixes_recorded"


@event.listens_for(Project.slug, "set", active_history=True)
def _load_previous_slug(target: Project, value: Any, old: Any, initiator: Any) -> Any:
    """Load the previous slug before it is replaced (see below).

    Without ``active_history`` an expired instance would not know its old
    slug at flush time.

    Args:
        target: The project.
        value: The new slug.
        old: The previous slug.
        initiator: The attribute event token.

    Returns:
        The new slug, unchanged.
    """
    return value


@event.listens_for(Project, "before_update")
def _record_former_slug(mapper: Any, connection: Any, target: Project) -> None:
    """Remember the previous slug whenever a project's slug changes.

    Issue keys synced under a former repository or path name keep that name
    until the issue changes upstream, so lookups that compose keys from the
    slug also need the former ones. Runs at flush time, after every attribute
    assignment, so it covers renames by sync and by the transfer endpoint.

    Args:
        mapper: The Project mapper.
        connection: The flush connection.
        target: The project being updated.
    """
    history = inspect(target).attrs.slug.history
    previous = [old for old in history.deleted if old and old != target.slug]
    if not previous:
        return
    meta = dict(target.meta_data or {})
    former = list(meta.get(FORMER_SLUGS_KEY) or [])
    for old in previous:
        if old not in former:
            former.append(old)
    meta[FORMER_SLUGS_KEY] = former
    target.meta_data = meta  # reassign: JSON column mutations are untracked
