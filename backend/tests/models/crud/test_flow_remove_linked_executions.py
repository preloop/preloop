"""Deleting a flow removes executions that point at each other.

A parked run points at the execution that resumed it
(``resume_execution_id``) and a retry points at the run it retried
(``retry_of_execution_id``). Neither foreign key cascades, so the flow
delete must clear the links before the executions are removed.
"""

import pytest

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from tests.models.crud.test_flow_execution_existing_ids import _make_flow


@pytest.mark.parametrize("link", ["resume_execution_id", "retry_of_execution_id"])
def test_flow_with_linked_executions_can_be_deleted(
    db_session, create_account, link: str
) -> None:
    account = create_account()
    flow = _make_flow(db_session, account)
    first = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="FAILED")
    )
    second = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="FAILED")
    )
    if link == "resume_execution_id":
        first.resume_execution_id = second.id
    else:
        second.retry_of_execution_id = first.id
    db_session.flush()
    ids = [str(first.id), str(second.id)]

    removed = crud_flow.remove(db=db_session, id=flow.id, account_id=account.id)

    assert removed is not None
    assert crud_flow_execution.existing_ids(db_session, ids) == set()
    assert crud_flow.get(db_session, id=flow.id) is None
