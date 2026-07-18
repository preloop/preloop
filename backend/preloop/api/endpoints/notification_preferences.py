"""API endpoints for notification preferences."""

import logging
import os
import threading
import time
from datetime import datetime, UTC
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import notification_preferences
from preloop.models.db.session import get_db_session
from preloop.api.auth import get_current_active_user
from preloop.schemas.notification_preferences import (
    NotificationPreferencesUpdate,
    NotificationPreferencesResponse,
    MobileDeviceRegistration,
    MobileDeviceRegistrationResponse,
    QRCodeResponse,
    TestPushRequest,
    TestPushResponse,
)
from preloop.services.push_notifications import (
    generate_registration_token,
    validate_registration_token,
    check_token_validity,
)
from preloop.services.push_notifications.token_validation import (
    validate_device_token,
)

router = APIRouter()

logger = logging.getLogger(__name__)

# In-memory sliding-window rate limiter for admin test sends: a diagnostic
# button must not become a way to spam a device. Per-process, matching the
# existing limiter pattern in plugins/push_notifications/endpoints.py — with
# multiple pods the effective ceiling scales with pod count, which is fine for
# a manually-triggered admin action.
_TEST_PUSH_LIMIT_PER_MINUTE = 5
_TEST_PUSH_WINDOW_SECONDS = 60.0
_test_push_lock = threading.Lock()
_test_push_state: dict[str, list[float]] = {}
# Entries for users who tried once and never again would otherwise live for
# the life of the process. Swept opportunistically on the next call after a
# full window has elapsed — no timer thread for what is an admin-only surface.
_test_push_last_sweep = 0.0


def _enforce_test_push_rate_limit(user_id: str) -> None:
    """Raise HTTP 429 if the user has exhausted their test-send budget.

    Args:
        user_id: Identifier of the user triggering the test send.

    Raises:
        HTTPException: 429 when the per-minute limit is exceeded.
    """
    global _test_push_last_sweep
    now = time.monotonic()
    window_start = now - _TEST_PUSH_WINDOW_SECONDS
    with _test_push_lock:
        if now - _test_push_last_sweep > _TEST_PUSH_WINDOW_SECONDS:
            _test_push_last_sweep = now
            for uid in [
                uid
                for uid, times in _test_push_state.items()
                if uid != user_id and all(t < window_start for t in times)
            ]:
                del _test_push_state[uid]
        recent = [t for t in _test_push_state.get(user_id, []) if t >= window_start]
        if len(recent) >= _TEST_PUSH_LIMIT_PER_MINUTE:
            _test_push_state[user_id] = recent
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded: {_TEST_PUSH_LIMIT_PER_MINUTE} test "
                    "notifications per minute."
                ),
            )
        recent.append(now)
        _test_push_state[user_id] = recent


