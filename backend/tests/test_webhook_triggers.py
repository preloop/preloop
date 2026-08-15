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


def _make_client(db: Session) -> TestClient:
    """Build a TestClient with the flows router and a db override."""
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
    return TestClient(app)


def _executions_for_flow(db: Session, flow_id):
    from preloop.models.models.flow_execution import FlowExecution

    return db.query(FlowExecution).filter(FlowExecution.flow_id == flow_id).all()


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

    from preloop.services.flow_trigger_service import FlowTriggerService

    client = _make_client(db)

    # Patch only the dispatch step: the real trigger path creates the
    # execution row; no orchestrator/worker is started.
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
    assert body["flow_id"] == str(flow.id)
    assert body["deduplicated"] is False

    # The response must include the created execution's id so callers
    # (CI/cron bridges) can poll for completion.
    execution_id = body["execution_id"]
    assert body["execution_status"] == "PENDING"
    assert execution_id in body["execution_url"]
    # Nested execution object mirrors the /flows/{flow_id}/trigger contract.
    assert body["execution"] == {
        "id": execution_id,
        "status": "PENDING",
        "flow_id": str(flow.id),
    }

    # The addressed flow was triggered directly: a real execution row exists
    # carrying the webhook payload as trigger event details.
    executions = _executions_for_flow(db, flow.id)
    assert len(executions) == 1
    execution = executions[0]
    assert str(execution.id) == execution_id
    details = execution.trigger_event_details
    assert details["source"] == "webhook"
    assert details["type"] == "webhook"
    assert details["payload"]["data"] == "test payload"
    assert details["test_mode"] is False


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


def _create_webhook_flow(db, test_user, name, **overrides):
    import secrets

    webhook_secret = secrets.token_urlsafe(32)
    fields = {
        "name": name,
        "trigger_event_source": "webhook",
        "trigger_event_types": ["webhook"],
        "webhook_config": schemas.WebhookConfig(webhook_secret=webhook_secret),
        "prompt_template": "Process: {{trigger_event.payload.data}}",
        "agent_type": "openhands",
        "agent_config": {},
        "allowed_mcp_servers": [],
        "allowed_mcp_tools": [],
        "is_enabled": True,
    }
    fields.update(overrides)
    flow_data = schemas.FlowCreate(**fields)
    flow = crud_flow.create(db=db, flow_in=flow_data, account_id=test_user.account_id)
    return flow, flow.webhook_config["webhook_secret"]


@pytest.mark.asyncio
async def test_webhook_trigger_creates_execution_when_generic_matching_would_skip(
    db_session: Session, test_user
):
    """Regression test: a valid webhook call must create an execution even when
    generic event matching would silently skip the flow.

    Previously the endpoint routed through process_event(), which re-matched
    flows by trigger_event_types. A flow whose trigger_event_types did not
    contain "webhook" was silently skipped while the endpoint still returned
    {"status": "triggered"} with no execution_id. The payload below DOES
    satisfy the flow's trigger_config, so the direct path must execute it.
    """
    from preloop.services.flow_trigger_service import FlowTriggerService

    db = db_session
    flow, webhook_secret = _create_webhook_flow(
        db,
        test_user,
        "Webhook Flow Generic Matching Would Skip",
        # Generic matching (process_event) requires "webhook" in
        # trigger_event_types; this flow would be silently dropped there.
        trigger_event_types=["push"],
        trigger_config={"branch": "main"},
    )

    client = _make_client(db)

    # Patch only the dispatch step so the execution record is still created
    # by the real trigger path, but no orchestrator/worker is started.
    with patch.object(
        FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
    ):
        response = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}",
            json={"branch": "main", "data": "test payload"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "triggered"
    assert body["deduplicated"] is False
    # No silent drop: an execution must exist and its id must be returned.
    assert "execution_id" in body, (
        "Webhook response must include execution_id so callers can poll"
    )
    executions = _executions_for_flow(db, flow.id)
    assert len(executions) == 1
    execution = executions[0]
    assert str(execution.id) == body["execution_id"]
    assert execution.trigger_event_details["payload"]["data"] == "test payload"


@pytest.mark.asyncio
async def test_webhook_trigger_config_mismatch_returns_422(
    db_session: Session, test_user
):
    """A payload that fails the addressed flow's trigger_config policy must be
    rejected with an explicit 422 — neither silent success (the old generic
    path) nor silent execution (ignoring the filter)."""
    from preloop.services.flow_trigger_service import FlowTriggerService

    db = db_session
    flow, webhook_secret = _create_webhook_flow(
        db,
        test_user,
        "Webhook Flow With Unmatched Trigger Config",
        trigger_config={"branch": "main"},
    )

    client = _make_client(db)

    with patch.object(
        FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
    ) as mock_dispatch:
        response = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}",
            json={"branch": "feature", "data": "test"},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "trigger_config" in detail
    assert "No execution was created" in detail
    # Policy rejection: nothing was executed and no row exists.
    mock_dispatch.assert_not_awaited()
    assert _executions_for_flow(db, flow.id) == []


