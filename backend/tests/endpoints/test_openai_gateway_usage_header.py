"""The OpenAI gateway returns the usage row id on non-streaming responses."""

from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.deps import get_budget_enforcer
from preloop.api.endpoints import openai_gateway
from preloop.api.gateway_auth_dependency import get_model_gateway_auth_context
from preloop.models.db.session import get_db_session

USAGE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(openai_gateway.router, prefix="/openai/v1")
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: MagicMock()
    app.dependency_overrides[get_budget_enforcer] = lambda: None
    with TestClient(app) as test_client:
        yield test_client


def _post(client: TestClient, service: Any) -> Any:
    with patch.object(openai_gateway, "OpenAIGatewayService", return_value=service):
        return client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": "Bearer ignored"},
            json={"model": "azure/chat-deployment", "messages": []},
        )


def test_chat_completion_returns_usage_id_header(client: TestClient) -> None:
    service = MagicMock()
    service.response_warning = None
    service.last_usage_id = USAGE_ID
    service.create_chat_completion.return_value = {"id": "chatcmpl-1"}

    response = _post(client, service)

    assert response.status_code == 200
    assert response.headers["X-Preloop-Usage-Id"] == USAGE_ID
    assert "X-Preloop-Warning" not in response.headers
    assert response.json() == {"id": "chatcmpl-1"}


def test_usage_id_and_warning_are_both_returned(client: TestClient) -> None:
    service = MagicMock()
    service.response_warning = "budget not enforced"
    service.last_usage_id = USAGE_ID
    service.create_chat_completion.return_value = {"id": "chatcmpl-1"}

    response = _post(client, service)

    assert response.headers["X-Preloop-Usage-Id"] == USAGE_ID
    assert response.headers["X-Preloop-Warning"] == "budget not enforced"


def test_no_usage_header_when_nothing_was_recorded(client: TestClient) -> None:
    service = MagicMock()
    service.response_warning = None
    service.last_usage_id = None
    service.create_chat_completion.return_value = {"id": "chatcmpl-1"}

    response = _post(client, service)

    assert response.status_code == 200
    assert "X-Preloop-Usage-Id" not in response.headers
