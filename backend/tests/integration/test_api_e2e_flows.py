"""End-to-end integration tests for Preloop backend API flows.

These tests exercise full API flows using TestClient with real DB (no external
services). Use client, db_session, test_user from conftest. Tests do NOT use
@pytest.mark.integration since they don't require external services.

Requires DATABASE_URL (e.g. from .env or docker-compose). Run:
    pytest backend/tests/integration/test_api_e2e*.py -v
"""

import time
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.models.db.session import get_db_session
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.user import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_e2e(db_session: Session, test_user: User):
    """Create FastAPI app for E2E tests with dependency overrides."""

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app = create_app()
    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    return app


@pytest.fixture
def client_e2e(app_e2e):
    """TestClient for E2E tests (authenticated as test_user)."""
    with TestClient(app_e2e) as client:
        yield client


@pytest.fixture
def app_auth_only(db_session: Session):
    """Create FastAPI app for auth E2E tests - no user override (public auth endpoints)."""

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app = create_app()
    app.dependency_overrides[get_db_session] = override_get_db
    return app


@pytest.fixture
def client_auth(app_auth_only):
    """TestClient for auth flow - no auth override for register/login/refresh."""
    with TestClient(app_auth_only) as client:
        yield client


# ---------------------------------------------------------------------------
# Auth E2E: Register → Login → Token Refresh
# ---------------------------------------------------------------------------


class TestAuthE2EFlow:
    """E2E tests for auth flow: register new user, login, token refresh."""

    @patch("preloop.api.auth.router.complete_new_account_setup_background")
    def test_register_login_refresh_flow(
        self, mock_setup, client_auth: TestClient, db_session: Session
    ):
        """Full auth flow: register new user → login → token refresh."""
        unique_suffix = str(int(time.time() * 1000000))[-8:]
        username = f"e2euser{unique_suffix}"
        email = f"e2e{unique_suffix}@example.com"
        password = "securepass123"

        # 1. Register new user
        register_resp = client_auth.post(
            "/api/v1/auth/register",
            json={
                "username": username,
                "email": email,
                "password": password,
                "full_name": "E2E Test User",
            },
        )
        assert register_resp.status_code == 201, register_resp.text
        data = register_resp.json()
        assert data["username"] == username
        assert data["email"] == email

        # 2. Login (use /token/json for JSON body)
        login_resp = client_auth.post(
            "/api/v1/auth/token/json",
            json={"username": username, "password": password},
        )
        assert login_resp.status_code == 200, login_resp.text
        token_data = login_resp.json()
        assert "access_token" in token_data
        assert "refresh_token" in token_data
        token_data["access_token"]
        refresh_token = token_data["refresh_token"]

        # 3. Token refresh
        refresh_resp = client_auth.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert refresh_resp.status_code == 200, refresh_resp.text
        new_tokens = refresh_resp.json()
        assert "access_token" in new_tokens
        assert "refresh_token" in new_tokens

        # 4. Verify new access token works (call protected endpoint)
        me_resp = client_auth.get(
            "/api/v1/auth/users/me",
            headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
        )
        assert me_resp.status_code == 200, me_resp.text
        me_data = me_resp.json()
        assert me_data["username"] == username


# ---------------------------------------------------------------------------
# Tracker E2E: Create → List → Get → Update → Delete
# ---------------------------------------------------------------------------


