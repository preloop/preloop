"""Real employee credentials retain Flow tools and owned native-policy identity."""

from datetime import UTC, datetime
from uuid import uuid4
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.requests import HTTPConnection
from sqlalchemy.orm import sessionmaker

from preloop.api.endpoints import agent_permission
from preloop.models.crud import (
    crud_api_key,
    crud_flow,
    crud_flow_execution,
    crud_managed_agent,
    crud_runtime_session,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_runtime_token import create_flow_runtime_token
from preloop.services.mcp_http import PreloopBearerAuthBackend
from preloop.services import mcp_http
from preloop.services import dynamic_fastmcp


@pytest.mark.asyncio
async def test_employee_token_authenticates_native_and_mcp_with_restricted_identity(
    db_session,
    test_user,
    monkeypatch,
):
    agent = crud_managed_agent.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "owner_user_id": test_user.id,
            "agent_kind": "codex",
            "session_source_type": "codex",
            "session_source_id": f"employee-{uuid4()}",
            "display_name": "Example employee",
            "lifecycle_state": "active",
            "lifecycle_updated_at": datetime.now(UTC),
            "last_seen_at": datetime.now(UTC),
        },
    )
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Employee credential fixture",
            agent_type="codex",
            prompt_template="Synthetic task",
            account_id=test_user.account_id,
            agent_config={
                "execution_path": "persistent",
                "target_agent_id": str(agent.id),
            },
            trigger_config={"employee_events": {"source": "discord"}},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[{"name": "get_issue"}],
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    session = crud_runtime_session.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "session_source_type": "flow_execution",
            "session_source_id": str(execution.id),
            "started_at": datetime.now(UTC),
        },
    )
    token, _ = create_flow_runtime_token(
        db_session, flow=flow, execution_id=execution.id, runtime_session_id=session.id
    )
    assert token
    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(agent_permission, "get_session_factory", lambda: factory)
    identity = agent_permission._resolve_permission_identity(token)
    assert identity.managed_agent_id == agent.id
    assert identity.runtime_session_id == session.id
    assert identity.account_id == str(test_user.account_id)
    key = crud_api_key.get_by_key(db_session, key=token)
    assert key.scopes == ["mcp:read", "mcp:write"]
    assert key.context_data["flow_execution_id"] == str(execution.id)
    assert key.context_data["allowed_mcp_tools"] == [{"name": "get_issue"}]
    monkeypatch.setattr(mcp_http, "get_db", lambda: iter([factory()]))
    connection = HTTPConnection(
        {
            "type": "http",
            "path": "/mcp/v1",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
        }
    )
    authenticated = await PreloopBearerAuthBackend().authenticate(connection)
    assert authenticated is not None
    _, auth = authenticated
    monkeypatch.setattr(dynamic_fastmcp, "get_db", lambda: iter([factory()]))
    context = dynamic_fastmcp.create_user_context_from_scope({"user": auth})
    assert context.managed_agent_id == str(agent.id)
    assert context.flow_execution_id == str(execution.id)
    assert context.allowed_flow_tools == ["get_issue"]
    # Native origin/session strings cannot turn this credential into another
    # agent's recorded session, even when that session is in the same account.
    foreign_session = crud_runtime_session.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "session_source_type": "codex",
            "session_source_id": "foreign-native-example",
            "started_at": datetime.now(UTC),
            "runtime_principal_type": "codex",
            "runtime_principal_id": "foreign-agent",
        },
    )
    assert (
        agent_permission._origin_runtime_session_id(
            identity, "codex", foreign_session.session_source_id
        )
        is None
    )
    decide = AsyncMock(return_value=("allow", "Synthetic scoped decision", None, False))
    monkeypatch.setattr(agent_permission, "request_agent_permission", decide)
    claim_note = Mock(return_value=None)
    monkeypatch.setattr(agent_permission, "_claim_operator_note", claim_note)
    result = await agent_permission.agent_permission_check(
        agent_permission.AgentPermissionCheckRequest(
            source="codex_cli",
            tool_name="shell",
            tool_input={"command": "echo example"},
            session_id=foreign_session.session_source_id,
        ),
        authorization=f"Bearer {token}",
    )
    assert result.decision == "allow"
    assert claim_note.call_args.kwargs["origin_session_id"] is None
    assert decide.await_args.kwargs["managed_agent_id"] == agent.id
    assert decide.await_args.kwargs["runtime_session_id"] == session.id
    assert (
        "runtime_session_id"
        not in decide.await_args.kwargs["tool_input"]["_preloop_origin"]
    )
    crud_managed_agent.update(
        db_session, db_obj=agent, obj_in={"lifecycle_state": "suspended"}
    )
    with pytest.raises(HTTPException) as exc:
        agent_permission._resolve_permission_identity(token)
    assert exc.value.status_code == 401
    assert await PreloopBearerAuthBackend().authenticate(connection) is None
    crud_managed_agent.update(
        db_session, db_obj=agent, obj_in={"lifecycle_state": "active"}
    )
    crud_api_key.deactivate(db_session, key_id=key.id)
    with pytest.raises(HTTPException) as exc:
        agent_permission._resolve_permission_identity(token)
    assert exc.value.status_code == 401
    assert await PreloopBearerAuthBackend().authenticate(connection) is None
