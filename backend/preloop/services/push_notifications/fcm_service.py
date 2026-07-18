"""Firebase Cloud Messaging (FCM) service for Android push notifications."""

import asyncio
import base64
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Thread pool for FCM operations to avoid blocking the event loop
_fcm_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fcm_")

# Global FCM service instance
_fcm_initialized = False

# Guards first-time initialization: concurrent sends from the FCM executor
# threads could otherwise interleave the global writes below. firebase_admin
# itself tolerates re-init attempts, so the race was benign, but benign races
# rot into real ones when someone adds a second global.
_fcm_init_lock = threading.Lock()

# Firebase project id of the loaded service-account credentials. Logged with
# every token failure so a "token minted for project A, server holds project B"
# mismatch is visible from the logs alone.
_fcm_project_id: Optional[str] = None


# --- Failure reasons -------------------------------------------------------
# Distinct causes so logs (and the admin test-send) can tell "the user
# reinstalled the app" apart from "the server holds the wrong Firebase
# credentials" apart from "we stored something that was never an FCM token".

FCM_REASON_MALFORMED_TOKEN = "malformed_token"
FCM_REASON_INVALID_TOKEN = "invalid_token"
FCM_REASON_UNREGISTERED = "unregistered"
FCM_REASON_SENDER_ID_MISMATCH = "sender_id_mismatch"
FCM_REASON_AUTH = "auth_error"
FCM_REASON_QUOTA = "quota_exceeded"
FCM_REASON_UNAVAILABLE = "unavailable"
FCM_REASON_INVALID_PAYLOAD = "invalid_payload"
FCM_REASON_NOT_CONFIGURED = "not_configured"
FCM_REASON_UNKNOWN = "unknown"


@dataclass(frozen=True)
class FcmErrorClassification:
    """Structured verdict for a failed FCM send.

    Attributes:
        reason: One of the ``FCM_REASON_*`` constants.
        should_prune: Whether the stored device token is dead and must be
            removed. Never true for server-side faults (auth, quota, outage),
            which would otherwise wipe every user's token during an incident.
        retryable: Whether a later retry with the same token could succeed.
        remediation: Operator-facing hint describing the likely fix.
    """

    reason: str
    should_prune: bool
    retryable: bool
    remediation: str


# An FCM registration token is an instance-id prefix, a colon, then a long
# body (e.g. "cXqN...pLd:APA91b<...>"). Neither the Android app's
# "fcm_unavailable_<ts>" placeholder nor a 64-char APNs hex token matches, so
# both are caught locally instead of costing a round-trip and a log line on
# every single approval.
_FCM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{8,}:[A-Za-z0-9_\-]{100,}$")


def looks_like_fcm_token(token: str) -> bool:
    """Report whether ``token`` is structurally a plausible FCM token.

    This is a cheap local guard, not a validity check — a well-formed token can
    still be expired or belong to another Firebase project.

    Args:
        token: Candidate device registration token.

    Returns:
        True if the token has the shape of an FCM registration token.
    """
    return bool(_FCM_TOKEN_RE.match((token or "").strip()))


