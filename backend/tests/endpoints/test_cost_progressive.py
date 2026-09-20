"""Selective Cost reads must preserve totals without running unused queries."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

import pytest
from preloop.models.crud import crud_api_usage
from preloop.services.tool_usage_stats import ToolUsageStatsService

BREAKDOWNS = {
    "models": "get_gateway_usage_by_model",
    "flows": "get_gateway_usage_by_flow",
    "sessions": "get_gateway_usage_by_session",
    "days": "get_gateway_usage_timeseries",
}


@pytest.mark.parametrize(
    "selected", [None, "models", "flows", "sessions", "days", "tools"]
)
def test_cost_queries_only_requested_breakdowns(
    client: Any, db_session: Any, test_user: Any, monkeypatch: Any, selected: str | None
) -> None:
    """Unselected aggregations must not execute, even for nonempty usage."""
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        model_alias="example-model",
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        estimated_cost=0.05,
    )
    spies = {}
    for name, method in BREAKDOWNS.items():
        spy = Mock(wraps=getattr(crud_api_usage, method))
        monkeypatch.setattr(crud_api_usage, method, spy)
        spies[name] = spy
    tool_spy = Mock(return_value=[])
    monkeypatch.setattr(ToolUsageStatsService, "get_account_usage_by_tool", tool_spy)
    params = {"breakdown": selected} if selected else {"include_breakdown": "false"}
    response = client.get("/api/v1/cost/summary", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["total_requests"] == 1
    assert body["token_usage"]["total_tokens"] == 20
    assert body["estimated_cost"] == 0.05
    for name, spy in {**spies, "tools": tool_spy}.items():
        assert spy.call_count == int(name == selected), name


def test_cost_rejects_unknown_breakdown(client: Any) -> None:
    """A typo must not accidentally run every expensive aggregation."""
    assert client.get("/api/v1/cost/summary?breakdown=unknown").status_code == 422


@pytest.mark.parametrize("mode", ["light", "sessions", "imported", "default"])
def test_imported_totals_remain_separate_while_details_are_deferred(
    client: Any, db_session: Any, test_user: Any, monkeypatch: Any, mode: str
) -> None:
    """First paint keeps imported spend visible without loading its detail tables."""
    crud_api_usage.log_imported_usage_event(
        db_session,
        account_id=str(test_user.account_id),
        timestamp=datetime.now(UTC),
        model_alias="example-model",
        source="example",
        total_tokens=100,
        cost_usd=2.5,
        conversation_id="example-conversation",
    )
    spies = []
    for method in ("get_imported_usage_by_model", "get_imported_usage_by_conversation"):
        spy = Mock(wraps=getattr(crud_api_usage, method))
        monkeypatch.setattr(crud_api_usage, method, spy)
        spies.append(spy)
    params = {
        "light": {"include_breakdown": "false", "breakdown": "imported"},
        "sessions": {"breakdown": "sessions"},
        "imported": {"breakdown": "imported"},
        "default": {},
    }[mode]
    response = client.get("/api/v1/cost/summary", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["estimated_cost"] == 0
    assert body["total_requests"] == 0
    assert body["imported_usage"]["imported_cost"] == 2.5
    assert body["imported_usage"]["total_tokens"] == 100
    included = mode in ("imported", "default")
    for spy in spies:
        assert spy.call_count == int(included)
    assert bool(body["imported_usage"]["usage_by_model"]) == included
    assert bool(body["imported_usage"]["usage_by_conversation"]) == included


def test_repeated_selection_preserves_requested_groups(
    client: Any, monkeypatch: Any
) -> None:
    """Repeatable query parameters request a union, with duplicates executed once."""
    spies = {}
    for name, method in BREAKDOWNS.items():
        spy = Mock(return_value=[])
        monkeypatch.setattr(crud_api_usage, method, spy)
        spies[name] = spy
    response = client.get(
        "/api/v1/cost/summary?breakdown=sessions&breakdown=flows&breakdown=sessions"
    )
    assert response.status_code == 200
    assert {name: spy.call_count for name, spy in spies.items()} == {
        "models": 0,
        "flows": 1,
        "sessions": 1,
        "days": 0,
    }
