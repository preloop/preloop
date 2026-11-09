"""Push device-token registration must reject blank tokens.

Regression: a blank token was accepted and stored, after which EVERY push
failed at APNs with 400 MissingDeviceToken while the account still looked
push-enabled — approvals silently never reached the phone/watch.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from preloop.models.models.notification_preferences import NotificationPreferences
from preloop.schemas.notification_preferences import (
    MobileDeviceRegistration,
    MobileDeviceTokenRegistration,
)


def test_schema_normalises_blank_token_to_none() -> None:
    """Missing, null and blank tokens all mean "no push token" (#2248).

    A fresh app install has no push token yet; the schema must not 422 the
    pairing call. Blank tokens become None so they can never be stored.
    """
    assert MobileDeviceRegistration(platform="ios").token is None
    assert MobileDeviceRegistration(platform="ios", token=None).token is None
    assert MobileDeviceRegistration(platform="ios", token="").token is None
    assert MobileDeviceRegistration(platform="ios", token="   ").token is None


def test_schema_still_requires_platform() -> None:
    """Platform remains required."""
    with pytest.raises(ValidationError):
        MobileDeviceRegistration(token="abc123")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "kwargs", [{}, {"token": None}, {"token": ""}, {"token": "  "}]
)
def test_push_only_schema_requires_non_blank_token(kwargs: dict) -> None:
    """me/register-device's schema rejects missing, null and blank tokens."""
    with pytest.raises(ValidationError):
        MobileDeviceTokenRegistration(platform="ios", **kwargs)


def test_push_only_schema_trims_token() -> None:
    """A valid token is trimmed by the push-only schema."""
    assert MobileDeviceTokenRegistration(platform="ios", token=" abc ").token == "abc"


def test_schema_trims_token() -> None:
    """Valid tokens are stored trimmed."""
    registration = MobileDeviceRegistration(platform="ios", token=" abc123 ")
    assert registration.token == "abc123"


def test_model_rejects_blank_token() -> None:
    """Defense in depth: the model refuses to persist a blank token."""
    prefs = NotificationPreferences()
    with pytest.raises(ValueError):
        prefs.add_device_token("ios", "")
    with pytest.raises(ValueError):
        prefs.add_device_token("ios", "  ")

    prefs.add_device_token("ios", "realtoken")
    assert prefs.mobile_device_tokens[0]["token"] == "realtoken"