def classify_fcm_error(error: BaseException) -> FcmErrorClassification:
    """Classify an exception raised by ``firebase_admin.messaging.send``.

    Uses the typed exception hierarchy rather than substring matching, so a
    change to Google's error wording cannot silently turn a dead token back
    into an "unknown" error that is retried forever.

    Args:
        error: Exception raised by the Firebase Admin SDK.

    Returns:
        The classification describing the reason and whether to prune.
    """
    try:
        from firebase_admin import exceptions as fb_exceptions
        from firebase_admin import messaging
    except ImportError:  # pragma: no cover - firebase-admin always installed
        return FcmErrorClassification(
            reason=FCM_REASON_UNKNOWN,
            should_prune=False,
            retryable=True,
            remediation="firebase-admin is not installed.",
        )

    if isinstance(error, messaging.UnregisteredError):
        return FcmErrorClassification(
            reason=FCM_REASON_UNREGISTERED,
            should_prune=True,
            retryable=False,
            remediation=(
                "Token was valid but is now dead (app uninstalled, data "
                "cleared, or token rotated). Pruned; the device re-registers "
                "on next launch."
            ),
        )

    if isinstance(error, messaging.SenderIdMismatchError):
        return FcmErrorClassification(
            reason=FCM_REASON_SENDER_ID_MISMATCH,
            should_prune=True,
            retryable=False,
            remediation=(
                "Token was minted by a different Firebase project than the "
                f"server's credentials (server project={_fcm_project_id!r}). "
                "Check that the app's google-services.json project_id matches "
                "FCM_CREDENTIALS_JSON."
            ),
        )

    if isinstance(error, messaging.QuotaExceededError) or isinstance(
        error, fb_exceptions.ResourceExhaustedError
    ):
        return FcmErrorClassification(
            reason=FCM_REASON_QUOTA,
            should_prune=False,
            retryable=True,
            remediation="FCM quota exceeded; back off and retry.",
        )

    if isinstance(
        error,
        (
            fb_exceptions.UnauthenticatedError,
            fb_exceptions.PermissionDeniedError,
            messaging.ThirdPartyAuthError,
        ),
    ):
        return FcmErrorClassification(
            reason=FCM_REASON_AUTH,
            should_prune=False,
            retryable=False,
            remediation=(
                "Server-side credential failure, not a device problem. Check "
                "FCM_CREDENTIALS_JSON is a valid, non-revoked service account "
                f"for the intended project (currently {_fcm_project_id!r})."
            ),
        )

    if isinstance(error, (fb_exceptions.UnavailableError, fb_exceptions.InternalError)):
        return FcmErrorClassification(
            reason=FCM_REASON_UNAVAILABLE,
            should_prune=False,
            retryable=True,
            remediation="Transient FCM outage; retry later.",
        )

    if isinstance(error, fb_exceptions.InvalidArgumentError):
        message = str(error).lower()
        # FCM answers a structurally bad or foreign token with INVALID_ARGUMENT
        # and this specific wording. Anything else with the same status is our
        # own malformed payload and must NOT cost the user their token.
        if "registration token" in message or "not a valid fcm" in message:
            return FcmErrorClassification(
                reason=FCM_REASON_INVALID_TOKEN,
                should_prune=True,
                retryable=False,
                remediation=(
                    "FCM rejected the token as not a valid registration token. "
                    "Either a non-FCM string was stored (placeholder/APNs "
                    "token) or it belongs to another Firebase project "
                    f"(server project={_fcm_project_id!r})."
                ),
            )
        return FcmErrorClassification(
            reason=FCM_REASON_INVALID_PAYLOAD,
            should_prune=False,
            retryable=False,
            remediation=(
                "FCM rejected the message payload, not the token. This is a "
                "server-side bug in the notification payload."
            ),
        )

    if isinstance(error, fb_exceptions.NotFoundError):
        return FcmErrorClassification(
            reason=FCM_REASON_UNREGISTERED,
            should_prune=True,
            retryable=False,
            remediation="FCM reports the token no longer exists. Pruned.",
        )

    return FcmErrorClassification(
        reason=FCM_REASON_UNKNOWN,
        should_prune=False,
        retryable=True,
        remediation="Unrecognised FCM error; inspect the raw error string.",
    )


def _initialize_fcm() -> bool:
    """Initialize Firebase Admin SDK if not already done.

    Thread-safe: double-checked around ``_fcm_init_lock`` so concurrent first
    sends cannot interleave the global writes in the unlocked body.

    Returns:
        True if initialization succeeded, False otherwise.
    """
    if _fcm_initialized:
        return True
    with _fcm_init_lock:
        if _fcm_initialized:
            return True
        return _initialize_fcm_unlocked()


def _initialize_fcm_unlocked() -> bool:
    """Perform the actual SDK initialization. Call only under ``_fcm_init_lock``.

    Environment variables:
        FCM_CREDENTIALS_JSON: JSON string of Firebase service account credentials (preferred for K8s)
        FCM_CREDENTIALS_PATH: Path to Firebase service account JSON file (fallback)

    Returns:
        True if initialization succeeded, False otherwise.
    """
    global _fcm_initialized, _fcm_project_id

    if _fcm_initialized:
        return True

    try:
        import firebase_admin
        from firebase_admin import credentials

        if firebase_admin._apps:
            _fcm_initialized = True
            return True

        # Try JSON string first (preferred for Kubernetes secrets)
        creds_json = os.getenv("FCM_CREDENTIALS_JSON")
        if creds_json:
            try:
                # Try to decode from base64 first (in case it's base64 encoded)
                try:
                    decoded = base64.b64decode(creds_json).decode("utf-8")
                    cred_dict = json.loads(decoded)
                    logger.debug("FCM credentials decoded from base64")
                except Exception:
                    # If base64 decode fails, assume it's already plain JSON
                    cred_dict = json.loads(creds_json)
                    logger.debug("FCM credentials parsed as plain JSON")

                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                _fcm_initialized = True
                _fcm_project_id = cred_dict.get("project_id")
                # The project id is the single most useful diagnostic for
                # "valid token, wrong project" failures.
                logger.info(
                    "FCM initialized from FCM_CREDENTIALS_JSON (project_id=%s)",
                    _fcm_project_id,
                )
                return True
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse FCM_CREDENTIALS_JSON: {e}")

        # Fallback to file path
        creds_path = os.getenv("FCM_CREDENTIALS_PATH")
        if creds_path and os.path.exists(creds_path):
            cred = credentials.Certificate(creds_path)
            firebase_admin.initialize_app(cred)
            _fcm_initialized = True
            try:
                with open(creds_path, "r", encoding="utf-8") as handle:
                    _fcm_project_id = json.load(handle).get("project_id")
            except Exception:  # pragma: no cover - diagnostics only
                _fcm_project_id = None
            logger.info(
                "FCM initialized from %s (project_id=%s)", creds_path, _fcm_project_id
            )
            return True

        logger.warning(
            "FCM not configured (no FCM_CREDENTIALS_JSON or FCM_CREDENTIALS_PATH)"
        )
        return False

    except ImportError:
        logger.warning("firebase-admin package not installed")
        return False
    except Exception as e:
        logger.error(f"Failed to initialize FCM: {e}")
        return False


