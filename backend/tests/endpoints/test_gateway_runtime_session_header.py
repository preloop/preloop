"""Gateway responses name the runtime session they were attributed to (#1358)."""

from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.deps import get_budget_enforcer
from preloop.api.endpoints import anthropic_gateway, gemini_gateway, openai_gateway
from preloop.api.gateway_auth_dependency import get_model_gateway_auth_context
from preloop.models.db.session import get_db_session

SESSION_ID = "00000000-0000-4000-8000-0000000000aa"
HEADER = "X-Preloop-Runtime-Session-Id"


def _fake_db_session() -> MagicMock:
    return MagicMock()


def _fake_auth_context() -> MagicMock:
    context = MagicMock()
    context.gateway_subject = None
    return context


def _no_budget_enforcer() -> None:
    return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(openai_gateway.router, prefix="/openai/v1")
    app.include_router(anthropic_gateway.router, prefix="/anthropic/v1")
    app.include_router(gemini_gateway.router, prefix="/gemini/v1beta")
    app.dependency_overrides[get_db_session] = _fake_db_session
    for dep in (
        get_model_gateway_auth_context,
        anthropic_gateway.get_anthropic_gateway_auth_context,
        gemini_gateway.get_gemini_gateway_auth_context,
    ):
        app.dependency_overrides[dep] = _fake_auth_context
    app.dependency_overrides[get_budget_enforcer] = _no_budget_enforcer
    with TestClient(app) as test_client:
        yield test_client


def _service(session_id: Any) -> MagicMock:
    service = MagicMock()
    service.response_warning = None
    service.last_usage_id = None
    service.upstream_response_headers = None
    service.codex_turn_state = None
    service.resolved_runtime_session_id = session_id
    service.create_chat_completion.return_value = {"id": "chatcmpl-1"}
    service.create_response.return_value = {"id": "resp-1"}
    service.create_message.return_value = {"id": "msg-1"}
    service.generate_content.return_value = {"candidates": []}
    events = iter(["data: {}\n\n"])
    service.stream_chat_completion.return_value = events
    service.stream_message.return_value = iter(["data: {}\n\n"])
    service.stream_generate_content.return_value = iter(["data: {}\n\n"])
    return service


CASES = [
    ("openai", "/openai/v1/chat/completions", {"model": "m", "messages": []}),
    (
        "openai",
        "/openai/v1/chat/completions",
        {"model": "m", "messages": [], "stream": True},
    ),
    ("openai", "/openai/v1/responses", {"model": "m", "input": "hi"}),
    ("anthropic", "/anthropic/v1/messages", {"model": "m", "messages": []}),
    (
        "anthropic",
        "/anthropic/v1/messages",
        {"model": "m", "messages": [], "stream": True},
    ),
    ("gemini", "/gemini/v1beta/models/m:generateContent", {"contents": []}),
    ("gemini", "/gemini/v1beta/models/m:streamGenerateContent", {"contents": []}),
]


def _post(client: TestClient, kind: str, url: str, body: dict, service: Any) -> Any:
    target = {
        "openai": (openai_gateway, "OpenAIGatewayService"),
        "anthropic": (anthropic_gateway, "OpenAIGatewayService"),
        "gemini": (gemini_gateway, "GeminiGatewayService"),
    }[kind]
    with patch.object(*target, return_value=service):
        return client.post(url, headers={"Authorization": "Bearer x"}, json=body)


@pytest.mark.parametrize(("kind", "url", "body"), CASES)
def test_session_attributed_request_carries_header(
    client: TestClient, kind: str, url: str, body: dict
) -> None:
    response = _post(client, kind, url, body, _service(SESSION_ID))
    assert response.status_code == 200, response.text
    assert response.headers[HEADER] == SESSION_ID


@pytest.mark.parametrize(("kind", "url", "body"), CASES)
def test_unattributed_request_has_no_header(
    client: TestClient, kind: str, url: str, body: dict
) -> None:
    response = _post(client, kind, url, body, _service(None))
    assert response.status_code == 200, response.text
    assert HEADER not in response.headers
