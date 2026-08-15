"""Tests for webhook-triggered flows."""

import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy.orm import Session

from preloop.models import schemas
from preloop.models.crud import crud_flow
from preloop.api.endpoints.flows import router as flows_router
from fastapi.testclient import TestClient
from fastapi import FastAPI


@pytest.fixture
def app_with_flows():
    """Create a test app with flows router."""
    app = FastAPI()
    app.include_router(flows_router, prefix="/api")
    return app


@pytest.fixture
def test_client(app_with_flows):
    """Create a test client."""
    return TestClient(app_with_flows)


def test_create_webhook_flow(db_session: Session, test_user):
    """Test creating a flow with webhook trigger."""
    import secrets

    db = db_session
    # Generate webhook config like the endpoint does
    webhook_secret = secrets.token_urlsafe(32)
    webhook_config = schemas.WebhookConfig(webhook_secret=webhook_secret)

    flow_data = schemas.FlowCreate(
        name="Test Webhook Flow",
        description="A flow triggered by webhook",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],  # Use array field
        webhook_config=webhook_config,
        prompt_template="Test prompt: {{trigger_event.payload.message}}",
        agent_type="openhands",
        agent_config={"max_iterations": 10},
        allowed_mcp_servers=["preloop-mcp"],
        allowed_mcp_tools=[],
    )

    flow = crud_flow.create(db=db, flow_in=flow_data, account_id=test_user.account_id)

    assert flow.id is not None
    assert flow.trigger_event_source == "webhook"
    assert flow.trigger_event_types == ["webhook"]  # Use array field
    assert flow.webhook_config is not None
    assert "webhook_secret" in flow.webhook_config
    # Verify webhook secret is a secure token (at least 32 characters)
    assert len(flow.webhook_config["webhook_secret"]) >= 32


def test_webhook_secret_is_unique(db_session: Session, test_user):
    """Test that each flow gets a unique webhook secret."""
    import secrets

    db = db_session
    # Generate unique webhook configs
    webhook_secret1 = secrets.token_urlsafe(32)
    webhook_secret2 = secrets.token_urlsafe(32)

    flow1_data = schemas.FlowCreate(
        name="Webhook Flow 1",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],  # Use array field
        webhook_config=schemas.WebhookConfig(webhook_secret=webhook_secret1),
        prompt_template="Test prompt 1",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
    )

    flow2_data = schemas.FlowCreate(
        name="Webhook Flow 2",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],  # Use array field
        webhook_config=schemas.WebhookConfig(webhook_secret=webhook_secret2),
        prompt_template="Test prompt 2",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
    )

    flow1 = crud_flow.create(db=db, flow_in=flow1_data, account_id=test_user.account_id)
    flow2 = crud_flow.create(db=db, flow_in=flow2_data, account_id=test_user.account_id)

    # Verify each flow has a unique webhook secret
    assert (
        flow1.webhook_config["webhook_secret"] != flow2.webhook_config["webhook_secret"]
    )


