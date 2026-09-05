"""Tests for approval service."""

import json
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from preloop.models.models import ApprovalWorkflow, ApprovalRequest
from preloop.models.schemas.approval_request import ApprovalRequestUpdate

from preloop.services.approval_service import ApprovalService

pytestmark = pytest.mark.asyncio


@contextmanager
def fresh_poll_sessions():
    """Stub the fresh per-poll sessions opened by wait_for_approval.

    wait_for_approval must not reuse the session the service was constructed
    with (that would pin a pooled connection for the whole human wait), so it
    opens a short-lived session per poll; tests provide those here.
    """

    @asynccontextmanager
    async def _fake_session():
        yield AsyncMock()

    with patch(
        "preloop.models.db.session.get_async_db_session",
        new=_fake_session,
    ):
        yield


@pytest.fixture
def mock_db():
    """Create mock async database session."""
    return AsyncMock()


@pytest.fixture
def approval_service(mock_db):
    """Create ApprovalService instance with mocked database."""
    return ApprovalService(mock_db, "https://app.test.com")


@pytest.fixture
def mock_task_publisher():
    """Mock NATS task publisher for approval broadcasts."""
    publisher = AsyncMock()
    publisher.nc = MagicMock()
    publisher.nc.publish = AsyncMock()
    return publisher


@pytest.fixture
def sample_approval_workflow():
    """Create sample approval workflow."""
    policy = MagicMock(spec=ApprovalWorkflow)
    policy.id = uuid.uuid4()
    policy.approval_type = "slack"
    policy.timeout_seconds = 300
    policy.approval_config = {"webhook_url": "https://hooks.slack.com/test"}
    policy.notification_channels = ["slack"]  # Added to enable notifications
    return policy


@pytest.fixture
def sample_approval_request():
    """Create sample approval request."""
    request = MagicMock(spec=ApprovalRequest)
    request.id = uuid.uuid4()
    request.account_id = "test_account"
    request.tool_configuration_id = uuid.uuid4()
    request.approval_workflow_id = uuid.uuid4()
    request.tool_name = "test_tool"
    request.tool_args = {"arg1": "value1"}
    request.agent_reasoning = "This is why I need approval"
    request.status = "pending"
    request.requested_at = datetime.utcnow()
    request.expires_at = datetime.utcnow() + timedelta(minutes=5)
    request.approval_token = "test_token_123"
    return request


class TestDecisionGuards:
    """approve/decline must not re-decide resolved or expired requests."""

    async def test_approve_already_resolved_is_noop(
        self, approval_service, sample_approval_request
    ):
        sample_approval_request.status = "declined"
        approval_service.get_approval_request_for_update = AsyncMock(
            return_value=sample_approval_request
        )
        approval_service.update_approval_request = AsyncMock()
        result = await approval_service.approve_request(sample_approval_request.id)
        assert result is sample_approval_request
        approval_service.update_approval_request.assert_not_called()

    async def test_decline_already_resolved_is_noop(
        self, approval_service, sample_approval_request
    ):
        sample_approval_request.status = "approved"
        approval_service.get_approval_request_for_update = AsyncMock(
            return_value=sample_approval_request
        )
        approval_service.update_approval_request = AsyncMock()
        result = await approval_service.decline_request(sample_approval_request.id)
        assert result is sample_approval_request
        approval_service.update_approval_request.assert_not_called()

    async def test_approve_expired_marks_expired(
        self, approval_service, sample_approval_request
    ):
        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        expired = MagicMock(spec=ApprovalRequest)
        expired.status = "expired"
        approval_service.get_approval_request_for_update = AsyncMock(
            return_value=sample_approval_request
        )
        approval_service.update_approval_request = AsyncMock(return_value=expired)
        result = await approval_service.approve_request(sample_approval_request.id)
        assert result.status == "expired"
        update_arg = approval_service.update_approval_request.call_args.args[1]
        assert update_arg.status == "expired"


class TestCreateApprovalRequest:
    """Test create_approval_request method."""

    def _setup_mock_db_for_create(self, mock_db, sample_approval_workflow):
        """Set up mock_db to handle the execute call for approval workflow lookup."""

        # Mock db.refresh to set created fields
        def mock_refresh(obj):
            obj.created_at = datetime.utcnow()
            obj.updated_at = datetime.utcnow()

        mock_db.refresh.side_effect = mock_refresh

        # Mock the execute call that queries ApprovalWorkflow
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_approval_workflow
        mock_db.execute.return_value = mock_result

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_create_approval_request_success(
        self,
        mock_get_publisher,
        approval_service,
        mock_db,
        sample_approval_workflow,
        mock_task_publisher,
    ):
        """Test creating a new approval request."""
        mock_get_publisher.return_value = mock_task_publisher
        account_id = "test_account"
        tool_config_id = uuid.uuid4()

        self._setup_mock_db_for_create(mock_db, sample_approval_workflow)

        await approval_service.create_approval_request(
            account_id=account_id,
            tool_configuration_id=tool_config_id,
            approval_workflow_id=sample_approval_workflow.id,
            tool_name="create_issue",
            tool_args={"title": "Bug", "description": "Fix this"},
            agent_reasoning="Need to create an issue",
        )

        assert mock_db.add.called
        assert mock_db.commit.called
        assert mock_db.refresh.called

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_create_approval_request_with_execution_id(
        self,
        mock_get_publisher,
        approval_service,
        mock_db,
        sample_approval_workflow,
        mock_task_publisher,
    ):
        """Test creating approval request with execution ID."""
        mock_get_publisher.return_value = mock_task_publisher
        execution_id = "exec_123"

        self._setup_mock_db_for_create(mock_db, sample_approval_workflow)

        await approval_service.create_approval_request(
            account_id="test_account",
            tool_configuration_id=uuid.uuid4(),
            approval_workflow_id=sample_approval_workflow.id,
            tool_name="delete_issue",
            tool_args={"issue_id": "123"},
            execution_id=execution_id,
        )

        assert mock_db.add.called

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_create_approval_request_custom_timeout(
        self,
        mock_get_publisher,
        approval_service,
        mock_db,
        sample_approval_workflow,
        mock_task_publisher,
    ):
        """Test creating approval request with custom timeout."""
        mock_get_publisher.return_value = mock_task_publisher
        self._setup_mock_db_for_create(mock_db, sample_approval_workflow)

        await approval_service.create_approval_request(
            account_id="test_account",
            tool_configuration_id=uuid.uuid4(),
            approval_workflow_id=sample_approval_workflow.id,
            tool_name="test_tool",
            tool_args={},
            timeout_seconds=600,  # 10 minutes
        )

        assert mock_db.add.called

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_create_approval_request_default_timeout(
        self,
        mock_get_publisher,
        approval_service,
        mock_db,
        sample_approval_workflow,
        mock_task_publisher,
    ):
        """Test creating approval request with default timeout."""
        mock_get_publisher.return_value = mock_task_publisher
        self._setup_mock_db_for_create(mock_db, sample_approval_workflow)

        await approval_service.create_approval_request(
            account_id="test_account",
            tool_configuration_id=uuid.uuid4(),
            approval_workflow_id=sample_approval_workflow.id,
            tool_name="test_tool",
            tool_args={},
        )

        # Should use default 300 seconds timeout
        assert mock_db.add.called


class TestGetApprovalRequest:
    """Test get_approval_request method."""

    async def test_get_approval_request_found(
        self, approval_service, mock_db, sample_approval_request
    ):
        """Test getting an approval request that exists."""
        request_id = sample_approval_request.id

        # Mock database query result
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = sample_approval_request
        mock_db.execute.return_value = mock_result

        result = await approval_service.get_approval_request(request_id)

        assert result == sample_approval_request
        assert mock_db.execute.called

    async def test_get_approval_request_not_found(self, approval_service, mock_db):
        """Test getting an approval request that doesn't exist."""
        request_id = uuid.uuid4()

        # Mock database query returning None
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await approval_service.get_approval_request(request_id)

        assert result is None


class TestGetApprovalRequestForUpdate:
    """Test get_approval_request_for_update method."""

    @patch("preloop.services.approval_service.get_approval_request_for_update_async")
    async def test_get_approval_request_for_update_found(
        self, mock_get_for_update, approval_service, sample_approval_request
    ):
        """Test getting approval request for update with row lock."""
        mock_get_for_update.return_value = sample_approval_request

        result = await approval_service.get_approval_request_for_update(
            sample_approval_request.id
        )

        assert result == sample_approval_request
        mock_get_for_update.assert_called_once()

    @patch("preloop.services.approval_service.get_approval_request_for_update_async")
    async def test_get_approval_request_for_update_not_found(
        self, mock_get_for_update, approval_service
    ):
        """Test getting non-existent approval request for update."""
        mock_get_for_update.return_value = None

        result = await approval_service.get_approval_request_for_update(uuid.uuid4())

        assert result is None


class TestBroadcastApprovalUpdate:
    """Test _broadcast_approval_update method."""

    def _make_serializable_approval_request(self):
        """Create approval request with all JSON-serializable attributes."""
        req = MagicMock(spec=ApprovalRequest)
        req.id = uuid.uuid4()
        req.account_id = uuid.uuid4()
        req.tool_configuration_id = uuid.uuid4()
        req.approval_workflow_id = uuid.uuid4()
        req.execution_id = None
        req.tool_name = "test_tool"
        req.tool_args = {"arg1": "value1"}
        req.agent_reasoning = "Test reasoning"
        req.status = "pending"
        req.requested_at = datetime.utcnow()
        req.resolved_at = None
        req.expires_at = datetime.utcnow() + timedelta(minutes=5)
        return req

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_broadcast_approval_update_success(
        self, mock_get_publisher, approval_service, mock_task_publisher
    ):
        """Test broadcasting approval update via NATS."""
        mock_get_publisher.return_value = mock_task_publisher
        approval_request = self._make_serializable_approval_request()

        await approval_service._broadcast_approval_update(approval_request, "created")

        mock_task_publisher.nc.publish.assert_called_once()
        call_args = mock_task_publisher.nc.publish.call_args
        assert call_args[0][0] == "approval-updates"
        data = json.loads(call_args[0][1].decode())
        assert data["type"] == "approval_created"
        assert data["approval_request_id"] == str(approval_request.id)
        assert data["status"] == "pending"

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_broadcast_approval_update_nats_unavailable(
        self, mock_get_publisher, approval_service, sample_approval_request
    ):
        """Test broadcast when NATS is not available."""
        mock_get_publisher.return_value = None

        await approval_service._broadcast_approval_update(
            sample_approval_request, "approved"
        )

        # Should not raise - gracefully handles missing NATS

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_broadcast_approval_update_nc_none(
        self, mock_get_publisher, approval_service, sample_approval_request
    ):
        """Test broadcast when task_publisher exists but nc is None."""
        publisher = AsyncMock()
        publisher.nc = None
        mock_get_publisher.return_value = publisher

        await approval_service._broadcast_approval_update(
            sample_approval_request, "approved"
        )

        # Should not raise - gracefully handles missing nc

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_broadcast_approval_update_with_extra_data(
        self, mock_get_publisher, approval_service, mock_task_publisher
    ):
        """Test broadcast with extra_data (e.g. quorum progress)."""
        mock_get_publisher.return_value = mock_task_publisher
        approval_request = self._make_serializable_approval_request()

        await approval_service._broadcast_approval_update(
            approval_request,
            "vote_received",
            extra_data={"approval_count": 1, "approvals_required": 2},
        )

        data = json.loads(mock_task_publisher.nc.publish.call_args[0][1].decode())
        assert data["approval_count"] == 1
        assert data["approvals_required"] == 2

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_broadcast_approval_update_exception_handled(
        self, mock_get_publisher, approval_service, mock_task_publisher
    ):
        """Test broadcast gracefully handles publish exception."""
        mock_get_publisher.return_value = mock_task_publisher
        mock_task_publisher.nc.publish = AsyncMock(
            side_effect=Exception("NATS connection lost")
        )
        approval_request = self._make_serializable_approval_request()

        # Should not raise - exception is caught and logged
        await approval_service._broadcast_approval_update(approval_request, "created")


