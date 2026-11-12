"""Metadata and attribution SQL must exclude retained workspace archives."""

import inspect
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import event, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import approval_requests as approvals
from preloop.api.endpoints import (
    flow_artifacts,
    mcp,
    publication_credentials,
    websockets,
)
from preloop.models import models
from preloop.models.crud import crud_flow_execution
from preloop.models.schemas.approval_request import ApprovalRequestResponse
from preloop.services import approval_attribution, dynamic_fastmcp_http


@pytest.fixture
def archived_world(db_session: Session) -> SimpleNamespace:
    account = models.Account(organization_name="Example")
    db_session.add(account)
    db_session.flush()
    flow = models.Flow(
        account_id=account.id,
        name="Example",
        prompt_template="Example",
        agent_type="codex",
        agent_config={},
    )
    workflow = models.ApprovalWorkflow(
        account_id=account.id, name="Example", approval_type="manual"
    )
    tool = models.ToolConfiguration(
        account_id=account.id, tool_name="example", tool_source="builtin"
    )
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="example",
        session_source_id="example",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add_all([flow, workflow, tool, session])
    db_session.flush()
    executions = []
    requests = []
    for _ in range(3):
        execution = models.FlowExecution(
            flow_id=flow.id,
            status="SUCCEEDED",
            workspace_snapshot=b"archive" * 150000,
            evidence_archive=b"evidence" * 150000,
            execution_logs=["log" * 100000],
            mcp_usage_logs=[{"message": "mcp" * 100000}],
            actions_taken_summary=[{"message": "action" * 100000}],
            result={"message": "result" * 100000},
        )
        db_session.add(execution)
        db_session.flush()
        request = models.ApprovalRequest(
            account_id=account.id,
            tool_configuration_id=tool.id,
            approval_workflow_id=workflow.id,
            tool_name="example",
            tool_args={},
            execution_id=str(execution.id),
            status="approved",
        )
        db_session.add(request)
        db_session.flush()
        executions.append(execution.id)
        requests.append(request.id)
    world = SimpleNamespace(
        account_id=account.id,
        flow_id=flow.id,
        session_id=session.id,
        executions=executions,
        requests=requests,
        user=SimpleNamespace(id=uuid4(), account_id=account.id, username="example"),
    )
    db_session.expunge_all()
    return world


@contextmanager
def captured_sql(db: Session) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(
        _conn: Any, _cursor: Any, sql: str, _params: Any, _context: Any, _many: bool
    ) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    connection = db.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", capture)


def _page(db: Session, world: SimpleNamespace) -> dict[str, Any]:
    response = Response()
    rows = inspect.unwrap(approvals.list_approval_requests)(
        response=response,
        status=None,
        execution_id=None,
        runtime_session_id=None,
        tool_name=None,
        q=None,
        limit=50,
        skip=0,
        current_user=world.user,
        db=db,
    )
    return {
        "total": response.headers["X-Total-Count"],
        "rows": [row.model_dump(mode="json") for row in rows],
    }


def test_approval_page_and_detail_match_full_rows_without_archive_sql(
    db_session: Session, archived_world: SimpleNamespace
) -> None:
    world = archived_world

    def full_lookup(db: Session, *, ids: list[Any], account_ids: list[Any]) -> dict:
        rows = db.execute(
            select(models.FlowExecution, models.Flow)
            .join(models.Flow)
            .where(
                models.FlowExecution.id.in_(ids),
                models.Flow.account_id.in_(account_ids),
            )
        ).all()
        return {execution.id: (execution, flow) for execution, flow in rows}

    with (
        patch.object(approvals, "ensure_permission_in_oss"),
        patch.object(approvals, "_record_viewed_event"),
    ):
        with patch.object(crud_flow_execution, "get_for_attribution", full_lookup):
            expected = _page(db_session, world)
            details = [
                inspect.unwrap(approvals.get_approval_request)(
                    request_id=rid, current_user=world.user, db=db_session
                ).model_dump(mode="json")
                for rid in world.requests
            ]
        db_session.expunge_all()
        with captured_sql(db_session) as statements:
            assert _page(db_session, world) == expected
            actual = [
                inspect.unwrap(approvals.get_approval_request)(
                    request_id=rid, current_user=world.user, db=db_session
                ).model_dump(mode="json")
                for rid in world.requests
            ]
        assert actual == details
    assert len(expected["rows"]) == 3
    assert all(
        row["flow_execution"]["flow_name"] == "Example" for row in expected["rows"]
    )
    for column in (
        "workspace_snapshot",
        "evidence_archive",
        "execution_logs",
        "mcp_usage_logs",
        "actions_taken_summary",
        "result",
    ):
        assert all(f"flow_execution.{column}" not in sql for sql in statements)


