"""Exact-intent binding and single-use persistence for owner consent."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models.crud import owner_consent


@pytest.mark.parametrize(
    "changed", ["owner", "subject", "details", "execution", "agent"]
)
def test_existing_request_rejects_changed_intent(changed: str) -> None:
    account, owner, agent = uuid4(), uuid4(), uuid4()
    consent = SimpleNamespace(
        owner_user_id=owner, subject_id="runner", approval_request_id=uuid4()
    )
    approval = SimpleNamespace(
        tool_args={"task": "work"}, execution_id="execution", managed_agent_id=agent
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = consent
    db.get.return_value = approval
    kwargs = dict(
        account_id=account,
        owner_user_id=owner,
        action="runner_flow",
        subject_id="runner",
        request_key="intent",
        details={"task": "work"},
        execution_id="execution",
        managed_agent_id=agent,
    )
    field = {
        "owner": "owner_user_id",
        "subject": "subject_id",
        "details": "details",
        "execution": "execution_id",
        "agent": "managed_agent_id",
    }[changed]
    kwargs[field] = (
        {"task": "different"}
        if changed == "details"
        else uuid4()
        if changed in {"owner", "agent"}
        else "different"
    )
    with patch.object(owner_consent.crud_account_halt, "lock_account"):
        with pytest.raises(ValueError, match="does not match"):
            owner_consent.get_or_create(db, **kwargs)
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_poll_expiry_fails_closed_and_retains_audit() -> None:
    owner = uuid4()
    consent = SimpleNamespace(
        owner_user_id=owner, subject_id="runner", approval_request_id=uuid4()
    )
    approval = SimpleNamespace(
        id=consent.approval_request_id,
        tool_args={},
        execution_id=None,
        managed_agent_id=None,
        status="pending",
        expires_at=datetime.utcnow() - timedelta(seconds=1),
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = consent
    db.get.return_value = approval
    with patch.object(owner_consent.crud_account_halt, "lock_account"):
        _, row, created = owner_consent.get_or_create(
            db,
            account_id=uuid4(),
            owner_user_id=owner,
            action="runner_flow",
            subject_id="runner",
            request_key="intent",
            details={},
        )
    assert not created
    assert row.status == "expired"
    assert db.add.call_args.args[0].event_type == "expired"
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    "consumed,status,expected",
    [
        (None, "approved", True),
        (datetime.utcnow(), "approved", False),
        (None, "pending", False),
        (None, "declined", False),
    ],
)
def test_consume_requires_unconsumed_approved_row(
    consumed: datetime | None, status: str, expected: bool
) -> None:
    db = MagicMock()
    consent = SimpleNamespace(consumed_at=consumed)
    db.query.return_value.filter_by.return_value.with_for_update.return_value.one.return_value = consent
    db.get.return_value = SimpleNamespace(status=status)
    assert owner_consent.consume(db, approval_id=uuid4()) == expected
    if expected:
        assert consent.consumed_at is not None
        db.commit.assert_called_once()
    else:
        db.rollback.assert_called_once()
        db.commit.assert_not_called()


@pytest.mark.parametrize("actor", ["owner", "requester", "other"])
@pytest.mark.parametrize("expired", [False, True])
def test_status_is_private_minimal_and_expires(actor: str, expired: bool) -> None:
    owner, requester = uuid4(), uuid4()
    consent = SimpleNamespace(owner_user_id=owner)
    approval = SimpleNamespace(
        tool_args={"requester_user_id": str(requester), "secret": "private"},
        status="pending",
        expires_at=datetime.utcnow() + timedelta(seconds=-1 if expired else 60),
    )
    db = MagicMock()
    db.query.return_value.join.return_value.filter.return_value.first.return_value = (
        consent,
        approval,
    )
    result = owner_consent.status_for_requester(
        db,
        approval_id=uuid4(),
        account_id=uuid4(),
        user_id={"owner": owner, "requester": requester, "other": uuid4()}[actor],
    )
    assert result == (
        None
        if actor == "other"
        else {
            "status": "expired" if expired else "pending",
            "expires_at": approval.expires_at,
        }
    )
    db.commit.assert_not_called()


def test_status_missing_or_cross_account_is_hidden() -> None:
    db = MagicMock()
    db.query.return_value.join.return_value.filter.return_value.first.return_value = (
        None
    )
    assert (
        owner_consent.status_for_requester(
            db, approval_id=uuid4(), account_id=uuid4(), user_id=uuid4()
        )
        is None
    )