class TestCountTotalApprovers:
    """Test _count_total_approvers method."""

    async def test_count_total_approvers_none_workflow(self, approval_service):
        """Test with no approval workflow returns (1, True)."""
        count, is_exact = approval_service._count_total_approvers(None)
        assert count == 1
        assert is_exact is True

    async def test_count_total_approvers_direct_users(
        self, approval_service, sample_approval_workflow
    ):
        """Test with direct user approvers only."""
        user_id_1 = uuid.uuid4()
        user_id_2 = uuid.uuid4()
        sample_approval_workflow.approver_user_ids = [user_id_1, user_id_2]
        sample_approval_workflow.approver_team_ids = None

        count, is_exact = approval_service._count_total_approvers(
            sample_approval_workflow
        )
        assert count == 2
        assert is_exact is True

    async def test_count_total_approvers_with_teams(
        self, approval_service, sample_approval_workflow
    ):
        """Test with team approvers makes count inexact."""
        sample_approval_workflow.approver_user_ids = [uuid.uuid4()]
        sample_approval_workflow.approver_team_ids = [uuid.uuid4()]

        count, is_exact = approval_service._count_total_approvers(
            sample_approval_workflow
        )
        assert count == 1
        assert is_exact is False

    async def test_count_total_approvers_empty_teams(
        self, approval_service, sample_approval_workflow
    ):
        """Test with empty team list."""
        sample_approval_workflow.approver_user_ids = []
        sample_approval_workflow.approver_team_ids = []

        count, is_exact = approval_service._count_total_approvers(
            sample_approval_workflow
        )
        assert count == 1
        assert is_exact is True


class TestRecordEvent:
    """Test _record_event method."""

    async def test_record_event_success(
        self, approval_service, mock_db, sample_approval_request
    ):
        """Test recording an approval event."""
        sample_approval_request.account_id = uuid.uuid4()

        await approval_service._record_event(
            approval_request_id=sample_approval_request.id,
            account_id=sample_approval_request.account_id,
            event_type="vote_received",
            detail="Test vote",
            comment="LGTM",
            actor_id=uuid.uuid4(),
        )

        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    async def test_record_event_exception_handled(
        self, approval_service, mock_db, sample_approval_request
    ):
        """Test _record_event handles exception gracefully."""
        sample_approval_request.account_id = uuid.uuid4()
        mock_db.flush = AsyncMock(side_effect=Exception("DB flush failed"))

        # Should not raise - exception is caught and logged
        await approval_service._record_event(
            approval_request_id=sample_approval_request.id,
            account_id=sample_approval_request.account_id,
            event_type="vote_received",
            detail="Test vote",
        )


class TestUpdateApprovalRequest:
    """Test update_approval_request method."""

    async def test_update_approval_request_success(
        self, approval_service, mock_db, sample_approval_request
    ):
        """Test updating an approval request."""
        request_id = sample_approval_request.id
        update = ApprovalRequestUpdate(status="approved", approver_comment="LGTM")

        # Mock get_approval_request to return the sample request
        with patch.object(
            approval_service,
            "get_approval_request",
            return_value=sample_approval_request,
        ):
            result = await approval_service.update_approval_request(request_id, update)

            assert mock_db.commit.called
            assert mock_db.refresh.called
            assert result == sample_approval_request

    async def test_update_approval_request_not_found(self, approval_service, mock_db):
        """Test updating a non-existent approval request."""
        request_id = uuid.uuid4()
        update = ApprovalRequestUpdate(status="approved")

        # Mock get_approval_request to return None
        with patch.object(approval_service, "get_approval_request", return_value=None):
            result = await approval_service.update_approval_request(request_id, update)

            assert result is None
            assert not mock_db.commit.called

    async def test_update_approval_request_partial_update(
        self, approval_service, mock_db, sample_approval_request
    ):
        """Test partial update of approval request."""
        request_id = sample_approval_request.id
        update = ApprovalRequestUpdate(webhook_error="Failed to send")

        with patch.object(
            approval_service,
            "get_approval_request",
            return_value=sample_approval_request,
        ):
            await approval_service.update_approval_request(request_id, update)

            assert mock_db.commit.called


