"""The executions collection bar filters and counts server-side.

The console page shows 25 rows over a window of thousands, so search, the
range and the total have to be answered by the query, not by the page the
browser happens to hold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Response

from preloop.api.endpoints import flows
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow_execution import TRIGGER_SUBJECT_KEY
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

from tests.conftest import maybe_await


def _create_flow(db_session, test_user, name):
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


def _create_execution(db_session, flow, *, subject=None, start_time=None):
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    if subject is not None:
        execution.trigger_event_details = {
            TRIGGER_SUBJECT_KEY: {"text": subject, "url": None}
        }
    if start_time is not None:
        execution.start_time = start_time
    db_session.add(execution)
    db_session.flush()
    return execution


@pytest.mark.asyncio
async def test_search_matches_flow_name_or_trigger_subject(db_session, test_user):
    """One search box over the two things a row is identified by."""
    reviewer = _create_flow(db_session, test_user, "Pull Request Reviewer")
    audit = _create_flow(db_session, test_user, "Release Security Audit")
    on_subject = _create_execution(
        db_session, audit, subject="preloop/preloop #138 · Pull Request Updated"
    )
    on_name = _create_execution(db_session, reviewer, subject="Scheduled")

    by_name = await maybe_await(
        flows.read_flow_executions(
            db=db_session, search="reviewer", current_user=test_user
        )
    )
    by_subject = await maybe_await(
        flows.read_flow_executions(db=db_session, search="#138", current_user=test_user)
    )

    assert [row.id for row in by_name] == [on_name.id]
    assert [row.id for row in by_subject] == [on_subject.id]


@pytest.mark.asyncio
async def test_started_after_narrows_the_window_and_total_counts_matches(
    db_session, test_user
):
    """The range pill is a filter, and `X-Total-Count` counts what it left."""
    flow = _create_flow(db_session, test_user, "Windowed Flow")
    now = datetime.now(timezone.utc)
    recent = _create_execution(db_session, flow, start_time=now - timedelta(hours=2))
    _create_execution(db_session, flow, start_time=now - timedelta(days=40))
    response = Response()

    rows = await maybe_await(
        flows.read_flow_executions(
            response=response,
            db=db_session,
            flow_id=flow.id,
            started_after=now - timedelta(days=30),
            current_user=test_user,
        )
    )

    assert [row.id for row in rows] == [recent.id]
    assert response.headers["X-Total-Count"] == "1"


@pytest.mark.asyncio
async def test_total_count_is_the_matched_rows_not_the_page(db_session, test_user):
    """ "25 of N" is only honest when N ignores the page window."""
    flow = _create_flow(db_session, test_user, "Paged Flow")
    for _ in range(3):
        _create_execution(db_session, flow)
    response = Response()

    rows = await maybe_await(
        flows.read_flow_executions(
            response=response,
            db=db_session,
            flow_id=flow.id,
            limit=2,
            current_user=test_user,
        )
    )

    assert len(rows) == 2
    assert response.headers["X-Total-Count"] == "3"