@pytest.mark.asyncio
async def test_trigger_flow_via_webhook_success(db_session: Session, test_user):
    """Test successfully triggering a flow via webhook."""
    import secrets

    db = db_session
    # Generate webhook config
    webhook_secret = secrets.token_urlsafe(32)

    # Create a webhook flow
    flow_data = schemas.FlowCreate(
        name="Webhook Test Flow",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],  # Use array field
        webhook_config=schemas.WebhookConfig(webhook_secret=webhook_secret),
        prompt_template="Process: {{trigger_event.payload.data}}",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
        is_enabled=True,
    )

    flow = crud_flow.create(db=db, flow_in=flow_data, account_id=test_user.account_id)
    webhook_secret = flow.webhook_config["webhook_secret"]

    # Mock the FlowTriggerService
    with patch(
        "preloop.services.flow_trigger_service.FlowTriggerService"
    ) as mock_trigger_service:
        mock_service = AsyncMock()
        mock_trigger_service.return_value = mock_service
        execution_id = "11111111-2222-3333-4444-555555555555"
        mock_service.trigger_flow.return_value = {
            "id": execution_id,
            "status": "PENDING",
            "flow_id": str(flow.id),
        }

        # Create test client with proper database override
        from fastapi import FastAPI
        from preloop.api.endpoints.flows import router
        from preloop.models.db.session import get_db_session as get_db

        def override_get_db():
            try:
                yield db
            finally:
                pass

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)

        # Trigger the webhook
        response = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}",
            json={"data": "test payload"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "triggered"
        assert body["flow_id"] == str(flow.id)
        # The response must include the created execution's id so callers
        # (CI/cron bridges) can poll for completion.
        assert body["execution_id"] == execution_id
        assert body["execution_status"] == "PENDING"
        assert execution_id in body["execution_url"]

        # Verify the addressed flow was triggered directly (not generic
        # event matching, which can silently skip the flow)
        mock_service.trigger_flow.assert_awaited_once()
        call_kwargs = mock_service.trigger_flow.call_args.kwargs
        assert call_kwargs["flow_id"] == flow.id
        assert call_kwargs["test_mode"] is False
        event_data = call_kwargs["trigger_event_data"]
        assert event_data["source"] == "webhook"
        assert event_data["type"] == "webhook"
        assert event_data["payload"]["data"] == "test payload"


@pytest.mark.asyncio
async def test_trigger_flow_via_webhook_invalid_secret(db_session: Session, test_user):
    """Test triggering a webhook with invalid secret."""
    import secrets

    db = db_session
    # Create a webhook flow with a proper webhook_secret
    webhook_secret = secrets.token_urlsafe(32)
    flow_data = schemas.FlowCreate(
        name="Webhook Test Flow",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],  # Use array field
        webhook_config=schemas.WebhookConfig(webhook_secret=webhook_secret),
        prompt_template="Test",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
        is_enabled=True,
    )

    flow = crud_flow.create(db=db, flow_in=flow_data, account_id=test_user.account_id)

    from fastapi import FastAPI
    from preloop.api.endpoints.flows import router
    from preloop.models.db.session import get_db_session as get_db

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    # Try to trigger with invalid secret
    response = client.post(
        f"/webhooks/flows/{flow.id}/invalid_secret",
        json={"data": "test"},
    )

    assert response.status_code == 403
    assert "Invalid webhook secret" in response.json()["detail"]


@pytest.mark.asyncio
async def test_trigger_non_webhook_flow_via_webhook(
    db_session: Session, test_user, test_tracker
):
    """Test that non-webhook flows cannot be triggered via webhook endpoint."""
    db = db_session
    # Create a regular tracker-based flow
    flow_data = schemas.FlowCreate(
        name="Tracker Flow",
        trigger_event_source=str(test_tracker.id),
        trigger_event_types=["issue_created"],  # Use array field
        prompt_template="Test",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
    )

    flow = crud_flow.create(db=db, flow_in=flow_data, account_id=test_user.account_id)

    from fastapi import FastAPI
    from preloop.api.endpoints.flows import router
    from preloop.models.db.session import get_db_session as get_db

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    # Try to trigger via webhook endpoint
    response = client.post(
        f"/webhooks/flows/{flow.id}/any_secret",
        json={"data": "test"},
    )

    assert response.status_code == 400
    assert "not configured for webhook triggers" in response.json()["detail"]