@pytest.mark.asyncio
async def test_async_attribution_executes_same_bounded_query(
    db_session: Session, archived_world: SimpleNamespace
) -> None:
    world = archived_world
    row = db_session.get(models.ApprovalRequest, world.requests[0])
    adapter = AsyncMock()
    adapter.execute.side_effect = db_session.execute
    with captured_sql(db_session) as statements:
        await approval_attribution.attributed_async(adapter, row)
        response = ApprovalRequestResponse.model_validate(row)
    assert response.flow_execution.flow_name == "Example"
    for column in (
        "workspace_snapshot",
        "evidence_archive",
        "execution_logs",
        "mcp_usage_logs",
        "actions_taken_summary",
        "result",
    ):
        assert all(f"flow_execution.{column}" not in sql for sql in statements)


@pytest.mark.parametrize("case", ["missing", "foreign", "empty_scope"])
def test_attribution_keeps_account_scope_and_missing_references(
    db_session: Session, archived_world: SimpleNamespace, case: str
) -> None:
    world = archived_world
    ids = [uuid4()] if case == "missing" else world.executions
    accounts = (
        []
        if case == "empty_scope"
        else [uuid4() if case == "foreign" else world.account_id]
    )
    assert (
        crud_flow_execution.get_for_attribution(
            db_session, ids=ids, account_ids=accounts
        )
        == {}
    )


@pytest.mark.parametrize(
    "case", ["ok", "missing", "foreign", "operation", "thread", "closed_put"]
)
def test_artifact_authorization_omits_archives(
    db_session: Session, archived_world: SimpleNamespace, case: str
) -> None:
    world = archived_world
    execution_id = uuid4() if case == "missing" else world.executions[0]
    operation = "put" if case == "closed_put" else "get"
    claims = dict(
        execution_id=execution_id,
        flow_id=world.flow_id,
        account_id=uuid4() if case == "foreign" else world.account_id,
        thread_id="other" if case == "thread" else str(execution_id),
        operation="put" if case == "operation" else operation,
    )
    with captured_sql(db_session) as statements:
        if case == "ok":
            flow_artifacts.authorize(db_session, claims, execution_id, operation)
        else:
            with pytest.raises(HTTPException) as error:
                flow_artifacts.authorize(db_session, claims, execution_id, operation)
            assert (
                error.value.status_code
                == {
                    "missing": 404,
                    "foreign": 404,
                    "operation": 403,
                    "thread": 403,
                    "closed_put": 409,
                }[case]
            )
    assert all("workspace_snapshot" not in sql for sql in statements)


@pytest.mark.parametrize("case", ["ok", "missing", "foreign", "context"])
def test_receipt_binding_omits_archives(
    db_session: Session, archived_world: SimpleNamespace, case: str
) -> None:
    world = archived_world
    execution_id = str(uuid4() if case == "missing" else world.executions[0])
    user = SimpleNamespace(
        account_id=uuid4() if case == "foreign" else world.account_id,
        _auth_api_key=SimpleNamespace(context_data={"flow_execution_id": execution_id}),
    )
    context = SimpleNamespace(
        flow_execution_id=str(uuid4()) if case == "context" else execution_id
    )
    with (
        patch.object(
            dynamic_fastmcp_http, "get_current_user_context", return_value=context
        ),
        captured_sql(db_session) as statements,
    ):
        assert mcp._review_receipt_execution_id(db_session, user) == (
            execution_id if case == "ok" else None
        )
    assert all("workspace_snapshot" not in sql for sql in statements)


