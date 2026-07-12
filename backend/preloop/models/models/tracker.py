"""Tracker model and related types."""

import enum
import uuid
from datetime import datetime

# Use TYPE_CHECKING to avoid circular imports
from typing import TYPE_CHECKING, Dict, List, Optional

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.types import JSON, DateTime

from .base import Base
from .tracker_scope_rule import TrackerScopeRule

_MISSING = object()

if TYPE_CHECKING:
    from .account import Account
    from .comment import Comment
    from .issue import Issue
    from .organization import Organization
    from .github_app_installation import OAuthAppInstallation
    from .secret_reference import SecretReference


class TrackerType(enum.Enum):
    """Enum for tracker types."""

    GITHUB = "github"
    GITLAB = "gitlab"
    JIRA = "jira"


class Tracker(Base):
    """Tracker model - represents an integration with an issue tracking system.

    A tracker is owned by a single account and determines ownership of organizations.
    The account that owns a tracker is considered the owner of all organizations
    linked to that tracker. This provides a clear ownership hierarchy:

    Account -> Tracker -> Organization -> Projects

    Where each entity is owned by the entity to its left.
    """

    # Tracker details
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tracker_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Possible values: github, gitlab, jira"
    )
    url: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
        comment="URL to the tracker (required for Jira, optional for others)",
    )
    api_key: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
        comment=(
            "Legacy plaintext API key/token. New writes store the credential in "
            "a SecretReference (credentials_secret_id) and leave this NULL; read "
            "via the resolved_api_key property."
        ),
    )
    credentials_secret_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Secret reference holding the encrypted tracker API key/token",
    )
    webhook_secret_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Secret reference holding the encrypted Jira webhook secret",
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    is_deleted: Mapped[bool] = mapped_column(
        default=False, index=True, comment="Flag for soft deletion"
    )
    is_owner_managed: Mapped[bool] = mapped_column(
        default=True,
        comment="If True, the account that owns this tracker also owns all organizations linked to it",
    )

    # Additional connection details stored as JSON
    # Structure examples:
    # GitHub: {
    #     "repository": "owner/repo",
    #     "private_key_path": "/path/to/key.pem",  # For GitHub Apps
    #     "app_id": "12345",                       # For GitHub Apps
    #     "installation_id": "67890"               # For GitHub Apps
    # }
    # GitLab: {
    #     "project_id": "12345",
    #     "group_path": "my-group"
    # }
    # Jira: {
    #     "project_key": "PROJECT",
    #     "cloud_id": "cloud-id-for-jira-cloud",
    #     "use_oauth": true,
    #     "oauth_settings": {...}
    # }
    connection_details: Mapped[Dict] = mapped_column(JSON, nullable=True, default=dict)

    # Generic metadata field for extensibility
    meta_data: Mapped[Dict] = mapped_column(JSON, nullable=True, default=dict)

    # Webhook event subscriptions
    subscribed_events: Mapped[Optional[List[str]]] = mapped_column(
        JSON,
        nullable=True,
        default=list,
        comment="List of specific webhook event names to subscribe to (e.g., ['push', 'issues']). Empty or None might imply default/all events based on client logic.",
    )

    # Jira Webhook specific fields
    jira_webhook_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="Stored Jira Webhook ID"
    )
    jira_webhook_secret: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Secret used to validate incoming Jira webhooks. Store encrypted if possible.",
    )

    # Authentication type for trackers
    auth_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="api_token",
        comment="Authentication type: 'api_token', 'github_app', or 'oauth_app'",
    )

    # Foreign keys
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    oauth_installation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_app_installation.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Reference to OAuth App installation for oauth_app auth type",
    )

    # Relationships
    account: Mapped["Account"] = relationship("Account", back_populates="trackers")
    credentials_secret: Mapped[Optional["SecretReference"]] = relationship(
        "SecretReference", foreign_keys=[credentials_secret_id]
    )
    webhook_secret: Mapped[Optional["SecretReference"]] = relationship(
        "SecretReference", foreign_keys=[webhook_secret_id]
    )
    organizations: Mapped[List["Organization"]] = relationship(
        "Organization", back_populates="tracker", cascade="all, delete-orphan"
    )
    issues: Mapped[List["Issue"]] = relationship(
        "Issue", back_populates="tracker", cascade="all, delete-orphan"
    )
    scope_rules: Mapped[List["TrackerScopeRule"]] = relationship(
        "TrackerScopeRule",
        back_populates="tracker",
        cascade="all, delete-orphan",
    )
    comments: Mapped[List["Comment"]] = relationship(
        "Comment", back_populates="tracker", cascade="all, delete-orphan"
    )
    oauth_installation: Mapped[Optional["OAuthAppInstallation"]] = relationship(
        "OAuthAppInstallation",
        back_populates="trackers",
        foreign_keys=[oauth_installation_id],
    )

    # Backward compatibility alias
    @property
    def github_installation(self) -> Optional["OAuthAppInstallation"]:
        """Alias for oauth_installation (GitHub compatibility)."""
        return self.oauth_installation

    @property
    def github_installation_id(self) -> Optional[uuid.UUID]:
        """Alias for oauth_installation_id (GitHub compatibility)."""
        return self.oauth_installation_id

    @property
    def resolved_api_key(self) -> str:
        """Return the tracker API key/token, decrypting the SecretReference.

        Falls back to the legacy plaintext ``api_key`` column for rows not yet
        migrated. Callers must use this instead of ``api_key`` directly.

        The decrypted value is cached on the instance for the lifetime of the
        ORM object so listing/scanning paths do not re-decrypt on every access.
        """
        cached = getattr(self, "_resolved_api_key_cache", None)
        if cached is not None:
            return cached
        if self.credentials_secret is not None:
            # Local import avoids a models -> services import cycle at load time.
            from preloop.services.secret_service import get_secret_service

            value = (
                get_secret_service()
                .resolve_secret_reference(self.credentials_secret)
                .value
            )
        else:
            value = self.api_key or ""
        object.__setattr__(self, "_resolved_api_key_cache", value)
        return value

    @property
    def resolved_webhook_secret(self) -> Optional[str]:
        """Return the Jira webhook secret, decrypting the SecretReference.

        Falls back to the legacy plaintext ``jira_webhook_secret`` column.
        Cached on the instance like :attr:`resolved_api_key`.
        """
        cached = getattr(self, "_resolved_webhook_secret_cache", _MISSING)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        if self.webhook_secret is not None:
            from preloop.services.secret_service import get_secret_service

            value = (
                get_secret_service().resolve_secret_reference(self.webhook_secret).value
            )
        else:
            value = self.jira_webhook_secret
        object.__setattr__(self, "_resolved_webhook_secret_cache", value)
        return value

    # Validation status
    is_valid: Mapped[bool] = mapped_column(default=False)
    last_validation: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    validation_message: Mapped[Optional[str]] = mapped_column(
        String(1000), nullable=True
    )

    # Timestamps
    created: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @validates("tracker_type")
    def validate_tracker_type(self, key, type_):
        """Validate that tracker_type is one of the allowed values."""
        if type_ not in [t.value for t in TrackerType]:
            raise ValueError(
                f"Invalid tracker type: {type_}. Must be one of: {', '.join([t.value for t in TrackerType])}"
            )
        return type_

    @validates("url")
    def validate_url(self, key, url):
        """Validate URL is provided for Jira trackers."""
        if self.tracker_type == TrackerType.JIRA.value and not url:
            raise ValueError("URL is required for Jira trackers")
        return url
