"""Tests for public approval API endpoints (token-based, no auth required)."""

import uuid
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from preloop.models.crud import crud_approval_workflow
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.tool_configuration import ToolConfiguration
from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate


class TestPublicApprovalGetData:
    """Test GET /approval/{request_id}/data endpoint."""

    def test_get_approval_data_success(self, client: TestClient, db_session, test_user):
        """Test GET approval data with valid token returns request details."""
        # Create approval workflow
        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Test Workflow", approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        # Create tool configuration
        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        # Create approval request with known token
        approval_token = "test-token-12345"
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            execution_id="exec-1",
            tool_name="test_tool",
            tool_args={"arg1": "value1"},
            agent_reasoning="Test reasoning",
            status="pending",
            requested_at=datetime.now(UTC),
            approval_token=approval_token,
        )
        db_session.add(approval_request)
        db_session.flush()

        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": approval_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(approval_request.id)
        assert data["tool_name"] == "test_tool"
        assert data["tool_args"] == {"arg1": "value1"}
        assert data["agent_reasoning"] == "Test reasoning"
        assert data["status"] == "pending"
        assert "requested_at" in data

    def test_get_approval_data_invalid_token(
        self, client: TestClient, db_session, test_user
    ):
        """Test GET approval data with invalid token returns 404."""
        request_id = uuid.uuid4()
        response = client.get(
            f"/approval/{request_id}/data",
            params={"token": "invalid-token"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_approval_data_missing_token(self, client: TestClient):
        """Test GET approval data without token returns 422."""
        request_id = uuid.uuid4()
        response = client.get(f"/approval/{request_id}/data")
        assert response.status_code == 422


class TestPublicApprovalDecide:
    """Test POST /approval/{request_id}/decide endpoint."""

    def test_decide_approval_invalid_token(self, client: TestClient):
        """Test POST decide with invalid token returns 404."""
        request_id = uuid.uuid4()
        response = client.post(
            f"/approval/{request_id}/decide",
            params={"token": "invalid-token"},
            json={"action": "approve", "comment": None},
        )
        assert response.status_code == 404

    def test_decide_approval_invalid_action(
        self, client: TestClient, db_session, test_user
    ):
        """Test POST decide with invalid action returns 400."""
        # Create minimal approval request
        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Test WF", approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        approval_token = "decide-test-token"
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            execution_id="exec-1",
            tool_name="test_tool",
            tool_args={},
            status="pending",
            requested_at=datetime.now(UTC),
            approval_token=approval_token,
        )
        db_session.add(approval_request)
        db_session.flush()

        with patch(
            "preloop.api.endpoints.public_approval.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.public_approval.ApprovalService"
            ) as mock_service_cls:
                mock_service = AsyncMock()
                mock_service_cls.return_value = mock_service

                response = client.post(
                    f"/approval/{approval_request.id}/decide",
                    params={"token": approval_token},
                    json={"action": "invalid_action", "comment": None},
                )
                assert response.status_code == 400
                assert "invalid" in response.json()["detail"].lower()

    def test_decide_approval_success(self, client: TestClient, db_session, test_user):
        """Test POST decide with approve action succeeds."""
        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Test WF", approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        approval_token = "decide-approve-token"
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            execution_id="exec-1",
            tool_name="test_tool",
            tool_args={},
            status="pending",
            requested_at=datetime.now(UTC),
            approval_token=approval_token,
        )
        db_session.add(approval_request)
        db_session.flush()

        updated_request = MagicMock()
        updated_request.id = approval_request.id
        updated_request.tool_name = "test_tool"
        updated_request.tool_args = {}
        updated_request.agent_reasoning = None
        updated_request.status = "approved"
        updated_request.requested_at = approval_request.requested_at
        updated_request.expires_at = None

        with patch(
            "preloop.api.endpoints.public_approval.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.public_approval.ApprovalService"
            ) as mock_service_cls:
                mock_service = AsyncMock()
                mock_service.approve_request = AsyncMock(return_value=updated_request)
                mock_service_cls.return_value = mock_service

                response = client.post(
                    f"/approval/{approval_request.id}/decide",
                    params={"token": approval_token},
                    json={"action": "approve", "comment": "Looks good"},
                )
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "approved"
                assert data["id"] == str(approval_request.id)