@router.get("/me", response_model=NotificationPreferencesResponse)
async def get_my_notification_preferences(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> models.NotificationPreferences:
    """Get current user's notification preferences.

    Returns:
        Notification preferences.
    """
    prefs = notification_preferences.get_or_create(db, current_user.id)
    db.commit()
    db.refresh(prefs)

    return prefs


@router.put("/me", response_model=NotificationPreferencesResponse)
async def update_my_notification_preferences(
    prefs_in: NotificationPreferencesUpdate,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> models.NotificationPreferences:
    """Update current user's notification preferences.

    Args:
        prefs_in: Preferences update data.

    Returns:
        Updated notification preferences.
    """
    prefs = notification_preferences.get_or_create(db, current_user.id)

    updated_prefs = notification_preferences.update(
        db,
        prefs,
        preferred_channel=prefs_in.preferred_channel,
        enable_email=prefs_in.enable_email,
        enable_mobile_push=prefs_in.enable_mobile_push,
    )

    db.commit()
    db.refresh(updated_prefs)

    return updated_prefs


@router.post("/me/register-device", response_model=NotificationPreferencesResponse)
async def register_mobile_device(
    device_in: MobileDeviceRegistration,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> models.NotificationPreferences:
    """Register a mobile device for push notifications.

    Args:
        device_in: Device registration data.

    Returns:
        Updated notification preferences.
    """
    # Reject structurally impossible tokens here rather than storing them and
    # failing every later send: a stored placeholder makes the account look
    # push-enabled while silently delivering nothing.
    is_valid, validation_error = validate_device_token(
        device_in.platform, device_in.token
    )
    if not is_valid:
        logger.warning(
            "Rejected device registration for user %s (platform=%s): %s",
            current_user.id,
            device_in.platform,
            validation_error,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=validation_error,
        )

    prefs = notification_preferences.add_device_token(
        db, current_user.id, device_in.platform, device_in.token
    )

    db.commit()
    db.refresh(prefs)

    return prefs


@router.delete("/me/device/{token}", response_model=NotificationPreferencesResponse)
async def unregister_mobile_device(
    token: str,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Optional[models.NotificationPreferences]:
    """Unregister a mobile device and delete associated API key.

    Args:
        token: Device token to remove.

    Returns:
        Updated notification preferences.

    Raises:
        HTTPException: If preferences not found.
    """
    from preloop.models.models.api_key import ApiKey

    # Find and delete any API keys that were created for this device token
    # We search for API keys that match the device token pattern in their name
    # or look for the token in a device_token metadata field if we stored it
    api_keys = (
        db.query(ApiKey)
        .filter(
            ApiKey.user_id == current_user.id,
            ApiKey.account_id == current_user.account_id,
        )
        .all()
    )

    # Delete API keys that match the device token
    for api_key in api_keys:
        # Check if this API key was created for this device
        # We stored the device token in the key metadata or name
        key_metadata = api_key.scopes or []
        if isinstance(key_metadata, list) and any(
            isinstance(s, dict) and s.get("device_token") == token for s in key_metadata
        ):
            db.delete(api_key)

    prefs = notification_preferences.remove_device_token(db, current_user.id, token)

    if not prefs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification preferences not found",
        )

    db.commit()
    db.refresh(prefs)

    return prefs


@router.post("/me/test-push", response_model=TestPushResponse)
async def send_test_push_notification(
    request_in: TestPushRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Send a clearly-labelled TEST notification to the caller's devices.

    Admin-only diagnostic. Exercises the real APNs/FCM/proxy delivery path and
    returns the provider's verbatim result per device, so an otherwise silent
    push failure becomes visible.

    The test is fully isolated from governance: no ApprovalRequest row is
    created, so nothing waits on it, it appears in no approval list, and
    approving or declining it changes no state.

    Args:
        request_in: Which kind of test to send ('approval' or 'question').

    Returns:
        Per-device send results.

    Raises:
        HTTPException: 403 if the caller is not an admin, 404 if no devices are
            registered, 429 if the rate limit is exceeded.
    """
    from preloop.services.push_notifications.test_send import send_test_push

    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    _enforce_test_push_rate_limit(str(current_user.id))

    prefs = notification_preferences.get_by_user(db, current_user.id)
    devices = list(prefs.mobile_device_tokens or []) if prefs else []

    if request_in.platform:
        devices = [d for d in devices if d.get("platform") == request_in.platform]

    if not devices:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No registered devices to send a test notification to",
        )

    logger.info(
        "Admin test push (%s) requested by user %s for %d device(s)",
        request_in.kind,
        current_user.id,
        len(devices),
    )

    report = await send_test_push(devices=devices, kind=request_in.kind)
    return report.as_dict()


@router.get("/me/qr-code", response_model=QRCodeResponse)
async def get_registration_qr_code(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Generate a QR code for mobile device registration.

    Returns:
        QR code data with registration token.
    """
    api_url = os.getenv("PRELOOP_URL", "http://localhost:8000")

    qr_data = generate_registration_token(
        db=db,
        user_id=current_user.id,
        api_url=api_url,
        expiry_minutes=15,
    )

    return qr_data


@router.post("/register-via-token", response_model=MobileDeviceRegistrationResponse)
async def register_device_via_token(
    token: str,
    device_in: MobileDeviceRegistration,
    db: Session = Depends(get_db_session),
) -> dict:
    """Register a mobile device using a QR code token and create an API key.

    This endpoint is called by the mobile app after scanning the QR code.
    It registers the device for push notifications AND creates an API key
    for the mobile app to authenticate API requests.

    Args:
        token: Registration token from QR code.
        device_in: Device registration data.

    Returns:
        Updated notification preferences with API key.

    Raises:
        HTTPException: If token is invalid or expired.
    """
    import secrets
    import string
    from datetime import timedelta
    from preloop.models.models.api_key import ApiKey
    from preloop.models.crud import crud_user

    # Validate token
    user_id = validate_registration_token(db, token)

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired registration token",
        )

    # Get user
    user = crud_user.get(db, id=user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Check if this is the first device being registered
    existing_prefs = notification_preferences.get_by_user(db, user_id)
    is_first_device = (
        not existing_prefs
        or not existing_prefs.mobile_device_tokens
        or len(existing_prefs.mobile_device_tokens) == 0
    )

    # Validate the push token, but never fail pairing over it: pairing also
    # issues the API key the app needs to function at all. A bad token is
    # skipped (not stored) so it cannot poison every future send, and the
    # caller is told push was not enabled.
    push_registered, push_error = validate_device_token(
        device_in.platform, device_in.token
    )

    if push_registered:
        prefs = notification_preferences.add_device_token(
            db, user_id, device_in.platform, device_in.token
        )
        # Enable push notifications by default when adding the first device
        if is_first_device and not prefs.enable_mobile_push:
            prefs.enable_mobile_push = True
            db.flush()
    else:
        logger.warning(
            "QR pairing for user %s completed WITHOUT push: platform=%s %s",
            user_id,
            device_in.platform,
            push_error,
        )
        prefs = notification_preferences.get_or_create(db, user_id)

    # Generate API key for mobile app
    alphabet = string.ascii_letters + string.digits
    api_key_value = "".join(secrets.choice(alphabet) for _ in range(40))

    # Create device name with timestamp to avoid conflicts
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base_name = device_in.device_name or f"{device_in.platform.title()} Device"
    device_name = f"{base_name} ({timestamp})"

    # Set expiration to 1 year
    expires_at = datetime.now(UTC) + timedelta(days=365)

    # Create API key with device_token reference for later cleanup
    # Store the device token in scopes metadata so we can delete this key
    # when the device is unregistered
    new_api_key = ApiKey(
        name=device_name,
        key=api_key_value,
        scopes=[{"device_token": device_in.token}],
        account_id=user.account_id,
        user_id=user_id,
        expires_at=expires_at,
    )

    db.add(new_api_key)
    db.commit()
    db.refresh(prefs)
    db.refresh(new_api_key)

    # Send WebSocket notification to user about device registration
    from preloop.services.websocket_manager import manager

    await manager.broadcast_json(
        {
            "type": "device_registered",
            "user_id": str(user_id),
            "account_id": str(user.account_id),
            "platform": device_in.platform,
            "device_name": device_name,
            "registered_at": prefs.mobile_device_tokens[-1]["registered_at"]
            if prefs.mobile_device_tokens
            else None,
        },
        account_id=str(user.account_id),
    )

    return {
        "preferences": prefs,
        "api_key": new_api_key.key,
        "api_key_id": new_api_key.id,
        "api_key_expires_at": new_api_key.expires_at,
        "push_registered": push_registered,
        "push_error": push_error,
    }


@router.get("/register-device", response_class=HTMLResponse, include_in_schema=False)
async def register_device_landing_page(
    request: Request, token: str, db: Session = Depends(get_db_session)
) -> str:
    """Landing page for device registration deep link.

    This endpoint serves as the universal link target for QR code scanning.
    It will:
    1. Attempt to open the Preloop.AI app if installed (via custom URL scheme)
    2. If app doesn't open within 2 seconds, redirect to appropriate app store

    Args:
        request: FastAPI request object (for user agent detection).
        token: Registration token from QR code.

    Returns:
        HTML page with app launch logic and store fallbacks.

    Note:
        For this to work as a universal link:
        - iOS: Configure Apple App Site Association (AASA) file at /.well-known/apple-app-site-association
        - Android: Configure Digital Asset Links at /.well-known/assetlinks.json
    """
    # Get environment variables for app store URLs
    app_store_url = os.getenv(
        "IOS_APP_STORE_URL", "https://apps.apple.com/app/preloop/id6757803021"
    )
    play_store_url = os.getenv(
        "ANDROID_PLAY_STORE_URL",
        "https://play.google.com/store/apps/details?id=ai.spacecode.preloop&pli=1",
    )

    # Get user agent to determine device type
    user_agent = request.headers.get("user-agent", "").lower()
    is_ios = "iphone" in user_agent or "ipad" in user_agent
    is_android = "android" in user_agent

    # Check if token is valid (just for messaging - don't consume it yet)
    # The app will consume it when it registers
    is_valid_token = check_token_validity(db, token)

    if not is_valid_token:
        # Token expired or invalid
        return """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Registration Link Expired - Preloop.AI</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    margin: 0;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    padding: 20px;
                }
                .container {
                    background: white;
                    padding: 40px;
                    border-radius: 12px;
                    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
                    max-width: 400px;
                    text-align: center;
                }
                h1 {
                    color: #333;
                    margin-bottom: 16px;
                    font-size: 24px;
                }
                p {
                    color: #666;
                    line-height: 1.6;
                    margin-bottom: 24px;
                }
                .error-icon {
                    font-size: 64px;
                    margin-bottom: 20px;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="error-icon">⏰</div>
                <h1>Registration Link Expired</h1>
                <p>This device registration link has expired or is invalid. Please generate a new QR code from your Preloop.AI settings.</p>
            </div>
        </body>
        </html>
        """

    # Valid token - serve the deep link page.
    #
    # The deep link below carries the token in its query string, which can
    # surface in OS diagnostic logs on some platforms. Accepted deliberately:
    # the token is single-use (validate_and_consume burns it on the first
    # registration) and expires after 15 minutes, so a logged copy is dead
    # the moment pairing completes — and the shipped mobile apps parse
    # exactly this URL shape, so changing the format breaks pairing for
    # every installed version.
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Opening Preloop.AI...</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                padding: 20px;
            }}
            .container {{
                background: white;
                padding: 40px;
                border-radius: 12px;
                box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
                max-width: 400px;
                text-align: center;
            }}
            h1 {{
                color: #333;
                margin-bottom: 16px;
                font-size: 24px;
            }}
            p {{
                color: #666;
                line-height: 1.6;
                margin-bottom: 24px;
            }}
            .spinner {{
                border: 4px solid #f3f3f3;
                border-top: 4px solid #667eea;
                border-radius: 50%;
                width: 50px;
                height: 50px;
                animation: spin 1s linear infinite;
                margin: 20px auto;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
            .store-links {{
                display: flex;
                flex-direction: column;
                gap: 12px;
                margin-top: 24px;
            }}
            .store-button {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 8px;
                padding: 12px 24px;
                background: #000;
                color: white;
                text-decoration: none;
                border-radius: 8px;
                font-weight: 500;
                transition: background 0.2s;
            }}
            .store-button:hover {{
                background: #333;
            }}
            .hidden {{
                display: none;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div id="loading">
                <div class="spinner"></div>
                <h1>Opening Preloop.AI...</h1>
                <p>If the app doesn't open automatically, use the buttons below to download it.</p>
            </div>

            <div id="fallback" class="hidden">
                <h1>Get Preloop.AI</h1>
                <p>Install the Preloop.AI app to complete device registration.</p>
                <div class="store-links">
                    <a href="{app_store_url}" class="store-button" {'style="display: none;"' if not is_ios else ""}>
                        <span>📱</span>
                        Download on App Store
                    </a>
                    <a href="{play_store_url}" class="store-button" {'style="display: none;"' if not is_android else ""}>
                        <span>🤖</span>
                        Get it on Google Play
                    </a>
                </div>
            </div>
        </div>

        <script>
            // Try to open the app with custom URL scheme
            const appUrl = 'preloop://register?token={token}';

            // Attempt to open the app
            window.location.href = appUrl;

            // After 2 seconds, show the store download options
            // If the app opened successfully, the user won't see this
            setTimeout(() => {{
                document.getElementById('loading').classList.add('hidden');
                document.getElementById('fallback').classList.remove('hidden');
            }}, 2000);

            // Alternative approach: Use iframe for better detection (iOS)
            const iframe = document.createElement('iframe');
            iframe.style.display = 'none';
            iframe.src = appUrl;
            document.body.appendChild(iframe);
            setTimeout(() => {{
                document.body.removeChild(iframe);
            }}, 500);
        </script>
    </body>
    </html>
    """