@pytest.mark.parametrize(
    "case", ["ok", "missing", "foreign", "no_approvals", "no_sessions", "no_execution"]
)
def test_session_attach_preserves_visibility_without_archives(
    db_session: Session, archived_world: SimpleNamespace, case: str
) -> None:
    world = archived_world
    execution_id = (
        None
        if case == "no_execution"
        else str(uuid4() if case == "missing" else world.executions[0])
    )
    if case == "foreign":
        foreign = models.Account(organization_name="Other")
        db_session.add(foreign)
        db_session.flush()
        other = models.Flow(
            account_id=foreign.id,
            name="Other",
            prompt_template="Example",
            agent_config={},
        )
        db_session.add(other)
        db_session.flush()
        run = models.FlowExecution(flow_id=other.id)
        db_session.add(run)
        db_session.flush()
        execution_id = str(run.id)
    with (
        patch.object(
            websockets,
            "_session_read_allowed",
            side_effect=HTTPException(403) if case == "no_sessions" else None,
        ),
        patch.object(
            websockets, "_approval_read_allowed", return_value=case != "no_approvals"
        ),
        captured_sql(db_session) as statements,
    ):
        result = websockets._authorize_session_attach(
            db_session,
            user=world.user,
            runtime_session_id=str(world.session_id),
            execution_id=execution_id,
        )
    if case in {"missing", "foreign", "no_sessions"}:
        assert result["error"] == (403 if case == "no_sessions" else 404)
    else:
        assert result["approvals_visible"] == (case != "no_approvals")
    assert all("workspace_snapshot" not in sql for sql in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ok", "missing", "foreign"])
async def test_legacy_websocket_preserves_denials_without_archives(
    db_session: Session, archived_world: SimpleNamespace, case: str
) -> None:
    world = archived_world
    user = SimpleNamespace(**vars(world.user))
    if case == "foreign":
        user.account_id = uuid4()
    socket = SimpleNamespace(
        accept=AsyncMock(),
        scope={"state": {"user": user}},
        headers={},
        query_params={},
        send_json=AsyncMock(),
        close=AsyncMock(),
        receive_json=AsyncMock(side_effect=WebSocketDisconnect()),
    )

    async def run(fn: Any) -> Any:
        return fn(db_session)

    bus = SimpleNamespace(
        connect=AsyncMock(),
        nc=SimpleNamespace(
            subscribe=AsyncMock(return_value=SimpleNamespace(unsubscribe=AsyncMock())),
            flush=AsyncMock(),
        ),
        close=AsyncMock(),
    )
    with (
        patch.object(websockets, "run_db_async", run),
        patch.object(websockets, "EventBus", return_value=bus),
        captured_sql(db_session) as statements,
    ):
        await websockets.flow_execution_websocket(
            socket, uuid4() if case == "missing" else world.executions[0]
        )
    if case != "ok":
        assert socket.send_json.call_args.args[0] == {
            "error": "Execution not found"
            if case == "missing"
            else "Unauthorized - you do not have access to this flow execution"
        }
        socket.close.assert_called_once_with(code=1008)
    assert all("workspace_snapshot" not in sql for sql in statements)


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("race", [None, "stop", "terminal", "deleted", "account"])
def test_publication_refresh_rechecks_stale_identity_without_archives(
    db_session: Session,
    archived_world: SimpleNamespace,
    managed: bool,
    race: str | None,
) -> None:
    from sqlalchemy import delete, update
    from datetime import timedelta

    world = archived_world
    execution_id = world.executions[0]
    execution = crud_flow_execution.get_without_workspace(
        db_session, execution_id, account_id=world.account_id
    )
    execution.status = "RUNNING"
    db_session.flush()
    tracker = SimpleNamespace(
        oauth_installation=None,
        tracker_type="bitbucket",
        auth_type="managed_oauth",
        connection_details={"workspace": "example", "repository": "repo"},
    )
    claims = dict(
        execution_id=execution_id,
        account_id=world.account_id,
        tracker_id=uuid4(),
        repository_url="https://bitbucket.org/example/repo.git",
    )

    # Another Session changes DB state without updating this Session's identity map.
    async def issue(*args: Any, **kwargs: Any) -> SimpleNamespace:
        with Session(bind=db_session.connection()) as other:
            if race == "stop":
                other.execute(
                    update(models.FlowExecution)
                    .where(models.FlowExecution.id == execution_id)
                    .values(stop_requested_at=datetime.now(timezone.utc))
                )
            elif race == "terminal":
                other.execute(
                    update(models.FlowExecution)
                    .where(models.FlowExecution.id == execution_id)
                    .values(status="STOPPED")
                )
            elif race == "deleted":
                other.execute(
                    delete(models.FlowExecution).where(
                        models.FlowExecution.id == execution_id
                    )
                )
            elif race == "account":
                account = models.Account(organization_name="Other")
                other.add(account)
                other.flush()
                other.execute(
                    update(models.Flow)
                    .where(models.Flow.id == world.flow_id)
                    .values(account_id=account.id)
                )
            other.commit()
        assert execution.status == "RUNNING"
        assert execution.stop_requested_at is None
        return SimpleNamespace(
            token="synthetic",
            access_token="synthetic",
            git_username="x-token-auth",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    import asyncio

    with (
        patch.object(publication_credentials.crud_tracker, "get", return_value=tracker),
        patch.object(
            publication_credentials, "is_managed_tracker", return_value=managed
        ),
        patch.object(publication_credentials, "mint_repository_lease", issue),
        patch.object(publication_credentials, "resolve_managed_credential", issue),
        patch.object(
            publication_credentials.from_thread,
            "run",
            side_effect=lambda fn, *args: asyncio.run(fn(*args)),
        ),
        captured_sql(db_session) as statements,
    ):
        response = Response()
        if race:
            with pytest.raises(HTTPException) as error:
                publication_credentials.refresh_publication_credential(
                    execution_id, response, claims, db_session
                )
            assert (error.value.status_code, error.value.detail) == (
                409,
                "publication_execution_closed",
            )
        else:
            result = publication_credentials.refresh_publication_credential(
                execution_id, response, claims, db_session
            )
            assert result["token"] == "synthetic"
            assert response.headers["Cache-Control"] == "no-store"
    execution_selects = [sql for sql in statements if "FROM flow_execution" in sql]
    assert len(execution_selects) == 2
    assert all("workspace_snapshot" not in sql for sql in statements)


@pytest.mark.parametrize("preloaded", [False, True])
def test_attribution_projection_preserves_existing_identity_without_loading_payloads(
    db_session: Session, archived_world: SimpleNamespace, preloaded: bool
) -> None:
    world = archived_world
    existing = (
        crud_flow_execution.get(db_session, world.executions[0]) if preloaded else None
    )
    with captured_sql(db_session) as statements:
        runs = crud_flow_execution.get_for_attribution(
            db_session, ids=world.executions, account_ids=[world.account_id]
        )
        execution, flow = runs[world.executions[0]]
        assert execution.id == world.executions[0]
        assert execution.flow_id == world.flow_id
        assert flow.name == "Example"
        if preloaded:
            assert execution is existing
            assert execution.evidence_archive == b"evidence" * 150000
        else:
            for column in (
                "workspace_snapshot",
                "evidence_archive",
                "execution_logs",
                "mcp_usage_logs",
                "actions_taken_summary",
                "result",
            ):
                with pytest.raises(InvalidRequestError):
                    getattr(execution, column)
    assert len(statements) == 1
    for column in (
        "workspace_snapshot",
        "evidence_archive",
        "execution_logs",
        "mcp_usage_logs",
        "actions_taken_summary",
        "result",
    ):
        assert f"flow_execution.{column}" not in statements[0]
