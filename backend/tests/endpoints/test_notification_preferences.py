"""Tests for notification preferences endpoints."""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_registration_token
from preloop.models.models.api_key import ApiKey
from preloop.models.models.user import User

# Device tokens are validated structurally at registration, so these fixtures
# must have the shape of the real thing: an APNs token is hex (the iOS app
# sends the raw 32-byte device token hex-encoded), and an FCM token is an
# installation-id prefix, a colon, then a long body.
IOS_TOKEN = "7f87557745bffcb16fa49fdf684e844f9f96582980b1fa290a786556c344fcae"
IOS_TOKEN_2 = "1c02de5a9b3f4e7d8a6c5b4e3f2a1d0c9b8a7f6e5d4c3b2a1908f7e6d5c4b3a2"
ANDROID_TOKEN = "cXqNs9TgQ1mR2vB3nK4pLd:APA91b" + ("Z" * 134)


def test_get_qr_code_success(client: TestClient, test_user: User, db_session: Session):
    """Test generating QR code for mobile device registration."""
    response = client.get("/api/v1/notification-preferences/me/qr-code")

    assert response.status_code == 200
    data = response.json()

    # Verify response structure
    assert "token" in data
    assert "qr_data" in data
    assert "expires_at" in data
    assert "expires_in_seconds" in data

    # Verify QR data contains the registration URL
    assert "/register-device?token=" in data["qr_data"]
    assert data["token"] in data["qr_data"]

    # Verify token was stored in database
    token_obj = crud_registration_token.get_by_token(db_session, token=data["token"])
    assert token_obj is not None
    assert token_obj.user_id == test_user.id
    assert not token_obj.is_consumed


def test_register_via_token_success(
    client: TestClient, test_user: User, db_session: Session
):
    """Test successful mobile device registration via QR token (happy path).

    This test verifies:
    1. Token is validated from database
    2. Device is registered for push notifications
    3. API key is created for mobile app authentication
    4. Token is consumed and cannot be reused
    """
    # First, generate a QR code token
    qr_response = client.get("/api/v1/notification-preferences/me/qr-code")
    assert qr_response.status_code == 200
    qr_data = qr_response.json()
    token = qr_data["token"]

    # Mock WebSocket broadcast to avoid connection issues in tests
    with patch(
        "preloop.services.websocket_manager.manager.broadcast_json",
        new_callable=AsyncMock,
    ):
        # Register device via token
        response = client.post(
            f"/api/v1/notification-preferences/register-via-token?token={token}",
            json={"platform": "ios", "token": IOS_TOKEN},
        )

    assert response.status_code == 200
    data = response.json()

    # Verify response structure
    assert "preferences" in data
    assert "api_key" in data
    assert "api_key_id" in data
    assert "api_key_expires_at" in data

    # Verify device token was added to preferences
    prefs = data["preferences"]
    assert prefs["mobile_device_tokens"] is not None
    assert len(prefs["mobile_device_tokens"]) > 0
    device_token_entry = prefs["mobile_device_tokens"][0]
    assert device_token_entry["platform"] == "ios"
    assert device_token_entry["token"] == IOS_TOKEN

    # Verify API key was created
    assert data["api_key"] is not None
    assert len(data["api_key"]) == 40  # API keys are 40 characters

    created_api_key = db_session.get(ApiKey, data["api_key_id"])
    assert created_api_key is not None
    assert created_api_key.account_id == test_user.account_id

    # Verify token was consumed in database
    token_obj = crud_registration_token.get_by_token(db_session, token=token)
    assert token_obj.is_consumed
    assert token_obj.used_at is not None


