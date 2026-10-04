"""Account summary reads must execute only the requested aggregates."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints import account
from preloop.services import model_gateway_usage as usage


@pytest.fixture
def projection_client(mocker: Any) -> tuple[TestClient, dict[str, Any]]:
    owner = SimpleNamespace(id=uuid4(), meta_data={})
    user = SimpleNamespace(id=uuid4(), account_id=owner.id)
    mocker.patch.object(
        usage,
        "restrict_history_window",
        side_effect=lambda db, **kwargs: (kwargs["start_date"], kwargs["end_date"]),
    )
    mocker.patch.object(
        usage.crud_api_usage,
        "get_gateway_usage_summary",
        return_value={
            "request_count": 7,
            "success_count": 6,
            "error_count": 1,
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
            "estimated_cost": 0.05,
        },
    )
    methods = {
        "models": "get_gateway_usage_by_model",
        "flows": "get_gateway_usage_by_flow",
        "sessions": "get_gateway_usage_by_session",
        "days": "get_gateway_usage_timeseries",
    }
    spies = {
        name: mocker.patch.object(usage.crud_api_usage, method, return_value=[])
        for name, method in methods.items()
    }
    spies["tools"] = mocker.patch.object(
        usage.ToolUsageStatsService, "get_account_usage_by_tool", return_value=[]
    )
    app = FastAPI()
    app.include_router(account.router, prefix="/api/v1")
    app.dependency_overrides[account.get_account_for_user] = lambda: owner
    app.dependency_overrides[account.get_current_active_user] = lambda: user
    app.dependency_overrides[account.get_db_session] = lambda: mocker.MagicMock()
    return TestClient(app), spies


@pytest.mark.parametrize(
    ("query", "selected"),
    [
        ("", {"models", "flows", "sessions", "tools", "days"}),
        ("?include_breakdown=false", set()),
        ("?breakdown=models", {"models"}),
        ("?breakdown=flows&breakdown=sessions&breakdown=flows", {"flows", "sessions"}),
        ("?include_breakdown=false&breakdown=tools", set()),
    ],
)
def test_account_projection_executes_selected_queries(
    projection_client: tuple[TestClient, dict[str, Any]],
    query: str,
    selected: set[str],
) -> None:
    client, spies = projection_client
    response = client.get(f"/api/v1/account/gateway-usage/summary{query}")
    assert response.status_code == 200
    body = response.json()
    assert body["total_requests"] == 7
    assert body["token_usage"]["total_tokens"] == 20
    assert body["estimated_cost"] == 0.05
    for name, spy in spies.items():
        assert spy.call_count == int(name in selected), name


@pytest.mark.parametrize("section", ["unknown", "imported"])
def test_account_projection_rejects_unsupported_sections(
    projection_client: tuple[TestClient, dict[str, Any]], section: str
) -> None:
    client, spies = projection_client
    response = client.get(f"/api/v1/account/gateway-usage/summary?breakdown={section}")
    assert response.status_code == 422
    assert all(spy.call_count == 0 for spy in spies.values())
