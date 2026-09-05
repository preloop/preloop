"""Existence check used by the log persister to detect orphaned log batches.

When log lines arrive for an execution row that no longer exists (e.g. the
flow was deleted while the agent was still streaming), the persister asks the
CRUD layer which execution ids are still present so it can drop the orphans
quietly instead of retrying a doomed FK-violating insert.
"""

import uuid

from preloop.models.crud import crud_ai_model, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _make_flow(db_session, account):
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Existing Ids Model {uuid.uuid4()}",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "provider-secret",
        },
        account_id=account.id,
    )
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"Existing Ids Flow {uuid.uuid4()}",
            prompt_template="Test",
            trigger_event_source="manual",
            trigger_event_types=["test"],
            ai_model_id=ai_model.id,
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account.id,
        ),
        account_id=account.id,
    )


def test_existing_ids_returns_only_live_executions(db_session, create_account):
    """Live ids come back in caller-supplied form; missing ids do not."""
    account = create_account()
    flow = _make_flow(db_session, account)
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    db_session.flush()

    live_id = str(execution.id)
    missing_id = str(uuid.uuid4())

    result = crud_flow_execution.existing_ids(db_session, [live_id, missing_id])

    assert result == {live_id}


def test_existing_ids_after_flow_delete_cascade(db_session, create_account):
    """A cascaded flow delete makes the execution id report as missing.

    This is the incident shape: the flow (and via cascade its execution) is
    deleted while an agent still streams logs for that execution id.
    """
    account = create_account()
    flow = _make_flow(db_session, account)
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    db_session.flush()
    execution_id = str(execution.id)

    assert crud_flow_execution.existing_ids(db_session, [execution_id]) == {
        execution_id
    }

    crud_flow.remove(db=db_session, id=flow.id, account_id=account.id)

    assert crud_flow_execution.existing_ids(db_session, [execution_id]) == set()


def test_existing_ids_skips_non_uuid_ids(db_session):
    """Ids that are not valid UUIDs cannot exist and are excluded, not fatal."""
    assert crud_flow_execution.existing_ids(db_session, ["not-a-uuid", None]) == set()


def test_existing_ids_empty_input(db_session):
    assert crud_flow_execution.existing_ids(db_session, []) == set()
