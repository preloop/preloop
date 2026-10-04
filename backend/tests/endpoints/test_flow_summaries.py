"""Flow list summaries defer heavy configuration and optional aggregates."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Query, Session

from preloop.api.endpoints import flows
from preloop.models.crud.flow import CRUDFlow
from preloop.models.schemas.flow import FlowResponse


def summary_row(account_id: Any) -> SimpleNamespace:
    """A row that includes fields needed by list presentation."""
    return SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        name="Example flow",
        description="Scheduled work",
        icon="clock",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        trigger_event_source="schedule",
        trigger_event_types=["schedule"],
        ai_model_id=None,
        ai_model_name=None,
        agent_type="codex",
        is_enabled=False,
        is_preset=False,
        source_preset_id=None,
        prompt_customized=False,
        tools_customized=False,
        preset_update_available=False,
        schedule_config={"cron": "0 9 * * *", "timezone": "UTC"},
        # These are absent from the projected contract even if callers reuse
        # a populated identity-map row.
        prompt_template="p" * 100_000,
        agent_config={"private_configuration": "x" * 100_000},
        execution_stats={"total_execs": 999},
    )


def test_summary_omits_heavy_fields_and_stats_queries(mocker: Any) -> None:
    user = SimpleNamespace(account_id=uuid4())
    row = summary_row(user.account_id)
    read = mocker.patch.object(flows.crud_flow, "get_multi", return_value=[row])
    aggregate = mocker.patch.object(
        flows.crud_flow_execution, "get_execution_stats_for_flows"
    )
    db = MagicMock()
    result = flows.read_flow_summaries(db=db, skip=0, limit=100, current_user=user)
    read.assert_called_once_with(
        db,
        account_id=user.account_id,
        skip=0,
        limit=100,
        include_shared=True,
        lightweight=True,
    )
    aggregate.assert_not_called()
    data = result[0].model_dump(mode="json")
    assert (
        not {"prompt_template", "agent_config", "allowed_mcp_tools", "schedule_config"}
        & data.keys()
    )
    assert data["execution_stats"] is None
    # Schedule interpretation must match the established full response,
    # including legacy cron shapes and paused schedules.
    full = FlowResponse.model_validate(row)
    assert result[0].schedule_state == full.schedule_state


def test_summary_uses_owned_stats_and_same_window_projection(mocker: Any) -> None:
    user = SimpleNamespace(account_id=uuid4())
    owned, shared = summary_row(user.account_id), summary_row(uuid4())
    mocker.patch.object(flows.crud_flow, "get_multi", return_value=[owned, shared])
    aggregate = mocker.patch.object(
        flows.crud_flow_execution, "get_execution_stats_for_flows", return_value=[]
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db = MagicMock()
    result = flows.read_flow_summaries(
        db=db,
        skip=0,
        limit=100,
        include_stats=True,
        stats_since=start,
        current_user=user,
    )
    aggregate.assert_called_once_with(db, [owned.id], start_date=start)
    assert all(row.execution_stats["since"] == start.isoformat() for row in result)
    assert all(row.execution_stats["runs"] == 0 for row in result)


def test_summary_preserves_authorizer_visibility(mocker: Any) -> None:
    user = SimpleNamespace(account_id=uuid4())
    denied, allowed = summary_row(user.account_id), summary_row(user.account_id)
    mocker.patch.object(flows.crud_flow, "get_multi", return_value=[denied, allowed])
    visibility = mocker.patch.object(flows, "filter_viewable", return_value=[allowed])
    db = MagicMock()
    result = flows.read_flow_summaries(db=db, skip=0, limit=100, current_user=user)
    visibility.assert_called_once_with(db, user, flows.VISIBLE_FLOW, [denied, allowed])
    assert [row.id for row in result] == [allowed.id]


def test_lightweight_crud_sql_excludes_large_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    def capture(query: Query) -> list[Any]:
        statements.append(str(query.statement))
        return []

    monkeypatch.setattr(Query, "all", capture)
    with Session() as session:
        CRUDFlow().get_multi(session, account_id=str(uuid4()), lightweight=True)
    sql = statements[0]
    assert "flow.name" in sql
    assert "ai_model_1.name" in sql
    assert "flow.schedule_config" in sql
    for column in (
        "prompt_template",
        "agent_config",
        "allowed_mcp_tools",
        "git_clone_config",
        "review_instructions",
    ):
        assert f"flow.{column}" not in sql
