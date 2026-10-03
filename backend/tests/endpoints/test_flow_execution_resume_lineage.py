"""List and detail expose resume_of and chain resume_totals."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from preloop.api.endpoints import flows
from preloop.models import models, schemas
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.execution_metrics import project_resume_lineage

from tests.conftest import maybe_await


def _create_flow(db_session, test_user, name="Resume Lineage Flow"):
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


def _execution(
    db_session,
    flow,
    *,
    total_tokens: int,
    estimated_cost: float,
    resume_root: str | None = None,
):
    details = {"event": "test"}
    if resume_root is not None:
        details["_resume"] = {
            "execution_id": resume_root,
            "resume_root": resume_root,
            "thread_id": str(uuid.uuid4()),
        }
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            trigger_event_details=details,
        ),
    )
    execution.total_tokens = total_tokens
    execution.estimated_cost = estimated_cost
    db_session.flush()
    return execution


@pytest.mark.asyncio
async def test_list_and_detail_project_resume_chain(db_session, test_user):
    flow = _create_flow(db_session, test_user)
    publisher = _execution(db_session, flow, total_tokens=1000, estimated_cost=0.10)
    repair = _execution(
        db_session,
        flow,
        total_tokens=400,
        estimated_cost=0.04,
        resume_root=str(publisher.id),
    )
    unrelated = _execution(db_session, flow, total_tokens=50, estimated_cost=0.01)

    rows = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    by_id = {
        str(row.id): schemas.FlowExecutionListResponse.model_validate(row)
        for row in rows
    }

    assert by_id[str(repair.id)].resume_of == publisher.id
    assert by_id[str(repair.id)].resume_totals is not None
    assert by_id[str(repair.id)].resume_totals.total_tokens == 1400
    assert by_id[str(repair.id)].resume_totals.estimated_cost == pytest.approx(0.14)

    assert by_id[str(publisher.id)].resume_of is None
    assert by_id[str(publisher.id)].resume_totals is not None
    assert by_id[str(publisher.id)].resume_totals.total_tokens == 1400

    assert by_id[str(unrelated.id)].resume_of is None
    assert by_id[str(unrelated.id)].resume_totals is None

    detail = await maybe_await(
        flows.read_flow_execution(
            db=db_session, execution_id=repair.id, current_user=test_user
        )
    )
    detail_row = schemas.FlowExecutionResponse.model_validate(detail)
    assert detail_row.resume_of == publisher.id
    assert detail_row.resume_totals is not None
    assert detail_row.resume_totals.total_tokens == 1400
    assert detail_row.resume_totals.estimated_cost == pytest.approx(0.14)
    navigation = detail_row.continuation_navigation
    assert navigation is not None
    assert navigation.original_execution_id == publisher.id
    assert [row.id for row in navigation.follow_ups] == [repair.id]

    publisher_detail = await maybe_await(
        flows.read_flow_execution(
            db=db_session, execution_id=publisher.id, current_user=test_user
        )
    )
    publisher_navigation = schemas.FlowExecutionResponse.model_validate(
        publisher_detail
    ).continuation_navigation
    assert publisher_navigation == navigation


def test_lightweight_lineage_does_not_load_trigger_payload(
    db_session, test_user
) -> None:
    """Rollup on a lightweight list row must not lazy-load the trigger JSONB."""
    flow = _create_flow(db_session, test_user, name="Resume Payload Flow")
    publisher = _execution(db_session, flow, total_tokens=1000, estimated_cost=0.10)
    _execution(
        db_session,
        flow,
        total_tokens=400,
        estimated_cost=0.04,
        resume_root=str(publisher.id),
    )
    db_session.flush()
    db_session.expire_all()
    rows = crud_flow_execution.get_multi(
        db_session,
        account_id=test_user.account_id,
        flow_id=flow.id,
        eager_load=True,
        lightweight=True,
        limit=100,
    )
    assert rows
    for row in rows:
        assert "trigger_event_details" in sa_inspect(row).unloaded
    project_resume_lineage(db_session, rows, account_id=test_user.account_id)
    for row in rows:
        assert "trigger_event_details" in sa_inspect(row).unloaded


def test_tree_rows_project_resume_of(db_session, test_user) -> None:
    """Delegation-tree rows can be serialized, including a projected resume_of."""
    flow = _create_flow(db_session, test_user, name="Resume Tree Flow")
    publisher = _execution(db_session, flow, total_tokens=10, estimated_cost=0.01)
    child = _execution(
        db_session,
        flow,
        total_tokens=5,
        estimated_cost=0.01,
        resume_root=str(publisher.id),
    )
    child.parent_execution_id = publisher.id
    child.root_execution_id = publisher.id
    db_session.flush()
    db_session.expire_all()

    rows = crud_flow_execution.get_lineage(
        db_session,
        root_execution_id=publisher.id,
        account_id=test_user.account_id,
    )
    assert [row.id for row in rows] == [child.id]
    assert "trigger_event_details" in sa_inspect(rows[0]).unloaded
    node = schemas.ExecutionTreeNode.model_validate(rows[0])
    assert node.resume_of == publisher.id


def test_continuation_navigation_is_account_scoped(db_session, test_user) -> None:
    """An unowned publisher cannot be exposed through a forged resume root."""
    flow = _create_flow(db_session, test_user)
    publisher = _execution(db_session, flow, total_tokens=1, estimated_cost=0)
    rows = crud_flow_execution.get_continuation_navigation(
        db_session, root_id=publisher.id, account_id=uuid.uuid4()
    )
    assert rows == []


def test_continuation_navigation_bounds_long_chains(db_session, test_user) -> None:
    """The detail projection loads at most 100 repairs plus an overflow row."""
    from preloop.services.flow_continuation_navigation import (
        project_continuation_navigation,
    )

    flow = _create_flow(db_session, test_user)
    publisher = _execution(db_session, flow, total_tokens=1, estimated_cost=0)
    for _ in range(105):
        _execution(
            db_session,
            flow,
            total_tokens=1,
            estimated_cost=0,
            resume_root=str(publisher.id),
        )
    publisher_id = publisher.id
    db_session.expire_all()
    rows = crud_flow_execution.get_continuation_navigation(
        db_session, root_id=publisher_id, account_id=test_user.account_id
    )
    assert len(rows) == 102
    assert rows[0].id == publisher_id
    assert all(not hasattr(row, "execution_logs") for row in rows)
    project_continuation_navigation(
        db_session, publisher, account_id=test_user.account_id
    )
    navigation = publisher.continuation_navigation
    assert navigation["follow_ups_truncated"] is True
    assert len(navigation["follow_ups"]) == 100


def test_navigation_reads_only_url_fields(
    db_session: Session, test_user: models.User
) -> None:
    """Large trigger/result documents are never selected for chain links."""
    from sqlalchemy import event

    flow = _create_flow(db_session, test_user)
    publisher = _execution(db_session, flow, total_tokens=1, estimated_cost=0)
    publisher.trigger_event_details = {
        "payload": {
            "issue": {"html_url": "https://github.com/org/repo/issues/1"},
            "unused": "x" * 1_000_000,
        }
    }
    publisher.result = {
        "pr_url": "https://github.com/org/repo/pull/2",
        "unused": "y" * 1_000_000,
    }
    publisher_id = publisher.id
    account_id = test_user.account_id
    db_session.flush()
    db_session.expire_all()
    statements: list[str] = []

    def record_query(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", record_query)
    try:
        rows = crud_flow_execution.get_continuation_navigation(
            db_session, root_id=publisher_id, account_id=account_id
        )
    finally:
        event.remove(connection, "before_cursor_execute", record_query)

    assert len(rows) == 1
    assert rows[0].issue_html_url == "https://github.com/org/repo/issues/1"
    assert rows[0].result_pr_url == "https://github.com/org/repo/pull/2"
    assert not hasattr(rows[0], "trigger_event_details")
    assert not hasattr(rows[0], "result")
    assert len(statements) == 1
    selected = statements[0].split("FROM", 1)[0]
    assert "flow_execution.trigger_event_details AS" not in selected
    assert "flow_execution.result AS" not in selected


@pytest.mark.parametrize("issue", [None, {}, [], False, 0, ""])
def test_navigation_empty_issue_uses_merge_request_attributes(
    db_session: Session, test_user: models.User, issue: Any
) -> None:
    """The SQL choice preserves all falsy JSON values accepted by old data."""
    from preloop.services.flow_continuation_navigation import (
        project_continuation_navigation,
    )

    flow = _create_flow(db_session, test_user)
    publisher = _execution(db_session, flow, total_tokens=1, estimated_cost=0)
    publisher.trigger_event_details = {
        "payload": {
            "issue": issue,
            "object_attributes": {"web_url": "https://gitlab.com/org/repo/issues/1"},
        }
    }
    db_session.flush()

    project_continuation_navigation(
        db_session, publisher, account_id=test_user.account_id
    )

    assert publisher.continuation_navigation["issue_url"] == (
        "https://gitlab.com/org/repo/issues/1"
    )


@pytest.mark.parametrize("issue", [{"html_url": "javascript:bad"}, "invalid", 1])
def test_navigation_rejects_invalid_primary_issue_link(
    db_session: Session, test_user: models.User, issue: Any
) -> None:
    """Truthy invalid issue data must not fall through to another subject."""
    from preloop.services.flow_continuation_navigation import (
        project_continuation_navigation,
    )

    flow = _create_flow(db_session, test_user)
    publisher = _execution(db_session, flow, total_tokens=1, estimated_cost=0)
    publisher.trigger_event_details = {
        "payload": {
            "issue": issue,
            "object_attributes": {"web_url": "https://gitlab.com/org/repo/issues/1"},
        },
        "_resume": {"pr_url": "https://gitlab.com/org/repo/merge_requests/2"},
    }
    publisher.result = {"pr_url": "javascript:bad"}
    db_session.flush()

    project_continuation_navigation(
        db_session, publisher, account_id=test_user.account_id
    )

    assert publisher.continuation_navigation["issue_url"] is None
    assert publisher.continuation_navigation["pr_url"] is None