def test_register_via_token_persistence_across_requests(
    client: TestClient, test_user: User, db_session: Session
):
    """Regression test: Verify tokens persist in DB and work across separate requests.

    This test specifically addresses the bug where tokens were stored in an
    in-memory dict, causing failures when requests were served by different
    workers/pods or after restarts.

    The test simulates two separate operations:
    1. Generate token (like QR code generation request to pod A)
    2. Register device (like registration request to pod B)

    If tokens were still in-memory, this would fail. By persisting in the
    database, the token is available to any pod/worker.
    """
    # Step 1: Generate token (simulating request to pod A)
    qr_response = client.get("/api/v1/notification-preferences/me/qr-code")
    assert qr_response.status_code == 200
    token = qr_response.json()["token"]

    # Verify token exists in database (not just in-memory)
    token_obj = crud_registration_token.get_by_token(db_session, token=token)
    assert token_obj is not None, "Token should be persisted in database"
    assert token_obj.user_id == test_user.id
    assert not token_obj.is_consumed

    # Step 2: Register device (simulating request to pod B)
    # In a real scenario, this could be served by a different pod/worker
    # that doesn't have the in-memory dict from step 1
    with patch(
        "preloop.services.websocket_manager.manager.broadcast_json",
        new_callable=AsyncMock,
    ):
        response = client.post(
            f"/api/v1/notification-preferences/register-via-token?token={token}",
            json={"platform": "android", "token": ANDROID_TOKEN},
        )

    # Should succeed because token is in database
    assert response.status_code == 200, "Token should be validated from database"

    # Verify token was consumed
    db_session.expire_all()  # Clear session cache to force DB read
    token_obj = crud_registration_token.get_by_token(db_session, token=token)
    assert token_obj.is_consumed, "Token should be marked as consumed in database"


def test_register_via_token_invalid_token(
    client: TestClient, test_user: User, db_session: Session
):
    """Test registration with invalid token."""
    response = client.post(
        "/api/v1/notification-preferences/register-via-token?token=invalid-token",
        json={"platform": "ios", "token": "test-device-token"},
    )

    assert response.status_code == 400
    assert "Invalid or expired" in response.json()["detail"]


def test_register_via_token_expired_token(
    client: TestClient, test_user: User, db_session: Session
):
    """Test registration with expired token."""
    # Create expired token directly in database
    from datetime import datetime, timezone, timedelta

    token_obj = crud_registration_token.create_token(
        db_session,
        user_id=test_user.id,
        expiry_minutes=-5,  # Already expired
    )
    # Manually set expires_at to past
    token_obj.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.add(token_obj)
    db_session.commit()

    response = client.post(
        f"/api/v1/notification-preferences/register-via-token?token={token_obj.token}",
        json={"platform": "ios", "token": "test-device-token"},
    )

    assert response.status_code == 400
    assert "Invalid or expired" in response.json()["detail"]


def test_register_via_token_already_consumed(
    client: TestClient, test_user: User, db_session: Session
):
    """Test registration with already consumed token (should fail)."""
    # Generate token
    qr_response = client.get("/api/v1/notification-preferences/me/qr-code")
    token = qr_response.json()["token"]

    # Register device first time (consumes token)
    with patch(
        "preloop.services.websocket_manager.manager.broadcast_json",
        new_callable=AsyncMock,
    ):
        response1 = client.post(
            f"/api/v1/notification-preferences/register-via-token?token={token}",
            json={"platform": "ios", "token": IOS_TOKEN},
        )
    assert response1.status_code == 200

    # Try to register again with same token (should fail)
    with patch(
        "preloop.services.websocket_manager.manager.broadcast_json",
        new_callable=AsyncMock,
    ):
        response2 = client.post(
            f"/api/v1/notification-preferences/register-via-token?token={token}",
            json={"platform": "android", "token": ANDROID_TOKEN},
        )

    assert response2.status_code == 400
    assert "Invalid or expired" in response2.json()["detail"]


