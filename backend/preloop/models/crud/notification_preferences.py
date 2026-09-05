"""CRUD operations for notification preferences."""

import uuid
from typing import Optional, List, Dict

from sqlalchemy.orm import Session

from ..models.notification_preferences import NotificationPreferences


def get_by_user(db: Session, user_id: uuid.UUID) -> Optional[NotificationPreferences]:
    """Get notification preferences by user ID.

    Args:
        db: Database session.
        user_id: User ID.

    Returns:
        NotificationPreferences if found, None otherwise.
    """
    return (
        db.query(NotificationPreferences)
        .filter(NotificationPreferences.user_id == user_id)
        .first()
    )


def create(
    db: Session,
    user_id: uuid.UUID,
    preferred_channel: str = "email",
    enable_email: bool = True,
    enable_mobile_push: bool = False,
    stagger_email: bool = True,
    mobile_device_tokens: Optional[List[Dict]] = None,
) -> NotificationPreferences:
    """Create notification preferences for a user.

    Args:
        db: Database session.
        user_id: User ID.
        preferred_channel: Preferred notification channel.
        enable_email: Whether email notifications are enabled.
        enable_mobile_push: Whether mobile push notifications are enabled.
        stagger_email: Whether to delay email when push is also enabled.
        mobile_device_tokens: List of mobile device tokens.

    Returns:
        Created NotificationPreferences.
    """
    prefs = NotificationPreferences(
        user_id=user_id,
        preferred_channel=preferred_channel,
        enable_email=enable_email,
        enable_mobile_push=enable_mobile_push,
        stagger_email=stagger_email,
        mobile_device_tokens=mobile_device_tokens or [],
    )
    db.add(prefs)
    db.flush()
    return prefs


def update(
    db: Session,
    prefs: NotificationPreferences,
    preferred_channel: Optional[str] = None,
    enable_email: Optional[bool] = None,
    enable_mobile_push: Optional[bool] = None,
    stagger_email: Optional[bool] = None,
    mobile_device_tokens: Optional[List[Dict]] = None,
) -> NotificationPreferences:
    """Update notification preferences.

    Args:
        db: Database session.
        prefs: Preferences to update.
        preferred_channel: Preferred notification channel.
        enable_email: Whether email notifications are enabled.
        enable_mobile_push: Whether mobile push notifications are enabled.
        stagger_email: Whether to delay email when push is also enabled.
        mobile_device_tokens: List of mobile device tokens.

    Returns:
        Updated NotificationPreferences.
    """
    if preferred_channel is not None:
        prefs.preferred_channel = preferred_channel
    if enable_email is not None:
        prefs.enable_email = enable_email
    if enable_mobile_push is not None:
        prefs.enable_mobile_push = enable_mobile_push
    if stagger_email is not None:
        prefs.stagger_email = stagger_email
    if mobile_device_tokens is not None:
        prefs.mobile_device_tokens = mobile_device_tokens

    db.flush()
    return prefs


def get_or_create(
    db: Session,
    user_id: uuid.UUID,
    preferred_channel: str = "email",
    enable_email: bool = True,
    enable_mobile_push: bool = False,
    stagger_email: bool = True,
) -> NotificationPreferences:
    """Get or create notification preferences for a user.

    Args:
        db: Database session.
        user_id: User ID.
        preferred_channel: Preferred notification channel (for creation).
        enable_email: Whether email is enabled (for creation).
        enable_mobile_push: Whether mobile push is enabled (for creation).
        stagger_email: Whether to delay email when push is also enabled.

    Returns:
        Existing or newly created NotificationPreferences.
    """
    prefs = get_by_user(db, user_id)
    if prefs:
        return prefs

    return create(
        db,
        user_id=user_id,
        preferred_channel=preferred_channel,
        enable_email=enable_email,
        enable_mobile_push=enable_mobile_push,
        stagger_email=stagger_email,
    )


def add_device_token(
    db: Session,
    user_id: uuid.UUID,
    platform: str,
    token: str,
) -> NotificationPreferences:
    """Add a mobile device token for a user.

    Args:
        db: Database session.
        user_id: User ID.
        platform: Device platform ('ios' or 'android').
        token: Device push notification token.

    Returns:
        Updated NotificationPreferences.
    """
    prefs = get_or_create(db, user_id)
    prefs.add_device_token(platform, token)
    db.flush()
    return prefs


def remove_device_token(
    db: Session,
    user_id: uuid.UUID,
    token: str,
) -> Optional[NotificationPreferences]:
    """Remove a mobile device token for a user.

    Args:
        db: Database session.
        user_id: User ID.
        token: Device push notification token to remove.

    Returns:
        Updated NotificationPreferences if found, None otherwise.
    """
    prefs = get_by_user(db, user_id)
    if not prefs:
        return None

    prefs.remove_device_token(token)
    db.flush()
    return prefs


def get_device_tokens(
    db: Session,
    user_id: uuid.UUID,
    platform: Optional[str] = None,
) -> List[str]:
    """Get all device tokens for a user.

    Args:
        db: Database session.
        user_id: User ID.
        platform: Optional platform filter ('ios' or 'android').

    Returns:
        List of device tokens.
    """
    prefs = get_by_user(db, user_id)
    if not prefs:
        return []

    return prefs.get_device_tokens(platform)
