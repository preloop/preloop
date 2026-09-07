"""Tool statistics load only metadata needed for schema costs."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_usage
from preloop.services.tool_usage_stats import ToolUsageStatsService


def test_tool_statistics_exclude_unrelated_large_metadata(
    db_session: Session, create_account: Any
) -> None:
    account = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    tools = [{"name": "example_tool", "schema_tokens_estimate": 10}]
    db_session.add(
        models.ApiUsage(
            account_id=account.id,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            prompt_tokens=100,
            estimated_cost=1.0,
            timestamp=now,
            meta_data={"tools_meta": tools, "request": "x" * 1_000_000},
        )
    )
    db_session.flush()
    rows = crud_api_usage.list_gateway_tool_usage_in_window(
        db_session,
        account_id=account.id,
        start=now - timedelta(seconds=1),
        end=now + timedelta(seconds=1),
    )
    assert len(rows) == 1
    assert rows[0].meta_data == {"tools_meta": tools}
    assert not isinstance(rows[0], models.ApiUsage)
    costs = ToolUsageStatsService(db_session)._aggregate_schema_costs(
        account_id=str(account.id),
        start=now - timedelta(seconds=1),
        end=now + timedelta(seconds=1),
    )
    assert costs["example_tool"].schema_injections == 1
    assert costs["example_tool"].schema_tokens_total == 10
    assert costs["example_tool"].estimated_schema_cost == 0.1


def test_tool_projection_preserves_account_window_and_row_limit(
    db_session: Session, create_account: Any
) -> None:
    account = create_account()
    other = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    for owner, seconds in ((account, -1), (account, 0), (account, 1), (other, 0)):
        db_session.add(
            models.ApiUsage(
                account_id=owner.id,
                endpoint="/v1/chat/completions",
                method="POST",
                status_code=200,
                duration=0.1,
                action_type="model_gateway",
                timestamp=now + timedelta(seconds=seconds),
                prompt_tokens=seconds + 10,
            )
        )
    db_session.flush()
    rows = crud_api_usage.list_gateway_tool_usage_in_window(
        db_session,
        account_id=account.id,
        start=now - timedelta(seconds=1),
        end=now + timedelta(seconds=1),
        limit=1,
    )
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 10
    assert rows[0].meta_data == {"tools_meta": None}
