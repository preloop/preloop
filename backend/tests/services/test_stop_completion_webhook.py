"""A stopped execution must notify subscribers without a live orchestrator."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models.crud.flow_execution import CRUDFlowExecution


@pytest.mark.parametrize("commit", [True, False])
def test_stop_queues_completion_in_status_transaction(commit: bool) -> None:
    db = MagicMock()
    execution = SimpleNamespace(id=uuid4(), flow_id=uuid4(), failure_category=None)
    flow = SimpleNamespace(id=execution.flow_id, account_id=uuid4())
    status_query, execution_query, flow_query = MagicMock(), MagicMock(), MagicMock()
    status_query.filter.return_value.update.return_value = 1
    execution_query.populate_existing.return_value.filter.return_value.one.return_value = execution
    flow_query.filter.return_value.one.return_value = flow
    db.query.side_effect = [status_query, execution_query, flow_query]
    with patch(
        "preloop.services.event_webhooks.emitters.emit_flow_execution_finished"
    ) as emit:
        emit.side_effect = lambda *args, **kwargs: db.commit.assert_not_called()
        assert CRUDFlowExecution().mark_stopped(
            db, execution_id=execution.id, error_message="budget expired", commit=commit
        )
    emit.assert_called_once_with(
        db, execution, flow, status="STOPPED", failure_category=None
    )
    assert db.commit.called is commit


def test_lost_stop_race_does_not_emit_false_terminal_event() -> None:
    db = MagicMock()
    db.query.return_value.filter.return_value.update.return_value = 0
    with patch(
        "preloop.services.event_webhooks.emitters.emit_flow_execution_finished"
    ) as emit:
        assert not CRUDFlowExecution().mark_stopped(
            db, execution_id=uuid4(), error_message="stop"
        )
    emit.assert_not_called()
