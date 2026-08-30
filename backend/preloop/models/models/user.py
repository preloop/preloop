"""User model for individual users within accounts."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Boolean

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .api_key import ApiKey
    from .api_usage import ApiUsage
    from .audit_log import AuditLog
    from .team import TeamMembership
    from .permission import UserRole
    from .notification_preferences import NotificationPreferences
    from .event import Event
    from .github_oauth_token import OAuthToken


class UserSource(str):
    """Source of user authentication."""

    LOCAL = "local"
    LDAP = "ldap"
    AD = "ad"
    SAML = "saml"
    OAUTH = "oauth"


class User(Base):
    """User model for individual users within an account.

    In the multi-user system, Users belong to Accounts. Each User can have
    different roles and permissions within their Account.

    Attributes:
        id: Unique identifier for the user.
        account_id: The account this user belongs to.
        username: Unique username for the user.
        email: User's email address.
        email_verified: Whether the email has been verified.
        full_name: User's full name.
        hashed_password: Hashed password (null for external auth).
        is_active: Whether the user account is active.
        is_superuser: Whether the user has superuser/admin privileges (platform-wide access).
        user_source: Source of authentication ('local', 'ldap', 'ad', 'saml', 'oauth').
        oauth_provider: OAuth provider if user_source is 'oauth'.
        oauth_id: OAuth provider's user ID.
        external_id: External system's user ID (for LDAP/AD/SAML).
        last_login: When the user last logged in.
        created_at: When the user was created.
        updated_at: When the user was last updated.
    """

    __tablename__ = "user"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE", name="fk_user_account"),
        nullable=False,
        index=True,
        comment="The account this user belongs to",
    )

    # User details
    username: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True, comment="Unique username"
    )
    email: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True, comment="User's email address"
    )
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="Whether the email has been verified"
    )
    full_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="User's full name"
    )

    # Authentication
    hashed_password: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="Hashed password (null for external auth)"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="Whether the user account is active"
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        comment="Whether the user has superuser/admin privileges",
    )

    # External authentication
    user_source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=UserSource.LOCAL,
        index=True,
        comment="Source of authentication: 'local', 'ldap', 'ad', 'saml', 'oauth'",
    )
    oauth_provider: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="OAuth provider if user_source is 'oauth'"
    )
    oauth_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="OAuth provider's user ID"
    )
    external_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="External system's user ID (for LDAP/AD/SAML)",
    )

    # Profile image
    avatar_url: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Profile image URL or base64 data URI",
    )
    avatar_source: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        comment="Avatar provenance: 'sso' or 'manual'",
    )

    # Timestamps
    last_login: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the user last logged in"
    )

    # Relationships
    account: Mapped["Account"] = relationship(
        "Account", back_populates="users", foreign_keys="[User.account_id]"
    )
    api_keys: Mapped[List["ApiKey"]] = relationship(
        "ApiKey", back_populates="creator", cascade="all, delete-orphan"
    )
    # No delete cascade: usage rows must survive user deletion so account
    # cost history stays intact (the DB FK is ON DELETE SET NULL); the ORM
    # nulls user_id instead of deleting rows.
    api_usages: Mapped[List["ApiUsage"]] = relationship(
        "ApiUsage", back_populates="user"
    )
    team_memberships: Mapped[List["TeamMembership"]] = relationship(
        "TeamMembership",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="[TeamMembership.user_id]",
    )
    roles: Mapped[List["UserRole"]] = relationship(
        "UserRole",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="[UserRole.user_id]",
    )
    # No delete cascade: audit logs must survive user deletion (the DB FK is
    # ON DELETE SET NULL); the ORM nulls user_id instead of deleting rows.
    audit_logs: Mapped[List["AuditLog"]] = relationship(
        "AuditLog", back_populates="user"
    )
    notification_preferences: Mapped[Optional["NotificationPreferences"]] = (
        relationship(
            "NotificationPreferences",
            back_populates="user",
            uselist=False,  # 1:1 relationship
            cascade="all, delete-orphan",
        )
    )
    # No delete cascade: events must survive user deletion (the DB FK is
    # ON DELETE SET NULL); the ORM nulls user_id instead of deleting rows.
    events: Mapped[List["Event"]] = relationship(
        "Event",
        back_populates="user",
        foreign_keys="[Event.user_id]",
    )
    oauth_tokens: Mapped[List["OAuthToken"]] = relationship(
        "OAuthToken", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<User(username={self.username}, email={self.email}, account_id={self.account_id})>"

    @property
    def is_local_user(self) -> bool:
        """Check if user uses local authentication."""
        return self.user_source == UserSource.LOCAL

    @property
    def is_external_user(self) -> bool:
        """Check if user uses external authentication."""
        return self.user_source != UserSource.LOCAL
