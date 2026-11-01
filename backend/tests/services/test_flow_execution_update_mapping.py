"""``crud_flow_execution.update`` accepts a plain mapping of column values."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from preloop.models.crud import crud_flow_execution
from preloop.models.schemas.flow_execution import FlowExecutionUpdate


def test_update_accepts_a_mapping_for_columns_outside_the_schema() -> None:
    """The orchestrator records native_result in trigger_event_details."""
    db = MagicMock()
    execution = SimpleNamespace(id="exec-1", trigger_event_details={"a": 1})
    details = {"a": 1, "native_result": {"value": True, "source": "default"}}

    result = crud_flow_execution.update(
        db, db_obj=execution, obj_in={"trigger_event_details": details}
    )

    assert result is execution
    assert execution.trigger_event_details == details
    db.flush.assert_called_once()


def test_update_still_accepts_the_schema() -> None:
    db = MagicMock()
    execution = SimpleNamespace(id="exec-2", error_message=None)

    crud_flow_execution.update(
        db, db_obj=execution, obj_in=FlowExecutionUpdate(error_message="boom")
    )

    assert execution.error_message == "boom"


def test_update_rejects_unknown_mapping_keys() -> None:
    db = MagicMock()
    execution = SimpleNamespace(id="exec-3", trigger_event_details={})

    with pytest.raises(ValueError, match="trigger_event_detail"):
        crud_flow_execution.update(
            db, db_obj=execution, obj_in={"trigger_event_detail": {}}
        )

    assert execution.trigger_event_details == {}
    db.flush.assert_not_called()
