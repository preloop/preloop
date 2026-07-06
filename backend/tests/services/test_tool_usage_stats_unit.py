"""Unit tests for tool usage stats helpers and edge cases."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from preloop.models.models.ai_model import AIModel
from preloop.models.models.api_usage import ApiUsage
from preloop.services.tool_usage_stats import (
    ToolUsageStatsService,
    _SchemaCostAggregate,
    _coerce_int,
    _empty_tool_merge_entry,
    _per_prompt_token_price,
    _tools_meta,
)


def test_coerce_int_handles_valid_invalid_and_negative() -> None:
    assert _coerce_int(12) == 12
    assert _coerce_int("7") == 7
    assert _coerce_int(None) == 0
    assert _coerce_int("bad") == 0
    assert _coerce_int(-4) == 0


def test_tools_meta_filters_non_dict_entries() -> None:
    row = MagicMock(spec=ApiUsage)
    row.meta_data = {
        "tools_meta": [
            {"name": "search", "schema_tokens_estimate": 10},
            "skip-me",
            {"name": "list"},
        ]
    }
    assert _tools_meta(row) == [
        {"name": "search", "schema_tokens_estimate": 10},
        {"name": "list"},
    ]


def test_tools_meta_returns_none_for_missing_or_invalid_meta() -> None:
    row = MagicMock(spec=ApiUsage)
    row.meta_data = None
    assert _tools_meta(row) is None

    row.meta_data = {"tools_meta": "not-a-list"}
    assert _tools_meta(row) is None


def test_per_prompt_token_price_uses_model_when_available() -> None:
    row = MagicMock(spec=ApiUsage)
    row.prompt_tokens = 100
    row.estimated_cost = 1.0
    model = MagicMock(spec=AIModel)

    with patch(
        "preloop.services.tool_usage_stats.estimate_ai_model_usage_cost",
        return_value=0.002,
    ):
        price, priced = _per_prompt_token_price(row, model)

    assert price == 0.002
    assert priced is True


def test_per_prompt_token_price_falls_back_to_row_cost() -> None:
    row = MagicMock(spec=ApiUsage)
    row.prompt_tokens = 50
    row.estimated_cost = 0.25

    price, priced = _per_prompt_token_price(row, None)

    assert price == 0.005
    assert priced is True


def test_per_prompt_token_price_zero_when_no_tokens_or_cost() -> None:
    row = MagicMock(spec=ApiUsage)
    row.prompt_tokens = 0
    row.estimated_cost = 0.0

    price, priced = _per_prompt_token_price(row, None)

    assert price == 0.0
    assert priced is True


def test_empty_tool_merge_entry_defaults() -> None:
    entry = _empty_tool_merge_entry("search_issues", server_name="preloop")
    assert entry["tool_name"] == "search_issues"
    assert entry["server_name"] == "preloop"
    assert entry["invocation_count"] == 0
    assert entry["usage_by_agent"] == []


@patch.object(ToolUsageStatsService, "_aggregate_schema_costs")
@patch("preloop.services.tool_usage_stats.crud_runtime_session_activity")
def test_schema_only_tool_has_zero_avg_cost_per_invocation(
    mock_activity: MagicMock,
    mock_schema: MagicMock,
    db_session,
) -> None:
    mock_activity.get_tool_summary_for_account.return_value = []
    mock_activity.get_tool_invocations_by_agent_for_account.return_value = []
    mock_schema.return_value = {
        "search_issues": _SchemaCostAggregate(
            tool_name="search_issues",
            schema_injections=2,
            schema_tokens_total=200,
            estimated_schema_cost=0.42,
        )
    }

    rows = ToolUsageStatsService(db_session).get_account_usage_by_tool(
        account_id="acct-1",
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "search_issues"
    assert row.invocation_count == 0
    assert row.estimated_schema_cost == 0.42
    assert row.avg_cost_per_invocation == 0.0


@patch.object(ToolUsageStatsService, "_aggregate_schema_costs")
@patch("preloop.services.tool_usage_stats.crud_runtime_session_activity")
def test_returns_empty_when_no_invocations_or_schema(
    mock_activity: MagicMock,
    mock_schema: MagicMock,
    db_session,
) -> None:
    mock_activity.get_tool_summary_for_account.return_value = []
    mock_activity.get_tool_invocations_by_agent_for_account.return_value = []
    mock_schema.return_value = {}

    rows = ToolUsageStatsService(db_session).get_account_usage_by_tool(
        account_id="acct-1",
    )

    assert rows == []
