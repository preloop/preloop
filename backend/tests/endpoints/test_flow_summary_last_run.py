"""The flows list's Last run cell comes from ``flows/summary`` (issue #1695).

The list stopped fetching ``flows/executions?limit=200``, which is where it
found the run to link and the status to print. The summary now carries the
last run of each flow, so the cell can state both again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from preloop.api.endpoints import flows
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _flow(db_session: Any, test_user: Any, name: str) -> Any:
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )


def _run(db_session: Any, flow: Any, *, status: str, started: datetime, text: str):
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status=status,
            trigger_event_details={
                "_subject": {
                    "text": text,
                    "url": "https://example.com/pr/1",
                    "ci": "GitHub Actions",
                    "ci_url": "https://example.com/actions/1",
                }
            },
        ),
    )
    execution.start_time = started
    db_session.flush()
    return execution


def test_last_runs_pick_the_latest_start_per_flow(db_session, test_user) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    busy = _flow(db_session, test_user, "Busy")
    idle = _flow(db_session, test_user, "Idle")
    _run(
        db_session,
        busy,
        status="SUCCEEDED",
        started=now - timedelta(days=3),
        text="old",
    )
    latest = _run(db_session, busy, status="FAILED", started=now, text="PR #9")
    _run(
        db_session,
        busy,
        status="SUCCEEDED",
        started=now - timedelta(hours=1),
        text="mid",
    )

    result = crud_flow_execution.get_last_runs_for_flows(db_session, [busy.id, idle.id])

    assert set(result) == {str(busy.id)}
    last = result[str(busy.id)]
    assert last["id"] == str(latest.id)
    assert last["status"] == "FAILED"
    assert last["trigger_subject"] == "PR #9"
    assert last["trigger_subject_url"] == "https://example.com/pr/1"
    assert last["trigger_subject_ci"] == "GitHub Actions"
    assert last["trigger_subject_ci_url"] == "https://example.com/actions/1"
    assert last["start_time"] == now.isoformat()
    assert crud_flow_execution.get_last_runs_for_flows(db_session, []) == {}


def test_summary_stats_carry_the_last_run(db_session, test_user) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    ran = _flow(db_session, test_user, "Ran")
    never = _flow(db_session, test_user, "Never")
    run = _run(db_session, ran, status="SUCCEEDED", started=now, text="PR #2")

    rows = flows.read_flow_summaries(
        db=db_session,
        skip=0,
        limit=100,
        include_stats=True,
        stats_since=now - timedelta(days=30),
        current_user=SimpleNamespace(
            account_id=test_user.account_id, id=test_user.id, is_superuser=True
        ),
    )
    by_id = {row.id: row for row in rows}
    assert by_id[ran.id].execution_stats["last_run"]["id"] == str(run.id)
    assert by_id[ran.id].execution_stats["last_run"]["status"] == "SUCCEEDED"
    assert by_id[never.id].execution_stats["last_run"] is None
