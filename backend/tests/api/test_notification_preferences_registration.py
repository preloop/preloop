"""Tests for registration-time push token validation on both register paths."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import notification_preferences as endpoint
from preloop.schemas.notification_preferences import MobileDeviceRegistration

pytestmark = pytest.mark.asyncio

VALID_FCM = "cXqNs9TgQ1mR2vB3nK4pLd:APA91b" + ("Z" * 134)
VALID_APNS = "7f87557745bffcb16fa49fdf684e844f9f96582980b1fa290a786556c344fcae"
PLACEHOLDER = "fcm_unavailable_1737051234567"


def _user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.account_id = uuid.uuid4()
    return user


class TestRegisterMobileDevice:
    """POST /me/register-device must refuse tokens that can never work."""

    async def test_accepts_valid_android_token(self) -> None:
        prefs = MagicMock()
        with patch.object(
            endpoint.notification_preferences, "add_device_token", return_value=prefs
        ) as add:
            await endpoint.register_mobile_device(
                MobileDeviceRegistration(platform="android", token=VALID_FCM),
                db=MagicMock(),
                current_user=_user(),
            )
        add.assert_called_once()

    async def test_accepts_valid_ios_token(self) -> None:
        with patch.object(
            endpoint.notification_preferences,
            "add_device_token",
            return_value=MagicMock(),
        ) as add:
            await endpoint.register_mobile_device(
                MobileDeviceRegistration(platform="ios", token=VALID_APNS),
                db=MagicMock(),
                current_user=_user(),
            )
        add.assert_called_once()

    async def test_rejects_placeholder_token_without_storing_it(self) -> None:
        with patch.object(endpoint.notification_preferences, "add_device_token") as add:
            with pytest.raises(HTTPException) as exc:
                await endpoint.register_mobile_device(
                    MobileDeviceRegistration(platform="android", token=PLACEHOLDER),
                    db=MagicMock(),
                    current_user=_user(),
                )
        assert exc.value.status_code == 400
        assert "placeholder" in exc.value.detail.lower()
        add.assert_not_called()

    async def test_rejects_apns_token_registered_as_android(self) -> None:
        with patch.object(endpoint.notification_preferences, "add_device_token") as add:
            with pytest.raises(HTTPException) as exc:
                await endpoint.register_mobile_device(
                    MobileDeviceRegistration(platform="android", token=VALID_APNS),
                    db=MagicMock(),
                    current_user=_user(),
                )
        assert exc.value.status_code == 400
        add.assert_not_called()

    async def test_rejects_unknown_platform(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await endpoint.register_mobile_device(
                MobileDeviceRegistration(platform="windows", token=VALID_FCM),
                db=MagicMock(),
                current_user=_user(),
            )
        assert exc.value.status_code == 400


class TestRegisterViaTokenKeepsPairingWorking:
    """QR pairing must still succeed when the push token is unusable."""

    @pytest.fixture
    def paired(self):
        """Patch the QR pairing collaborators and expose add_device_token."""
        from contextlib import ExitStack

        user = _user()
        prefs = MagicMock()
        prefs.mobile_device_tokens = []
        prefs.enable_mobile_push = False

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    endpoint, "validate_registration_token", return_value=user.id
                )
            )
            stack.enter_context(
                patch("preloop.models.crud.crud_user.get", return_value=user)
            )
            stack.enter_context(
                patch.object(
                    endpoint.notification_preferences,
                    "get_by_user",
                    return_value=None,
                )
            )
            stack.enter_context(
                patch.object(
                    endpoint.notification_preferences,
                    "get_or_create",
                    return_value=prefs,
                )
            )
            stack.enter_context(
                patch(
                    "preloop.services.websocket_manager.manager.broadcast_json",
                    new_callable=AsyncMock,
                )
            )
            add = stack.enter_context(
                patch.object(
                    endpoint.notification_preferences,
                    "add_device_token",
                    return_value=prefs,
                )
            )
            yield add

    async def test_placeholder_token_pairs_but_does_not_enable_push(
        self, paired
    ) -> None:
        result = await endpoint.register_device_via_token(
            token="qr-token",
            device_in=MobileDeviceRegistration(platform="android", token=PLACEHOLDER),
            db=MagicMock(),
        )

        # Pairing still yields an API key ...
        assert result["api_key"]
        # ... but the poison token was never stored.
        paired.assert_not_called()
        assert result["push_registered"] is False
        assert "placeholder" in (result["push_error"] or "").lower()

    async def test_valid_token_is_stored_and_push_reported_enabled(
        self, paired
    ) -> None:
        result = await endpoint.register_device_via_token(
            token="qr-token",
            device_in=MobileDeviceRegistration(platform="android", token=VALID_FCM),
            db=MagicMock(),
        )

        paired.assert_called_once()
        assert result["push_registered"] is True
        assert result["push_error"] is None


if __name__ == "__main__":
    pytest.main([__file__])
