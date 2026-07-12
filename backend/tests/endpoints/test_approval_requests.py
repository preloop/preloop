"""Tests for approval_requests API endpoints."""

import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import approval_requests


@pytest.fixture
def mock_user():
    """Create a mock user with account_id."""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.account_id = str(uuid.uuid4())
    user.username = "testuser"
    user.is_active = True
    return user


@pytest.fixture
def mock_approval_request(mock_user):
    """Create a mock approval request."""
    request = MagicMock()
    request.id = uuid.uuid4()
    request.account_id = mock_user.account_id
    request.tool_name = "test_tool"
    request.tool_args = {"arg1": "value1"}
    request.status = "pending"
    request.requested_at = datetime.now(UTC)
    request.resolved_at = None
    request.expires_at = None
    request.approver_comment = None
    request.agent_reasoning = "Test reasoning"
    request.execution_id = str(uuid.uuid4())
    request.tool_configuration_id = uuid.uuid4()
    request.approval_workflow_id = uuid.uuid4()
    request.webhook_posted_at = None
    request.webhook_error = None
    return request


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    return MagicMock()


class TestGetApprovalRequest:
    """Tests for the get_approval_request endpoint."""

    def test_get_approval_request_success(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test successful retrieval of an approval request."""
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as mock_crud:
            mock_crud.get.return_value = mock_approval_request

            result = approval_requests.get_approval_request(
                request_id=mock_approval_request.id,
                current_user=mock_user,
                db=mock_db_session,
            )

            assert result == mock_approval_request
            mock_crud.get.assert_called_once_with(
                mock_db_session,
                id=str(mock_approval_request.id),
                account_id=mock_user.account_id,
            )

    def test_get_approval_request_not_found(self, mock_user, mock_db_session):
        """Test 404 when approval request is not found."""
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as mock_crud:
            mock_crud.get.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                approval_requests.get_approval_request(
                    request_id=uuid.uuid4(),
                    current_user=mock_user,
                    db=mock_db_session,
                )

            assert exc_info.value.status_code == 404
            assert exc_info.value.detail == "Approval request not found"


class TestListApprovalRequests:
    """Tests for the list_approval_requests endpoint."""

    def test_list_approval_requests_success(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test successful listing of approval requests."""
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as mock_crud:
            mock_crud.get_multi_by_account.return_value = [mock_approval_request]

            result = approval_requests.list_approval_requests(
                status=None,
                execution_id=None,
                limit=50,
                skip=0,
                current_user=mock_user,
                db=mock_db_session,
            )

            assert len(result) == 1
            assert result[0] == mock_approval_request
            mock_crud.get_multi_by_account.assert_called_once_with(
                mock_db_session,
                account_id=mock_user.account_id,
                execution_id=None,
                status=None,
                skip=0,
                limit=50,
            )

    def test_list_approval_requests_with_status_filter(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test listing with status filter."""
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as mock_crud:
            mock_crud.get_multi_by_account.return_value = [mock_approval_request]

            result = approval_requests.list_approval_requests(
                status="pending",
                execution_id=None,
                limit=50,
                skip=0,
                current_user=mock_user,
                db=mock_db_session,
            )

            mock_crud.get_multi_by_account.assert_called_once_with(
                mock_db_session,
                account_id=mock_user.account_id,
                execution_id=None,
                status="pending",
                skip=0,
                limit=50,
            )

    def test_list_approval_requests_with_execution_id_filter(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test listing with execution_id filter."""
        execution_id = str(uuid.uuid4())
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as mock_crud:
            mock_crud.get_multi_by_account.return_value = [mock_approval_request]

            result = approval_requests.list_approval_requests(
                status=None,
                execution_id=execution_id,
                limit=50,
                skip=0,
                current_user=mock_user,
                db=mock_db_session,
            )

            mock_crud.get_multi_by_account.assert_called_once_with(
                mock_db_session,
                account_id=mock_user.account_id,
                execution_id=execution_id,
                status=None,
                skip=0,
                limit=50,
            )

    def test_list_approval_requests_empty(self, mock_user, mock_db_session):
        """Test listing when no approval requests exist."""
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as mock_crud:
            mock_crud.get_multi_by_account.return_value = []

            result = approval_requests.list_approval_requests(
                status=None,
                execution_id=None,
                limit=50,
                skip=0,
                current_user=mock_user,
                db=mock_db_session,
            )

            assert len(result) == 0


class TestApproveRequest:
    """Tests for the approve_request endpoint."""

    @pytest.mark.asyncio
    async def test_approve_request_success(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test successful approval of a request."""
        from preloop.models.schemas.approval_request import (
            ApprovalDecision,
            ApprovalRequestResponse,
        )

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=True, comment="Approved for testing")

        updated_request = MagicMock()
        updated_request.id = mock_approval_request.id
        updated_request.status = "approved"

        # Build expected Pydantic response
        expected_response = MagicMock(spec=ApprovalRequestResponse)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = mock_approval_request
                mock_service.approve_request.return_value = updated_request
                mock_approval_service.return_value = mock_service

                with patch(
                    "preloop.api.endpoints.approval_requests.ApprovalRequestResponse"
                ) as mock_response_cls:
                    mock_response_cls.model_validate.return_value = expected_response

                    result = await approval_requests.approve_request(
                        request_id=mock_approval_request.id,
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                    assert result == expected_response
                    mock_response_cls.model_validate.assert_called_once_with(
                        updated_request
                    )
                    mock_service.approve_request.assert_called_once_with(
                        mock_approval_request.id,
                        decision.comment,
                        user_id=mock_user.id,
                    )

    @pytest.mark.asyncio
    async def test_approve_request_not_found(self, mock_user, mock_db_session):
        """Test 404 when approval request is not found."""
        from preloop.models.schemas.approval_request import ApprovalDecision

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=True, comment=None)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = None
                mock_approval_service.return_value = mock_service

                with pytest.raises(HTTPException) as exc_info:
                    await approval_requests.approve_request(
                        request_id=uuid.uuid4(),
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                assert exc_info.value.status_code == 404
                assert exc_info.value.detail == "Approval request not found"

    @pytest.mark.asyncio
    async def test_approve_request_unauthorized(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test 403 when user is not authorized to approve."""
        from preloop.models.schemas.approval_request import ApprovalDecision

        # Change account_id so it doesn't match
        mock_approval_request.account_id = str(uuid.uuid4())

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=True, comment=None)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = mock_approval_request
                mock_approval_service.return_value = mock_service

                with pytest.raises(HTTPException) as exc_info:
                    await approval_requests.approve_request(
                        request_id=mock_approval_request.id,
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                assert exc_info.value.status_code == 403
                assert "Not authorized" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_approve_request_already_resolved(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test 400 when request is already resolved."""
        from preloop.models.schemas.approval_request import ApprovalDecision

        mock_approval_request.status = "approved"

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=True, comment=None)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = mock_approval_request
                mock_approval_service.return_value = mock_service

                with pytest.raises(HTTPException) as exc_info:
                    await approval_requests.approve_request(
                        request_id=mock_approval_request.id,
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                assert exc_info.value.status_code == 400
                assert "already" in exc_info.value.detail


class TestDeclineRequest:
    """Tests for the decline_request endpoint."""

    @pytest.mark.asyncio
    async def test_decline_request_success(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test successful decline of a request."""
        from preloop.models.schemas.approval_request import (
            ApprovalDecision,
            ApprovalRequestResponse,
        )

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=False, comment="Declined for security")

        updated_request = MagicMock()
        updated_request.id = mock_approval_request.id
        updated_request.status = "declined"

        expected_response = MagicMock(spec=ApprovalRequestResponse)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = mock_approval_request
                mock_service.decline_request.return_value = updated_request
                mock_approval_service.return_value = mock_service

                with patch(
                    "preloop.api.endpoints.approval_requests.ApprovalRequestResponse"
                ) as mock_response_cls:
                    mock_response_cls.model_validate.return_value = expected_response

                    result = await approval_requests.decline_request(
                        request_id=mock_approval_request.id,
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                    assert result == expected_response
                    mock_response_cls.model_validate.assert_called_once_with(
                        updated_request
                    )
                    mock_service.decline_request.assert_called_once_with(
                        mock_approval_request.id,
                        decision.comment,
                        user_id=mock_user.id,
                    )

    @pytest.mark.asyncio
    async def test_decline_request_not_found(self, mock_user, mock_db_session):
        """Test 404 when approval request is not found."""
        from preloop.models.schemas.approval_request import ApprovalDecision

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=False, comment=None)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = None
                mock_approval_service.return_value = mock_service

                with pytest.raises(HTTPException) as exc_info:
                    await approval_requests.decline_request(
                        request_id=uuid.uuid4(),
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                assert exc_info.value.status_code == 404


class TestDecideRequest:
    """Tests for the decide_request endpoint."""

    @pytest.mark.asyncio
    async def test_decide_request_approve(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test decide endpoint with approved=True."""
        from preloop.models.schemas.approval_request import (
            ApprovalDecision,
            ApprovalRequestResponse,
        )

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=True, comment="Approved via decide")

        updated_request = MagicMock()
        updated_request.id = mock_approval_request.id
        updated_request.status = "approved"

        expected_response = MagicMock(spec=ApprovalRequestResponse)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = mock_approval_request
                mock_service.approve_request.return_value = updated_request
                mock_approval_service.return_value = mock_service

                with patch(
                    "preloop.api.endpoints.approval_requests.ApprovalRequestResponse"
                ) as mock_response_cls:
                    mock_response_cls.model_validate.return_value = expected_response

                    result = await approval_requests.decide_request(
                        request_id=mock_approval_request.id,
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                    assert result == expected_response
                    mock_service.approve_request.assert_called_once()
                    mock_service.decline_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_decide_request_decline(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test decide endpoint with approved=False."""
        from preloop.models.schemas.approval_request import (
            ApprovalDecision,
            ApprovalRequestResponse,
        )

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=False, comment="Declined via decide")

        updated_request = MagicMock()
        updated_request.id = mock_approval_request.id
        updated_request.status = "declined"

        expected_response = MagicMock(spec=ApprovalRequestResponse)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = mock_approval_request
                mock_service.decline_request.return_value = updated_request
                mock_approval_service.return_value = mock_service

                with patch(
                    "preloop.api.endpoints.approval_requests.ApprovalRequestResponse"
                ) as mock_response_cls:
                    mock_response_cls.model_validate.return_value = expected_response

                    result = await approval_requests.decide_request(
                        request_id=mock_approval_request.id,
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                    assert result == expected_response
                    mock_service.decline_request.assert_called_once()
                    mock_service.approve_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_decide_request_failure(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """Test 500 when decision processing fails."""
        from preloop.models.schemas.approval_request import ApprovalDecision

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"

        decision = ApprovalDecision(approved=True, comment=None)

        with patch(
            "preloop.api.endpoints.approval_requests.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.approval_requests.ApprovalService"
            ) as mock_approval_service:
                mock_service = AsyncMock()
                mock_service.get_approval_request.return_value = mock_approval_request
                mock_service.approve_request.return_value = None  # Simulate failure
                mock_approval_service.return_value = mock_service

                with pytest.raises(HTTPException) as exc_info:
                    await approval_requests.decide_request(
                        request_id=mock_approval_request.id,
                        decision=decision,
                        request=mock_http_request,
                        current_user=mock_user,
                        db=mock_db_session,
                    )

                assert exc_info.value.status_code == 500
