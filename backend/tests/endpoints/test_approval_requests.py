"""Tests for approval_requests API endpoints."""

import asyncio
import functools
import importlib.util
import inspect
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import approval_requests
from preloop.models import models
from preloop.models.crud import crud_account_halt
from preloop.models.db.session import get_db_session
from preloop.models.schemas.approval_request import ApprovalRequestResponse


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
    request = models.ApprovalRequest(decided_by_ai=False)
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

            assert result == ApprovalRequestResponse.model_validate(
                mock_approval_request
            )
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
            assert result[0] == ApprovalRequestResponse.model_validate(
                mock_approval_request
            )
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

            approval_requests.list_approval_requests(
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

            approval_requests.list_approval_requests(
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
                        channel=approval_requests.AUTHENTICATED_DECISION_CHANNEL,
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
                        channel=approval_requests.AUTHENTICATED_DECISION_CHANNEL,
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


class TestDecideRequestsBatch:
    """Tests for the decide-batch endpoint (bulk approvals from the console)."""

    @staticmethod
    def _pending(account_id, status="pending", expires_at=None):
        request = MagicMock()
        request.id = uuid.uuid4()
        request.account_id = account_id
        request.status = status
        request.expires_at = expires_at
        return request

    @staticmethod
    def _service(requests_by_id, decided_status="approved"):
        """An ApprovalService whose lookups answer from a dict of requests."""
        service = AsyncMock()

        async def _get(request_id):
            return requests_by_id.get(request_id)

        async def _decide(request_id, comment, user_id=None, channel=None):
            decided = MagicMock()
            decided.id = request_id
            decided.status = decided_status
            return decided

        service.get_approval_request.side_effect = _get
        service.approve_request.side_effect = _decide
        service.decline_request.side_effect = _decide
        return service

    @staticmethod
    async def _call(service, user, ids, *, approved=True, comment=None, session=None):
        """Invoke the handler with a patched service and an async session."""
        from preloop.models.schemas.approval_request import ApprovalBatchDecision

        mock_http_request = MagicMock()
        mock_http_request.base_url = "http://localhost"
        session = session or AsyncMock()
        with patch(
            "preloop.api.endpoints.approval_requests.ApprovalService",
            return_value=service,
        ):
            response = await approval_requests.decide_requests_batch(
                decision=ApprovalBatchDecision(
                    ids=ids, approved=approved, comment=comment
                ),
                request=mock_http_request,
                current_user=user,
                db=session,
            )
        return response, session

    @pytest.mark.asyncio
    async def test_batch_approves_every_pending_request(self, mock_user):
        """Each id is approved once and reported once, in the order sent."""
        first = self._pending(mock_user.account_id)
        second = self._pending(mock_user.account_id)
        service = self._service({first.id: first, second.id: second})

        response, _session = await self._call(
            service, mock_user, [first.id, second.id], comment="ship it"
        )

        assert [result.id for result in response.results] == [first.id, second.id]
        assert all(result.ok for result in response.results)
        assert response.succeeded == 2
        assert response.failed == 0
        assert service.approve_request.await_count == 2
        service.decline_request.assert_not_awaited()
        assert service.approve_request.await_args_list[0].kwargs["channel"] == (
            approval_requests.AUTHENTICATED_DECISION_CHANNEL
        )

    @pytest.mark.asyncio
    async def test_batch_declines_when_not_approved(self, mock_user):
        """approved=False routes every id to decline, never to approve."""
        target = self._pending(mock_user.account_id)
        service = self._service({target.id: target}, decided_status="declined")

        response, _session = await self._call(
            service, mock_user, [target.id], approved=False
        )

        assert response.results[0].ok is True
        assert response.results[0].status == "declined"
        service.approve_request.assert_not_awaited()
        assert service.decline_request.await_count == 1

    @pytest.mark.asyncio
    async def test_batch_reports_per_id_and_keeps_going(self, mock_user):
        """A missing, foreign or resolved id costs that row and no other."""
        good = self._pending(mock_user.account_id)
        resolved = self._pending(mock_user.account_id, status="expired")
        foreign = self._pending(str(uuid.uuid4()))
        missing_id = uuid.uuid4()
        service = self._service(
            {good.id: good, resolved.id: resolved, foreign.id: foreign}
        )

        response, _session = await self._call(
            service, mock_user, [missing_id, resolved.id, foreign.id, good.id]
        )

        by_id = {result.id: result for result in response.results}
        assert by_id[missing_id].error == "Approval request not found"
        assert by_id[resolved.id].error == "Request already expired"
        # A request from another account is reported exactly like a missing one
        # so ids cannot be probed across accounts.
        assert by_id[foreign.id].error == "Approval request not found"
        assert by_id[good.id].ok is True
        assert response.succeeded == 1
        assert response.failed == 3
        # Only the one decidable request reached the service.
        assert service.approve_request.await_count == 1

    @pytest.mark.asyncio
    async def test_batch_leaves_expiry_to_the_service(self, mock_user):
        """A past-deadline row is still handed to the service, then reported."""
        stale = self._pending(
            mock_user.account_id,
            expires_at=datetime.utcnow() - timedelta(minutes=5),
        )
        fresh = self._pending(
            mock_user.account_id,
            expires_at=datetime.utcnow() + timedelta(minutes=5),
        )
        service = self._service({stale.id: stale, fresh.id: fresh})

        async def _approve(request_id, comment, user_id=None, channel=None):
            decided = MagicMock()
            decided.id = request_id
            # What _reject_if_not_actionable does to an unfrozen late row.
            decided.status = "expired" if request_id == stale.id else "approved"
            return decided

        service.approve_request.side_effect = _approve

        response, _session = await self._call(service, mock_user, [stale.id, fresh.id])

        by_id = {result.id: result for result in response.results}
        assert by_id[stale.id].ok is False
        assert by_id[stale.id].status == "expired"
        assert by_id[stale.id].error == "Request expired"
        assert by_id[fresh.id].ok is True
        assert by_id[fresh.id].status == "approved"
        assert response.succeeded == 1
        assert response.failed == 1
        # No expiry pre-check here: the kill-switch freeze exception (#157)
        # lives in the service, so every pending id has to reach it.
        assert service.approve_request.await_count == 2

    @pytest.mark.asyncio
    async def test_batch_decides_a_repeated_id_once(self, mock_user):
        """Duplicate ids in the body are collapsed before anything is decided."""
        target = self._pending(mock_user.account_id)
        service = self._service({target.id: target})

        response, _session = await self._call(
            service, mock_user, [target.id, target.id]
        )

        assert len(response.results) == 1
        assert service.approve_request.await_count == 1

    @pytest.mark.asyncio
    async def test_batch_survives_one_failing_decision(self, mock_user):
        """A raising decision is one failed result, not a failed batch."""
        boom = self._pending(mock_user.account_id)
        fine = self._pending(mock_user.account_id)
        service = self._service({boom.id: boom, fine.id: fine})

        async def _approve(request_id, comment, user_id=None, channel=None):
            if request_id == boom.id:
                raise RuntimeError("database is on fire")
            decided = MagicMock()
            decided.id = request_id
            decided.status = "approved"
            return decided

        service.approve_request.side_effect = _approve

        response, session = await self._call(service, mock_user, [boom.id, fine.id])

        by_id = {result.id: result for result in response.results}
        assert by_id[boom.id].ok is False
        assert by_id[boom.id].error == "Failed to process decision"
        assert by_id[fine.id].ok is True
        session.rollback.assert_awaited()

    @pytest.mark.asyncio
    async def test_batch_rolls_back_before_the_next_lookup(self, mock_user):
        """A flush/commit failure must not poison the rest of the batch."""
        boom = self._pending(mock_user.account_id)
        fine = self._pending(mock_user.account_id)
        requests_by_id = {boom.id: boom, fine.id: fine}
        service = self._service(requests_by_id)
        session = AsyncMock()
        needs_rollback = False

        async def _rollback() -> None:
            nonlocal needs_rollback
            needs_rollback = False

        session.rollback.side_effect = _rollback

        async def _get(request_id):
            if needs_rollback:
                raise RuntimeError("This session is in 'needs rollback' state")
            return requests_by_id.get(request_id)

        async def _approve(request_id, comment, user_id=None, channel=None):
            nonlocal needs_rollback
            if request_id == boom.id:
                needs_rollback = True
                raise RuntimeError("flush failed")
            decided = MagicMock()
            decided.id = request_id
            decided.status = "approved"
            return decided

        service.get_approval_request.side_effect = _get
        service.approve_request.side_effect = _approve

        response, _session = await self._call(
            service, mock_user, [boom.id, fine.id], session=session
        )

        by_id = {result.id: result for result in response.results}
        assert by_id[boom.id].ok is False
        assert by_id[fine.id].ok is True
        session.rollback.assert_awaited()

    def test_batch_rejects_an_empty_or_oversized_body(self):
        """The body carries at least one id and at most one page of them."""
        import pydantic

        from preloop.models.schemas.approval_request import (
            MAX_BATCH_DECISION_IDS,
            ApprovalBatchDecision,
        )

        with pytest.raises(pydantic.ValidationError):
            ApprovalBatchDecision(ids=[], approved=True)
        with pytest.raises(pydantic.ValidationError):
            ApprovalBatchDecision(
                ids=[uuid.uuid4() for _ in range(MAX_BATCH_DECISION_IDS + 1)],
                approved=True,
            )


class TestDecideRequestsBatchPermission:
    """decide-batch must still enforce decide_approvals with RBAC switched on.

    The handler holds an ``AsyncSession`` so its decisions stay off the event
    loop, but the RBAC check is synchronous CRUD. Naming the async session
    ``db`` handed it to that check and turned every RBAC-on request into a
    500, so the check lives in its own synchronous dependency.
    """

    @staticmethod
    def _rbac_plugin(seen):
        """Build a plugin decorator shaped like ``plugins/rbac/permissions``.

        The real plugin reads ``kwargs["db"]`` and immediately runs sync CRUD
        against it, which is where an ``AsyncSession`` blows up.
        """

        def require_permission(permission_name: str):
            def decorator(func):
                def _enforce(kwargs) -> None:
                    db = kwargs.get("db")
                    seen["db"] = db
                    seen["permission"] = permission_name
                    if seen.get("deny"):
                        raise HTTPException(
                            status_code=403,
                            detail=(
                                f"Insufficient permissions. Required: {permission_name}"
                            ),
                        )
                    db.query(models.UserRole).filter(
                        models.UserRole.user_id == kwargs["current_user"].id
                    )

                if asyncio.iscoroutinefunction(func):

                    @functools.wraps(func)
                    async def async_wrapper(*args, **kwargs):
                        _enforce(kwargs)
                        return await func(*args, **kwargs)

                    return async_wrapper

                @functools.wraps(func)
                def sync_wrapper(*args, **kwargs):
                    _enforce(kwargs)
                    return func(*args, **kwargs)

                return sync_wrapper

            return decorator

        return require_permission

    @classmethod
    def _router_with_rbac(cls, monkeypatch, seen):
        """Load a private copy of the endpoint module with RBAC enforcing.

        ``require_permission`` binds the plugin when the module is imported,
        and the installed build has no RBAC plugin, so the copy is executed
        under a patched ``preloop.utils.permissions``. It is a separate module
        object, so the shared import other tests use is left alone.
        """
        import preloop.utils.permissions as perms

        monkeypatch.setattr(perms, "_plugin_require_permission", cls._rbac_plugin(seen))
        monkeypatch.setattr(perms, "_rbac_checks_enabled", lambda: True)
        spec = importlib.util.spec_from_file_location(
            "approval_requests_rbac_probe", approval_requests.__file__
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _client(module, user):
        """Serve the copied router with the session dependencies stubbed."""
        app = FastAPI()
        app.include_router(module.router, prefix="/api/v1")
        app.dependency_overrides[get_current_active_user] = lambda: user
        # Unbound, so the RBAC query builds without touching a database.
        app.dependency_overrides[get_db_session] = lambda: Session()
        app.dependency_overrides[module._async_db_session] = lambda: AsyncMock()
        # Surface an unhandled error as the 500 a caller would actually see.
        return TestClient(app, raise_server_exceptions=False)

    def test_rbac_enabled_batch_decide_does_not_500(self, mock_user, monkeypatch):
        """The permission check gets a sync session, so the route answers 200."""
        seen: dict = {}
        module = self._router_with_rbac(monkeypatch, seen)
        service = AsyncMock()
        service.get_approval_request.return_value = None

        with patch.object(module, "ApprovalService", return_value=service):
            with self._client(module, mock_user) as client:
                response = client.post(
                    "/api/v1/approval-requests/decide-batch",
                    json={"ids": [str(uuid.uuid4())], "approved": True},
                )

        assert response.status_code == 200
        assert seen["permission"] == "decide_approvals"
        assert isinstance(seen["db"], Session)
        assert not isinstance(seen["db"], AsyncSession)
        # The batch itself still ran: the id simply does not exist.
        assert response.json()["results"][0]["error"] == "Approval request not found"

    def test_rbac_denial_blocks_the_batch(self, mock_user, monkeypatch):
        """A missing permission is a 403 and no decision is attempted."""
        seen: dict = {"deny": True}
        module = self._router_with_rbac(monkeypatch, seen)
        service = AsyncMock()

        with patch.object(module, "ApprovalService", return_value=service):
            with self._client(module, mock_user) as client:
                response = client.post(
                    "/api/v1/approval-requests/decide-batch",
                    json={"ids": [str(uuid.uuid4())], "approved": True},
                )

        assert response.status_code == 403
        service.get_approval_request.assert_not_awaited()

    def test_permission_check_runs_off_the_event_loop(self):
        """FastAPI dispatches a ``def`` dependency on its threadpool.

        That is what keeps the RBAC pool checkout from stalling the loop, the
        reason the async session is not simply renamed to ``db``.
        """
        guard = approval_requests._require_decide_approvals

        assert not asyncio.iscoroutinefunction(guard)
        assert inspect.signature(guard).parameters["db"].annotation is Session


class TestDecideRequestsBatchAgainstPostgres:
    """decide-batch on the real database, where expiry is an actual status write."""

    @staticmethod
    @asynccontextmanager
    async def _session():
        """An AsyncSession on the test database, like the route dependency."""
        url = os.environ["DATABASE_URL"].replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
        finally:
            await engine.dispose()

    @staticmethod
    def _seed(db_engine, *, expires_at, halt_activated_at=None):
        """Commit one pending request, optionally under a tools halt.

        Committed rather than nested in the ``db_session`` transaction: the
        route runs on its own asyncpg connection and would not see the rows.
        """
        account_id = uuid.uuid4()
        with Session(db_engine) as db:
            db.add(models.Account(id=account_id, organization_name="batch-expiry"))
            db.flush()
            workflow = models.ApprovalWorkflow(
                account_id=account_id, name="batch expiry", workflow_type="simple"
            )
            tool = models.ToolConfiguration(
                account_id=account_id,
                tool_name="batch-expiry",
                tool_source="builtin",
            )
            db.add_all([workflow, tool])
            db.flush()
            approval_request = models.ApprovalRequest(
                account_id=account_id,
                tool_configuration_id=tool.id,
                approval_workflow_id=workflow.id,
                tool_name="batch-expiry",
                tool_args={},
                requested_at=expires_at - timedelta(minutes=30),
                expires_at=expires_at,
                status="pending",
            )
            db.add(approval_request)
            if halt_activated_at is not None:
                crud_account_halt.set_scopes(
                    db,
                    account_id=account_id,
                    scopes=["tools"],
                    active=True,
                    user_id=None,
                    now=halt_activated_at.replace(tzinfo=UTC),
                    commit=False,
                )
            db.commit()
            return account_id, approval_request.id

    @staticmethod
    def _drop(db_engine, account_id):
        """Remove the seeded account; every child row cascades with it."""
        with Session(db_engine) as db:
            db.query(models.Account).filter(models.Account.id == account_id).delete()
            db.commit()

    @staticmethod
    def _status(db_engine, request_id):
        with Session(db_engine) as db:
            return db.get(models.ApprovalRequest, request_id).status

    @classmethod
    async def _decide(cls, account_id, request_id):
        """Approve one id through the route with a real ApprovalService."""
        from preloop.models.schemas.approval_request import ApprovalBatchDecision

        user = MagicMock()
        user.id = uuid.uuid4()
        user.account_id = account_id
        http_request = MagicMock()
        http_request.base_url = "http://localhost"
        async with cls._session() as session:
            response = await approval_requests.decide_requests_batch(
                decision=ApprovalBatchDecision(ids=[request_id], approved=True),
                request=http_request,
                current_user=user,
                db=session,
            )
        return response.results[0]

    @pytest.mark.asyncio
    async def test_batch_decides_a_past_deadline_request_frozen_by_the_kill_switch(
        self, db_engine
    ):
        """An incident freeze keeps a late request decidable (#157)."""
        deadline = datetime.utcnow() - timedelta(minutes=1)
        account_id, request_id = self._seed(
            db_engine,
            expires_at=deadline,
            halt_activated_at=deadline - timedelta(minutes=10),
        )
        try:
            result = await self._decide(account_id, request_id)

            assert result.ok is True
            assert result.status == "approved"
            assert self._status(db_engine, request_id) == "approved"
        finally:
            self._drop(db_engine, account_id)

    @pytest.mark.asyncio
    async def test_batch_expires_a_past_deadline_request_when_nothing_is_frozen(
        self, db_engine
    ):
        """Without a freeze the row is reported expired and written expired."""
        account_id, request_id = self._seed(
            db_engine, expires_at=datetime.utcnow() - timedelta(minutes=1)
        )
        try:
            result = await self._decide(account_id, request_id)

            assert result.ok is False
            assert result.status == "expired"
            # The old pre-check reported this without ever writing it.
            assert self._status(db_engine, request_id) == "expired"
        finally:
            self._drop(db_engine, account_id)


class TestGetApprovalRequestHistory:
    """Tests for the workflow-history timeline endpoint (issue #335)."""

    def _mock_event(self, event_type: str, detail: str, actor_id=None):
        event = MagicMock()
        event.id = uuid.uuid4()
        event.event_type = event_type
        event.detail = detail
        event.comment = None
        event.actor_id = actor_id
        event.timestamp = datetime.now(UTC)
        return event

    def test_history_success(self, mock_user, mock_approval_request, mock_db_session):
        """Events are returned ordered with actor identities resolved."""
        actor_id = uuid.uuid4()
        events = [
            self._mock_event("approval_requested", "Approval requested"),
            self._mock_event("vote_received", "Approved by actor", actor_id=actor_id),
        ]
        actor = MagicMock()
        actor.id = actor_id
        actor.email = "approver@example.com"
        mock_db_session.query.return_value.filter.return_value.all.return_value = [
            actor
        ]

        with (
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_request"
            ) as mock_crud,
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_event"
            ) as mock_event_crud,
        ):
            mock_crud.get.return_value = mock_approval_request
            mock_event_crud.get_by_request.return_value = events

            result = approval_requests.get_approval_request_history(
                request_id=mock_approval_request.id,
                current_user=mock_user,
                db=mock_db_session,
            )

        assert len(result) == 2
        assert result[0].event_type == "approval_requested"
        assert result[0].actor_email is None
        assert result[1].actor_email == "approver@example.com"
        mock_event_crud.get_by_request.assert_called_once_with(
            mock_db_session, approval_request_id=mock_approval_request.id
        )

    def test_history_request_not_found(self, mock_user, mock_db_session):
        """404 when the request is missing or outside the account."""
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as mock_crud:
            mock_crud.get.return_value = None

            with pytest.raises(HTTPException) as exc_info:
                approval_requests.get_approval_request_history(
                    request_id=uuid.uuid4(),
                    current_user=mock_user,
                    db=mock_db_session,
                )

        assert exc_info.value.status_code == 404

    def test_history_unknown_actor_left_unresolved(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """An actor that no longer resolves still renders the event."""
        events = [
            self._mock_event(
                "vote_received", "Approved by former user", actor_id=uuid.uuid4()
            )
        ]
        mock_db_session.query.return_value.filter.return_value.all.return_value = []

        with (
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_request"
            ) as mock_crud,
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_event"
            ) as mock_event_crud,
        ):
            mock_crud.get.return_value = mock_approval_request
            mock_event_crud.get_by_request.return_value = events

            result = approval_requests.get_approval_request_history(
                request_id=mock_approval_request.id,
                current_user=mock_user,
                db=mock_db_session,
            )

        assert len(result) == 1
        assert result[0].actor_email is None


