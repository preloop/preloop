"""Exercise reservation contention without requiring a shared database."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from preloop.models.crud import crud_flow_execution


def test_concurrent_delivery_returns_committed_winner():
    db = MagicMock()
    winner = SimpleNamespace(id=uuid4(), status="PENDING")
    db.query.return_value.join.return_value.filter.return_value.first.side_effect = [
        None,
        winner,
    ]
    db.commit.side_effect = IntegrityError(
        "insert", {}, Exception("uq_flow_execution_webhook_delivery")
    )
    row, duplicate = crud_flow_execution.reserve_employee_event(
        db,
        flow_id=uuid4(),
        account_id=str(uuid4()),
        event={},
        delivery_key="delivery:employee:example",
    )
    assert row is winner and duplicate
    db.rollback.assert_called_once()


def test_unrelated_integrity_error_is_observable():
    db = MagicMock()
    db.query.return_value.join.return_value.filter.return_value.first.return_value = (
        None
    )
    db.commit.side_effect = IntegrityError("insert", {}, Exception("other_constraint"))
    with pytest.raises(IntegrityError):
        crud_flow_execution.reserve_employee_event(
            db,
            flow_id=uuid4(),
            account_id=str(uuid4()),
            event={},
            delivery_key="delivery:employee:example",
        )
    db.rollback.assert_called_once()