class TestTrackerE2EFlow:
    """E2E tests for tracker CRUD flow via API."""

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.trackers.send_tracker_registered_email")
    @patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
    @patch("preloop.api.endpoints.trackers.create_tracker_client")
    def test_tracker_full_crud_flow(
        self,
        mock_create_client,
        mock_publish_task,
        mock_send_email,
        client_e2e: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Full tracker flow: create → list → get by ID → update → delete."""
        mock_tracker_client = AsyncMock()
        mock_tracker_client.test_connection.return_value = AsyncMock(connected=True)
        mock_create_client.return_value = mock_tracker_client

        # 1. Create tracker
        create_resp = client_e2e.post(
            "/api/v1/trackers",
            json={
                "name": "E2E Test Tracker",
                "type": "jira",
                "url": "https://test-jira.example.com",
                "api_key": "test_api_key",
                "config": {"username": "testuser"},
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        create_data = create_resp.json()
        tracker_id = create_data["id"]
        assert tracker_id

        # 2. List trackers
        list_resp = client_e2e.get("/api/v1/trackers")
        assert list_resp.status_code == 200, list_resp.text
        trackers = list_resp.json()
        assert any(t["id"] == tracker_id for t in trackers)
        tracker = next(t for t in trackers if t["id"] == tracker_id)
        assert tracker["name"] == "E2E Test Tracker"

        # 3. Get by ID
        get_resp = client_e2e.get(f"/api/v1/trackers/{tracker_id}")
        assert get_resp.status_code == 200, get_resp.text
        get_data = get_resp.json()
        assert get_data["id"] == tracker_id
        assert get_data["name"] == "E2E Test Tracker"

        # 4. Update
        update_resp = client_e2e.put(
            f"/api/v1/trackers/{tracker_id}",
            json={"name": "E2E Updated Tracker"},
        )
        assert update_resp.status_code == 200, update_resp.text
        update_data = update_resp.json()
        assert update_data["name"] == "E2E Updated Tracker"

        # 5. Delete
        delete_resp = client_e2e.delete(f"/api/v1/trackers/{tracker_id}")
        assert delete_resp.status_code == 200, delete_resp.text

        # 6. Verify soft-deleted (get returns 404 or tracker marked deleted)
        get_after_resp = client_e2e.get(f"/api/v1/trackers/{tracker_id}")
        assert get_after_resp.status_code == 404, get_after_resp.text


# ---------------------------------------------------------------------------
# Approval E2E: Workflow → Tool Config → Approval Request → Approve/Decline
# ---------------------------------------------------------------------------


class TestApprovalE2EFlow:
    """E2E tests for approval workflow: create workflow, tool config, request, approve/decline."""

    def test_approval_workflow_approve_flow(
        self,
        client_e2e: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Create workflow → create tool config with workflow → simulate request → approve."""
        # 1. Create approval workflow
        workflow_resp = client_e2e.post(
            "/api/v1/approval-workflows",
            json={
                "name": f"E2E Workflow {uuid.uuid4().hex[:8]}",
                "approval_type": "manual",
                "timeout_seconds": 300,
            },
        )
        assert workflow_resp.status_code == 201, workflow_resp.text
        workflow_data = workflow_resp.json()
        workflow_id = workflow_data["id"]

        # 2. Create tool config with approval workflow
        config_resp = client_e2e.post(
            "/api/v1/tool-configurations",
            json={
                "tool_name": f"e2e_test_tool_{uuid.uuid4().hex[:8]}",
                "tool_source": "builtin",
                "account_id": str(test_user.account_id),
                "approval_workflow_id": workflow_id,
            },
        )
        assert config_resp.status_code == 201, config_resp.text
        config_data = config_resp.json()
        config_id = config_data["id"]

        # 3. Simulate approval request (create via model - no public API for this)
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=uuid.UUID(config_id),
            approval_workflow_id=uuid.UUID(workflow_id),
            tool_name=config_data["tool_name"],
            tool_args={"arg1": "value1"},
            agent_reasoning="E2E test approval request",
            status="pending",
        )
        db_session.add(approval_request)
        db_session.commit()
        db_session.refresh(approval_request)
        request_id = str(approval_request.id)

        # 4. List approval requests
        list_resp = client_e2e.get("/api/v1/approval-requests")
        assert list_resp.status_code == 200, list_resp.text
        requests_list = list_resp.json()
        assert any(r["id"] == request_id for r in requests_list)

        # 5. Get approval request
        get_resp = client_e2e.get(f"/api/v1/approval-requests/{request_id}")
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["status"] == "pending"

        # 6. Approve via CRUD (approve API uses get_async_db_session which can't
        # see our test data; full API approve would need get_async_db override)
        approval_request.status = "approved"
        approval_request.resolved_at = datetime.now(UTC)
        approval_request.approver_comment = "Approved for E2E test"
        db_session.add(approval_request)
        db_session.flush()

        # Verify approval via list API
        list_after = client_e2e.get("/api/v1/approval-requests")
        assert list_after.status_code == 200
        approved = next((r for r in list_after.json() if r["id"] == request_id), None)
        assert approved is not None
        assert approved["status"] == "approved"

    def test_approval_workflow_decline_flow(
        self,
        client_e2e: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Create workflow → tool config → request → decline."""
        # 1. Create approval workflow
        workflow_resp = client_e2e.post(
            "/api/v1/approval-workflows",
            json={
                "name": f"E2E Decline Workflow {uuid.uuid4().hex[:8]}",
                "approval_type": "manual",
                "timeout_seconds": 300,
            },
        )
        assert workflow_resp.status_code == 201, workflow_resp.text
        workflow_id = workflow_resp.json()["id"]

        # 2. Create tool config
        config_resp = client_e2e.post(
            "/api/v1/tool-configurations",
            json={
                "tool_name": f"e2e_decline_tool_{uuid.uuid4().hex[:8]}",
                "tool_source": "builtin",
                "account_id": str(test_user.account_id),
                "approval_workflow_id": workflow_id,
            },
        )
        assert config_resp.status_code == 201, config_resp.text
        config_data = config_resp.json()
        config_id = config_data["id"]

        # 3. Create approval request
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=uuid.UUID(config_id),
            approval_workflow_id=uuid.UUID(workflow_id),
            tool_name=config_data["tool_name"],
            tool_args={"arg1": "value1"},
            agent_reasoning="E2E decline test",
            status="pending",
        )
        db_session.add(approval_request)
        db_session.commit()
        db_session.refresh(approval_request)
        request_id = str(approval_request.id)

        # 4. Decline via CRUD (approve/decline API uses async session which can't
        # see our test data; full API test would need get_async_db override)
        approval_request.status = "declined"
        approval_request.resolved_at = datetime.now(UTC)
        approval_request.approver_comment = "Declined for E2E test"
        db_session.add(approval_request)
        db_session.flush()

        # Verify decline via list API
        list_after = client_e2e.get("/api/v1/approval-requests")
        assert list_after.status_code == 200
        declined = next((r for r in list_after.json() if r["id"] == request_id), None)
        assert declined is not None
        assert declined["status"] == "declined"


# ---------------------------------------------------------------------------
# Flow E2E: Create → List → Get → Update → Delete
# ---------------------------------------------------------------------------


class TestFlowE2EFlow:
    """E2E tests for flow CRUD: create, list, get, update, delete."""

    def test_flow_crud_flow(
        self,
        client_e2e: TestClient,
        test_user: User,
    ):
        """Create flow → list → get by ID → update → delete."""
        flow_name = f"E2E Flow {uuid.uuid4().hex[:8]}"

        # 1. Create flow (ai_model_id optional; use webhook trigger for simplicity)
        create_resp = client_e2e.post(
            "/api/v1/flows",
            json={
                "name": flow_name,
                "description": "E2E test flow",
                "trigger_event_source": "webhook",
                "trigger_event_types": ["webhook"],
                "prompt_template": "Test prompt for E2E",
                "agent_type": "openhands",
                "agent_config": {"agent_type": "CodeActAgent"},
                "allowed_mcp_servers": [],
                "allowed_mcp_tools": [],
            },
        )
        assert create_resp.status_code in (200, 201), create_resp.text
        create_data = create_resp.json()
        flow_id = create_data["id"]
        assert flow_id
        assert create_data["name"] == flow_name

        # 2. List flows
        list_resp = client_e2e.get("/api/v1/flows")
        assert list_resp.status_code == 200, list_resp.text
        flows = list_resp.json()
        assert any(f["id"] == flow_id for f in flows)

        # 3. Get by ID
        get_resp = client_e2e.get(f"/api/v1/flows/{flow_id}")
        assert get_resp.status_code == 200, get_resp.text
        get_data = get_resp.json()
        assert get_data["id"] == flow_id
        assert get_data["name"] == flow_name

        # 4. Update
        update_resp = client_e2e.put(
            f"/api/v1/flows/{flow_id}",
            json={
                "name": f"{flow_name} Updated",
                "description": "Updated description",
                "trigger_event_source": "webhook",
                "trigger_event_types": ["webhook"],
                "prompt_template": "Updated prompt",
                "agent_type": "openhands",
                "agent_config": {"agent_type": "CodeActAgent"},
                "allowed_mcp_servers": [],
                "allowed_mcp_tools": [],
            },
        )
        assert update_resp.status_code == 200, update_resp.text
        update_data = update_resp.json()
        assert update_data["name"] == f"{flow_name} Updated"

        # 5. Delete
        delete_resp = client_e2e.delete(f"/api/v1/flows/{flow_id}")
        assert delete_resp.status_code == 200, delete_resp.text

        # 6. Verify deleted (get returns 404)
        get_after_resp = client_e2e.get(f"/api/v1/flows/{flow_id}")
        assert get_after_resp.status_code == 404, get_after_resp.text


# ---------------------------------------------------------------------------
# MCP Tools E2E: List tools (with trackers)
# ---------------------------------------------------------------------------


class TestMCPToolsE2EFlow:
    """E2E tests for MCP tools listing (requires trackers for built-in tools)."""

    def test_list_tools_without_trackers(self, client_e2e: TestClient):
        """List tools when account has no trackers - may return empty or minimal list."""
        resp = client_e2e.get("/api/v1/tools")
        assert resp.status_code == 200, resp.text
        tools = resp.json()
        assert isinstance(tools, list)

    def test_list_approval_workflows(self, client_e2e: TestClient):
        """List approval workflows."""
        resp = client_e2e.get("/api/v1/approval-workflows")
        assert resp.status_code == 200, resp.text
        workflows = resp.json()
        assert isinstance(workflows, list)