class TestViewedEventRecording:
    """Opening an approval must land one `viewed` entry per viewer."""

    def test_viewed_event_recorded_once_per_actor(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        with (
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_request"
            ) as mock_crud,
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_event"
            ) as mock_event_crud,
        ):
            mock_crud.get.return_value = mock_approval_request
            mock_event_crud.has_event.return_value = False

            approval_requests.get_approval_request(
                request_id=mock_approval_request.id,
                current_user=mock_user,
                db=mock_db_session,
            )

            mock_event_crud.record.assert_called_once()
            kwargs = mock_event_crud.record.call_args.kwargs
            assert kwargs["event_type"] == "viewed"
            assert kwargs["actor_id"] == mock_user.id
            assert kwargs["approval_request_id"] == mock_approval_request.id

    def test_viewed_event_deduped(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """A repeat view by the same viewer does not add another entry."""
        with (
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_request"
            ) as mock_crud,
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_event"
            ) as mock_event_crud,
        ):
            mock_crud.get.return_value = mock_approval_request
            mock_event_crud.has_event.return_value = True

            approval_requests.get_approval_request(
                request_id=mock_approval_request.id,
                current_user=mock_user,
                db=mock_db_session,
            )

            mock_event_crud.record.assert_not_called()

    def test_view_tracking_failure_does_not_break_read(
        self, mock_user, mock_approval_request, mock_db_session
    ):
        """A failing timeline write must not fail the request read."""
        with (
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_request"
            ) as mock_crud,
            patch(
                "preloop.api.endpoints.approval_requests.crud_approval_event"
            ) as mock_event_crud,
        ):
            mock_crud.get.return_value = mock_approval_request
            mock_event_crud.has_event.side_effect = Exception("db down")

            result = approval_requests.get_approval_request(
                request_id=mock_approval_request.id,
                current_user=mock_user,
                db=mock_db_session,
            )

        assert result == ApprovalRequestResponse.model_validate(mock_approval_request)