@pytest.mark.asyncio
async def test_trigger_disabled_webhook_flow(db_session: Session, test_user):
    """Test that disabled webhook flows cannot be triggered."""
    import secrets

    db = db_session
    # Create a disabled webhook flow with proper webhook_secret
    webhook_secret = secrets.token_urlsafe(32)
    flow_data = schemas.FlowCreate(
        name="Disabled Webhook Flow",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],  # Use array field
        webhook_config=schemas.WebhookConfig(webhook_secret=webhook_secret),
        prompt_template="Test",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
        is_enabled=False,
    )

    flow = crud_flow.create(db=db, flow_in=flow_data, account_id=test_user.account_id)
    webhook_secret = flow.webhook_config["webhook_secret"]

    from fastapi import FastAPI
    from preloop.api.endpoints.flows import router
    from preloop.models.db.session import get_db_session as get_db

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    # Try to trigger disabled flow
    response = client.post(
        f"/webhooks/flows/{flow.id}/{webhook_secret}",
        json={"data": "test"},
    )

    assert response.status_code == 400
    assert "Flow is disabled" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_trigger_creates_execution_when_generic_matching_would_skip(
    db_session: Session, test_user
):
    """Regression test: a valid webhook call must create an execution even when
    generic event matching would silently skip the flow.

    Previously the endpoint routed through process_event(), which re-matched
    flows by trigger_event_types/trigger_config. A flow with a trigger_config
    filter not satisfied by the webhook payload was silently skipped while the
    endpoint still returned {"status": "triggered"} with no execution_id.
    """
    import secrets

    from preloop.models.crud.flow_execution import CRUDFlowExecution
    from preloop.services.flow_trigger_service import FlowTriggerService

    db = db_session
    webhook_secret = secrets.token_urlsafe(32)

    flow_data = schemas.FlowCreate(
        name="Webhook Flow With Unmatched Trigger Config",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],
        # This filter does not match the webhook payload below; the generic
        # matching path (process_event) silently drops the flow for it.
        trigger_config={"branch": "main"},
        webhook_config=schemas.WebhookConfig(webhook_secret=webhook_secret),
        prompt_template="Process: {{trigger_event.payload.data}}",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
        is_enabled=True,
    )
    flow = crud_flow.create(db=db, flow_in=flow_data, account_id=test_user.account_id)
    webhook_secret = flow.webhook_config["webhook_secret"]

    from fastapi import FastAPI
    from preloop.api.endpoints.flows import router
    from preloop.models.db.session import get_db_session as get_db

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    # Patch only the dispatch step so the execution record is still created
    # by the real trigger path, but no orchestrator/worker is started.
    with patch.object(
        FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
    ):
        response = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}",
            json={"data": "test payload"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "triggered"
    # No silent drop: an execution must exist and its id must be returned.
    assert "execution_id" in body, (
        "Webhook response must include execution_id so callers can poll"
    )
    crud_flow_execution = CRUDFlowExecution()
    execution = crud_flow_execution.get(db, id=body["execution_id"])
    assert execution is not None
    assert str(execution.flow_id) == str(flow.id)
    assert execution.trigger_event_details["payload"]["data"] == "test payload"


@pytest.mark.asyncio
async def test_webhook_trigger_reports_error_when_no_execution_created(
    db_session: Session, test_user
):
    """If no execution can be created, the webhook must NOT report success."""
    import secrets

    db = db_session
    webhook_secret = secrets.token_urlsafe(32)

    flow_data = schemas.FlowCreate(
        name="Webhook Flow Failing Execution Creation",
        trigger_event_source="webhook",
        trigger_event_types=["webhook"],
        webhook_config=schemas.WebhookConfig(webhook_secret=webhook_secret),
        prompt_template="Test",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
        is_enabled=True,
    )
    flow = crud_flow.create(db=db, flow_in=flow_data, account_id=test_user.account_id)
    webhook_secret = flow.webhook_config["webhook_secret"]

    from fastapi import FastAPI
    from preloop.api.endpoints.flows import router
    from preloop.models.db.session import get_db_session as get_db

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    with patch(
        "preloop.services.flow_trigger_service.FlowTriggerService"
    ) as mock_trigger_service:
        mock_service = AsyncMock()
        mock_trigger_service.return_value = mock_service
        mock_service.trigger_flow.side_effect = RuntimeError("db write failed")

        response = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}",
            json={"data": "test"},
        )

    assert response.status_code == 500
    body = response.json()
    assert "no execution could be created" in body["detail"]
    assert body.get("status") != "triggered"
