"""Bind an Agent Control command onto a flow execution without a migration."""

import uuid

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.models.flow_execution import AGENT_CONTROL_BINDING_KEY
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _make_account(db: Session):
    account = crud_account.create(
        db,
        obj_in={
            "organization_name": f"control-bind-{uuid.uuid4().hex[:8]}",
            "is_active": True,
            "meta_data": {},
        },
    )
    db.commit()
    db.refresh(account)
    return account


def _make_flow(db: Session, account_id):
    flow = crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"control-bind-{uuid.uuid4().hex[:8]}",
            prompt_template="hello",
            trigger_event_source="github",
            trigger_event_types=["pull_request"],
            agent_type="codex",
            agent_config={
                "execution_path": "persistent",
                "target_agent_id": str(uuid.uuid4()),
            },
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account_id,
        ),
        account_id=account_id,
    )
    db.commit()
    db.refresh(flow)
    return flow


def test_bind_agent_control_command_stores_reserved_key(db_session: Session) -> None:
    account = _make_account(db_session)
    flow = _make_flow(db_session, account.id)
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="RUNNING",
            trigger_event_details={"source": "github"},
        ),
    )
    db_session.commit()
    command_id = str(uuid.uuid4())
    managed_agent_id = uuid.uuid4()
    history_session_id = uuid.uuid4()

    updated = crud_flow_execution.bind_agent_control_command(
        db_session,
        execution_id=execution.id,
        command_id=command_id,
        managed_agent_id=managed_agent_id,
        runtime_session_id=None,
        history_session_id=history_session_id,
        commit=True,
    )

    assert updated is not None
    binding = updated.trigger_event_details[AGENT_CONTROL_BINDING_KEY]
    assert binding["command_id"] == command_id
    assert binding["managed_agent_id"] == str(managed_agent_id)
    assert binding["history_session_id"] == str(history_session_id)
    assert updated.agent_session_reference == (
        f"control:{managed_agent_id}:{command_id}"
    )
    assert updated.trigger_event_details["source"] == "github"
