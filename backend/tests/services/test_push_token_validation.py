"""Tests for registration-time device token validation."""

import pytest

from preloop.services.push_notifications.token_validation import (
    is_placeholder_token,
    looks_like_apns_token,
    validate_device_token,
)

VALID_FCM = "cXqNs9TgQ1mR2vB3nK4pLd:APA91b" + ("Z" * 134)
VALID_APNS = "7f87557745bffcb16fa49fdf684e844f9f96582980b1fa290a786556c344fcae"


class TestLooksLikeApnsToken:
    def test_accepts_64_char_hex(self) -> None:
        assert looks_like_apns_token(VALID_APNS) is True

    def test_rejects_non_hex(self) -> None:
        assert looks_like_apns_token("z" * 64) is False

    def test_rejects_fcm_token(self) -> None:
        assert looks_like_apns_token(VALID_FCM) is False

    def test_rejects_short_hex(self) -> None:
        assert looks_like_apns_token("abcd") is False


class TestIsPlaceholderToken:
    def test_detects_android_placeholder(self) -> None:
        assert is_placeholder_token("fcm_unavailable_1737051234567") is True

    def test_detects_case_insensitively(self) -> None:
        assert is_placeholder_token("  FCM_Unavailable_123  ") is True

    def test_real_tokens_are_not_placeholders(self) -> None:
        assert is_placeholder_token(VALID_FCM) is False
        assert is_placeholder_token(VALID_APNS) is False


class TestValidateDeviceToken:
    def test_accepts_valid_pairs(self) -> None:
        assert validate_device_token("android", VALID_FCM) == (True, None)
        assert validate_device_token("ios", VALID_APNS) == (True, None)

    def test_rejects_unknown_platform(self) -> None:
        ok, error = validate_device_token("windows", VALID_FCM)
        assert ok is False
        assert "Platform must be" in (error or "")

    def test_rejects_blank_token(self) -> None:
        ok, error = validate_device_token("android", "   ")
        assert ok is False
        assert "must not be empty" in (error or "")

    def test_rejects_the_android_placeholder(self) -> None:
        """This is the exact string that poisoned prod and staging."""
        ok, error = validate_device_token("android", "fcm_unavailable_1737051234567")
        assert ok is False
        assert "placeholder" in (error or "").lower()

    def test_rejects_apns_token_registered_as_android(self) -> None:
        ok, error = validate_device_token("android", VALID_APNS)
        assert ok is False
        assert "FCM registration token" in (error or "")

    def test_rejects_fcm_token_registered_as_ios(self) -> None:
        ok, error = validate_device_token("ios", VALID_FCM)
        assert ok is False
        assert "platform field" in (error or "")

    def test_platform_is_case_insensitive(self) -> None:
        assert validate_device_token("Android", VALID_FCM) == (True, None)

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert validate_device_token("android", f"  {VALID_FCM}  ") == (True, None)


if __name__ == "__main__":
    pytest.main([__file__])
