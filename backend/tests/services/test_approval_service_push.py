"""Tests for ApprovalService push notification integration."""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from preloop.models.models import ApprovalWorkflow, ApprovalRequest

from preloop.services.approval_service import ApprovalService

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_db():
    """Create mock async database session."""
    mock = AsyncMock()
    # Mock the bind.sync_engine for Session creation
    mock.bind.sync_engine = MagicMock()
    return mock


@pytest.fixture
def approval_service(mock_db):
    """Create ApprovalService instance with mocked database."""
    return ApprovalService(mock_db, "https://app.test.com")


@pytest.fixture
def sample_approval_workflow():
    """Create sample approval workflow with mobile_push channel."""
    policy = MagicMock(spec=ApprovalWorkflow)
    policy.id = uuid.uuid4()
    policy.notification_channels = ["mobile_push"]
    policy.approver_user_ids = [uuid.uuid4(), uuid.uuid4()]
    return policy


@pytest.fixture
def sample_approval_request():
    """Create sample approval request."""
    request = MagicMock(spec=ApprovalRequest)
    request.id = uuid.uuid4()
    request.account_id = "test_account"
    request.tool_configuration_id = uuid.uuid4()
    request.approval_workflow_id = uuid.uuid4()
    request.tool_name = "create_issue"
    request.tool_args = {"title": "Test Issue"}
    request.agent_reasoning = "Need to create an issue"
    request.status = "pending"
    request.requested_at = datetime.utcnow()
    request.expires_at = datetime.utcnow() + timedelta(minutes=5)
    request.priority = "medium"
    return request


@pytest.fixture
def mock_apns_service():
    """Mock APNs service."""
    mock_service = AsyncMock()
    mock_service.send_notification = AsyncMock(return_value=(True, 200, None))
    return mock_service