@pytest.mark.asyncio
async def test_webhook_double_delivery_deduplicates_on_commit_sha(
    db_session: Session, test_user
):
    """At-least-once webhook redelivery for the same commit must not create a
    duplicate execution: the second delivery returns 200 with the EXISTING
    execution_id and "deduplicated": true (never a bare skip).

    Uses the real trigger_flow(); only worker dispatch is patched out.
    """
    from preloop.services.flow_trigger_service import FlowTriggerService

    db = db_session
    flow, webhook_secret = _create_webhook_flow(
        db, test_user, "Webhook Flow Dedup On Commit SHA"
    )

    client = _make_client(db)
    payload = {"sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", "data": "ci run"}

    with patch.object(
        FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
    ):
        first = client.post(f"/webhooks/flows/{flow.id}/{webhook_secret}", json=payload)
        second = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}", json=payload
        )

    assert first.status_code == 200
    first_body = first.json()
    assert first_body["status"] == "triggered"
    assert first_body["deduplicated"] is False

    assert second.status_code == 200
    second_body = second.json()
    assert second_body["deduplicated"] is True
    # The existing execution is returned, not a new one.
    assert second_body["execution_id"] == first_body["execution_id"]
    assert second_body["execution"]["id"] == first_body["execution_id"]

    # Exactly one execution row exists for the flow.
    executions = _executions_for_flow(db, flow.id)
    assert len(executions) == 1
    assert str(executions[0].id) == first_body["execution_id"]

    # A different commit for the same flow is NOT deduplicated.
    with patch.object(
        FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
    ):
        third = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}",
            json={"sha": "ffff111122223333444455556666777788889999", "data": "x"},
        )
    assert third.status_code == 200
    assert third.json()["deduplicated"] is False
    assert len(_executions_for_flow(db, flow.id)) == 2


@pytest.mark.asyncio
async def test_webhook_trigger_returns_202_when_dispatch_fails_after_commit(
    db_session: Session, test_user
):
    """If the execution row is committed but dispatch fails afterwards, the
    endpoint must return 202 with the execution id — not a 500 claiming that
    no execution was created (which would invite duplicating retries).

    Uses the real trigger_flow(); the failure is injected below the
    transaction boundary (worker dispatch), after the row is committed.
    """
    from preloop.services.flow_trigger_service import FlowTriggerService

    db = db_session
    flow, webhook_secret = _create_webhook_flow(
        db, test_user, "Webhook Flow Dispatch Failure"
    )

    client = _make_client(db)
    payload = {"sha": "0123456789abcdef0123456789abcdef01234567", "data": "ci"}

    with patch.object(
        FlowTriggerService,
        "_start_flow_execution",
        new_callable=AsyncMock,
        side_effect=RuntimeError("NATS publish failed"),
    ):
        response = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}", json=payload
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert "could not be dispatched" in body["detail"]

    # The committed row is reported honestly with its id.
    executions = _executions_for_flow(db, flow.id)
    assert len(executions) == 1
    execution = executions[0]
    assert str(execution.id) == body["execution_id"]
    assert execution.status == "PENDING"
    assert body["execution"]["id"] == str(execution.id)

    # A redelivery of the same payload dedups against the committed PENDING
    # row instead of creating a duplicate.
    with patch.object(
        FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
    ):
        retry = client.post(f"/webhooks/flows/{flow.id}/{webhook_secret}", json=payload)
    assert retry.status_code == 200
    retry_body = retry.json()
    assert retry_body["deduplicated"] is True
    assert retry_body["execution_id"] == str(execution.id)
    assert len(_executions_for_flow(db, flow.id)) == 1


@pytest.mark.asyncio
async def test_webhook_trigger_reports_error_when_no_execution_created(
    db_session: Session, test_user
):
    """If no execution row can be created (failure at/before the insert), the
    webhook must NOT report success and must not leave any execution row."""
    db = db_session
    flow, webhook_secret = _create_webhook_flow(
        db, test_user, "Webhook Flow Failing Execution Creation"
    )

    client = _make_client(db)

    # Fail below trigger_flow() at the row insert itself, so the real
    # endpoint + service code runs and no row is ever committed.
    with patch(
        "preloop.services.flow_trigger_service.crud_flow_execution.create",
        side_effect=RuntimeError("db write failed"),
    ):
        response = client.post(
            f"/webhooks/flows/{flow.id}/{webhook_secret}",
            json={"data": "test"},
        )

    assert response.status_code == 500
    body = response.json()
    assert "no execution could be created" in body["detail"]
    assert body.get("status") != "triggered"
    # Matches the wording: truly no execution row exists.
    assert _executions_for_flow(db, flow.id) == []
