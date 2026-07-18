"""Push notification services for mobile approval notifications."""

import os
import logging
from typing import Optional

from .apns_service import APNsService
from .fcm_service import (
    classify_fcm_error,
    get_fcm_project_id,
    is_fcm_configured,
    looks_like_fcm_token,
    send_fcm_notification,
)
from .notification_payloads import NotificationPayloadBuilder
from .qr_service import (
    generate_registration_token,
    validate_registration_token,
    check_token_validity,
)

logger = logging.getLogger(__name__)

# Global APNs service instance
_apns_service: Optional[APNsService] = None


def get_apns_service() -> Optional[APNsService]:
    """Get or create APNs service singleton.

    Environment variables:
        APNS_TEAM_ID: Apple Developer Team ID (required)
        APNS_KEY_ID: APNs Auth Key ID (required)
        APNS_AUTH_KEY: The .p8 key content directly (preferred for K8s)
        APNS_AUTH_KEY_PATH: Path to .p8 key file (fallback)
        APNS_BUNDLE_ID: App bundle identifier (default: spacecode.ai.Preloop)
        APNS_USE_SANDBOX: Use sandbox environment (default: false for production)

    Returns:
        APNsService instance if configured, None otherwise.
    """
    global _apns_service

    if _apns_service is not None:
        return _apns_service

    # Check if APNs is configured
    team_id = os.getenv("APNS_TEAM_ID")
    key_id = os.getenv("APNS_KEY_ID")
    auth_key = os.getenv("APNS_AUTH_KEY")  # Direct key content (preferred)
    auth_key_path = os.getenv("APNS_AUTH_KEY_PATH")  # File path (fallback)
    bundle_id = os.getenv("APNS_BUNDLE_ID", "spacecode.ai.Preloop")
    # Default to production (false) - sandbox should be explicitly enabled for development
    use_sandbox = os.getenv("APNS_USE_SANDBOX", "false").lower() == "true"

    if not team_id or not key_id:
        logger.warning("APNs not configured (missing APNS_TEAM_ID or APNS_KEY_ID)")
        return None

    if not auth_key and not auth_key_path:
        logger.warning(
            "APNs not configured (missing APNS_AUTH_KEY or APNS_AUTH_KEY_PATH)"
        )
        return None

    try:
        _apns_service = APNsService(
            team_id=team_id,
            key_id=key_id,
            bundle_id=bundle_id,
            use_sandbox=use_sandbox,
            auth_key=auth_key,
            auth_key_path=auth_key_path,
        )
        logger.info(f"APNs service initialized (sandbox={use_sandbox})")
        return _apns_service
    except Exception as e:
        logger.error(f"Failed to initialize APNs service: {e}")
        return None


__all__ = [
    "APNsService",
    "get_apns_service",
    "NotificationPayloadBuilder",
    "send_fcm_notification",
    "is_fcm_configured",
    "classify_fcm_error",
    "get_fcm_project_id",
    "looks_like_fcm_token",
    "generate_registration_token",
    "validate_registration_token",
    "check_token_validity",
]
