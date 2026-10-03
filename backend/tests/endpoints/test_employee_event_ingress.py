"""Raw-body authenticity and scope tests using synthetic local requests."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints import employee_events as endpoint
from preloop.models.db.session import get_db_session
from preloop.models.schemas.flow import WebhookConfig
from preloop.services.employee_events import EmployeeEventReceipt


def test_employee_secret_survives_writes_but_is_redacted_from_responses():
    from datetime import UTC, datetime

    from preloop.models.schemas.flow import FlowCreate, FlowResponse, FlowUpdate

    secret = "synthetic-secret-at-least-thirty-two-characters"
    request = FlowCreate(
        name="Example employee",
        prompt_template="Handle the event",
        agent_config={},
        webhook_config={"employee_secret": secret},
    )
    stored = request.model_dump()
    assert stored["webhook_config"]["employee_secret"] == secret
    assert (
        FlowUpdate(**stored).model_dump()["webhook_config"]["employee_secret"] == secret
    )
    response = FlowResponse(
        **stored, id=uuid4(), created_at=datetime.now(UTC), updated_at=datetime.now(UTC)
    )
    assert "employee_secret" not in response.model_dump()["webhook_config"]
    assert secret not in response.model_dump_json()


def test_glitchtip_hmac_verification_and_bounded_payload(monkeypatch):
    account, flow_id = uuid4(), uuid4()
    secret = "synthetic-secret-at-least-thirty-two-characters"
    flow = SimpleNamespace(
        account_id=account,
        webhook_config=WebhookConfig(employee_secret=secret).model_dump(),
        trigger_config={
            "employee_events": {
                "source": "glitchtip",
                "connection_id": "connection-example",
            }
        },
    )
    monkeypatch.setattr(endpoint.crud_flow, "get", lambda *args, **kwargs: flow)
    intake = AsyncMock(
        return_value=EmployeeEventReceipt("execution-example", "PENDING", False)
    )
    monkeypatch.setattr(endpoint, "ingest_employee_event", intake)
    app = FastAPI()
    app.include_router(endpoint.router)
    app.dependency_overrides[get_db_session] = lambda: object()
    client = TestClient(app)
    body = json.dumps(
        {
            "data": {
                "project": {"id": "project-example"},
                "event": {"event_id": "event-example", "group_id": "issue-example"},
            }
        }
    ).encode()
    url = f"/employee-events/{flow_id}"
    assert client.post(url, content=body).status_code == 401
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Sentry-Hook-Signature": signature}
    assert client.post(url, content=body + b" ", headers=headers).status_code == 401
    response = client.post(url, content=body, headers=headers)
    assert response.status_code == 200 and response.json()["status"] == "PENDING"
    kwargs = intake.await_args.kwargs
    assert kwargs["account_id"] == account and kwargs["flow_id"] == flow_id
    assert kwargs["subject"] == "project:project-example:issue:issue-example"
    assert kwargs["event_id"] == "event-example"
    assert client.post(url, content=b"x" * 65537, headers=headers).status_code == 413
    intake.assert_awaited_once()


def test_application_registers_signed_ingress_without_bearer_schema():
    from preloop.api.app import create_app

    schema = create_app().openapi()
    operation = schema["paths"]["/api/v1/employee-events/{flow_id}"]["post"]
    assert not operation.get("security")
