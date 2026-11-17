"""QR code service for mobile device registration."""

import uuid
import logging
from typing import Optional, Dict, Any
from datetime import timezone

from sqlalchemy.orm import Session
from preloop.models.crud import crud_registration_token

logger = logging.getLogger(__name__)


def generate_registration_token(
    db: Session,
    user_id: uuid.UUID,
    api_url: str,
    expiry_minutes: int = 15,
) -> Dict[str, Any]:
    """Generate a registration token and QR code data.

    Args:
        db: Database session
        user_id: User ID for the registration.
        api_url: Base API URL for the mobile app to connect to.
        expiry_minutes: Token expiry time in minutes.

    Returns:
        Dict with token, qr_data, and expiry.
    """
    # Create token in database
    token_obj = crud_registration_token.create_token(
        db, user_id=user_id, expiry_minutes=expiry_minutes
    )

    # Build QR code data (URL that mobile app will scan)
    # Format: HTTPS URL for Universal Links (iOS) and App Links (Android)
    # This will open the app if installed, or show web page with app store links
    qr_data = f"{api_url}/api/v1/notification-preferences/register-device?token={token_obj.token}"

    # Ensure expires_at is timezone-aware for isoformat
    expires_at_aware = (
        token_obj.expires_at
        if token_obj.expires_at.tzinfo
        else token_obj.expires_at.replace(tzinfo=timezone.utc)
    )

    return {
        "token": token_obj.token,
        "qr_data": qr_data,
        "expires_at": expires_at_aware.isoformat(),
        "expires_in_seconds": expiry_minutes * 60,
    }


def check_token_validity(db: Session, token: str) -> bool:
    """Check if a registration token is valid without consuming it.

    Args:
        db: Database session
        token: Registration token to check.

    Returns:
        True if valid and not expired, False otherwise.
    """
    token_obj = crud_registration_token.get_by_token(db, token=token)

    if not token_obj:
        return False

    # Check if valid (not consumed and not expired)
    return token_obj.is_valid


def validate_registration_token(db: Session, token: str) -> Optional[uuid.UUID]:
    """Validate a registration token and return the user ID.

    Args:
        db: Database session
        token: Registration token to validate.

    Returns:
        User ID if valid, None if invalid or expired.
    """
    # Validate and consume the token (one-time use)
    token_obj = crud_registration_token.validate_and_consume(db, token=token)

    if not token_obj:
        logger.warning("Invalid or expired registration token")
        return None

    logger.info(f"Validated registration token for user {token_obj.user_id}")

    return token_obj.user_id


def cleanup_expired_tokens(db: Session) -> int:
    """Clean up expired registration tokens.

    Args:
        db: Database session

    Returns:
        Number of tokens cleaned up.
    """
    deleted_count = crud_registration_token.cleanup_expired(db)

    if deleted_count > 0:
        logger.info(f"Cleaned up {deleted_count} expired registration tokens")

    return deleted_count
