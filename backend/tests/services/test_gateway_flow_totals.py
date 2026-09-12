"""Flow totals must not inherit the limits of recent-activity lists."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from sqlalchemy.orm import Query, Session

from preloop.models import models
from preloop.models.crud import crud_api_usage
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.services.tool_usage_stats import ToolUsageStatsService


def test_account_flow_totals_include_groups_beyond_top_twenty() -> None:
    """The account summary retains quiet flows, with the same account/window scope."""
    account = models.Account(id=UUID(int=1), meta_data={})
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 2, 1, tzinfo=UTC)
    # Synthetic grouped database results: a quiet, expensive flow sorts last.
    rows = [
        SimpleNamespace(
            flow_id=UUID(int=index + 2),
            flow_name=f"Flow {index}",
            request_count=21 - index,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            estimated_cost=40.0 if index == 20 else 1.0,
        )
        for index in range(21)
    ]
    totals = {
        "request_count": 231,
        "success_count": 231,
        "error_count": 0,
        "prompt_tokens": 210,
        "completion_tokens": 105,
        "total_tokens": 315,
        "estimated_cost": 60.0,
    }
    queries: list[Query] = []

    def grouped_results(query: Query) -> list[SimpleNamespace]:
        queries.append(query)
        # Respect the actual SQL query's LIMIT without opening a connection.
        limit = query._limit_clause
        return rows if limit is None else rows[: limit.value]

    with (
        Session() as db,
        patch.object(Query, "all", grouped_results),
        patch.object(crud_api_usage, "get_gateway_usage_summary", return_value=totals),
        patch.object(crud_api_usage, "get_gateway_usage_by_model", return_value=[]),
        patch.object(crud_api_usage, "get_gateway_usage_by_session", return_value=[]),
        patch.object(crud_api_usage, "get_gateway_usage_timeseries", return_value=[]),
        patch.object(
            ToolUsageStatsService, "get_account_usage_by_tool", return_value=[]
        ),
    ):
        summary = ModelGatewayUsageService(db).get_account_summary(
            account=account,
            start_date=start,
            end_date=end,
            runtime_principal_id="principal-example",
        )

    assert len(summary.usage_by_flow) == 21
    assert summary.usage_by_flow[-1].estimated_cost == 40.0
    assert sum(row.estimated_cost for row in summary.usage_by_flow) == 60.0
    assert len(queries) == 1
    parameters = queries[0].statement.compile().params.values()
    assert str(account.id) in parameters
    assert start in parameters
    assert end in parameters
    assert "principal-example" in parameters
