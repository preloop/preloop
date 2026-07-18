"""Tests for FCM error classification, token shape validation and pruning.

These cover the failure mode seen on prod/staging where FCM rejected stored
tokens with "The registration token is not a valid FCM registration token"
and the backend neither pruned nor correctly classified them.
"""

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.push_notifications.fcm_service import (
    FCM_REASON_AUTH,
    FCM_REASON_INVALID_TOKEN,
    FCM_REASON_MALFORMED_TOKEN,
    FCM_REASON_SENDER_ID_MISMATCH,
    FCM_REASON_UNAVAILABLE,
    FCM_REASON_UNREGISTERED,
    _send_fcm_sync,
    classify_fcm_error,
    looks_like_fcm_token,
)

# A structurally valid FCM registration token: instance-id prefix, colon, body.
VALID_TOKEN = "cXqNs9TgQ1mR2vB3nK4pLd:APA91b" + ("Z" * 134)


class TestLooksLikeFcmToken:
    """Local shape validation catches non-FCM tokens without a round-trip."""

    def test_accepts_realistic_fcm_token(self) -> None:
        assert looks_like_fcm_token(VALID_TOKEN) is True

    def test_rejects_android_placeholder_token(self) -> None:
        """QRScannerViewModel stores this literal when Firebase init fails."""
        assert looks_like_fcm_token("fcm_unavailable_1737051234567") is False

    def test_rejects_apns_hex_token(self) -> None:
        """An iOS APNs token must never be accepted on the FCM path."""
        assert looks_like_fcm_token("a" * 64) is False

    def test_rejects_blank_and_whitespace(self) -> None:
        assert looks_like_fcm_token("") is False
        assert looks_like_fcm_token("   ") is False

    def test_rejects_truncated_token(self) -> None:
        assert looks_like_fcm_token("cXqNs9TgQ1mR2vB3nK4pLd:APA91b") is False


class TestClassifyFcmError:
    """Provider errors map to distinct, actionable reasons."""

    def test_unregistered_is_pruned_and_not_retryable(self) -> None:
        from firebase_admin.messaging import UnregisteredError

        result = classify_fcm_error(
            UnregisteredError("Requested entity was not found.")
        )
        assert result.reason == FCM_REASON_UNREGISTERED
        assert result.should_prune is True
        assert result.retryable is False

    def test_invalid_argument_token_message_is_pruned(self) -> None:
        """The exact prod error string must be classified, not ignored."""
        from firebase_admin.exceptions import InvalidArgumentError

        result = classify_fcm_error(
            InvalidArgumentError(
                "The registration token is not a valid FCM registration token"
            )
        )
        assert result.reason == FCM_REASON_INVALID_TOKEN
        assert result.should_prune is True
        assert result.retryable is False

    def test_sender_id_mismatch_signals_wrong_firebase_project(self) -> None:
        from firebase_admin.messaging import SenderIdMismatchError

        result = classify_fcm_error(SenderIdMismatchError("SenderId mismatch"))
        assert result.reason == FCM_REASON_SENDER_ID_MISMATCH
        assert result.should_prune is True
        assert "project" in result.remediation.lower()

    def test_auth_error_is_never_pruned(self) -> None:
        """Bad server credentials must not delete every user's device token."""
        from firebase_admin.exceptions import UnauthenticatedError

        result = classify_fcm_error(UnauthenticatedError("Invalid JWT"))
        assert result.reason == FCM_REASON_AUTH
        assert result.should_prune is False

    def test_unavailable_is_retryable_and_not_pruned(self) -> None:
        from firebase_admin.exceptions import UnavailableError

        result = classify_fcm_error(UnavailableError("Service unavailable"))
        assert result.reason == FCM_REASON_UNAVAILABLE
        assert result.should_prune is False
        assert result.retryable is True

    def test_invalid_argument_unrelated_to_token_is_not_pruned(self) -> None:
        """A bad payload must not be blamed on the device token."""
        from firebase_admin.exceptions import InvalidArgumentError

        result = classify_fcm_error(
            InvalidArgumentError("Invalid value at 'message.android.ttl'")
        )
        assert result.reason != FCM_REASON_INVALID_TOKEN
        assert result.should_prune is False


class TestSendFcmSync:
    """The send path short-circuits bad tokens and surfaces real errors."""

    def test_malformed_token_never_reaches_firebase(self) -> None:
        with patch(
            "firebase_admin.messaging.send", side_effect=AssertionError("called")
        ) as send:
            result: Dict[str, Any] = _send_fcm_sync(
                token="fcm_unavailable_1737051234567", title="t", body="b"
            )
        send.assert_not_called()
        assert result["success"] is False
        assert result["error_reason"] == FCM_REASON_MALFORMED_TOKEN
        assert result["invalid_token"] is True
        assert result["should_prune"] is True

    def test_successful_send_returns_message_id(self) -> None:
        with patch(
            "firebase_admin.messaging.send", return_value="projects/p/messages/1"
        ):
            result = _send_fcm_sync(token=VALID_TOKEN, title="t", body="b")
        assert result["success"] is True
        assert result["message_id"] == "projects/p/messages/1"

    def test_provider_error_is_classified_and_surfaced(self) -> None:
        from firebase_admin.messaging import UnregisteredError

        with patch(
            "firebase_admin.messaging.send",
            side_effect=UnregisteredError("Requested entity was not found."),
        ):
            result = _send_fcm_sync(token=VALID_TOKEN, title="t", body="b")
        assert result["success"] is False
        assert result["error_reason"] == FCM_REASON_UNREGISTERED
        assert result["should_prune"] is True
        assert "not was found" not in result["error"]
        assert result["error"]

    def test_data_payload_values_are_stringified(self) -> None:
        captured = {}

        def _capture(message: MagicMock) -> str:
            captured["data"] = message.data
            return "ok"

        with patch("firebase_admin.messaging.send", side_effect=_capture):
            _send_fcm_sync(
                token=VALID_TOKEN,
                title="t",
                body="b",
                data={"n": 1, "nested": {"a": 1}, "s": "x"},
            )
        assert captured["data"] == {"n": "1", "nested": '{"a": 1}', "s": "x"}


class TestSendFcmSyncFailuresAreContained:
    """A push failure must never raise into the approval flow."""

    def test_unexpected_exception_returns_error_dict(self) -> None:
        with patch("firebase_admin.messaging.send", side_effect=RuntimeError("boom")):
            result = _send_fcm_sync(token=VALID_TOKEN, title="t", body="b")
        assert result["success"] is False
        assert "boom" in result["error"]
        assert result["should_prune"] is False


if __name__ == "__main__":
    pytest.main([__file__])