def is_fcm_configured() -> bool:
    """Check if FCM is configured and can be initialized."""
    creds_json = os.getenv("FCM_CREDENTIALS_JSON")
    creds_path = os.getenv("FCM_CREDENTIALS_PATH")
    return bool(creds_json or (creds_path and os.path.exists(creds_path)))


def get_fcm_project_id() -> Optional[str]:
    """Return the Firebase project id of the loaded credentials, if any.

    Returns:
        The ``project_id`` from the service account, or None if FCM has not
        been initialized successfully.
    """
    if not _fcm_initialized:
        _initialize_fcm()
    return _fcm_project_id


def _mask_token(token: str) -> str:
    """Return a short, non-sensitive identifier for a device token."""
    cleaned = (token or "").strip()
    if not cleaned:
        return "<blank>"
    if len(cleaned) <= 12:
        return cleaned
    return f"{cleaned[:8]}...{cleaned[-4:]}"


def _send_fcm_sync(
    token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    priority: str = "high",
) -> Dict[str, Any]:
    """Synchronous FCM send - runs in thread pool to avoid blocking event loop."""
    token = (token or "").strip()

    # Reject structurally impossible tokens before spending a network call.
    if not looks_like_fcm_token(token):
        logger.warning(
            "Refusing FCM send: stored token is not an FCM registration token "
            "(token=%s, length=%d, reason=%s). It was never usable — most "
            "likely an app-side placeholder or an APNs token stored under "
            "platform='android'.",
            _mask_token(token),
            len(token),
            FCM_REASON_MALFORMED_TOKEN,
        )
        return {
            "success": False,
            "error": (
                "Stored device token is not a valid FCM registration token "
                "(failed local format check)."
            ),
            "error_reason": FCM_REASON_MALFORMED_TOKEN,
            "invalid_token": True,
            "should_prune": True,
            "retryable": False,
            "remediation": (
                "The device must re-register a real FCM token. Check the "
                "Android app is not storing a placeholder when Firebase "
                "initialisation fails."
            ),
            "project_id": _fcm_project_id,
        }

    try:
        from firebase_admin import messaging

        # Convert data values to strings (FCM requirement)
        string_data = {}
        if data:
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    string_data[key] = json.dumps(value)
                else:
                    string_data[key] = str(value)

        # Build FCM message
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=string_data,
            token=token,
            android=messaging.AndroidConfig(
                priority=priority,
                notification=messaging.AndroidNotification(
                    channel_id="preloop_approvals",  # Must match Android app's CHANNEL_APPROVALS
                    priority="max" if priority == "high" else "default",
                ),
            ),
        )

        # Send message (blocking HTTP call)
        response = messaging.send(message)
        logger.info(f"FCM notification sent: {response}")

        return {
            "success": True,
            "message_id": response,
            "project_id": _fcm_project_id,
        }

    except Exception as e:
        error_str = str(e)
        classification = classify_fcm_error(e)

        logger.error(
            "FCM send failed: reason=%s token=%s project_id=%s prune=%s "
            "retryable=%s error=%s | %s",
            classification.reason,
            _mask_token(token),
            _fcm_project_id,
            classification.should_prune,
            classification.retryable,
            error_str,
            classification.remediation,
        )

        return {
            "success": False,
            "error": error_str,
            "error_reason": classification.reason,
            # Retained for backwards compatibility with existing callers.
            "invalid_token": classification.should_prune,
            "should_prune": classification.should_prune,
            "retryable": classification.retryable,
            "remediation": classification.remediation,
            "project_id": _fcm_project_id,
        }


async def send_fcm_notification(
    token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    priority: str = "high",
) -> Dict[str, Any]:
    """Send a push notification via Firebase Cloud Messaging.

    Runs the Firebase SDK call in a thread pool to avoid blocking the event loop.
    Never raises: every failure is reported in the returned dict so a push
    problem can never break the approval flow that triggered it.

    Args:
        token: FCM device token.
        title: Notification title.
        body: Notification body.
        data: Optional data payload.
        priority: Message priority ("high" or "normal").

    Returns:
        Dict with send result including:
            - success: bool
            - message_id: str (if successful)
            - error: str (if failed)
            - error_reason: str, one of the ``FCM_REASON_*`` constants
            - invalid_token: bool (deprecated alias of should_prune)
            - should_prune: bool, whether the stored token must be removed
            - retryable: bool, whether a later retry could succeed
            - remediation: str, operator-facing hint
            - project_id: str, Firebase project of the server credentials
    """
    if not _initialize_fcm():
        return {
            "success": False,
            "error": "FCM not configured",
            "error_reason": FCM_REASON_NOT_CONFIGURED,
            "invalid_token": False,
            "should_prune": False,
            "retryable": False,
            "remediation": (
                "Set FCM_CREDENTIALS_JSON (or FCM_CREDENTIALS_PATH) on the "
                "server. Android push is disabled until then."
            ),
            "project_id": None,
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _fcm_executor,
        partial(_send_fcm_sync, token, title, body, data, priority),
    )
