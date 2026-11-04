"""Small execution reads must not transfer saved workspace archives."""

import inspect
import os
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session

from preloop.api.endpoints import flows
from preloop.models import models
from preloop.models.crud import crud_flow_execution


@pytest.fixture
def saved_execution(db_session: Session) -> tuple:
    account = models.Account(organization_name="Example")
    db_session.add(account)
    db_session.flush()
    flow = models.Flow(
        account_id=account.id,
        name="Example",
        prompt_template="Test",
        agent_type="claude_code",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(
        flow_id=flow.id, status="SUCCEEDED", workspace_snapshot=os.urandom(1024 * 1024)
    )
    db_session.add(execution)
    db_session.flush()
    for index in range(3):
        db_session.add(
            models.FlowExecutionLog(
                execution_id=execution.id,
                log_type="agent_log_line",
                message=f"line-{index}",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
                + timedelta(seconds=index),
            )
        )
    db_session.flush()
    result = (execution.id, account.id, bytes(execution.workspace_snapshot))
    db_session.expunge_all()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_logs_do_not_load_workspace(
    db_session: Session, saved_execution: tuple, running: bool
) -> None:
    execution_id, account_id, _ = saved_execution
    if running:
        execution = crud_flow_execution.get(db_session, execution_id)
        execution.status = "RUNNING"
        execution.agent_session_reference = f"runner:{uuid4()}:{execution_id}"
        db_session.flush()
        db_session.expunge_all()
    statements = []

    def capture(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _params: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        result = await inspect.unwrap(flows.get_flow_execution_logs)(
            db=db_session,
            execution_id=execution_id,
            current_user=SimpleNamespace(account_id=account_id),
            tail=2,
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert [row["payload"]["message"] for row in result["logs"]] == ["line-1", "line-2"]
    assert result["source"] == "database"
    assert len(statements) == 2
    assert all("workspace_snapshot" not in sql for sql in statements)


def test_scoped_read_refuses_lazy_archive_and_preserves_download(
    db_session: Session, saved_execution: tuple
) -> None:
    execution_id, account_id, archive = saved_execution
    execution = crud_flow_execution.get_without_workspace(
        db_session, id=execution_id, account_id=account_id
    )
    assert "workspace_snapshot" not in execution.__dict__
    with pytest.raises(InvalidRequestError):
        _ = execution.workspace_snapshot
    assert (
        crud_flow_execution.get_without_workspace(
            db_session, id=execution_id, account_id=uuid4()
        )
        is None
    )
    assert (
        crud_flow_execution.get_without_workspace(
            db_session, id=uuid4(), account_id=account_id
        )
        is None
    )
    response = inspect.unwrap(flows.get_flow_execution_workspace)(
        db=db_session,
        execution_id=execution_id,
        current_user=SimpleNamespace(account_id=account_id),
    )
    assert response.body == archive


@pytest.mark.asyncio
async def test_logs_hide_other_accounts(
    db_session: Session, saved_execution: tuple
) -> None:
    execution_id, _, _ = saved_execution
    with pytest.raises(HTTPException) as error:
        await inspect.unwrap(flows.get_flow_execution_logs)(
            db=db_session,
            execution_id=execution_id,
            current_user=SimpleNamespace(account_id=uuid4()),
            tail=2,
        )
    assert error.value.status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "get_flow_execution_metrics",
        "get_flow_execution_gateway_events",
        "get_flow_execution_evidence_status",
        "get_flow_execution_evidence",
        "get_flow_execution_evidence_members",
    ],
)
def test_subresources_do_not_reload_workspace(
    db_session: Session, saved_execution: tuple, name: str
) -> None:
    execution_id, account_id, _ = saved_execution
    statements = []

    def capture(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _params: Any,
        _context: Any,
        _many: bool,
    ) -> None:
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        try:
            inspect.unwrap(getattr(flows, name))(
                db=db_session,
                execution_id=execution_id,
                current_user=SimpleNamespace(account_id=account_id),
            )
        except HTTPException as error:
            # This execution has no evidence pack; preserve its missing outcome.
            assert name in (
                "get_flow_execution_evidence",
                "get_flow_execution_evidence_members",
            )
            assert error.status_code == 404
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements
    assert all("workspace_snapshot" not in sql for sql in statements)
