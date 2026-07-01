"""Tests for account-wide tool usage stats aggregation."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models.crud import crud_api_usage, crud_runtime_session_activity
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services.tool_usage_stats import ToolUsageStatsService


def _log_gateway_with_tools_meta(db, *, account_id, user_id, tool_name, tokens=100):
    return crud_api_usage.log_gateway_request(
        db,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user_id),
        account_id=str(account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=tokens,
        completion_tokens=10,
        total_tokens=tokens + 10,
        estimated_cost=0.05,
        runtime_principal_type="managed_agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        meta_data={
            "tools_meta": [
                {
                    "name": tool_name,
                    "source": "payload",
                    "schema_tokens_estimate": tokens,
                    "stripped": False,
                }
            ]
        },
    )


def test_get_account_usage_by_tool_merges_invocations_and_schema_cost(
    db_session, test_user
):
    now = datetime.now(UTC)
    account_id = test_user.account_id
    runtime_session = RuntimeSession(
        id=uuid4(),
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id="workspace-stats",
        session_reference="workspace-stats",
        runtime_principal_type="managed_agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=now - timedelta(hours=2),
        last_activity_at=now - timedelta(hours=1),
    )
    db_session.add(runtime_session)
    db_session.flush()

    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=account_id,
        runtime_session_id=runtime_session.id,
        tool_name="search_issues",
        server_name="preloop",
        status="success",
        timestamp=now - timedelta(hours=1),
    )
    _log_gateway_with_tools_meta(
        db_session,
        account_id=account_id,
        user_id=test_user.id,
        tool_name="search_issues",
        tokens=120,
    )
    db_session.commit()

    rows = ToolUsageStatsService(db_session).get_account_usage_by_tool(
        account_id=str(account_id),
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(minutes=1),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.tool_name == "search_issues"
    assert row.invocation_count == 1
    assert row.schema_injections == 1
    assert row.schema_tokens_total == 120
    assert row.estimated_schema_cost > 0
    assert row.avg_cost_per_invocation > 0
    assert len(row.usage_by_agent) == 1
    assert row.usage_by_agent[0].invocation_count == 1