class TestSendPushNotification:
    """Test _send_push_notification method."""

    async def test_send_push_notification_success(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        mock_apns_service,
    ):
        """Test successful push notification send."""
        # Mock notification preferences and sync session
        mock_prefs = MagicMock()
        mock_prefs.enable_mobile_push = True
        mock_prefs.get_device_tokens.return_value = ["a" * 64]

        mock_sync_session = MagicMock()
        mock_sync_session.close = MagicMock()

        # Mock the _get_all_approver_user_ids_sync to return approvers
        user_id_1, user_id_2 = sample_approval_workflow.approver_user_ids

        # Create a mock that returns the approvers and tokens when executor runs
        def mock_run_in_executor(executor, func):
            """Execute the sync function directly for testing."""
            import asyncio

            # Return a future that resolves to the function result
            future = asyncio.Future()
            # We need to mock what the function returns (approvers, ios_tokens, android_tokens)
            future.set_result(([user_id_1], [(user_id_1, "a" * 64)], []))
            return future

        with patch(
            "preloop.services.push_notifications.get_apns_service",
            return_value=mock_apns_service,
        ):
            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = mock_run_in_executor
                mock_get_loop.return_value = mock_loop

                result = await approval_service._send_push_notification(
                    sample_approval_request, sample_approval_workflow
                )

                assert result["success"] is True
                assert result["sent"] == 1

    async def test_send_push_notification_multiple_approvers(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        mock_apns_service,
    ):
        """Test sending to multiple approvers."""
        user_id_1, user_id_2 = sample_approval_workflow.approver_user_ids

        def mock_run_in_executor(executor, func):
            import asyncio

            future = asyncio.Future()
            # Two approvers, each with an iOS token (approvers, ios_tokens, android_tokens)
            future.set_result(
                (
                    [user_id_1, user_id_2],
                    [(user_id_1, "a" * 64), (user_id_2, "b" * 64)],
                    [],
                )
            )
            return future

        with patch(
            "preloop.services.push_notifications.get_apns_service",
            return_value=mock_apns_service,
        ):
            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = mock_run_in_executor
                mock_get_loop.return_value = mock_loop

                result = await approval_service._send_push_notification(
                    sample_approval_request, sample_approval_workflow
                )

                assert result["sent"] == 2

    async def test_send_push_notification_no_approvers(
        self, approval_service, sample_approval_request, mock_apns_service
    ):
        """Test handling when no approvers are configured."""
        policy_no_approvers = MagicMock(spec=ApprovalWorkflow)
        policy_no_approvers.id = uuid.uuid4()
        policy_no_approvers.approver_user_ids = []
        policy_no_approvers.approver_team_ids = []

        def mock_run_in_executor(executor, func):
            import asyncio

            future = asyncio.Future()
            future.set_result(
                ([], [], [])
            )  # No approvers (approvers, ios_tokens, android_tokens)
            return future

        with patch(
            "preloop.services.push_notifications.get_apns_service",
            return_value=mock_apns_service,
        ):
            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = mock_run_in_executor
                mock_get_loop.return_value = mock_loop

                result = await approval_service._send_push_notification(
                    sample_approval_request, policy_no_approvers
                )

                assert result["success"] is False
                assert result["error"] == "No approvers configured"

    async def test_send_push_notification_apns_not_configured(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test graceful handling when push notifications are not configured."""
        with patch(
            "preloop.services.push_notifications.get_apns_service", return_value=None
        ):
            with patch(
                "preloop.services.push_notifications.is_fcm_configured",
                return_value=False,
            ):
                with patch(
                    "preloop.services.push_proxy.is_push_proxy_configured",
                    return_value=False,
                ):
                    result = await approval_service._send_push_notification(
                        sample_approval_request, sample_approval_workflow
                    )

                    assert result["success"] is False
                    assert result["error"] == "Push notifications not configured"

    async def test_send_push_notification_invalid_token_removal(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        mock_apns_service,
    ):
        """Test that invalid tokens are removed (410 response)."""
        # Mock 410 response (token no longer valid)
        mock_apns_service.send_notification = AsyncMock(
            return_value=(False, 410, "Unregistered")
        )

        user_id_1, user_id_2 = sample_approval_workflow.approver_user_ids

        def mock_run_in_executor(executor, func):
            import asyncio

            future = asyncio.Future()
            # Two approvers, each with an iOS token (approvers, ios_tokens, android_tokens)
            future.set_result(
                (
                    [user_id_1, user_id_2],
                    [(user_id_1, "a" * 64), (user_id_2, "b" * 64)],
                    [],
                )
            )
            return future

        with patch(
            "preloop.services.push_notifications.get_apns_service",
            return_value=mock_apns_service,
        ):
            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = mock_run_in_executor
                mock_get_loop.return_value = mock_loop

                result = await approval_service._send_push_notification(
                    sample_approval_request, sample_approval_workflow
                )

                # Both tokens should be marked for removal
                assert result["invalid_tokens_removed"] == 2

    async def test_send_push_notification_prunes_blank_ios_token(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        mock_apns_service,
    ):
        """Legacy blank tokens are pruned without calling APNs."""
        user_id_1, _user_id_2 = sample_approval_workflow.approver_user_ids

        def mock_run_in_executor(executor, func):
            import asyncio

            future = asyncio.Future()
            future.set_result(([user_id_1], [(user_id_1, "   ")], []))
            return future

        with patch(
            "preloop.services.push_notifications.get_apns_service",
            return_value=mock_apns_service,
        ):
            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = mock_run_in_executor
                mock_get_loop.return_value = mock_loop

                result = await approval_service._send_push_notification(
                    sample_approval_request, sample_approval_workflow
                )

                assert result["success"] is True
                assert result["sent"] == 0
                assert result["failed"] == 0
                assert result["invalid_tokens_removed"] == 1
                assert result["failure_details"] == [
                    {
                        "platform": "ios",
                        "token": "<blank>",
                        "status_code": 400,
                        "reason": "MissingDeviceToken",
                        "pruned": True,
                    }
                ]
                mock_apns_service.send_notification.assert_not_called()

    async def test_send_push_notification_partial_failure_alert_includes_reason(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        mock_apns_service,
    ):
        """Admin alert includes masked token and provider reason for real failures."""
        user_id_1, user_id_2 = sample_approval_workflow.approver_user_ids
        mock_apns_service.send_notification = AsyncMock(
            side_effect=[
                (True, 200, None),
                (False, 500, "InternalServerError"),
            ]
        )

        def mock_run_in_executor(executor, func):
            import asyncio

            future = asyncio.Future()
            future.set_result(
                (
                    [user_id_1, user_id_2],
                    [(user_id_1, "a" * 64), (user_id_2, "b" * 64)],
                    [],
                )
            )
            return future

        task_publisher = AsyncMock()

        with patch(
            "preloop.services.push_notifications.get_apns_service",
            return_value=mock_apns_service,
        ):
            with patch(
                "preloop.services.approval_service.get_task_publisher",
                new=AsyncMock(return_value=task_publisher),
            ):
                with patch("asyncio.get_event_loop") as mock_get_loop:
                    mock_loop = MagicMock()
                    mock_loop.run_in_executor = mock_run_in_executor
                    mock_get_loop.return_value = mock_loop

                    result = await approval_service._send_push_notification(
                        sample_approval_request, sample_approval_workflow
                    )

        assert result["success"] is False
        assert result["sent"] == 1
        assert result["failed"] == 1
        assert result["invalid_tokens_removed"] == 0
        assert result["error"] == "InternalServerError"
        assert result["failure_details"] == [
            {
                "platform": "ios",
                "token": "bbbbbbbb...bbbb",
                "status_code": 500,
                "reason": "InternalServerError",
                "pruned": False,
            }
        ]
        task_publisher.publish_task.assert_awaited_once()
        publish_kwargs = task_publisher.publish_task.await_args.kwargs
        assert publish_kwargs["subject"] == "Push Notification Delivery Failed"
        assert "Sent: 1, Failed: 1" in publish_kwargs["message"]
        assert (
            "ios token=bbbbbbbb...bbbb status=500 reason=InternalServerError"
            in publish_kwargs["message"]
        )

    async def test_send_notifications_triggers_push(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        mock_apns_service,
    ):
        """Test that send_notifications triggers push notification when mobile_push is in channels."""
        # Ensure policy has mobile_push in channels
        sample_approval_workflow.notification_channels = ["mobile_push"]

        # Mock push notification method
        with patch.object(
            approval_service,
            "_send_push_notification",
            new=AsyncMock(return_value={"success": True, "sent": 1}),
        ) as mock_send_push:
            await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

            # Verify _send_push_notification was called
            mock_send_push.assert_called_once()

    async def test_send_notifications_always_attempts_push(
        self, approval_service, sample_approval_request, mock_apns_service
    ):
        """Test that push is always attempted (filtering happens based on user preferences)."""
        # Create policy - notification_channels no longer controls push behavior
        policy = MagicMock(spec=ApprovalWorkflow)
        policy.id = uuid.uuid4()
        policy.notification_channels = ["email"]  # Doesn't affect push anymore
        policy.approval_type = "standard"

        # Mock email notification method
        with patch.object(
            approval_service,
            "_send_email_notification",
            new=AsyncMock(return_value={"success": True}),
        ):
            # Mock push notification method
            with patch.object(
                approval_service,
                "_send_push_notification",
                new=AsyncMock(return_value={"success": True, "sent": 0}),
            ) as mock_send_push:
                await approval_service.send_notifications(
                    sample_approval_request, policy
                )

                # Push is always attempted - filtering is internal based on user prefs
                mock_send_push.assert_called_once()

    async def test_send_push_notification_handles_exceptions(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        mock_apns_service,
    ):
        """Test that exceptions in push notification don't crash the service."""
        # Mock exception during send
        mock_apns_service.send_notification = AsyncMock(
            side_effect=Exception("Network error")
        )

        user_id_1, user_id_2 = sample_approval_workflow.approver_user_ids

        def mock_run_in_executor(executor, func):
            import asyncio

            future = asyncio.Future()
            # Two approvers, each with an iOS token (approvers, ios_tokens, android_tokens)
            future.set_result(
                (
                    [user_id_1, user_id_2],
                    [(user_id_1, "a" * 64), (user_id_2, "b" * 64)],
                    [],
                )
            )
            return future

        with patch(
            "preloop.services.push_notifications.get_apns_service",
            return_value=mock_apns_service,
        ):
            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = mock_run_in_executor
                mock_get_loop.return_value = mock_loop

                result = await approval_service._send_push_notification(
                    sample_approval_request, sample_approval_workflow
                )

                # Should handle exception gracefully
                assert result["failed"] == 2  # Both approvers failed
                assert result["sent"] == 0
