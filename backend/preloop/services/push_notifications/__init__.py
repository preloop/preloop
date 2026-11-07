"""Push notification services for mobile approval notifications."""

import os
import logging
from typing import Dict, Optional

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

APNS_ENVIRONMENT_SANDBOX = "sandbox"
APNS_ENVIRONMENT_PRODUCTION = "production"
APNS_ENVIRONMENTS = (APNS_ENVIRONMENT_SANDBOX, APNS_ENVIRONMENT_PRODUCTION)

# One APNs client per environment. Both share the same .p8 key: Apple issues
# token-based keys that are valid for sandbox and production alike, so only
# the host differs.
_apns_services: Dict[str, APNsService] = {}


def normalize_apns_environment(value: Optional[str]) -> Optional[str]:
    """Return ``"sandbox"`` or ``"production"`` for a reported value.

    Accepts the aps-environment entitlement spelling (``development``) as
    an alias for sandbox. Unknown or empty values return None.
    """
    cleaned = (value or "").strip().lower()
    if cleaned in ("sandbox", "development"):
        return APNS_ENVIRONMENT_SANDBOX
    if cleaned == APNS_ENVIRONMENT_PRODUCTION:
        return APNS_ENVIRONMENT_PRODUCTION
    return None


def default_apns_environment() -> str:
    """Environment used for iOS tokens that did not report one.

    ``APNS_USE_SANDBOX=true`` keeps its old meaning here: unlabeled tokens
    go to the sandbox host. Otherwise they go to production.
    """
    if os.getenv("APNS_USE_SANDBOX", "false").lower() == "true":
        return APNS_ENVIRONMENT_SANDBOX
    return APNS_ENVIRONMENT_PRODUCTION


def resolve_apns_environment(value: Optional[str]) -> str:
    """Environment a token should be sent to: its label, else the default."""
    return normalize_apns_environment(value) or default_apns_environment()


def get_apns_service(environment: Optional[str] = None) -> Optional[APNsService]:
    """Get or create the APNs client for one environment.

    Environment variables:
        APNS_TEAM_ID: Apple Developer Team ID (required)
        APNS_KEY_ID: APNs Auth Key ID (required)
        APNS_AUTH_KEY: The .p8 key content directly (preferred for K8s)
        APNS_AUTH_KEY_PATH: Path to .p8 key file (fallback)
        APNS_BUNDLE_ID: App bundle identifier (default: spacecode.ai.Preloop)
        APNS_USE_SANDBOX: Send tokens that do not report an environment to
            the sandbox host (default: false, production)

    Args:
        environment: ``"sandbox"`` or ``"production"`` (the token's label).
            None or an unknown value selects the default environment.

    Returns:
        APNsService instance if configured, None otherwise.
    """
    env = resolve_apns_environment(environment)
    cached = _apns_services.get(env)
    if cached is not None:
        return cached

    team_id = os.getenv("APNS_TEAM_ID")
    key_id = os.getenv("APNS_KEY_ID")
    auth_key = os.getenv("APNS_AUTH_KEY")  # Direct key content (preferred)
    auth_key_path = os.getenv("APNS_AUTH_KEY_PATH")  # File path (fallback)
    bundle_id = os.getenv("APNS_BUNDLE_ID", "spacecode.ai.Preloop")

    if not team_id or not key_id:
        logger.warning("APNs not configured (missing APNS_TEAM_ID or APNS_KEY_ID)")
        return None

    if not auth_key and not auth_key_path:
        logger.warning(
            "APNs not configured (missing APNS_AUTH_KEY or APNS_AUTH_KEY_PATH)"
        )
        return None

    try:
        service = APNsService(
            team_id=team_id,
            key_id=key_id,
            bundle_id=bundle_id,
            use_sandbox=env == APNS_ENVIRONMENT_SANDBOX,
            auth_key=auth_key,
            auth_key_path=auth_key_path,
        )
    except Exception as e:
        logger.error(f"Failed to initialize APNs service: {e}")
        return None
    _apns_services[env] = service
    logger.info("APNs service initialized (environment=%s)", env)
    return service


def select_apns_service(
    default_service: Optional[APNsService], environment: Optional[str]
) -> Optional[APNsService]:
    """Pick the APNs client for one token.

    ``default_service`` is the client the caller already resolved with
    ``get_apns_service()``. Tokens whose label matches the default
    environment reuse it; tokens labeled with the other environment get
    that environment's client.
    """
    env = resolve_apns_environment(environment)
    if default_service is None or env == default_apns_environment():
        return default_service
    import preloop.services.push_notifications as push_module

    return push_module.get_apns_service(env)


__all__ = [
    "APNsService",
    "get_apns_service",
    "APNS_ENVIRONMENTS",
    "APNS_ENVIRONMENT_PRODUCTION",
    "APNS_ENVIRONMENT_SANDBOX",
    "default_apns_environment",
    "normalize_apns_environment",
    "resolve_apns_environment",
    "select_apns_service",
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
