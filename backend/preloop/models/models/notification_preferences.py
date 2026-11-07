"""Notification preferences model for user notification settings."""

import uuid
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Boolean, DateTime
from sqlalchemy.sql import func

from .base import Base

if TYPE_CHECKING:
    from .user import User


class NotificationPreferences(Base):
    """User notification preferences for approval requests.

    Stores per-user notification settings including preferred channels
    and mobile device tokens for push notifications.

    This model has a 1:1 relationship with User. Each user can have
    one set of notification preferences.

    Attributes:
        id: Unique identifier for the preferences.
        user_id: Reference to the user (unique, 1:1).
        preferred_channel: Preferred notification channel ('email' or 'mobile_push').
        mobile_device_tokens: List of mobile device tokens for push notifications.
            Format: [{platform: 'ios'|'android', token: '...', registered_at: '...',
            apns_environment: 'sandbox'|'production' (iOS only, optional)}]
        enable_email: Whether email notifications are enabled.
        enable_mobile_push: Whether mobile push notifications are enabled.
        created_at: When the preferences were created.
        updated_at: When the preferences were last updated.
    """

    __tablename__ = "notification_preferences"

    weekly_report_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # 1:1 relationship
        index=True,
        comment="Reference to the user",
    )

    preferred_channel: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="email",
        comment="Preferred notification channel: 'email' or 'mobile_push'",
    )

    mobile_device_tokens: Mapped[Optional[List[Dict]]] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "List of mobile device tokens: [{platform: 'ios'|'android', "
            "token: '...', registered_at: '...', apns_environment: "
            "'sandbox'|'production' (iOS only, optional)}]"
        ),
    )

    enable_email: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether email notifications are enabled",
    )

    enable_mobile_push: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether mobile push notifications are enabled",
    )

    stagger_email: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment=(
            "When True and push is enabled, delay approval email until the "
            "request is still pending after the stagger window"
        ),
    )

    notify_when_needed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Push when the agent needs the operator (approvals, ask_user)",
    )

    # Timestamps
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the preferences were created",
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="When the preferences were last updated",
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User",
        back_populates="notification_preferences",
        uselist=False,  # 1:1 relationship
    )

    def __repr__(self) -> str:
        """String representation."""
        channels = []
        if self.enable_email:
            channels.append("email")
        if self.enable_mobile_push:
            channels.append("mobile_push")
        device_count = (
            len(self.mobile_device_tokens) if self.mobile_device_tokens else 0
        )
        return (
            f"<NotificationPreferences(user_id={self.user_id}, "
            f"preferred={self.preferred_channel}, channels={channels}, devices={device_count})>"
        )

    def add_device_token(
        self,
        platform: str,
        token: str,
        apns_environment: Optional[str] = None,
    ) -> None:
        """Add a mobile device token.

        Args:
            platform: Device platform ('ios' or 'android').
            token: Device push notification token.
            apns_environment: For iOS, the APNs environment that issued the
                token ('sandbox' or 'production'). Xcode development builds
                get sandbox tokens, which production APNs rejects. None
                means the app did not report it.

        Raises:
            ValueError: If the token is blank. Storing an empty token makes
                every later push fail with APNs 400 MissingDeviceToken while
                the account still *looks* push-enabled.
        """
        token = (token or "").strip()
        if not token:
            raise ValueError("Device push token must not be empty")

        if self.mobile_device_tokens is None:
            self.mobile_device_tokens = []

        # Remove existing token for this platform
        self.mobile_device_tokens = [
            d for d in self.mobile_device_tokens if d.get("platform") != platform
        ]

        entry: Dict[str, str] = {
            "platform": platform,
            "token": token,
            "registered_at": datetime.now(UTC).isoformat(),
        }
        if platform == "ios" and apns_environment:
            entry["apns_environment"] = apns_environment
        self.mobile_device_tokens.append(entry)

    def remove_device_token(self, token: str) -> None:
        """Remove a mobile device token.

        Args:
            token: Device push notification token to remove.
        """
        if self.mobile_device_tokens is not None:
            self.mobile_device_tokens = [
                d for d in self.mobile_device_tokens if d.get("token") != token
            ]

    def get_device_tokens(self, platform: Optional[str] = None) -> List[str]:
        """Get all device tokens, optionally filtered by platform.

        Args:
            platform: Optional platform filter ('ios' or 'android').

        Returns:
            List of device tokens.
        """
        if self.mobile_device_tokens is None:
            return []

        tokens = []
        for device in self.mobile_device_tokens:
            if platform is None or device.get("platform") == platform:
                tokens.append(device.get("token"))

        return [t for t in tokens if t is not None]

    def get_device_entries(
        self, platform: Optional[str] = None
    ) -> List[Tuple[str, Optional[str]]]:
        """Get (token, apns_environment) pairs, optionally filtered by platform.

        Args:
            platform: Optional platform filter ('ios' or 'android').

        Returns:
            List of (token, apns_environment) tuples. The environment is
            None for Android tokens and for iOS tokens registered by app
            builds that did not report it.
        """
        entries: List[Tuple[str, Optional[str]]] = []
        for device in self.mobile_device_tokens or []:
            if platform is not None and device.get("platform") != platform:
                continue
            token = device.get("token")
            if token is None:
                continue
            entries.append((token, device.get("apns_environment")))
        return entries