def test_register_via_token_invalid_platform(
    client: TestClient, test_user: User, db_session: Session
):
    """Test registration with invalid platform.

    Note: Platform validation currently happens at the add_device_token level,
    which accepts any platform string. The validation only exists in the
    direct register-device endpoint, not register-via-token.
    This test documents the current behavior - platforms other than ios/android
    are accepted but may not receive notifications.
    """
    qr_response = client.get("/api/v1/notification-preferences/me/qr-code")
    token = qr_response.json()["token"]

    with patch(
        "preloop.services.websocket_manager.manager.broadcast_json",
        new_callable=AsyncMock,
    ):
        response = client.post(
            f"/api/v1/notification-preferences/register-via-token?token={token}",
            json={"platform": "windows", "token": "test-device-token"},
        )

    # Currently accepts any platform (validation needed in future)
    assert response.status_code == 200


def test_check_token_validity_valid(
    client: TestClient, test_user: User, db_session: Session
):
    """Test token validity check with valid token."""
    # Generate token
    qr_response = client.get("/api/v1/notification-preferences/me/qr-code")
    token = qr_response.json()["token"]

    # Check landing page (uses check_token_validity internally)
    response = client.get(
        f"/api/v1/notification-preferences/register-device?token={token}"
    )

    # Should show the deep link page (not the expired page)
    assert response.status_code == 200
    assert "Opening Preloop.AI" in response.text
    assert "Registration Link Expired" not in response.text


def test_check_token_validity_expired(
    client: TestClient, test_user: User, db_session: Session
):
    """Test token validity check with expired token."""
    # Create expired token
    token_obj = crud_registration_token.create_token(
        db_session, user_id=test_user.id, expiry_minutes=1
    )
    token_obj.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.add(token_obj)
    db_session.commit()

    # Check landing page
    response = client.get(
        f"/api/v1/notification-preferences/register-device?token={token_obj.token}"
    )

    # Should show expired page
    assert response.status_code == 200
    assert "Registration Link Expired" in response.text
    assert "Opening Preloop.AI" not in response.text


def test_get_my_notification_preferences(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting current user's notification preferences."""
    response = client.get("/api/v1/notification-preferences/me")

    assert response.status_code == 200
    data = response.json()

    # Verify default values
    assert data["user_id"] == str(test_user.id)
    assert "preferred_channel" in data
    assert "enable_email" in data
    assert "enable_mobile_push" in data


def test_update_my_notification_preferences(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating current user's notification preferences."""
    response = client.put(
        "/api/v1/notification-preferences/me",
        json={
            "preferred_channel": "mobile_push",
            "enable_email": True,
            "enable_mobile_push": True,
        },
    )

    assert response.status_code == 200
    data = response.json()

    assert data["preferred_channel"] == "mobile_push"
    assert data["enable_email"] is True
    assert data["enable_mobile_push"] is True


def test_register_mobile_device_direct(
    client: TestClient, test_user: User, db_session: Session
):
    """Test direct device registration (without QR token)."""
    response = client.post(
        "/api/v1/notification-preferences/me/register-device",
        json={"platform": "ios", "token": IOS_TOKEN},
    )

    assert response.status_code == 200
    data = response.json()

    # Verify device was added
    assert len(data["mobile_device_tokens"]) > 0
    device = data["mobile_device_tokens"][0]
    assert device["platform"] == "ios"
    assert device["token"] == IOS_TOKEN


def test_unregister_mobile_device(
    client: TestClient, test_user: User, db_session: Session
):
    """Test unregistering a mobile device."""
    # First register a device
    register_response = client.post(
        "/api/v1/notification-preferences/me/register-device",
        json={"platform": "ios", "token": IOS_TOKEN_2},
    )
    assert register_response.status_code == 200

    # Unregister the device
    response = client.delete(
        f"/api/v1/notification-preferences/me/device/{IOS_TOKEN_2}"
    )

    assert response.status_code == 200
    data = response.json()

    # Verify device was removed
    assert len(data["mobile_device_tokens"]) == 0


def test_unregister_nonexistent_device(
    client: TestClient, test_user: User, db_session: Session
):
    """Test unregistering a device that doesn't exist."""
    response = client.delete(
        "/api/v1/notification-preferences/me/device/nonexistent-token"
    )

    # Should return 404 when trying to remove device that doesn't exist
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
