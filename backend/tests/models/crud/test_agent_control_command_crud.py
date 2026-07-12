"""Tests for durable Agent Control command CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models.crud import crud_agent_control_command, crud_managed_agent


def _create_agent(db_session, account_id):
    return crud_managed_agent.create_custom_agent(
        db_session,
        account_id=account_id,
        display_name="Control CRUD Agent",
        commit=True,
    )


def _envelope(*, account_id, managed_agent_id, command_id: str) -> dict:
    return {
        "type": "command",
        "name": "send_message",
        "message_id": command_id,
        "account_id": str(account_id),
        "managed_agent_id": str(managed_agent_id),
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": {"text": "hello"},
    }


def test_create_command_persists_pending(db_session, create_account) -> None:
    account = create_account()
    agent = _create_agent(db_session, account.id)
    command_id = str(uuid4())

    record = crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=command_id,
        envelope=_envelope(
            account_id=account.id,
            managed_agent_id=agent.id,
            command_id=command_id,
        ),
        source="console",
    )

    assert record.id is not None
    assert record.status == "pending"
    assert record.command_id == command_id
    assert record.delivered_at is None

    fetched = crud_agent_control_command.get_by_command_id(
        db_session, account_id=account.id, command_id=command_id
    )
    assert fetched is not None
    assert fetched.id == record.id


def test_mark_delivered_and_acked_state_machine(db_session, create_account) -> None:
    account = create_account()
    agent = _create_agent(db_session, account.id)
    command_id = str(uuid4())
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=command_id,
        envelope=_envelope(
            account_id=account.id,
            managed_agent_id=agent.id,
            command_id=command_id,
        ),
    )
    now = datetime.now(UTC)

    delivered = crud_agent_control_command.mark_delivered(
        db_session,
        account_id=account.id,
        command_id=command_id,
        delivered_at=now,
    )
    assert delivered is not None
    assert delivered.status == "delivered"
    assert delivered.delivered_at is not None

    # Idempotent: already-delivered stays delivered.
    again = crud_agent_control_command.mark_delivered(
        db_session,
        account_id=account.id,
        command_id=command_id,
        delivered_at=now + timedelta(seconds=5),
    )
    assert again is not None
    assert again.status == "delivered"

    acked = crud_agent_control_command.mark_acked(
        db_session,
        account_id=account.id,
        command_id=command_id,
        acked_at=now + timedelta(seconds=10),
    )
    assert acked is not None
    assert acked.status == "acked"
    assert acked.acked_at is not None


def test_mark_acked_from_pending_backfills_delivered_at(
    db_session, create_account
) -> None:
    account = create_account()
    agent = _create_agent(db_session, account.id)
    command_id = str(uuid4())
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=command_id,
        envelope=_envelope(
            account_id=account.id,
            managed_agent_id=agent.id,
            command_id=command_id,
        ),
    )
    acked_at = datetime.now(UTC)

    acked = crud_agent_control_command.mark_acked(
        db_session,
        account_id=account.id,
        command_id=command_id,
        acked_at=acked_at,
    )

    assert acked is not None
    assert acked.status == "acked"
    assert acked.delivered_at == acked.acked_at


def test_mark_failed_only_transitions_pending(db_session, create_account) -> None:
    account = create_account()
    agent = _create_agent(db_session, account.id)
    command_id = str(uuid4())
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=command_id,
        envelope=_envelope(
            account_id=account.id,
            managed_agent_id=agent.id,
            command_id=command_id,
        ),
    )

    failed = crud_agent_control_command.mark_failed(
        db_session,
        account_id=account.id,
        command_id=command_id,
        error="no connected websocket",
    )
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error == "no connected websocket"

    # Already terminal: status stays failed but last_error updates.
    failed_again = crud_agent_control_command.mark_failed(
        db_session,
        account_id=account.id,
        command_id=command_id,
        error="still offline",
    )
    assert failed_again is not None
    assert failed_again.status == "failed"
    assert failed_again.last_error == "still offline"


def test_get_undelivered_skips_expired_and_orders_by_created_at(
    db_session, create_account
) -> None:
    account = create_account()
    agent = _create_agent(db_session, account.id)
    now = datetime.now(UTC)

    first_id = str(uuid4())
    second_id = str(uuid4())
    expired_id = str(uuid4())

    first = crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=first_id,
        envelope=_envelope(
            account_id=account.id, managed_agent_id=agent.id, command_id=first_id
        ),
        expires_at=now + timedelta(hours=1),
        commit=False,
    )
    second = crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=second_id,
        envelope=_envelope(
            account_id=account.id, managed_agent_id=agent.id, command_id=second_id
        ),
        expires_at=now + timedelta(hours=1),
        commit=False,
    )
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=expired_id,
        envelope=_envelope(
            account_id=account.id, managed_agent_id=agent.id, command_id=expired_id
        ),
        expires_at=now - timedelta(minutes=1),
        commit=True,
    )

    pending = crud_agent_control_command.get_undelivered_for_agent(
        db_session, managed_agent_id=agent.id, now=now
    )
    assert [row.command_id for row in pending] == [first_id, second_id]
    assert first.created_at <= second.created_at


def test_expire_stale_marks_pending_past_expires_at(db_session, create_account) -> None:
    account = create_account()
    agent = _create_agent(db_session, account.id)
    now = datetime.now(UTC)
    command_id = str(uuid4())

    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=command_id,
        envelope=_envelope(
            account_id=account.id, managed_agent_id=agent.id, command_id=command_id
        ),
        expires_at=now - timedelta(seconds=1),
    )

    expired_count = crud_agent_control_command.expire_stale(db_session, now=now)
    assert expired_count == 1

    record = crud_agent_control_command.get_by_command_id(
        db_session, account_id=account.id, command_id=command_id
    )
    assert record is not None
    assert record.status == "expired"


def test_list_recent_for_agent_newest_first(db_session, create_account) -> None:
    account = create_account()
    agent = _create_agent(db_session, account.id)
    older_id = str(uuid4())
    newer_id = str(uuid4())

    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=older_id,
        envelope=_envelope(
            account_id=account.id, managed_agent_id=agent.id, command_id=older_id
        ),
        commit=False,
    )
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=newer_id,
        envelope=_envelope(
            account_id=account.id, managed_agent_id=agent.id, command_id=newer_id
        ),
        commit=True,
    )

    recent = crud_agent_control_command.list_recent_for_agent(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        limit=10,
    )
    assert [row.command_id for row in recent] == [newer_id, older_id]


def test_mark_acked_rejects_cross_agent_command(db_session, create_account) -> None:
    account = create_account()
    agent_a = _create_agent(db_session, account.id)
    agent_b = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=account.id,
        display_name="Peer Agent",
        commit=True,
    )
    command_id = str(uuid4())
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent_a.id,
        runtime_session_id=None,
        command_id=command_id,
        envelope=_envelope(
            account_id=account.id, managed_agent_id=agent_a.id, command_id=command_id
        ),
    )

    spoofed = crud_agent_control_command.mark_acked(
        db_session,
        account_id=account.id,
        managed_agent_id=agent_b.id,
        command_id=command_id,
        acked_at=datetime.now(UTC),
    )
    assert spoofed is None

    owned = crud_agent_control_command.mark_acked(
        db_session,
        account_id=account.id,
        managed_agent_id=agent_a.id,
        command_id=command_id,
        acked_at=datetime.now(UTC),
    )
    assert owned is not None
    assert owned.status == "acked"


def test_mark_delivered_many_batches_pending_only(db_session, create_account) -> None:
    """Redelivery path: batch-mark pending commands delivered in one commit."""
    account = create_account()
    agent = _create_agent(db_session, account.id)
    peer = _create_agent(db_session, account.id)
    now = datetime.now(UTC)
    pending_ids = [str(uuid4()), str(uuid4())]
    already_acked = str(uuid4())
    peer_pending = str(uuid4())

    for command_id in pending_ids:
        crud_agent_control_command.create_command(
            db_session,
            account_id=account.id,
            managed_agent_id=agent.id,
            runtime_session_id=None,
            command_id=command_id,
            envelope=_envelope(
                account_id=account.id,
                managed_agent_id=agent.id,
                command_id=command_id,
            ),
            commit=False,
        )
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        command_id=already_acked,
        envelope=_envelope(
            account_id=account.id,
            managed_agent_id=agent.id,
            command_id=already_acked,
        ),
        commit=False,
    )
    crud_agent_control_command.create_command(
        db_session,
        account_id=account.id,
        managed_agent_id=peer.id,
        runtime_session_id=None,
        command_id=peer_pending,
        envelope=_envelope(
            account_id=account.id,
            managed_agent_id=peer.id,
            command_id=peer_pending,
        ),
        commit=False,
    )
    db_session.commit()

    crud_agent_control_command.mark_acked(
        db_session,
        account_id=account.id,
        command_id=already_acked,
        managed_agent_id=agent.id,
        acked_at=now,
    )

    updated = crud_agent_control_command.mark_delivered_many(
        db_session,
        account_id=account.id,
        managed_agent_id=agent.id,
        command_ids=[*pending_ids, already_acked, peer_pending, "missing"],
        delivered_at=now,
    )
    assert updated == 2
    assert (
        crud_agent_control_command.mark_delivered_many(
            db_session,
            account_id=account.id,
            managed_agent_id=agent.id,
            command_ids=[],
            delivered_at=now,
        )
        == 0
    )

    for command_id in pending_ids:
        record = crud_agent_control_command.get_by_command_id(
            db_session, account_id=account.id, command_id=command_id
        )
        assert record is not None
        assert record.status == "delivered"
        assert record.delivered_at is not None

    acked = crud_agent_control_command.get_by_command_id(
        db_session, account_id=account.id, command_id=already_acked
    )
    assert acked is not None
    assert acked.status == "acked"

    peer_record = crud_agent_control_command.get_by_command_id(
        db_session, account_id=account.id, command_id=peer_pending
    )
    assert peer_record is not None
    assert peer_record.status == "pending"


def test_get_undelivered_respects_limit(db_session, create_account) -> None:
    account = create_account()
    agent = _create_agent(db_session, account.id)
    now = datetime.now(UTC)
    for _ in range(5):
        command_id = str(uuid4())
        crud_agent_control_command.create_command(
            db_session,
            account_id=account.id,
            managed_agent_id=agent.id,
            runtime_session_id=None,
            command_id=command_id,
            envelope=_envelope(
                account_id=account.id,
                managed_agent_id=agent.id,
                command_id=command_id,
            ),
            expires_at=now + timedelta(hours=1),
            commit=False,
        )
    db_session.commit()

    pending = crud_agent_control_command.get_undelivered_for_agent(
        db_session, managed_agent_id=agent.id, now=now, limit=2
    )
    assert len(pending) == 2


def test_unknown_command_marks_return_none(db_session, create_account) -> None:
    account = create_account()
    now = datetime.now(UTC)

    assert (
        crud_agent_control_command.mark_delivered(
            db_session,
            account_id=account.id,
            command_id="missing",
            delivered_at=now,
        )
        is None
    )
    assert (
        crud_agent_control_command.mark_acked(
            db_session,
            account_id=account.id,
            command_id="missing",
            acked_at=now,
        )
        is None
    )
    assert (
        crud_agent_control_command.mark_failed(
            db_session,
            account_id=account.id,
            command_id="missing",
            error="gone",
        )
        is None
    )
