"""Tests for the admin diagnostic test-send path."""

import uuid
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services.push_notifications.test_send import (
    TEST_TITLE_PREFIX,
    TEST_TOOL_NAME,
    build_test_payload,
    mask_token,
    send_test_push,
)

pytestmark = pytest.mark.asyncio

MODULE = "preloop.services.push_notifications.test_send"
VALID_FCM_TOKEN = "cXqNs9TgQ1mR2vB3nK4pLd:APA91b" + ("Z" * 134)
VALID_APNS_TOKEN = "a" * 64


class TestBuildTestPayload:
    """The payload must be unmistakably a test and structurally real."""

    def test_approval_payload_is_labelled_test(self) -> None:
        payload = build_test_payload("approval", str(uuid.uuid4()))
        assert payload["is_test"] is True
        assert payload["data"]["is_test"] is True
        assert payload["aps"]["alert"]["title"].startswith(TEST_TITLE_PREFIX)
        assert TEST_TITLE_PREFIX in payload["aps"]["alert"]["body"]
        assert payload["tool_name"] == TEST_TOOL_NAME

    def test_question_payload_carries_question_fields(self) -> None:
        payload = build_test_payload("question", str(uuid.uuid4()))
        assert payload["data"]["is_question"] is True
        assert payload["data"]["question_options"] == ["Yes, received", "No"]
        assert payload["data"]["allow_free_text"] is True
        assert payload["aps"]["alert"]["title"].startswith(TEST_TITLE_PREFIX)

    def test_request_id_is_synthetic_and_unique(self) -> None:
        """The id must be a fresh UUID that maps to no stored request."""
        first = build_test_payload("approval", str(uuid.uuid4()))
        second = build_test_payload("approval", str(uuid.uuid4()))
        assert first["approval_request_id"] != second["approval_request_id"]
        uuid.UUID(first["approval_request_id"])  # parses => well-formed

    def test_payload_uses_the_production_builder(self) -> None:
        """A bespoke payload would not prove the real path works."""
        with patch(
            "preloop.services.push_notifications.notification_payloads."
            "NotificationPayloadBuilder.new_approval_request",
            return_value={"aps": {"alert": {"title": "x", "body": "y"}}, "data": {}},
        ) as builder:
            build_test_payload("approval", "req-1")
        builder.assert_called_once()


class TestMaskToken:
    def test_masks_long_tokens(self) -> None:
        assert mask_token(VALID_FCM_TOKEN).startswith("cXqNs9Tg")
        assert "..." in mask_token(VALID_FCM_TOKEN)

    def test_blank_token(self) -> None:
        assert mask_token("") == "<blank>"


class TestSendTestPush:
    """The test send must use the real transports and surface real errors."""

    async def test_android_uses_real_fcm_sender(self) -> None:
        fcm = AsyncMock(
            return_value={"success": True, "message_id": "projects/p/messages/1"}
        )
        with (
            patch("preloop.services.push_notifications.get_apns_service", lambda: None),
            patch(
                "preloop.services.push_notifications.fcm_service.is_fcm_configured",
                return_value=True,
            ),
            patch(
                "preloop.services.push_notifications.fcm_service.send_fcm_notification",
                fcm,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
        ):
            report = await send_test_push(
                [{"platform": "android", "token": VALID_FCM_TOKEN}], kind="approval"
            )

        fcm.assert_awaited_once()
        assert report.sent == 1
        assert report.failed == 0
        assert report.results[0].transport == "fcm"
        assert report.results[0].success is True

    async def test_fcm_error_is_surfaced_verbatim(self) -> None:
        """This is the diagnostic the founder needs: the real provider error."""
        fcm = AsyncMock(
            return_value={
                "success": False,
                "error": "The registration token is not a valid FCM registration token",
                "error_reason": "invalid_token",
                "remediation": "Check the Firebase project.",
                "project_id": "preloop-ai",
            }
        )
        with (
            patch(
                "preloop.services.push_notifications.fcm_service.is_fcm_configured",
                return_value=True,
            ),
            patch(
                "preloop.services.push_notifications.fcm_service.send_fcm_notification",
                fcm,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
        ):
            report = await send_test_push(
                [{"platform": "android", "token": VALID_FCM_TOKEN}]
            )

        result = report.results[0]
        assert report.failed == 1
        assert result.success is False
        assert "not a valid FCM registration token" in (result.error or "")
        assert result.error_reason == "invalid_token"
        assert result.project_id == "preloop-ai"
        assert result.remediation

    async def test_ios_uses_real_apns_service(self) -> None:
        apns = MagicMock()
        apns.send_notification = AsyncMock(return_value=(True, 200, None))
        with (
            patch("preloop.services.push_notifications.get_apns_service", lambda: apns),
            patch(
                "preloop.services.push_notifications.fcm_service.is_fcm_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
        ):
            report = await send_test_push(
                [{"platform": "ios", "token": VALID_APNS_TOKEN}]
            )

        apns.send_notification.assert_awaited_once()
        assert report.results[0].transport == "apns"
        assert report.results[0].success is True

    async def test_apns_failure_reports_status_and_remediation(self) -> None:
        apns = MagicMock()
        apns.send_notification = AsyncMock(return_value=(False, 400, "BadDeviceToken"))
        with (
            patch("preloop.services.push_notifications.get_apns_service", lambda: apns),
            patch(
                "preloop.services.push_notifications.fcm_service.is_fcm_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
        ):
            report = await send_test_push(
                [{"platform": "ios", "token": VALID_APNS_TOKEN}]
            )

        result = report.results[0]
        assert result.success is False
        assert result.status_code == 400
        assert result.error_reason == "BadDeviceToken"
        assert "APNS_BUNDLE_ID" in (result.remediation or "")

    async def test_reports_when_push_is_not_configured(self) -> None:
        with (
            patch("preloop.services.push_notifications.get_apns_service", lambda: None),
            patch(
                "preloop.services.push_notifications.fcm_service.is_fcm_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
        ):
            report = await send_test_push(
                [{"platform": "android", "token": VALID_FCM_TOKEN}]
            )

        assert report.results[0].error_reason == "not_configured"
        assert report.results[0].transport == "none"

    async def test_blank_and_unknown_platform_are_reported_not_raised(self) -> None:
        with (
            patch("preloop.services.push_notifications.get_apns_service", lambda: None),
            patch(
                "preloop.services.push_notifications.fcm_service.is_fcm_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
        ):
            report = await send_test_push(
                [
                    {"platform": "android", "token": "  "},
                    {"platform": "windows", "token": "abc"},
                ]
            )

        reasons = [r.error_reason for r in report.results]
        assert "blank_token" in reasons
        assert "unsupported_platform" in reasons
        assert report.failed == 2

    async def test_report_is_json_serialisable(self) -> None:
        with (
            patch("preloop.services.push_notifications.get_apns_service", lambda: None),
            patch(
                "preloop.services.push_notifications.fcm_service.is_fcm_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
        ):
            report = await send_test_push(
                [{"platform": "android", "token": VALID_FCM_TOKEN}]
            )

        as_dict: Dict[str, Any] = report.as_dict()
        assert as_dict["kind"] == "approval"
        assert as_dict["failed"] == 1
        results: List[Dict[str, Any]] = as_dict["results"]
        assert results[0]["token"].startswith("cXqNs9Tg")
        # The full token must never leave the server.
        assert VALID_FCM_TOKEN not in str(as_dict)


if __name__ == "__main__":
    pytest.main([__file__])