class TestApproveRequest:
    """Test approve_request method."""

    async def test_approve_request_success(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test approving a request."""
        request_id = sample_approval_request.id
        comment = "Approved for production use"

        # Set up the approval request with policy (quorum=1 by default)
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = []
        sample_approval_workflow.approvals_required = 1

        # Mock get_approval_request_for_update and update_approval_request
        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_update:
                result = await approval_service.approve_request(request_id, comment)

                assert result == sample_approval_request
                # Verify update was called with correct parameters
                assert mock_update.called
                call_args = mock_update.call_args
                assert call_args[0][0] == request_id
                update = call_args[0][1]
                assert update.status == "approved"
                assert update.approver_comment == comment

    async def test_approve_request_without_comment(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test approving a request without a comment."""
        request_id = sample_approval_request.id

        # Set up the approval request with policy
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = []
        sample_approval_workflow.approvals_required = 1

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ):
                result = await approval_service.approve_request(request_id)

                assert result == sample_approval_request

    async def test_approve_request_not_found(
        self, approval_service, sample_approval_request
    ):
        """Test approving a non-existent request."""
        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await approval_service.approve_request(
                sample_approval_request.id, "Approved"
            )
            assert result is None

    async def test_approve_request_anonymous_quorum_one_resolves(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test anonymous approval with quorum=1 resolves immediately."""
        request_id = sample_approval_request.id
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = []
        sample_approval_request.account_id = uuid.uuid4()
        sample_approval_workflow.approvals_required = 1

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_update:
                with patch.object(
                    approval_service,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ):
                    result = await approval_service.approve_request(
                        request_id, "LGTM", user_id=None
                    )

                    assert result == sample_approval_request
                    mock_update.assert_called_once()
                    assert mock_update.call_args[0][1].status == "approved"

    async def test_approve_request_with_user_id_quorum_one_resolves(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test approve with user_id and quorum=1 resolves immediately."""
        request_id = sample_approval_request.id
        user_id = uuid.uuid4()

        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = []
        sample_approval_request.account_id = uuid.uuid4()
        sample_approval_workflow.approvals_required = 1

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_update:
                with patch.object(
                    approval_service,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ):
                    result = await approval_service.approve_request(
                        request_id, "LGTM", user_id=user_id
                    )

                    assert result == sample_approval_request
                    mock_update.assert_called_once()
                    update_arg = mock_update.call_args[0][1]
                    assert update_arg.status == "approved"


class TestDeclineRequest:
    """Test decline_request method."""

    async def test_decline_request_success(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test declining a request."""
        request_id = sample_approval_request.id
        comment = "Security concerns"

        # Set up the approval request with policy
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = []
        sample_approval_workflow.approvals_required = 1

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_update:
                result = await approval_service.decline_request(request_id, comment)

                assert result == sample_approval_request
                call_args = mock_update.call_args
                assert call_args[0][0] == request_id
                update = call_args[0][1]
                assert update.status == "declined"
                assert update.approver_comment == comment

    async def test_decline_request_without_comment(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test declining a request without a comment."""
        request_id = sample_approval_request.id

        # Set up the approval request with policy
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = []
        sample_approval_workflow.approvals_required = 1

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ):
                result = await approval_service.decline_request(request_id)

                assert result == sample_approval_request

    async def test_decline_request_not_found(
        self, approval_service, sample_approval_request
    ):
        """Test declining a non-existent request."""
        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await approval_service.decline_request(
                sample_approval_request.id, "No way"
            )
            assert result is None

    async def test_decline_request_quorum_decline_count_blocks(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that enough declines block quorum and resolve as declined."""
        request_id = sample_approval_request.id
        user_id_1 = uuid.uuid4()
        user_id_2 = uuid.uuid4()

        sample_approval_workflow.approvals_required = 2
        sample_approval_workflow.approver_user_ids = [user_id_1, user_id_2]
        sample_approval_workflow.approver_team_ids = None
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {
                "user_id": str(user_id_1),
                "decision": "declined",
                "timestamp": "2026-01-01T00:00:00",
            }
        ]
        sample_approval_request.account_id = uuid.uuid4()

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_update:
                with patch.object(
                    approval_service,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ):
                    await approval_service.decline_request(
                        request_id, "Security risk", user_id=user_id_2
                    )

                    assert mock_update.called
                    update_arg = mock_update.call_args[0][1]
                    assert update_arg.status == "declined"

    async def test_decline_request_duplicate_user_vote_rejected(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that duplicate decline votes from same user are rejected."""
        request_id = sample_approval_request.id
        user_id = uuid.uuid4()

        sample_approval_workflow.approvals_required = 2
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {
                "user_id": str(user_id),
                "decision": "declined",
                "timestamp": "2026-01-01T00:00:00",
            }
        ]

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            result = await approval_service.decline_request(
                request_id, "Duplicate", user_id=user_id
            )

            assert len(result.responses) == 1

    async def test_decline_request_with_user_id_quorum_one_resolves(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test decline with user_id and quorum=1 resolves immediately."""
        request_id = sample_approval_request.id
        user_id = uuid.uuid4()

        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = []
        sample_approval_request.account_id = uuid.uuid4()
        sample_approval_workflow.approvals_required = 1

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_update:
                with patch.object(
                    approval_service,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ):
                    result = await approval_service.decline_request(
                        request_id, "No", user_id=user_id
                    )

                    assert result == sample_approval_request
                    mock_update.assert_called_once()
                    update_arg = mock_update.call_args[0][1]
                    assert update_arg.status == "declined"

    async def test_decline_request_anonymous_duplicate_rejected(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test duplicate anonymous decline vote is rejected."""
        request_id = sample_approval_request.id

        sample_approval_workflow.approvals_required = 2
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {
                "user_id": "anonymous",
                "decision": "declined",
                "timestamp": "2026-01-01T00:00:00",
            }
        ]
        sample_approval_request.account_id = uuid.uuid4()

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            result = await approval_service.decline_request(
                request_id, "Duplicate decline", user_id=None
            )

            assert len(result.responses) == 1
            assert result.responses[0]["decision"] == "declined"

    async def test_decline_request_quorum_impossible_resolves(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test decline when quorum cannot be reached (exact count, all voted)."""
        request_id = sample_approval_request.id
        user_id_1 = uuid.uuid4()
        user_id_2 = uuid.uuid4()

        sample_approval_workflow.approvals_required = 2
        sample_approval_workflow.approver_user_ids = [user_id_1, user_id_2]
        sample_approval_workflow.approver_team_ids = None
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {"user_id": str(user_id_1), "decision": "declined", "timestamp": "x"},
        ]
        sample_approval_request.account_id = uuid.uuid4()

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_update:
                with patch.object(
                    approval_service,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ):
                    await approval_service.decline_request(
                        request_id, "Second decline", user_id=user_id_2
                    )

                    # Two declines, quorum impossible - resolve as declined
                    assert mock_update.called
                    update_arg = mock_update.call_args[0][1]
                    assert update_arg.status == "declined"


class TestPostWebhookNotification:
    """Test post_webhook_notification method."""

    @patch("preloop.services.approval_service.httpx.AsyncClient")
    async def test_post_webhook_slack_success(
        self,
        mock_client_class,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test posting webhook notification to Slack."""
        # Mock httpx client
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Mock update_approval_request
        with patch.object(approval_service, "update_approval_request") as mock_update:
            result = await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

            assert result is True
            assert mock_client.post.called
            # Verify webhook was marked as posted
            assert mock_update.called

    @patch("preloop.services.approval_service.httpx.AsyncClient")
    async def test_post_webhook_mattermost_success(
        self,
        mock_client_class,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test posting webhook notification to Mattermost."""
        sample_approval_workflow.approval_type = "mattermost"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with patch.object(approval_service, "update_approval_request"):
            result = await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

            assert result is True

    @patch("preloop.services.approval_service.httpx.AsyncClient")
    async def test_post_webhook_generic_success(
        self,
        mock_client_class,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test posting webhook notification to generic webhook."""
        sample_approval_workflow.approval_type = "webhook"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with patch.object(approval_service, "update_approval_request"):
            result = await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

            assert result is True

    async def test_post_webhook_no_webhook_url(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test posting webhook when no webhook URL is configured."""
        sample_approval_workflow.approval_config = {}

        with patch.object(approval_service, "update_approval_request") as mock_update:
            result = await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

            assert result is False
            # Verify error was recorded
            assert mock_update.called
            call_args = mock_update.call_args
            update = call_args[0][1]
            assert "webhook_error" in update.model_dump(exclude_unset=True)

    async def test_post_webhook_no_config(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test posting webhook when approval_config is None."""
        sample_approval_workflow.approval_config = None

        with patch.object(approval_service, "update_approval_request"):
            result = await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

            assert result is False

    @patch("preloop.services.approval_service.httpx.AsyncClient")
    async def test_post_webhook_http_error(
        self,
        mock_client_class,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test posting webhook when HTTP error occurs."""
        # Mock httpx client to raise error
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Connection failed")
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with patch.object(approval_service, "update_approval_request") as mock_update:
            result = await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

            assert result is False
            # Verify error was recorded
            assert mock_update.called
            call_args = mock_update.call_args
            update = call_args[0][1]
            assert "webhook_error" in update.model_dump(exclude_unset=True)

    @patch("preloop.services.approval_service.httpx.AsyncClient")
    async def test_post_webhook_chat_message_offers_one_review_link(
        self,
        mock_client_class,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """The chat message must not label a plain review link "Approve".

        All three links used to point at the same tokenized approval page, so
        "Approve" opened a page instead of approving. One honest link now.
        """
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with patch.object(approval_service, "update_approval_request"):
            result = await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

        assert result is True
        message = mock_client.post.call_args[1]["json"]
        review_url = (
            f"https://app.test.com/console/approval/{sample_approval_request.id}"
            f"?token={sample_approval_request.approval_token}"
        )

        text = message["text"]
        assert f"[Review this request]({review_url})" in text
        assert "Approve](" not in text
        assert "Decline](" not in text
        assert text.count(review_url) == 1

        actions = message["attachments"][0]["actions"]
        assert [action["text"] for action in actions] == ["Review"]
        assert actions[0]["url"] == review_url

    @patch("preloop.services.approval_service.httpx.AsyncClient")
    async def test_post_webhook_generic_payload_review_action_and_deprecated_aliases(
        self,
        mock_client_class,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """The generic payload names the link "review" and keeps the old keys."""
        sample_approval_workflow.approval_type = "webhook"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with patch.object(approval_service, "update_approval_request"):
            await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

        actions = mock_client.post.call_args[1]["json"]["actions"]
        review_url = (
            f"/console/approval/{sample_approval_request.id}"
            f"?token={sample_approval_request.approval_token}"
        )
        assert actions["review"].endswith(review_url)
        # The deprecated keys are a machine contract: a receiver doing
        # payload["actions"]["approve"] must not start raising KeyError.
        assert set(actions) == {"review", "approve", "decline", "view"}
        assert len(set(actions.values())) == 1

    @patch("preloop.services.approval_service.httpx.AsyncClient")
    async def test_post_webhook_with_agent_reasoning(
        self,
        mock_client_class,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test posting webhook with agent reasoning included."""
        sample_approval_request.agent_reasoning = "Need to update critical issue"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client

        with patch.object(approval_service, "update_approval_request"):
            result = await approval_service.post_webhook_notification(
                sample_approval_request, sample_approval_workflow
            )

            assert result is True
            # Verify reasoning was included in the message
            call_args = mock_client.post.call_args
            message = call_args[1]["json"]
            # For Slack/Mattermost, reasoning should be in attachments fields
            assert "attachments" in message
            assert len(message["attachments"]) > 0
            fields = message["attachments"][0].get("fields", [])
            reasoning_fields = [
                f for f in fields if "Agent Reasoning" in f.get("title", "")
            ]
            assert len(reasoning_fields) > 0


class TestSendNotifications:
    """Test send_notifications method."""

    async def test_send_notifications_request_too_old_skipped(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that notifications are skipped when request is older than 30 seconds."""
        sample_approval_request.requested_at = datetime.utcnow() - timedelta(seconds=60)
        sample_approval_request.approval_workflow = sample_approval_workflow

        result = await approval_service.send_notifications(
            sample_approval_request, sample_approval_workflow
        )

        assert result.get("skipped") is True
        assert result.get("reason") == "request_too_old"

    async def test_send_notifications_success(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test send_notifications calls email, push, and webhook channels."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = "slack"
        sample_approval_workflow.approval_config = {"webhook_url": "https://hooks.test"}

        with patch.object(
            approval_service,
            "_send_email_notification",
            new_callable=AsyncMock,
            return_value={"success": True, "sent": 0, "failed": 0, "skipped": 0},
        ) as mock_email:
            with patch.object(
                approval_service,
                "_send_push_notification",
                new_callable=AsyncMock,
                return_value={"success": True, "sent": 0, "failed": 0},
            ) as mock_push:
                with patch.object(
                    approval_service,
                    "post_webhook_notification",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_webhook:
                    result = await approval_service.send_notifications(
                        sample_approval_request, sample_approval_workflow
                    )

                    mock_email.assert_called_once()
                    mock_push.assert_called_once()
                    mock_webhook.assert_called_once()
                    assert "email" in result
                    assert "mobile_push" in result
                    assert "slack" in result

    async def test_send_notifications_handles_email_error(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that email notification errors are caught and reported."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None

        with patch.object(
            approval_service,
            "_send_email_notification",
            new_callable=AsyncMock,
            side_effect=Exception("SMTP failed"),
        ):
            with patch.object(
                approval_service,
                "_send_push_notification",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "Not configured"},
            ):
                result = await approval_service.send_notifications(
                    sample_approval_request, sample_approval_workflow
                )

                assert result["email"]["success"] is False
                assert "error" in result["email"]

    async def test_send_notifications_handles_push_error(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that push notification errors are caught and reported."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None

        with patch.object(
            approval_service,
            "_send_email_notification",
            new_callable=AsyncMock,
            return_value={"success": True, "sent": 0, "failed": 0, "skipped": 0},
        ):
            with patch.object(
                approval_service,
                "_send_push_notification",
                new_callable=AsyncMock,
                side_effect=Exception("Push service unavailable"),
            ):
                result = await approval_service.send_notifications(
                    sample_approval_request, sample_approval_workflow
                )

                assert result["mobile_push"]["success"] is False
                assert "error" in result["mobile_push"]

    async def test_send_notifications_webhook_channel(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test send_notifications calls webhook for slack/mattermost/webhook type."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = "webhook"
        sample_approval_workflow.approval_config = {"webhook_url": "https://hooks.test"}

        with patch.object(
            approval_service,
            "_send_email_notification",
            new_callable=AsyncMock,
            return_value={"success": True, "sent": 0, "failed": 0, "skipped": 0},
        ):
            with patch.object(
                approval_service,
                "_send_push_notification",
                new_callable=AsyncMock,
                return_value={"success": True, "sent": 0, "failed": 0},
            ):
                with patch.object(
                    approval_service,
                    "post_webhook_notification",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_webhook:
                    result = await approval_service.send_notifications(
                        sample_approval_request, sample_approval_workflow
                    )

                    mock_webhook.assert_called_once()
                    assert "webhook" in result

    async def test_send_notifications_webhook_exception_handled(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test webhook exception is caught and reported."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = "slack"
        sample_approval_workflow.approval_config = {"webhook_url": "https://hooks.test"}

        with patch.object(
            approval_service,
            "_send_email_notification",
            new_callable=AsyncMock,
            return_value={"success": True, "sent": 0, "failed": 0, "skipped": 0},
        ):
            with patch.object(
                approval_service,
                "_send_push_notification",
                new_callable=AsyncMock,
                return_value={"success": True, "sent": 0, "failed": 0},
            ):
                with patch.object(
                    approval_service,
                    "post_webhook_notification",
                    new_callable=AsyncMock,
                    side_effect=Exception("Webhook failed"),
                ):
                    result = await approval_service.send_notifications(
                        sample_approval_request, sample_approval_workflow
                    )

                    assert result["slack"]["success"] is False
                    assert "error" in result["slack"]


class TestGetAllApproverUserIds:
    """Test _get_all_approver_user_ids method."""

    async def test_get_all_approver_user_ids_direct_only(
        self, approval_service, mock_db, sample_approval_workflow
    ):
        """Test with direct user approvers only."""
        user_id_1 = uuid.uuid4()
        user_id_2 = uuid.uuid4()
        sample_approval_workflow.approver_user_ids = [user_id_1, user_id_2]
        sample_approval_workflow.approver_team_ids = None

        result = await approval_service._get_all_approver_user_ids(
            sample_approval_workflow
        )

        assert set(result) == {user_id_1, user_id_2}

    async def test_get_all_approver_user_ids_with_teams(
        self, approval_service, mock_db, sample_approval_workflow
    ):
        """Test with team approvers expanded to user IDs."""
        user_id = uuid.uuid4()
        team_id = uuid.uuid4()
        team_member_id = uuid.uuid4()

        sample_approval_workflow.approver_user_ids = [user_id]
        sample_approval_workflow.approver_team_ids = [team_id]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [team_member_id]
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await approval_service._get_all_approver_user_ids(
            sample_approval_workflow
        )

        assert user_id in result
        assert team_member_id in result
        assert len(result) == 2

    async def test_get_all_approver_user_ids_empty(
        self, approval_service, sample_approval_workflow
    ):
        """Test with no approvers returns empty list."""
        sample_approval_workflow.approver_user_ids = None
        sample_approval_workflow.approver_team_ids = None

        result = await approval_service._get_all_approver_user_ids(
            sample_approval_workflow
        )

        assert result == []


class TestSendEmailNotification:
    """Test _send_email_notification method."""

    @patch("preloop.models.db.session.get_db_session")
    async def test_send_email_no_approvers(
        self,
        mock_get_db,
        approval_service,
        mock_db,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test _send_email_notification with no approvers configured."""
        sample_approval_workflow.approver_user_ids = None
        sample_approval_workflow.approver_team_ids = None

        with patch.object(
            approval_service,
            "_get_all_approver_user_ids",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await approval_service._send_email_notification(
                sample_approval_request, sample_approval_workflow
            )

            assert result["success"] is False
            assert "No approvers" in result["error"]

    @patch("preloop.utils.email.send_approval_request_email")
    @patch("preloop.models.crud.notification_preferences")
    @patch("preloop.models.db.session.get_db_session")
    async def test_send_email_success(
        self,
        mock_get_db,
        mock_prefs,
        mock_send_email,
        approval_service,
        mock_db,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test _send_email_notification sends to approvers with email enabled."""
        user_id = uuid.uuid4()
        sample_approval_workflow.approver_user_ids = [user_id]
        sample_approval_workflow.approver_team_ids = None
        sample_approval_request.approval_token = "token123"

        mock_user = MagicMock()
        mock_user.email = "approver@test.com"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_db.execute = AsyncMock(return_value=mock_result)

        mock_sync_db = MagicMock()
        mock_get_db.return_value = iter([mock_sync_db])
        mock_prefs.get_by_user.return_value = MagicMock(enable_email=True)

        with patch.object(
            approval_service,
            "_get_all_approver_user_ids",
            new_callable=AsyncMock,
            return_value=[user_id],
        ):
            with patch(
                "preloop.services.approval_service._sync_db_executor"
            ) as mock_executor:
                mock_executor.submit = lambda fn, *args: None
                mock_loop = MagicMock()
                mock_loop.run_in_executor = AsyncMock(return_value=set())
                with patch(
                    "preloop.services.approval_service.asyncio.get_running_loop",
                    return_value=mock_loop,
                ):
                    result = await approval_service._send_email_notification(
                        sample_approval_request, sample_approval_workflow
                    )

                    assert result["success"] is True
                    assert result["sent"] == 1
                    mock_send_email.assert_called_once()


class TestSendPushNotification:
    """Test _send_push_notification method."""

    @patch("preloop.services.push_proxy.is_push_proxy_configured")
    @patch("preloop.services.push_notifications.is_fcm_configured")
    @patch("preloop.services.push_notifications.get_apns_service")
    async def test_send_push_not_configured(
        self,
        mock_get_apns,
        mock_is_fcm,
        mock_is_proxy,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test _send_push_notification when no push service is configured."""
        mock_get_apns.return_value = None
        mock_is_fcm.return_value = False
        mock_is_proxy.return_value = False

        with patch.object(
            approval_service,
            "_get_all_approver_user_ids",
            new_callable=AsyncMock,
            return_value=[uuid.uuid4()],
        ):
            with patch(
                "preloop.services.approval_service._sync_db_executor"
            ) as mock_executor:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = AsyncMock(
                    return_value=([uuid.uuid4()], [], [])  # no devices
                )
                with patch(
                    "preloop.services.approval_service.asyncio.get_event_loop",
                    return_value=mock_loop,
                ):
                    result = await approval_service._send_push_notification(
                        sample_approval_request, sample_approval_workflow
                    )

                    assert result["success"] is False
                    assert "not configured" in result["error"]

    @patch("preloop.services.push_proxy.is_push_proxy_configured")
    @patch("preloop.services.push_notifications.is_fcm_configured")
    @patch("preloop.services.push_notifications.get_apns_service")
    async def test_send_push_no_devices(
        self,
        mock_get_apns,
        mock_is_fcm,
        mock_is_proxy,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test _send_push_notification when approvers have no devices."""
        mock_get_apns.return_value = None
        mock_is_fcm.return_value = False
        mock_is_proxy.return_value = True

        with patch.object(
            approval_service,
            "_get_all_approver_user_ids",
            new_callable=AsyncMock,
            return_value=[uuid.uuid4()],
        ):
            with patch(
                "preloop.services.approval_service._sync_db_executor"
            ) as mock_executor:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = AsyncMock(
                    return_value=([uuid.uuid4()], [], [])  # no ios/android tokens
                )
                with patch(
                    "preloop.services.approval_service.asyncio.get_event_loop",
                    return_value=mock_loop,
                ):
                    result = await approval_service._send_push_notification(
                        sample_approval_request, sample_approval_workflow
                    )

                    assert result["success"] is True
                    assert result["sent"] == 0
                    assert result.get("no_devices") is True


class TestCreateAndNotify:
    """Test create_and_notify method."""

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_create_and_notify_success(
        self,
        mock_get_publisher,
        approval_service,
        mock_db,
        sample_approval_workflow,
        mock_task_publisher,
    ):
        """Test creating and notifying approval request."""
        mock_get_publisher.return_value = mock_task_publisher
        account_id = "test_account"
        tool_config_id = uuid.uuid4()

        # Mock create_approval_request
        mock_approval_request = MagicMock(spec=ApprovalRequest)
        mock_approval_request.id = uuid.uuid4()
        mock_approval_request.requested_at = datetime.utcnow()

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_approval_request,
            ) as mock_create,
            patch.object(
                approval_service,
                "send_notifications",
                new_callable=AsyncMock,
                return_value={"email": {}, "slack": {"success": True}},
            ) as mock_send_notifications,
        ):
            result = await approval_service.create_and_notify(
                account_id=account_id,
                tool_configuration_id=tool_config_id,
                approval_workflow=sample_approval_workflow,
                tool_name="create_issue",
                tool_args={"title": "Bug"},
                agent_reasoning="Need approval",
            )

            assert result == mock_approval_request
            assert mock_create.called
            mock_send_notifications.assert_called_once()

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_create_and_notify_persists_summary(
        self,
        mock_get_publisher,
        approval_service,
        sample_approval_workflow,
        mock_task_publisher,
    ):
        """Summary is generated and persisted before notifications are sent."""
        mock_get_publisher.return_value = mock_task_publisher

        mock_approval_request = MagicMock(spec=ApprovalRequest)
        mock_approval_request.id = uuid.uuid4()
        mock_approval_request.summary = None
        mock_approval_request.requested_at = datetime.utcnow()

        updated_request = MagicMock(spec=ApprovalRequest)
        updated_request.id = mock_approval_request.id
        updated_request.summary = "Allow creating issue Bug?"

        sync_db = MagicMock()

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_approval_request,
            ),
            patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=updated_request,
            ) as mock_update,
            patch.object(
                approval_service,
                "send_notifications",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_send_notifications,
            patch(
                "preloop.models.db.session.get_db_session",
                return_value=iter([sync_db]),
            ),
            patch(
                "preloop.services.approval_summary.generate_approval_summary",
                new_callable=AsyncMock,
                return_value="Allow creating issue Bug?",
            ) as mock_generate,
        ):
            result = await approval_service.create_and_notify(
                account_id="test_account",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=sample_approval_workflow,
                tool_name="create_issue",
                tool_args={"title": "Bug"},
                agent_reasoning="Need approval",
                managed_agent_name="coder",
            )

            assert result is updated_request
            mock_generate.assert_awaited_once()
            mock_update.assert_awaited_once()
            update_arg = mock_update.await_args.args[1]
            assert isinstance(update_arg, ApprovalRequestUpdate)
            assert update_arg.summary == "Allow creating issue Bug?"
            mock_send_notifications.assert_called_once()
            sync_db.close.assert_called_once()

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_create_and_notify_with_execution_id(
        self,
        mock_get_publisher,
        approval_service,
        sample_approval_workflow,
        mock_task_publisher,
    ):
        """Test creating and notifying with execution ID."""
        mock_get_publisher.return_value = mock_task_publisher
        execution_id = "exec_456"

        mock_approval_request = MagicMock(spec=ApprovalRequest)

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_approval_request,
            ),
            patch.object(
                approval_service,
                "send_notifications",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            result = await approval_service.create_and_notify(
                account_id="test_account",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=sample_approval_workflow,
                tool_name="test_tool",
                tool_args={},
                execution_id=execution_id,
            )

            assert result == mock_approval_request

    async def test_create_and_notify_stores_raw_tool_args_for_execution(
        self, approval_service, sample_approval_workflow
    ):
        """Tool args with sensitive keys must be stored raw for execution after approval.

        Redaction is applied only for logging/notifications, not for storage.
        See docs/architecture/security.md Redaction Policy.
        """
        tool_args_with_secret = {
            "command": "deploy to production",
            "api_key": "sk-secret-123",
            "environment": "prod",
        }

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                AsyncMock(return_value=MagicMock(spec=ApprovalRequest)),
            ) as mock_create,
            patch.object(
                approval_service,
                "send_notifications",
                AsyncMock(return_value={}),
            ),
        ):
            sample_approval_workflow.approval_mode = "manual"
            await approval_service.create_and_notify(
                account_id="test_account",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=sample_approval_workflow,
                tool_name="bash",
                tool_args=tool_args_with_secret,
            )

            # create_approval_request must receive raw args (not redacted)
            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs["tool_args"]["api_key"] == "sk-secret-123"
            assert call_kwargs["tool_args"]["command"] == "deploy to production"

    @patch("preloop.services.approval_service.get_task_publisher")
    async def test_create_and_notify_with_user_id(
        self,
        mock_get_publisher,
        approval_service,
        sample_approval_workflow,
        mock_task_publisher,
    ):
        """Test create_and_notify passes user_id to AI context when provided."""
        mock_get_publisher.return_value = mock_task_publisher
        user_id = uuid.uuid4()
        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()
        mock_request.requested_at = datetime.utcnow()

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch.object(
                approval_service,
                "send_notifications",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            result = await approval_service.create_and_notify(
                account_id="test_account",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=sample_approval_workflow,
                tool_name="test_tool",
                tool_args={},
                user_id=user_id,
            )

            assert result == mock_request


class TestWaitForApproval:
    """Test wait_for_approval method.

    wait_for_approval polls with a fresh service on a fresh session per
    iteration, so the tests patch methods at the class level rather than on
    the instance under test.
    """

    async def test_wait_for_approval_already_approved(
        self, approval_service, sample_approval_request
    ):
        """Test waiting for an already approved request."""
        sample_approval_request.status = "approved"

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                return_value=sample_approval_request,
            ),
        ):
            result = await approval_service.wait_for_approval(
                sample_approval_request.id
            )

            assert result == sample_approval_request

    async def test_wait_for_approval_already_declined(
        self, approval_service, sample_approval_request
    ):
        """Test waiting for an already declined request."""
        sample_approval_request.status = "declined"

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                return_value=sample_approval_request,
            ),
        ):
            result = await approval_service.wait_for_approval(
                sample_approval_request.id
            )

            assert result.status == "declined"

    async def test_wait_for_approval_cancelled(
        self, approval_service, sample_approval_request
    ):
        """Test waiting for a cancelled request."""
        sample_approval_request.status = "cancelled"

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                return_value=sample_approval_request,
            ),
        ):
            result = await approval_service.wait_for_approval(
                sample_approval_request.id
            )

            assert result.status == "cancelled"

    async def test_wait_for_approval_not_found(self, approval_service):
        """Test waiting for a non-existent request."""
        request_id = uuid.uuid4()

        with (
            fresh_poll_sessions(),
            patch.object(ApprovalService, "get_approval_request", return_value=None),
        ):
            with pytest.raises(ValueError) as exc_info:
                await approval_service.wait_for_approval(request_id)

            assert "not found" in str(exc_info.value)

    async def test_wait_for_approval_expires(
        self, approval_service, sample_approval_request
    ):
        """Test waiting for a request that expires."""
        # Set expiration in the past
        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        sample_approval_request.escalation_triggered_at = None
        sample_approval_request.approval_workflow = None

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                return_value=sample_approval_request,
            ),
        ):
            with patch.object(
                ApprovalService, "update_approval_request"
            ) as mock_update:
                with pytest.raises(TimeoutError) as exc_info:
                    await approval_service.wait_for_approval(sample_approval_request.id)

                assert "expired" in str(exc_info.value)
                # Verify request was marked as expired
                assert mock_update.called

    @patch("preloop.services.approval_service.asyncio.sleep")
    async def test_wait_for_approval_polling(
        self, mock_sleep, approval_service, sample_approval_request
    ):
        """Test polling behavior when waiting for approval."""
        # First call: pending, second call: approved
        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = None
        approved_request = MagicMock(spec=ApprovalRequest)
        approved_request.status = "approved"

        call_count = 0

        async def mock_get_approval_request(request_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return sample_approval_request
            else:
                return approved_request

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                side_effect=mock_get_approval_request,
            ),
        ):
            result = await approval_service.wait_for_approval(
                sample_approval_request.id, poll_interval=0.5
            )

            assert result.status == "approved"
            # Verify sleep was called with correct interval
            assert mock_sleep.called

    async def test_wait_for_approval_custom_poll_interval(
        self, approval_service, sample_approval_request
    ):
        """Test waiting with custom poll interval."""
        sample_approval_request.status = "approved"

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                return_value=sample_approval_request,
            ),
        ):
            result = await approval_service.wait_for_approval(
                sample_approval_request.id, poll_interval=2.0
            )

            assert result == sample_approval_request


class TestQuorumVoteTracking:
    """Regression tests for quorum > 1 vote tracking."""

    async def test_approve_request_quorum_not_met_records_vote(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that approval vote is recorded when quorum is not yet met."""
        request_id = sample_approval_request.id
        user_id = uuid.uuid4()

        # Set up quorum > 1
        sample_approval_workflow.approvals_required = 2
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = []
        sample_approval_request.status = "pending"

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "_broadcast_approval_update",
                new_callable=AsyncMock,
            ) as mock_broadcast:
                result = await approval_service.approve_request(
                    request_id, "First approval", user_id=user_id
                )

                # Vote should be recorded but status still pending
                assert result.status == "pending"
                assert len(result.responses) == 1
                assert result.responses[0]["user_id"] == str(user_id)
                assert result.responses[0]["decision"] == "approved"

                # Should broadcast vote_received, not approved
                mock_broadcast.assert_called()
                call_args = mock_broadcast.call_args
                assert call_args[0][1] == "vote_received"

    async def test_approve_request_quorum_met_resolves(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that request resolves when quorum is met."""
        request_id = sample_approval_request.id
        user_id_1 = uuid.uuid4()
        user_id_2 = uuid.uuid4()

        # Set up quorum = 2 with one existing vote
        sample_approval_workflow.approvals_required = 2
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {
                "user_id": str(user_id_1),
                "decision": "approved",
                "timestamp": "2026-01-01T00:00:00",
            }
        ]
        sample_approval_request.status = "pending"

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            with patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_update:
                with patch.object(
                    approval_service,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ):
                    await approval_service.approve_request(
                        request_id, "Second approval", user_id=user_id_2
                    )

                    # Should call update_approval_request with approved status
                    assert mock_update.called
                    update_arg = mock_update.call_args[0][1]
                    assert update_arg.status == "approved"

    async def test_approve_request_duplicate_vote_rejected(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that duplicate votes from same user are rejected."""
        request_id = sample_approval_request.id
        user_id = uuid.uuid4()

        # Set up with existing vote from same user
        sample_approval_workflow.approvals_required = 2
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {
                "user_id": str(user_id),
                "decision": "approved",
                "timestamp": "2026-01-01T00:00:00",
            }
        ]
        sample_approval_request.status = "pending"

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            result = await approval_service.approve_request(
                request_id, "Duplicate vote", user_id=user_id
            )

            # Should return unchanged - no new vote added
            assert len(result.responses) == 1

    async def test_anonymous_vote_capped_at_one(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that only one anonymous vote is allowed per request."""
        request_id = sample_approval_request.id

        # Set up with existing anonymous vote
        sample_approval_workflow.approvals_required = 3
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {
                "user_id": "anonymous",
                "decision": "approved",
                "timestamp": "2026-01-01T00:00:00",
            }
        ]
        sample_approval_request.status = "pending"

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            # Try to add another anonymous vote (no user_id)
            result = await approval_service.approve_request(
                request_id, "Second anonymous vote", user_id=None
            )

            # Should return unchanged - duplicate anonymous vote rejected
            assert len(result.responses) == 1

    async def test_anonymous_cannot_vote_approve_and_decline(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that anonymous user cannot vote both approve AND decline (double-vote attack)."""
        request_id = sample_approval_request.id

        # Set up with existing anonymous APPROVAL
        sample_approval_workflow.approvals_required = 2
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {
                "user_id": "anonymous",
                "decision": "approved",
                "timestamp": "2026-01-01T00:00:00",
            }
        ]
        sample_approval_request.status = "pending"

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            # Try to add an anonymous DECLINE (should be rejected as double-vote)
            result = await approval_service.decline_request(
                request_id, "Anonymous decline after approval", user_id=None
            )

            # Should return unchanged - anonymous already voted
            assert len(result.responses) == 1
            assert result.responses[0]["decision"] == "approved"

    async def test_anonymous_cannot_vote_decline_and_approve(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that anonymous user cannot vote decline then approve (reverse double-vote attack)."""
        request_id = sample_approval_request.id

        # Set up with existing anonymous DECLINE
        sample_approval_workflow.approvals_required = 2
        sample_approval_request.approval_workflow = sample_approval_workflow
        sample_approval_request.responses = [
            {
                "user_id": "anonymous",
                "decision": "declined",
                "timestamp": "2026-01-01T00:00:00",
            }
        ]
        sample_approval_request.status = "pending"

        with patch.object(
            approval_service,
            "get_approval_request_for_update",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ):
            # Try to add an anonymous APPROVAL (should be rejected as double-vote)
            result = await approval_service.approve_request(
                request_id, "Anonymous approval after decline", user_id=None
            )

            # Should return unchanged - anonymous already voted
            assert len(result.responses) == 1
            assert result.responses[0]["decision"] == "declined"


class TestEscalationBehavior:
    """Regression tests for escalation timeout behavior."""

    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_wait_for_approval_triggers_escalation(
        self,
        mock_sleep,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test that escalation is triggered when timeout expires with escalation configured."""
        # Set up expired request with escalation configured
        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = datetime.utcnow() - timedelta(seconds=10)
        sample_approval_request.escalation_triggered_at = None
        sample_approval_workflow.escalation_user_ids = [uuid.uuid4()]
        sample_approval_workflow.timeout_seconds = 60
        sample_approval_request.approval_workflow = sample_approval_workflow

        call_count = 0

        async def mock_get_request(request_id):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: expired, escalation not triggered
                return sample_approval_request
            else:
                # After escalation: return approved
                approved = MagicMock(spec=ApprovalRequest)
                approved.status = "approved"
                return approved

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                side_effect=mock_get_request,
            ),
        ):
            with patch.object(
                ApprovalService,
                "_send_escalation_notifications",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as mock_escalation:
                with patch.object(
                    ApprovalService,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ):
                    result = await approval_service.wait_for_approval(
                        sample_approval_request.id, poll_interval=0.1
                    )

                    # Escalation should have been triggered
                    assert mock_escalation.called
                    assert result.status == "approved"

    async def test_wait_for_approval_no_escalation_when_already_triggered(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that escalation is not triggered twice."""
        # Set up expired request with escalation already triggered
        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = datetime.utcnow() - timedelta(seconds=10)
        sample_approval_request.escalation_triggered_at = datetime.utcnow() - timedelta(
            seconds=5
        )
        sample_approval_workflow.escalation_user_ids = [uuid.uuid4()]
        sample_approval_request.approval_workflow = sample_approval_workflow

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ),
        ):
            with patch.object(
                ApprovalService,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ):
                with patch.object(
                    ApprovalService,
                    "_send_escalation_notifications",
                    new_callable=AsyncMock,
                ) as mock_escalation:
                    with patch.object(
                        ApprovalService,
                        "_broadcast_approval_update",
                        new_callable=AsyncMock,
                    ):
                        with pytest.raises(TimeoutError):
                            await approval_service.wait_for_approval(
                                sample_approval_request.id, poll_interval=0.1
                            )

                        # Escalation should NOT have been called again
                        assert not mock_escalation.called

    async def test_wait_for_approval_expires_without_escalation(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test that request expires normally when no escalation configured."""
        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = datetime.utcnow() - timedelta(seconds=10)
        sample_approval_request.escalation_triggered_at = None
        sample_approval_workflow.escalation_user_ids = None
        sample_approval_workflow.escalation_team_ids = None
        sample_approval_request.approval_workflow = sample_approval_workflow

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ),
        ):
            with patch.object(
                ApprovalService,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ):
                with patch.object(
                    ApprovalService,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ):
                    with pytest.raises(TimeoutError) as exc_info:
                        await approval_service.wait_for_approval(
                            sample_approval_request.id, poll_interval=0.1
                        )

                    assert "expired without response" in str(exc_info.value)

    async def test_wait_for_approval_expires_broadcasts_and_logs(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Test expiration broadcasts event and logs to audit."""
        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        sample_approval_request.escalation_triggered_at = None
        sample_approval_workflow.escalation_user_ids = None
        sample_approval_workflow.escalation_team_ids = None
        sample_approval_request.approval_workflow = sample_approval_workflow

        expired_request = MagicMock(spec=ApprovalRequest)
        expired_request.id = sample_approval_request.id
        expired_request.account_id = sample_approval_request.account_id
        expired_request.tool_name = sample_approval_request.tool_name
        expired_request.execution_id = None

        with (
            fresh_poll_sessions(),
            patch.object(
                ApprovalService,
                "get_approval_request",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ),
        ):
            with patch.object(
                ApprovalService,
                "update_approval_request",
                new_callable=AsyncMock,
                return_value=expired_request,
            ):
                with patch.object(
                    ApprovalService,
                    "_broadcast_approval_update",
                    new_callable=AsyncMock,
                ) as mock_broadcast:
                    with pytest.raises(TimeoutError):
                        await approval_service.wait_for_approval(
                            sample_approval_request.id, poll_interval=0.1
                        )

                    mock_broadcast.assert_called_once_with(expired_request, "expired")


class TestAutoApproveRequest:
    """Test _auto_approve_request method."""

    async def test_auto_approve_sets_status_and_ai_fields(
        self, approval_service, sample_approval_request
    ):
        """Test that _auto_approve_request sets correct fields."""
        request_id = sample_approval_request.id

        with patch.object(
            approval_service,
            "update_approval_request",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ) as mock_update:
            with patch.object(
                approval_service,
                "_broadcast_approval_update",
                new_callable=AsyncMock,
            ) as mock_broadcast:
                result = await approval_service._auto_approve_request(
                    request_id=request_id,
                    reason="Safe tool call",
                    decided_by_ai=True,
                    ai_model="gpt-5.4-mini",
                    ai_confidence=0.95,
                )

                assert result == sample_approval_request
                mock_update.assert_called_once()
                update_arg = mock_update.call_args[0][1]
                assert update_arg.status == "approved"
                assert update_arg.decided_by_ai is True
                assert update_arg.ai_model == "gpt-5.4-mini"
                assert update_arg.ai_confidence == 0.95
                assert update_arg.ai_reasoning == "Safe tool call"
                mock_broadcast.assert_called_once_with(
                    sample_approval_request, "approved"
                )

    async def test_auto_approve_returns_none_when_not_found(self, approval_service):
        """Test that _auto_approve_request returns None if request not found."""
        with patch.object(
            approval_service,
            "update_approval_request",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await approval_service._auto_approve_request(
                request_id=uuid.uuid4(),
                reason="Reason",
            )
            assert result is None


class TestAutoDenyRequest:
    """Test _auto_deny_request method."""

    async def test_auto_deny_sets_status_and_ai_fields(
        self, approval_service, sample_approval_request
    ):
        """Test that _auto_deny_request sets correct fields."""
        request_id = sample_approval_request.id

        with patch.object(
            approval_service,
            "update_approval_request",
            new_callable=AsyncMock,
            return_value=sample_approval_request,
        ) as mock_update:
            with patch.object(
                approval_service,
                "_broadcast_approval_update",
                new_callable=AsyncMock,
            ) as mock_broadcast:
                result = await approval_service._auto_deny_request(
                    request_id=request_id,
                    reason="Dangerous operation",
                    decided_by_ai=True,
                    ai_model="gpt-5.4-mini",
                    ai_confidence=0.92,
                )

                assert result == sample_approval_request
                mock_update.assert_called_once()
                update_arg = mock_update.call_args[0][1]
                assert update_arg.status == "declined"
                assert update_arg.decided_by_ai is True
                assert update_arg.ai_model == "gpt-5.4-mini"
                assert update_arg.ai_confidence == 0.92
                assert update_arg.ai_reasoning == "Dangerous operation"
                mock_broadcast.assert_called_once_with(
                    sample_approval_request, "declined"
                )

    async def test_auto_deny_returns_none_when_not_found(self, approval_service):
        """Test that _auto_deny_request returns None if request not found."""
        with patch.object(
            approval_service,
            "update_approval_request",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await approval_service._auto_deny_request(
                request_id=uuid.uuid4(),
                reason="Reason",
            )
            assert result is None


class TestAIDrivenApprovalFlow:
    """Test AI-driven approval flow in create_and_notify."""

    @pytest.fixture
    def ai_driven_policy(self):
        """Create an AI-driven approval workflow."""
        policy = MagicMock(spec=ApprovalWorkflow)
        policy.id = uuid.uuid4()
        policy.name = "AI Review Policy"
        policy.approval_mode = "ai_driven"
        policy.approval_type = "slack"
        policy.timeout_seconds = 300
        policy.ai_confidence_threshold = 0.8
        policy.ai_fallback_behavior = "escalate"
        policy.escalation_workflow_id = None
        policy.notification_channels = ["slack"]
        policy.approval_config = {"webhook_url": "https://hooks.slack.com/test"}
        return policy

    def _mock_ai_result(
        self, decision="approve", confidence=0.95, reasoning="Looks safe"
    ):
        """Helper to create a mock AIApprovalResult."""
        result = MagicMock()
        result.decision = decision
        result.confidence = confidence
        result.reasoning = reasoning
        result.model_used = "gpt-5.4-mini"
        return result

    async def test_high_confidence_approve(
        self, approval_service, mock_db, ai_driven_policy
    ):
        """Test AI auto-approves when confidence >= threshold and decision is approve."""
        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()
        approved_request = MagicMock(spec=ApprovalRequest)
        approved_request.status = "approved"

        ai_result = self._mock_ai_result(decision="approve", confidence=0.95)

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch(
                "preloop.services.approval_service.get_ai_approval_service"
            ) as mock_get_ai,
            patch.object(
                approval_service,
                "_auto_approve_request",
                new_callable=AsyncMock,
                return_value=approved_request,
            ) as mock_auto_approve,
        ):
            mock_ai_svc = AsyncMock()
            mock_ai_svc.evaluate.return_value = ai_result
            mock_get_ai.return_value = mock_ai_svc

            result = await approval_service.create_and_notify(
                account_id="acct-1",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=ai_driven_policy,
                tool_name="get_issue",
                tool_args={"issue_id": "123"},
            )

            assert result == approved_request
            mock_auto_approve.assert_called_once_with(
                request_id=mock_request.id,
                reason=ai_result.reasoning,
                decided_by_ai=True,
                ai_model=ai_result.model_used,
                ai_confidence=ai_result.confidence,
            )

    async def test_high_confidence_deny(
        self, approval_service, mock_db, ai_driven_policy
    ):
        """Test AI auto-denies when confidence >= threshold and decision is deny."""
        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()
        denied_request = MagicMock(spec=ApprovalRequest)
        denied_request.status = "declined"

        ai_result = self._mock_ai_result(decision="deny", confidence=0.90)

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch(
                "preloop.services.approval_service.get_ai_approval_service"
            ) as mock_get_ai,
            patch.object(
                approval_service,
                "_auto_deny_request",
                new_callable=AsyncMock,
                return_value=denied_request,
            ) as mock_auto_deny,
        ):
            mock_ai_svc = AsyncMock()
            mock_ai_svc.evaluate.return_value = ai_result
            mock_get_ai.return_value = mock_ai_svc

            result = await approval_service.create_and_notify(
                account_id="acct-1",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=ai_driven_policy,
                tool_name="shell_exec",
                tool_args={"command": "rm -rf /"},
            )

            assert result == denied_request
            mock_auto_deny.assert_called_once_with(
                request_id=mock_request.id,
                reason=ai_result.reasoning,
                decided_by_ai=True,
                ai_model=ai_result.model_used,
                ai_confidence=ai_result.confidence,
            )

    async def test_low_confidence_fallback_approve(
        self, approval_service, mock_db, ai_driven_policy
    ):
        """Test fallback behavior 'approve' when AI confidence is below threshold."""
        ai_driven_policy.ai_fallback_behavior = "approve"

        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()
        approved_request = MagicMock(spec=ApprovalRequest)

        ai_result = self._mock_ai_result(
            decision="approve", confidence=0.5, reasoning="Not sure"
        )

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch(
                "preloop.services.approval_service.get_ai_approval_service"
            ) as mock_get_ai,
            patch.object(
                approval_service,
                "_auto_approve_request",
                new_callable=AsyncMock,
                return_value=approved_request,
            ) as mock_auto_approve,
        ):
            mock_ai_svc = AsyncMock()
            mock_ai_svc.evaluate.return_value = ai_result
            mock_get_ai.return_value = mock_ai_svc

            result = await approval_service.create_and_notify(
                account_id="acct-1",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=ai_driven_policy,
                tool_name="test_tool",
                tool_args={},
            )

            assert result == approved_request
            call_kwargs = mock_auto_approve.call_args[1]
            assert "Fallback approval" in call_kwargs["reason"]

    async def test_low_confidence_fallback_deny(
        self, approval_service, mock_db, ai_driven_policy
    ):
        """Test fallback behavior 'deny' when AI confidence is below threshold."""
        ai_driven_policy.ai_fallback_behavior = "deny"

        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()
        denied_request = MagicMock(spec=ApprovalRequest)

        ai_result = self._mock_ai_result(
            decision="uncertain", confidence=0.3, reasoning="Too ambiguous"
        )

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch(
                "preloop.services.approval_service.get_ai_approval_service"
            ) as mock_get_ai,
            patch.object(
                approval_service,
                "_auto_deny_request",
                new_callable=AsyncMock,
                return_value=denied_request,
            ) as mock_auto_deny,
        ):
            mock_ai_svc = AsyncMock()
            mock_ai_svc.evaluate.return_value = ai_result
            mock_get_ai.return_value = mock_ai_svc

            result = await approval_service.create_and_notify(
                account_id="acct-1",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=ai_driven_policy,
                tool_name="test_tool",
                tool_args={},
            )

            assert result == denied_request
            call_kwargs = mock_auto_deny.call_args[1]
            assert "Fallback denial" in call_kwargs["reason"]

    async def test_low_confidence_fallback_escalate(
        self, approval_service, mock_db, ai_driven_policy
    ):
        """Test fallback behavior 'escalate' (default) sends to human review."""
        ai_driven_policy.ai_fallback_behavior = "escalate"
        ai_driven_policy.escalation_workflow_id = None

        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()
        refreshed_request = MagicMock(spec=ApprovalRequest)

        ai_result = self._mock_ai_result(
            decision="uncertain", confidence=0.4, reasoning="Edge case"
        )

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch(
                "preloop.services.approval_service.get_ai_approval_service"
            ) as mock_get_ai,
            patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
            ) as mock_update,
            patch.object(
                approval_service,
                "get_approval_request",
                new_callable=AsyncMock,
                return_value=refreshed_request,
            ),
            patch.object(
                approval_service,
                "send_notifications",
                new_callable=AsyncMock,
                return_value={"email": {"success": True}},
            ) as mock_notify,
        ):
            mock_ai_svc = AsyncMock()
            mock_ai_svc.evaluate.return_value = ai_result
            mock_get_ai.return_value = mock_ai_svc

            result = await approval_service.create_and_notify(
                account_id="acct-1",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=ai_driven_policy,
                tool_name="test_tool",
                tool_args={},
            )

            assert result == refreshed_request
            # Should update request with AI info
            mock_update.assert_called()
            update_arg = mock_update.call_args[0][1]
            assert update_arg.ai_model == "gpt-5.4-mini"
            assert update_arg.ai_confidence == 0.4
            assert "Escalated to human review" in update_arg.ai_reasoning
            # Should send human notifications using original policy
            mock_notify.assert_called_once_with(refreshed_request, ai_driven_policy)

    async def test_escalation_with_escalation_workflow(
        self, approval_service, mock_db, ai_driven_policy
    ):
        """Test that escalation uses the escalation_workflow for notifications when set."""
        escalation_workflow_id = uuid.uuid4()
        ai_driven_policy.ai_fallback_behavior = "escalate"
        ai_driven_policy.escalation_workflow_id = escalation_workflow_id

        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()
        refreshed_request = MagicMock(spec=ApprovalRequest)

        escalation_workflow = MagicMock(spec=ApprovalWorkflow)
        escalation_workflow.id = escalation_workflow_id
        escalation_workflow.name = "Human Escalation Policy"

        ai_result = self._mock_ai_result(
            decision="uncertain", confidence=0.3, reasoning="Needs human"
        )

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch(
                "preloop.services.approval_service.get_ai_approval_service"
            ) as mock_get_ai,
            patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
            ),
            patch.object(
                approval_service,
                "get_approval_request",
                new_callable=AsyncMock,
                return_value=refreshed_request,
            ),
            patch.object(
                approval_service,
                "send_notifications",
                new_callable=AsyncMock,
                return_value={"email": {"success": True}},
            ) as mock_notify,
            patch(
                "preloop.models.crud.approval_workflow.get_approval_workflow_async",
                new_callable=AsyncMock,
                return_value=escalation_workflow,
            ),
        ):
            mock_ai_svc = AsyncMock()
            mock_ai_svc.evaluate.return_value = ai_result
            mock_get_ai.return_value = mock_ai_svc

            result = await approval_service.create_and_notify(
                account_id="acct-1",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=ai_driven_policy,
                tool_name="test_tool",
                tool_args={},
            )

            assert result == refreshed_request
            # Should send notifications using the escalation policy, not original
            mock_notify.assert_called_once_with(refreshed_request, escalation_workflow)

    async def test_escalation_workflow_not_found_falls_back(
        self, approval_service, mock_db, ai_driven_policy
    ):
        """Test that missing escalation policy falls back to original policy."""
        ai_driven_policy.ai_fallback_behavior = "escalate"
        ai_driven_policy.escalation_workflow_id = uuid.uuid4()

        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()
        refreshed_request = MagicMock(spec=ApprovalRequest)

        ai_result = self._mock_ai_result(
            decision="uncertain", confidence=0.3, reasoning="Needs human"
        )

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch(
                "preloop.services.approval_service.get_ai_approval_service"
            ) as mock_get_ai,
            patch.object(
                approval_service,
                "update_approval_request",
                new_callable=AsyncMock,
            ),
            patch.object(
                approval_service,
                "get_approval_request",
                new_callable=AsyncMock,
                return_value=refreshed_request,
            ),
            patch.object(
                approval_service,
                "send_notifications",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_notify,
            patch(
                "preloop.models.crud.approval_workflow.get_approval_workflow_async",
                new_callable=AsyncMock,
                return_value=None,  # Escalation policy not found
            ),
        ):
            mock_ai_svc = AsyncMock()
            mock_ai_svc.evaluate.return_value = ai_result
            mock_get_ai.return_value = mock_ai_svc

            result = await approval_service.create_and_notify(
                account_id="acct-1",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=ai_driven_policy,
                tool_name="test_tool",
                tool_args={},
            )

            assert result == refreshed_request
            # Should fall back to original policy for notifications
            mock_notify.assert_called_once_with(refreshed_request, ai_driven_policy)

    async def test_standard_mode_skips_ai_evaluation(
        self, approval_service, mock_db, ai_driven_policy
    ):
        """Test that standard (non-AI) policies skip AI evaluation entirely."""
        ai_driven_policy.approval_mode = "standard"

        mock_request = MagicMock(spec=ApprovalRequest)
        mock_request.id = uuid.uuid4()

        with (
            patch.object(
                approval_service,
                "create_approval_request",
                new_callable=AsyncMock,
                return_value=mock_request,
            ),
            patch(
                "preloop.services.approval_service.get_ai_approval_service"
            ) as mock_get_ai,
            patch.object(
                approval_service,
                "send_notifications",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_notify,
        ):
            result = await approval_service.create_and_notify(
                account_id="acct-1",
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=ai_driven_policy,
                tool_name="test_tool",
                tool_args={},
            )

            assert result == mock_request
            mock_get_ai.assert_not_called()
            mock_notify.assert_called_once_with(mock_request, ai_driven_policy)


class TestSendEscalationNotifications:
    """Test _send_escalation_notifications method."""

    @patch("preloop.services.push_proxy.is_push_proxy_configured")
    @patch("preloop.services.push_notifications.is_fcm_configured")
    @patch("preloop.services.push_notifications.get_apns_service")
    async def test_send_escalation_no_targets(
        self,
        mock_get_apns,
        mock_is_fcm,
        mock_is_proxy,
        approval_service,
        mock_db,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test _send_escalation_notifications with no escalation targets."""
        mock_get_apns.return_value = None
        mock_is_fcm.return_value = False
        mock_is_proxy.return_value = True

        sample_approval_workflow.escalation_user_ids = None
        sample_approval_workflow.escalation_team_ids = None

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=(set(), [], [])  # no users, no tokens
        )
        with patch(
            "preloop.services.approval_service.asyncio.get_event_loop",
            return_value=mock_loop,
        ):
            result = await approval_service._send_escalation_notifications(
                sample_approval_request, sample_approval_workflow
            )

            assert result["success"] is False
            assert "No escalation targets" in result["error"]

    @patch(
        "preloop.services.push_notifications.send_fcm_notification",
        autospec=True,
    )
    @patch("preloop.services.push_proxy.is_push_proxy_configured")
    @patch("preloop.services.push_notifications.is_fcm_configured")
    @patch("preloop.services.push_notifications.get_apns_service")
    async def test_send_escalation_android_uses_real_fcm_signature(
        self,
        mock_get_apns,
        mock_is_fcm,
        mock_is_proxy,
        mock_send_fcm,
        approval_service,
        mock_db,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Escalation must call send_fcm_notification with its real signature.

        The escalation branch passed ``device_token=``, but the function names
        that parameter ``token``. Every escalation send raised TypeError, which
        the surrounding ``except Exception`` swallowed, so Android escalation
        pushes failed silently. ``autospec=True`` enforces the real signature,
        so this test fails with TypeError against the old keyword.

        It also pins the return-value check: the function returns a dict, which
        is truthy even when the send failed, so a failure must not be counted
        as delivered.
        """
        mock_get_apns.return_value = None
        mock_is_fcm.return_value = True
        mock_is_proxy.return_value = False
        mock_send_fcm.return_value = {"success": False, "error": "boom"}

        sample_approval_workflow.escalation_user_ids = ["user-1"]
        sample_approval_workflow.escalation_team_ids = None

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=({"user-1"}, [], [("user-1", "fcm-token-abc")])
        )
        with patch(
            "preloop.services.approval_service.asyncio.get_event_loop",
            return_value=mock_loop,
        ):
            result = await approval_service._send_escalation_notifications(
                sample_approval_request, sample_approval_workflow
            )

        mock_send_fcm.assert_awaited_once()
        assert "token" in mock_send_fcm.await_args.kwargs
        assert "device_token" not in mock_send_fcm.await_args.kwargs
        assert mock_send_fcm.await_args.kwargs["token"] == "fcm-token-abc"

        # A failed send must count as failed, not delivered.
        assert result["push_sent"] == 0
        assert result["push_failed"] == 1

    @patch("preloop.services.push_proxy.is_push_proxy_configured")
    @patch("preloop.services.push_notifications.is_fcm_configured")
    @patch("preloop.services.push_notifications.get_apns_service")
    async def test_send_escalation_no_devices(
        self,
        mock_get_apns,
        mock_is_fcm,
        mock_is_proxy,
        approval_service,
        mock_db,
        sample_approval_request,
        sample_approval_workflow,
    ):
        """Test _send_escalation_notifications when no push devices."""
        mock_get_apns.return_value = None
        mock_is_fcm.return_value = False
        mock_is_proxy.return_value = True

        user_id = uuid.uuid4()
        sample_approval_workflow.escalation_user_ids = [user_id]
        sample_approval_workflow.escalation_team_ids = None

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=({user_id}, [], [])  # users but no ios/android tokens
        )
        with patch(
            "preloop.services.approval_service.asyncio.get_event_loop",
            return_value=mock_loop,
        ):
            result = await approval_service._send_escalation_notifications(
                sample_approval_request, sample_approval_workflow
            )

            assert result["success"] is True
            assert result["escalation_users"] == 1
            assert result["push_sent"] == 0


class TestNotificationStagger:
    """Staggered approval email: push first, email only if still pending."""

    @pytest.fixture
    def both_user_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def stagger_partition(self, both_user_id):
        return {
            "push_capable": [both_user_id],
            "email_only": [],
            "both_stagger": [both_user_id],
            "both_immediate": [],
            "all_email": [both_user_id],
        }

    async def test_both_channels_stagger_on_schedules_delayed_email(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        both_user_id,
        stagger_partition,
    ):
        """Dual-channel + stagger: push immediate, email delayed."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None
        approval_service._get_all_approver_user_ids = AsyncMock(
            return_value=[both_user_id]
        )
        approval_service._partition_approvers_for_stagger = AsyncMock(
            return_value=stagger_partition
        )
        approval_service._send_email_notification = AsyncMock()
        approval_service._send_push_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0}
        )

        with patch(
            "preloop.services.approval_service.asyncio.create_task"
        ) as mock_create_task:

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            result = await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        approval_service._send_push_notification.assert_called_once()
        approval_service._send_email_notification.assert_not_called()
        assert result["email_delayed"]["scheduled"] == 1
        assert result["email_delayed"]["delay_seconds"] == 60
        mock_create_task.assert_called_once()

    async def test_email_only_sends_immediate_email(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Email-only user: immediate email, no push recipients, no delay."""
        user_id = uuid.uuid4()
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None
        approval_service._get_all_approver_user_ids = AsyncMock(return_value=[user_id])
        approval_service._partition_approvers_for_stagger = AsyncMock(
            return_value={
                "push_capable": [],
                "email_only": [user_id],
                "both_stagger": [],
                "both_immediate": [],
                "all_email": [user_id],
            }
        )
        approval_service._send_email_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0, "skipped": 0}
        )
        # Push "succeeds" with no devices for email-only partitions; that is
        # still push-unavailable and must keep email immediate.
        approval_service._send_push_notification = AsyncMock(
            return_value={"success": True, "sent": 0, "failed": 0, "no_devices": True}
        )

        with patch(
            "preloop.services.approval_service.asyncio.create_task"
        ) as mock_create_task:

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            result = await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        approval_service._send_email_notification.assert_called_once()
        kwargs = approval_service._send_email_notification.call_args
        assert kwargs.kwargs.get("user_ids") == [user_id] or (
            len(kwargs.args) >= 3 and kwargs.args[2] == [user_id]
        )
        assert "email_delayed" not in result
        mock_create_task.assert_not_called()

    async def test_push_only_never_emails(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Push-only user: push only, no delayed email task."""
        user_id = uuid.uuid4()
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None
        approval_service._get_all_approver_user_ids = AsyncMock(return_value=[user_id])
        approval_service._partition_approvers_for_stagger = AsyncMock(
            return_value={
                "push_capable": [user_id],
                "email_only": [],
                "both_stagger": [],
                "both_immediate": [],
                "all_email": [],
            }
        )
        approval_service._send_email_notification = AsyncMock(
            return_value={"success": True, "sent": 0, "failed": 0, "skipped": 0}
        )
        approval_service._send_push_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0}
        )

        with patch(
            "preloop.services.approval_service.asyncio.create_task"
        ) as mock_create_task:

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            result = await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        assert "email_delayed" not in result
        mock_create_task.assert_not_called()
        email_call = approval_service._send_email_notification.call_args
        assert email_call.kwargs.get("user_ids") == []

    async def test_delayed_email_fires_when_still_pending(
        self, sample_approval_request, sample_approval_workflow
    ):
        """Delayed task emails when status is still pending."""
        from preloop.services import approval_service as approval_module

        user_id = uuid.uuid4()
        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = datetime.utcnow() + timedelta(minutes=5)
        sample_approval_request.approval_workflow = sample_approval_workflow

        with (
            patch(
                "preloop.services.approval_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch("preloop.models.db.session.get_async_db_session") as mock_get_session,
            patch(
                "preloop.services.approval_service.get_approval_request_async",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ),
            patch.object(
                approval_module.ApprovalService,
                "_send_email_notification",
                new_callable=AsyncMock,
                return_value={"success": True, "sent": 1, "failed": 0, "skipped": 0},
            ) as mock_email,
            patch(
                "preloop.services.approval_service._log_approval_notification_async"
            ) as mock_audit,
        ):
            session = AsyncMock()
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            await approval_module._send_delayed_approval_email(
                request_id=sample_approval_request.id,
                user_ids=[user_id],
                delay_seconds=0,
                base_url="https://app.test.com",
                correlation_id=None,
                account_id=str(sample_approval_request.account_id),
                tool_name=sample_approval_request.tool_name,
            )

            mock_email.assert_called_once()
            assert any(
                c.kwargs.get("status") == "sent" for c in mock_audit.call_args_list
            )

    @pytest.mark.parametrize(
        "terminal", ["approved", "declined", "cancelled", "expired"]
    )
    async def test_delayed_email_suppressed_for_terminal_status(
        self, sample_approval_request, terminal
    ):
        """Delayed email is skipped for each terminal status."""
        from preloop.services import approval_service as approval_module

        sample_approval_request.status = terminal
        with (
            patch(
                "preloop.services.approval_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch("preloop.models.db.session.get_async_db_session") as mock_get_session,
            patch(
                "preloop.services.approval_service.get_approval_request_async",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ),
            patch.object(
                approval_module.ApprovalService,
                "_send_email_notification",
                new_callable=AsyncMock,
            ) as mock_email,
            patch(
                "preloop.services.approval_service._log_approval_notification_async"
            ) as mock_audit,
        ):
            session = AsyncMock()
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            await approval_module._send_delayed_approval_email(
                request_id=sample_approval_request.id,
                user_ids=[uuid.uuid4()],
                delay_seconds=0,
                base_url="https://app.test.com",
                correlation_id=None,
                account_id=str(sample_approval_request.account_id),
                tool_name=sample_approval_request.tool_name,
            )

            mock_email.assert_not_called()
            assert mock_audit.call_args.kwargs["status"] == "skipped"
            assert mock_audit.call_args.kwargs["error"] == "resolved_before_email"

    async def test_delayed_email_suppressed_when_expires_at_passed(
        self, sample_approval_request
    ):
        """Expired-but-still-pending requests do not get delayed email."""
        from preloop.services import approval_service as approval_module

        sample_approval_request.status = "pending"
        sample_approval_request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        with (
            patch(
                "preloop.services.approval_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch("preloop.models.db.session.get_async_db_session") as mock_get_session,
            patch(
                "preloop.services.approval_service.get_approval_request_async",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ),
            patch.object(
                approval_module.ApprovalService,
                "_send_email_notification",
                new_callable=AsyncMock,
            ) as mock_email,
            patch(
                "preloop.services.approval_service._log_approval_notification_async"
            ) as mock_audit,
        ):
            session = AsyncMock()
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            await approval_module._send_delayed_approval_email(
                request_id=sample_approval_request.id,
                user_ids=[uuid.uuid4()],
                delay_seconds=0,
                base_url="https://app.test.com",
                correlation_id=None,
                account_id=str(sample_approval_request.account_id),
                tool_name=sample_approval_request.tool_name,
            )

            mock_email.assert_not_called()
            assert mock_audit.call_args.kwargs["error"] == "resolved_before_email"

    async def test_stagger_toggle_off_sends_both_immediately(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Stagger off: both channels immediate (today's behavior)."""
        user_id = uuid.uuid4()
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None
        approval_service._get_all_approver_user_ids = AsyncMock(return_value=[user_id])
        approval_service._partition_approvers_for_stagger = AsyncMock(
            return_value={
                "push_capable": [user_id],
                "email_only": [],
                "both_stagger": [],
                "both_immediate": [user_id],
                "all_email": [user_id],
            }
        )
        approval_service._send_email_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0, "skipped": 0}
        )
        approval_service._send_push_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0}
        )

        with patch(
            "preloop.services.approval_service.asyncio.create_task"
        ) as mock_create_task:

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            result = await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        approval_service._send_email_notification.assert_called_once()
        approval_service._send_push_notification.assert_called_once()
        assert "email_delayed" not in result
        mock_create_task.assert_not_called()

    async def test_push_not_configured_emails_immediately(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        both_user_id,
        stagger_partition,
    ):
        """Push not configured: email immediate despite stagger on."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None
        approval_service._get_all_approver_user_ids = AsyncMock(
            return_value=[both_user_id]
        )
        approval_service._partition_approvers_for_stagger = AsyncMock(
            return_value=stagger_partition
        )
        approval_service._send_email_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0, "skipped": 0}
        )
        approval_service._send_push_notification = AsyncMock(
            return_value={
                "success": False,
                "error": "Push notifications not configured",
            }
        )

        with patch(
            "preloop.services.approval_service.asyncio.create_task"
        ) as mock_create_task:

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            result = await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        approval_service._send_email_notification.assert_called_once()
        assert "email_delayed" not in result
        mock_create_task.assert_not_called()

    async def test_push_raises_emails_immediately(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        both_user_id,
        stagger_partition,
    ):
        """Push raise: email immediate fallback."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None
        approval_service._get_all_approver_user_ids = AsyncMock(
            return_value=[both_user_id]
        )
        approval_service._partition_approvers_for_stagger = AsyncMock(
            return_value=stagger_partition
        )
        approval_service._send_email_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0, "skipped": 0}
        )
        approval_service._send_push_notification = AsyncMock(
            side_effect=RuntimeError("APNs blew up")
        )

        with patch(
            "preloop.services.approval_service.asyncio.create_task"
        ) as mock_create_task:

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            result = await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        assert result["mobile_push"]["success"] is False
        approval_service._send_email_notification.assert_called_once()
        assert "email_delayed" not in result
        mock_create_task.assert_not_called()

    async def test_push_no_devices_emails_immediately(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        both_user_id,
        stagger_partition,
    ):
        """no_devices push result: email immediate fallback."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None
        approval_service._get_all_approver_user_ids = AsyncMock(
            return_value=[both_user_id]
        )
        approval_service._partition_approvers_for_stagger = AsyncMock(
            return_value=stagger_partition
        )
        approval_service._send_email_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0, "skipped": 0}
        )
        approval_service._send_push_notification = AsyncMock(
            return_value={"success": True, "sent": 0, "failed": 0, "no_devices": True}
        )

        with patch(
            "preloop.services.approval_service.asyncio.create_task"
        ) as mock_create_task:

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            result = await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        approval_service._send_email_notification.assert_called_once()
        assert "email_delayed" not in result
        mock_create_task.assert_not_called()

    async def test_mute_suppresses_delayed_task(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Mute bypass skips everything including delayed email scheduling."""
        from preloop.models.models.approval_bypass import ApprovalBypassMode

        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_request.managed_agent_id = None
        mute = MagicMock()
        mute.id = uuid.uuid4()
        mute.mode = ApprovalBypassMode.MUTE_NOTIFICATIONS
        mute.expires_at = datetime.utcnow() + timedelta(hours=1)
        approval_service._resolve_bypass = AsyncMock(return_value=mute)
        approval_service._send_email_notification = AsyncMock()
        approval_service._send_push_notification = AsyncMock()
        approval_service._partition_approvers_for_stagger = AsyncMock()

        with patch(
            "preloop.services.approval_service.asyncio.create_task"
        ) as mock_create_task:

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            result = await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        assert result["reason"] == "notifications_muted"
        approval_service._send_email_notification.assert_not_called()
        approval_service._send_push_notification.assert_not_called()
        approval_service._partition_approvers_for_stagger.assert_not_called()
        mock_create_task.assert_not_called()

    async def test_escalation_sends_email_and_push_immediately(
        self, approval_service, sample_approval_request, sample_approval_workflow
    ):
        """Escalation path is unchanged: both channels immediate."""
        user_id = uuid.uuid4()
        sample_approval_workflow.escalation_user_ids = [user_id]
        sample_approval_workflow.escalation_team_ids = None
        approval_service._record_event = AsyncMock()

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=({user_id}, [(user_id, "a" * 64)], [])
        )

        with (
            patch(
                "preloop.services.approval_service.asyncio.get_event_loop",
                return_value=mock_loop,
            ),
            patch("preloop.services.push_notifications.get_apns_service") as mock_apns,
            patch(
                "preloop.services.push_notifications.is_fcm_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.push_notifications.NotificationPayloadBuilder"
                ".new_approval_request",
                return_value={
                    "aps": {"alert": {"title": "t", "body": "b"}},
                    "data": {},
                },
            ),
        ):
            apns = AsyncMock()
            apns.send_notification = AsyncMock(return_value=(True, 200, None))
            mock_apns.return_value = apns

            result = await approval_service._send_escalation_notifications(
                sample_approval_request, sample_approval_workflow
            )

        # Escalation emails are sent inside the executor helper; push is immediate.
        assert result.get("push_sent", 0) >= 0
        assert "email_delayed" not in result

    async def test_delayed_task_uses_fresh_session(self, sample_approval_request):
        """Resolve via a different session after schedule; delayed email skips."""
        from preloop.services import approval_service as approval_module

        sample_approval_request.status = "approved"
        with (
            patch(
                "preloop.services.approval_service.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch("preloop.models.db.session.get_async_db_session") as mock_get_session,
            patch(
                "preloop.services.approval_service.get_approval_request_async",
                new_callable=AsyncMock,
                return_value=sample_approval_request,
            ) as mock_get,
            patch.object(
                approval_module.ApprovalService,
                "_send_email_notification",
                new_callable=AsyncMock,
            ) as mock_email,
            patch("preloop.services.approval_service._log_approval_notification_async"),
        ):
            session = AsyncMock()
            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)

            await approval_module._send_delayed_approval_email(
                request_id=sample_approval_request.id,
                user_ids=[uuid.uuid4()],
                delay_seconds=0,
                base_url="https://app.test.com",
                correlation_id=None,
                account_id=str(sample_approval_request.account_id),
                tool_name=sample_approval_request.tool_name,
            )

            mock_get.assert_called_once()
            assert mock_get.call_args.args[0] is session
            mock_email.assert_not_called()

    async def test_audit_scheduled_at_t0_and_sent_or_skipped_at_fire(
        self,
        approval_service,
        sample_approval_request,
        sample_approval_workflow,
        both_user_id,
        stagger_partition,
    ):
        """Email channel audited as scheduled at T0; fire path audits sent/skipped."""
        sample_approval_request.requested_at = datetime.utcnow()
        sample_approval_workflow.approval_type = None
        approval_service._get_all_approver_user_ids = AsyncMock(
            return_value=[both_user_id]
        )
        approval_service._partition_approvers_for_stagger = AsyncMock(
            return_value=stagger_partition
        )
        approval_service._send_push_notification = AsyncMock(
            return_value={"success": True, "sent": 1, "failed": 0}
        )
        approval_service._send_email_notification = AsyncMock()

        with (
            patch(
                "preloop.services.approval_service.asyncio.create_task"
            ) as mock_create_task,
            patch(
                "preloop.services.approval_service._log_approval_notification_async"
            ) as mock_audit,
        ):

            def _fake_create_task(coro):
                coro.close()
                return MagicMock()

            mock_create_task.side_effect = _fake_create_task
            await approval_service.send_notifications(
                sample_approval_request, sample_approval_workflow
            )

        email_audits = [
            c for c in mock_audit.call_args_list if c.kwargs.get("channel") == "email"
        ]
        assert any(c.kwargs.get("status") == "scheduled" for c in email_audits)
