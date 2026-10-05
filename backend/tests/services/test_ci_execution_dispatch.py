"""CI dispatch and delegation cannot bypass admission before runtime launch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.services import ci_execution, flow_execution_runner
from preloop.services.flow_delegation_call import (
    DelegationRefusedError,
    evaluate_delegation,
)
from tests.api.test_ci_execution import review_binding
from tests.api.test_ci_principal import ci_resources as create_ci_resources
from tests.api.test_ci_principal import provision


@pytest.mark.asyncio
async def test_dispatch_denial_persists_failed_without_orchestrator_side_effects(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resources = create_ci_resources.__wrapped__(db_session)
    _, _, token = provision(db_session, resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    execution_id = execution.id

    async def deny_admission(*args: object, **kwargs: object) -> None:
        db_session.expire_all()
        raise PermissionError("synthetic-private-error")

    admission = AsyncMock(side_effect=deny_admission)
    monkeypatch.setattr(ci_execution, "ensure_ci_dispatch_admission", admission)
    run = AsyncMock()
    await flow_execution_runner.run_existing_execution(
        SimpleNamespace(db=db_session, execution_log=execution, run=run)
    )
    run.assert_not_awaited()
    failed = crud.crud_flow_execution.get(db_session, id=execution_id)
    assert failed.status == "FAILED"
    assert failed.failure_category == "verification_blocked"
    assert failed.error_message == "Restricted CI execution admission denied"
    assert "synthetic-private-error" not in caplog.text
    assert "synthetic-private-error" not in str(caplog.records)
    safe = [
        row
        for row in caplog.records
        if row.message == "Restricted CI execution admission blocked"
    ]
    assert len(safe) == 1
    assert safe[0].execution_id == str(execution_id)
    assert safe[0].ci_principal_id == str(context.principal_id)
    assert safe[0].ci_error_type == "PermissionError"
    assert safe[0].exc_info is None


def test_ci_delegation_denies_even_after_flow_configuration_changes() -> None:
    parent = models.FlowExecution(id=uuid4(), flow_id=uuid4(), ci_principal_id=uuid4())
    with pytest.raises(DelegationRefusedError, match="one execution"):
        evaluate_delegation(
            None, parent_execution=parent, parent_flow=None, reference="other-flow"
        )


def test_dispatch_failure_does_not_overwrite_terminal_execution(
    db_session: Session,
) -> None:
    resources = create_ci_resources.__wrapped__(db_session)
    _, _, token = provision(db_session, resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    from preloop.models.schemas.flow_execution import FlowExecutionUpdate

    crud.crud_flow_execution.update(
        db_session,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(status="SUCCEEDED", result={"review": "saved"}),
    )
    crud.crud_ci_execution.reject_dispatch(db_session, execution=execution)
    persisted = crud.crud_flow_execution.get(db_session, id=execution.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.result == {"review": "saved"}