class TestPublicApprovalPage:
    """GET /approval/{id} is the public HTML page, only with a token."""

    def test_bare_path_redirects_to_console(self, client: TestClient):
        request_id = uuid.uuid4()
        response = client.get(f"/approval/{request_id}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == f"/console/approval/{request_id}"

    def test_empty_token_redirects_to_console(self, client: TestClient):
        request_id = uuid.uuid4()
        response = client.get(
            f"/approval/{request_id}",
            params={"token": "  "},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == f"/console/approval/{request_id}"

    def test_token_query_serves_public_html(self, client: TestClient):
        request_id = uuid.uuid4()
        response = client.get(
            f"/approval/{request_id}",
            params={"token": "email-token"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert b"token" in response.content or b"Approval" in response.content

    def test_token_query_still_serves_html_for_non_uuid(self, client: TestClient):
        """Email/Slack `?token=` must keep serving the public page."""
        response = client.get(
            "/approval/not-a-uuid",
            params={"token": "email-token"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_non_uuid_request_id_does_not_redirect(self, client: TestClient):
        """Reject unvalidated path segments so Location cannot be attacker-controlled."""
        response = client.get("/approval/not-a-uuid", follow_redirects=False)
        assert response.status_code == 404
        assert "location" not in response.headers

    def test_open_redirect_payload_does_not_302(self, client: TestClient):
        response = client.get(
            "/approval/https:%2F%2Fevil.example",
            follow_redirects=False,
        )
        assert response.status_code == 404
        location = response.headers.get("location", "")
        assert location == ""
        assert "evil.example" not in location


class TestPublicApprovalExpiredAndHistory:
    """Expired requests must stay viewable and expose their timeline (#335)."""

    def _make_request(
        self,
        db_session,
        test_user,
        *,
        status: str,
        token: str,
    ):
        from preloop.models.models.approval_event import ApprovalEvent

        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="WF " + status, approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            tool_name="test_tool",
            tool_args={"arg1": "value1"},
            status=status,
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) - timedelta(minutes=1)
            if status == "expired"
            else datetime.now(UTC) + timedelta(minutes=5),
            resolved_at=datetime.now(UTC) if status != "pending" else None,
            approval_token=token,
        )
        db_session.add(approval_request)
        db_session.flush()

        db_session.add(
            ApprovalEvent(
                approval_request_id=approval_request.id,
                account_id=test_user.account_id,
                event_type="approval_requested",
                detail="Approval requested for tool 'test_tool'",
            )
        )
        db_session.add(
            ApprovalEvent(
                approval_request_id=approval_request.id,
                account_id=test_user.account_id,
                event_type="expired",
                detail="Expired: no response within the approval window",
            )
        )
        db_session.flush()
        return approval_request

    def test_get_approval_data_returns_expired_request(
        self, client: TestClient, db_session, test_user
    ):
        """An expired request is retrievable with its token — not an error."""
        approval_request = self._make_request(
            db_session, test_user, status="expired", token="expired-token-1"
        )

        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "expired-token-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "expired"
        assert data["resolved_at"] is not None

    def test_get_approval_data_includes_history(
        self, client: TestClient, db_session, test_user
    ):
        """The public payload carries the timeline (without actor identities)."""
        approval_request = self._make_request(
            db_session, test_user, status="expired", token="expired-token-2"
        )

        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "expired-token-2"},
        )
        assert response.status_code == 200
        history = response.json()["history"]
        types = [event["event_type"] for event in history]
        assert "approval_requested" in types
        assert "expired" in types
        assert all("actor_id" not in event for event in history)

    def test_get_approval_data_records_viewed_event(
        self, client: TestClient, db_session, test_user
    ):
        """Opening the link lands a single anonymous `viewed` entry."""
        approval_request = self._make_request(
            db_session, test_user, status="pending", token="viewed-token-1"
        )

        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "viewed-token-1"},
        )
        assert response.status_code == 200
        assert "viewed" in [event["event_type"] for event in response.json()["history"]]

        # Second load is deduped: still exactly one viewed entry.
        client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "viewed-token-1"},
        )
        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "viewed-token-1"},
        )
        viewed_count = sum(
            1 for event in response.json()["history"] if event["event_type"] == "viewed"
        )
        assert viewed_count == 1
