"""Structural validation of device push tokens at registration time.

Storing a token that was never valid is worse than storing none: the account
still *looks* push-enabled, every later send fails, and the logs fill with
provider errors that look like an outage. These checks reject such tokens at
the door.

They are deliberately structural only — a well-formed token can still be
expired or belong to another Firebase project, which only the provider can
tell us.
"""

import re
from typing import Optional, Tuple

from .fcm_service import looks_like_fcm_token

SUPPORTED_PLATFORMS = ("ios", "android")

# APNs device tokens are hex. Historically 32 bytes (64 hex chars); Apple has
# reserved the right to lengthen them, so accept a generous hex range rather
# than pinning to 64.
_APNS_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64,200}$")

# Placeholders the mobile apps have been observed to store when the push SDK
# fails to produce a real token. These must never reach a provider.
_KNOWN_PLACEHOLDER_PREFIXES = (
    "fcm_unavailable",
    "apns_unavailable",
    "unavailable",
    "placeholder",
    "test",
    "none",
    "null",
    "undefined",
)


def looks_like_apns_token(token: str) -> bool:
    """Report whether ``token`` is structurally a plausible APNs token.

    Args:
        token: Candidate device token.

    Returns:
        True if the token has the shape of an APNs device token.
    """
    return bool(_APNS_TOKEN_RE.match((token or "").strip()))


def is_placeholder_token(token: str) -> bool:
    """Report whether ``token`` is a known app-side placeholder string.

    Args:
        token: Candidate device token.

    Returns:
        True if the token is a recognised placeholder.
    """
    cleaned = (token or "").strip().lower()
    return any(cleaned.startswith(prefix) for prefix in _KNOWN_PLACEHOLDER_PREFIXES)


def validate_device_token(platform: str, token: str) -> Tuple[bool, Optional[str]]:
    """Validate a device token against the expected shape for its platform.

    Args:
        platform: Device platform, 'ios' or 'android'.
        token: Device push token.

    Returns:
        Tuple of (is_valid, error_message). ``error_message`` is None when the
        token is acceptable.
    """
    cleaned_platform = (platform or "").strip().lower()
    cleaned_token = (token or "").strip()

    if cleaned_platform not in SUPPORTED_PLATFORMS:
        return False, f"Platform must be one of {list(SUPPORTED_PLATFORMS)}"

    if not cleaned_token:
        return False, "Device push token must not be empty"

    if is_placeholder_token(cleaned_token):
        return (
            False,
            "Device push token is a placeholder, not a real token. The app "
            "failed to obtain a push token and must retry registration.",
        )

    if cleaned_platform == "android" and not looks_like_fcm_token(cleaned_token):
        return (
            False,
            "Token is not a valid FCM registration token. An Android device "
            "must register the token returned by FirebaseMessaging.",
        )

    if cleaned_platform == "ios" and not looks_like_apns_token(cleaned_token):
        # An FCM-shaped token here means the platform field is wrong, which
        # would route an Android token down the APNs path (and vice versa).
        if looks_like_fcm_token(cleaned_token):
            return (
                False,
                "Token looks like an FCM registration token but was "
                "registered as iOS. Check the platform field.",
            )
        return (
            False,
            "Token is not a valid APNs device token (expected hex).",
        )

    return True, None
