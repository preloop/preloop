"""Tests for the admin-only test-push endpoint: gating, rate limit, results."""

import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import notification_preferences as endpoint
from preloop.schemas.notification_preferences import TestPushRequest
from preloop.services.push_notifications.test_send import (
    DeviceSendResult,
    TestPushReport,
)

pytestmark = pytest.mark.asyncio

VALID_FCM_TOKEN = "cXqNs9TgQ1mR2vB3nK4pLd:APA91b" + ("Z" * 134)


def _user(is_superuser: bool) -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.is_superuser = is_superuser
    return user


def _prefs_with_devices(devices: list) -> MagicMock:
    prefs = MagicMock()
    prefs.mobile_device_tokens = devices
    return prefs


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Keep the module-level sliding window from leaking between tests."""
    endpoint._test_push_state.clear()
    yield
    endpoint._test_push_state.clear()


class TestAdminGating:
    async def test_non_admin_is_rejected_with_403(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await endpoint.send_test_push_notification(
                TestPushRequest(kind="approval"),
                db=MagicMock(),
                current_user=_user(is_superuser=False),
            )
        assert exc.value.status_code == 403
        assert "Admin access required" in exc.value.detail

    async def test_non_admin_never_reaches_the_send_path(self) -> None:
        sender = AsyncMock()
        with patch(
            "preloop.services.push_notifications.test_send.send_test_push", sender
        ):
            with pytest.raises(HTTPException):
                await endpoint.send_test_push_notification(
                    TestPushRequest(kind="approval"),
                    db=MagicMock(),
                    current_user=_user(is_superuser=False),
                )
        sender.assert_not_awaited()

    async def test_non_admin_does_not_consume_rate_budget(self) -> None:
        user = _user(is_superuser=False)
        for _ in range(10):
            with pytest.raises(HTTPException) as exc:
                await endpoint.send_test_push_notification(
                    TestPushRequest(kind="approval"),
                    db=MagicMock(),
                    current_user=user,
                )
            assert exc.value.status_code == 403


class TestNoDevices:
    async def test_admin_without_devices_gets_404(self) -> None:
        with patch.object(
            endpoint.notification_preferences,
            "get_by_user",
            return_value=_prefs_with_devices([]),
        ):
            with pytest.raises(HTTPException) as exc:
                await endpoint.send_test_push_notification(
                    TestPushRequest(kind="approval"),
                    db=MagicMock(),
                    current_user=_user(is_superuser=True),
                )
        assert exc.value.status_code == 404

    async def test_platform_filter_narrows_devices(self) -> None:
        devices = [
            {"platform": "android", "token": VALID_FCM_TOKEN},
            {"platform": "ios", "token": "a" * 64},
        ]
        sender = AsyncMock(
            return_value=TestPushReport(kind="approval", request_id="r", sent=1)
        )
        with (
            patch.object(
                endpoint.notification_preferences,
                "get_by_user",
                return_value=_prefs_with_devices(devices),
            ),
            patch(
                "preloop.services.push_notifications.test_send.send_test_push", sender
            ),
        ):
            await endpoint.send_test_push_notification(
                TestPushRequest(kind="approval", platform="ios"),
                db=MagicMock(),
                current_user=_user(is_superuser=True),
            )
        assert sender.await_args.kwargs["devices"] == [devices[1]]


class TestSuccessfulTestSend:
    async def test_admin_receives_per_device_provider_result(self) -> None:
        report = TestPushReport(
            kind="approval",
            request_id="req-1",
            sent=0,
            failed=1,
            results=[
                DeviceSendResult(
                    platform="android",
                    token="cXqNs9Tg...ZZZZ",
                    transport="fcm",
                    success=False,
                    error=(
                        "The registration token is not a valid FCM registration token"
                    ),
                    error_reason="invalid_token",
                    remediation="Check the Firebase project.",
                    project_id="preloop-ai",
                )
            ],
        )
        with (
            patch.object(
                endpoint.notification_preferences,
                "get_by_user",
                return_value=_prefs_with_devices(
                    [{"platform": "android", "token": VALID_FCM_TOKEN}]
                ),
            ),
            patch(
                "preloop.services.push_notifications.test_send.send_test_push",
                AsyncMock(return_value=report),
            ),
        ):
            result = await endpoint.send_test_push_notification(
                TestPushRequest(kind="approval"),
                db=MagicMock(),
                current_user=_user(is_superuser=True),
            )

        assert result["failed"] == 1
        assert "not a valid FCM registration token" in result["results"][0]["error"]
        assert result["results"][0]["project_id"] == "preloop-ai"

    async def test_question_kind_is_passed_through(self) -> None:
        sender = AsyncMock(
            return_value=TestPushReport(kind="question", request_id="r", sent=1)
        )
        with (
            patch.object(
                endpoint.notification_preferences,
                "get_by_user",
                return_value=_prefs_with_devices(
                    [{"platform": "android", "token": VALID_FCM_TOKEN}]
                ),
            ),
            patch(
                "preloop.services.push_notifications.test_send.send_test_push", sender
            ),
        ):
            await endpoint.send_test_push_notification(
                TestPushRequest(kind="question"),
                db=MagicMock(),
                current_user=_user(is_superuser=True),
            )
        assert sender.await_args.kwargs["kind"] == "question"


class TestRateLimiting:
    async def test_sixth_send_in_a_minute_is_rejected(self) -> None:
        user = _user(is_superuser=True)
        with (
            patch.object(
                endpoint.notification_preferences,
                "get_by_user",
                return_value=_prefs_with_devices(
                    [{"platform": "android", "token": VALID_FCM_TOKEN}]
                ),
            ),
            patch(
                "preloop.services.push_notifications.test_send.send_test_push",
                AsyncMock(
                    return_value=TestPushReport(kind="approval", request_id="r", sent=1)
                ),
            ),
        ):
            for _ in range(endpoint._TEST_PUSH_LIMIT_PER_MINUTE):
                await endpoint.send_test_push_notification(
                    TestPushRequest(kind="approval"),
                    db=MagicMock(),
                    current_user=user,
                )

            with pytest.raises(HTTPException) as exc:
                await endpoint.send_test_push_notification(
                    TestPushRequest(kind="approval"),
                    db=MagicMock(),
                    current_user=user,
                )

        assert exc.value.status_code == 429

    async def test_limit_is_per_user(self) -> None:
        first, second = _user(True), _user(True)
        with (
            patch.object(
                endpoint.notification_preferences,
                "get_by_user",
                return_value=_prefs_with_devices(
                    [{"platform": "android", "token": VALID_FCM_TOKEN}]
                ),
            ),
            patch(
                "preloop.services.push_notifications.test_send.send_test_push",
                AsyncMock(
                    return_value=TestPushReport(kind="approval", request_id="r", sent=1)
                ),
            ),
        ):
            for _ in range(endpoint._TEST_PUSH_LIMIT_PER_MINUTE):
                await endpoint.send_test_push_notification(
                    TestPushRequest(kind="approval"),
                    db=MagicMock(),
                    current_user=first,
                )
            # The second admin still has a full budget.
            await endpoint.send_test_push_notification(
                TestPushRequest(kind="approval"),
                db=MagicMock(),
                current_user=second,
            )


class TestRateLimiterEviction:
    def test_stale_entries_are_swept(self) -> None:
        """Users who tried once and never returned must not accumulate forever.

        The dict previously only ever gained keys; entries for one-time
        callers lived for the life of the process. The sweep runs
        opportunistically once per window, so seed a stale user, force the
        sweep to be due, and enforce for a different user.
        """
        stale_time = time.monotonic() - (endpoint._TEST_PUSH_WINDOW_SECONDS * 3)
        endpoint._test_push_state["stale-user"] = [stale_time]
        # Force the next call to consider a sweep due.
        endpoint._test_push_last_sweep = stale_time

        endpoint._enforce_test_push_rate_limit("active-user")

        assert "stale-user" not in endpoint._test_push_state
        assert "active-user" in endpoint._test_push_state

    def test_current_user_survives_sweep(self) -> None:
        """The caller's own fresh entry must never be evicted by the sweep."""
        endpoint._test_push_last_sweep = time.monotonic() - (
            endpoint._TEST_PUSH_WINDOW_SECONDS * 3
        )
        endpoint._enforce_test_push_rate_limit("caller")
        endpoint._enforce_test_push_rate_limit("caller")
        assert len(endpoint._test_push_state["caller"]) == 2


if __name__ == "__main__":
    pytest.main([__file__])
