"""Push device-token registration must reject blank tokens.

Regression: a blank token was accepted and stored, after which EVERY push
failed at APNs with 400 MissingDeviceToken while the account still looked
push-enabled — approvals silently never reached the phone/watch.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from preloop.models.models.notification_preferences import NotificationPreferences
from preloop.schemas.notification_preferences import MobileDeviceRegistration


def test_schema_rejects_blank_token() -> None:
    """The API schema refuses empty/whitespace tokens."""
    with pytest.raises(ValidationError):
        MobileDeviceRegistration(platform="ios", token="")
    with pytest.raises(ValidationError):
        MobileDeviceRegistration(platform="ios", token="   ")


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
