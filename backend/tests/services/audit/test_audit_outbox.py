"""Durable transactional audit outbox (EE #94)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.services.audit import async_writer, outbox
from preloop.services.audit import service as audit
from preloop.models.crud import audit_outbox as crud_audit_outbox
from preloop.models.crud import crud_account, crud_audit_log
from preloop.models.models import (
    AUDIT_OUTBOX_FAILED,
    AUDIT_OUTBOX_PENDING,
    AuditLog,
    AuditOutbox,
)


class _Borrowed:
    """The test session, with ``close`` disarmed so the relay can 'own' it."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(outbox.FLAG_ENV, "true")


@pytest.fixture
def account(db_session: Session):
    return crud_account.create(
        db_session, obj_in={"organization_name": "Outbox EE", "is_active": True}
    )


@pytest.fixture
def service():
    svc = audit.AuditService()
    svc.enabled = True
    return svc


def _relay(db_session, service, **kw):
    return outbox.AuditOutboxRelay(
        session_factory=lambda: _Borrowed(db_session),
        service_getter=lambda: service,
        **kw,
    )


def _policy_fields(account):
    return {
        "account_id": account.id,
        "tool_name": "deploy",
        "action": "deny",
        "user_id": None,
        "execution_id": UUID("00000000-0000-4000-8000-000000000001"),
        "extra_details": {"note": "kept"},
    }


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(outbox.FLAG_ENV, raising=False)
    assert outbox.outbox_enabled() is False


def test_payload_round_trip_keeps_uuid_and_datetime():
    value = {"a": uuid4(), "b": [datetime(2026, 1, 1)], "c": {"d": 1}, "e": None}
    encoded = outbox.encode_payload(value)
    import json

    json.dumps(encoded)
    assert outbox.decode_payload(encoded) == value


def test_unsupported_operation_is_rejected(db_session, account, service, flag_on):
    with pytest.raises(ValueError):
        service.enqueue_in_transaction(
            db_session, "enable", account_id=account.id, tool_name="x"
        )


def test_flag_off_async_path_keeps_the_direct_write(monkeypatch, service):
    monkeypatch.delenv(outbox.FLAG_ENV, raising=False)
    seen: list[Any] = []
    monkeypatch.setattr(
        async_writer.writer,
        "submit",
        lambda loop, factory, op, fields, priority=False: seen.append(op),
    )
    async_writer.schedule_audit_write(
        lambda: None,
        service.log_policy_decision,
        allow_without_loop=True,
        account_id="a",
        tool_name="t",
        action="allow",
    )
    assert seen == [service.log_policy_decision]


def test_flag_on_async_path_persists_an_intent_not_a_row(
    monkeypatch, db_session, account, service, flag_on
):
    captured: list[Any] = []
    monkeypatch.setattr(
        async_writer.writer,
        "submit",
        lambda loop, factory, op, fields, priority=False: captured.append(
            (op, fields, priority)
        ),
    )
    async_writer.schedule_audit_write(
        lambda: db_session,
        service.log_policy_decision,
        priority=True,
        allow_without_loop=True,
        **_policy_fields(account),
    )
    op, fields, priority = captured[0]
    assert op is not service.log_policy_decision
    op(db=db_session, **fields)
    rows = db_session.query(AuditOutbox).filter_by(account_id=account.id).all()
    assert [(r.operation, r.priority) for r in rows] == [("log_policy_decision", True)]
    assert crud_audit_log.get_by_account(db_session, account_id=account.id) == []


def test_intent_rolls_back_with_the_business_write(
    db_session, account, service, flag_on
):
    savepoint = db_session.begin_nested()
    service.enqueue_in_transaction(
        db_session, "log_policy_decision", **_policy_fields(account)
    )
    savepoint.rollback()
    assert db_session.query(AuditOutbox).filter_by(account_id=account.id).count() == 0


def test_relay_delivers_exactly_once_with_the_audit_row(
    db_session, account, service, flag_on
):
    savepoint = db_session.begin_nested()
    service.enqueue_in_transaction(
        db_session, "log_policy_decision", priority=True, **_policy_fields(account)
    )
    savepoint.commit()

    relay = _relay(db_session, service)
    assert relay.deliver_one() is True
    assert relay.deliver_one() is None

    # Delivered intents are deleted with the audit row commit.
    assert db_session.query(AuditOutbox).filter_by(account_id=account.id).count() == 0
    logs = crud_audit_log.get_by_account(db_session, account_id=account.id)
    assert len(logs) == 1
    assert logs[0].action == audit.AuditActionType.POLICY_DENY
    assert logs[0].details["note"] == "kept"
    assert logs[0].details["execution_id"] == "00000000-0000-4000-8000-000000000001"
    assert "outbox_enqueued_at" in logs[0].details
    # Unsealed: the chain sealer still owns sequencing.
    assert logs[0].chain_seq is None


def test_failed_delivery_writes_nothing_and_backs_off(
    db_session, account, service, flag_on, monkeypatch
):
    crud_audit_outbox.enqueue(
        db_session,
        account_id=account.id,
        operation="log_policy_decision",
        payload=outbox.encode_payload(_policy_fields(account)),
    )
    db_session.commit()

    def boom(**_kw):
        raise RuntimeError("sink down")

    monkeypatch.setattr(service, "log_policy_decision", boom)
    relay = _relay(db_session, service, max_attempts=2)
    assert relay.deliver_one() is False
    row = db_session.query(AuditOutbox).filter_by(account_id=account.id).one()
    db_session.refresh(row)
    assert (row.status, row.attempt_count) == (AUDIT_OUTBOX_PENDING, 1)
    assert row.last_error == "RuntimeError"
    assert crud_audit_log.get_by_account(db_session, account_id=account.id) == []
    # Not due yet, so a second pass does not hammer the sink.
    assert relay.deliver_one() is None

    row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=1
    )
    db_session.commit()
    assert relay.deliver_one() is False
    db_session.refresh(row)
    assert row.status == AUDIT_OUTBOX_FAILED


def test_swallowed_write_failure_is_not_marked_delivered(
    db_session, account, service, flag_on
):
    """log_* methods swallow their own errors; the relay must still notice."""
    crud_audit_outbox.enqueue(
        db_session,
        account_id=account.id,
        operation="log_policy_decision",
        payload=outbox.encode_payload(_policy_fields(account)),
    )
    db_session.commit()
    service.enabled = False  # method returns without writing or committing
    assert _relay(db_session, service).deliver_one() is False
    row = db_session.query(AuditOutbox).filter_by(account_id=account.id).one()
    db_session.refresh(row)
    assert row.status == AUDIT_OUTBOX_PENDING


def test_new_relay_replays_intents_left_by_a_restart(
    db_session, account, service, flag_on
):
    for _ in range(3):
        crud_audit_outbox.enqueue(
            db_session,
            account_id=account.id,
            operation="log_policy_decision",
            payload=outbox.encode_payload(_policy_fields(account)),
        )
    db_session.commit()
    # A fresh relay with no in-memory state drains everything.
    assert _relay(db_session, service).drain() == 3
    assert len(crud_audit_log.get_by_account(db_session, account_id=account.id)) == 3


def test_relay_rows_seal_from_genesis(db_session, account, service, flag_on):
    from preloop.models.models import GENESIS_HASH
    from preloop.services import audit_chain

    crud_audit_outbox.enqueue(
        db_session,
        account_id=account.id,
        operation="log_policy_decision",
        payload=outbox.encode_payload(_policy_fields(account)),
    )
    db_session.commit()
    _relay(db_session, service).drain()
    audit_chain.seal_account(db_session, account_id=account.id, lag=timedelta(0))
    log = db_session.query(AuditLog).filter_by(account_id=account.id).one()
    db_session.refresh(log)
    assert log.chain_seq == 1
    assert log.prev_hash == GENESIS_HASH


def test_start_relay_is_a_no_op_with_the_flag_off(monkeypatch):
    monkeypatch.delenv(outbox.FLAG_ENV, raising=False)
    assert outbox.start_relay() is None


def test_unknown_stored_operation_is_parked(db_session, account, service, flag_on):
    row = crud_audit_outbox.enqueue(
        db_session, account_id=account.id, operation="enable", payload={}
    )
    db_session.commit()
    assert _relay(db_session, service).deliver_one() is False
    db_session.refresh(row)
    assert row.status == AUDIT_OUTBOX_FAILED
    assert service.enabled is True


def test_intent_payload_is_redacted_before_it_is_stored(
    db_session, account, service, flag_on
):
    service.enqueue_in_transaction(
        db_session,
        "log_tool_call",
        account_id=account.id,
        tool_name="deploy",
        tool_args={"password": "hunter2-secret", "region": "eu"},
        error_reason="Authorization: Bearer abc.def.ghi failed",
    )
    row = db_session.query(AuditOutbox).filter_by(account_id=account.id).one()
    stored = str(row.payload)
    assert "hunter2-secret" not in stored
    assert row.payload["tool_args"]["region"] == "eu"


def test_operation_allowlists_stay_in_step():
    assert audit._OUTBOX_OPERATION_NAMES == outbox.OUTBOX_OPERATIONS


def test_flag_off_enqueue_in_transaction_falls_back_and_warns(
    monkeypatch, service, caplog
):
    monkeypatch.delenv(outbox.FLAG_ENV, raising=False)
    monkeypatch.setattr(audit, "_warned_outbox_fallback", False)
    calls: list[Any] = []
    monkeypatch.setattr(
        audit,
        "schedule_audit_write",
        lambda factory, op, **kw: calls.append((op.__name__, kw)),
    )
    assert service.durable_outbox_enabled() is False
    with caplog.at_level("WARNING"):
        service.enqueue_in_transaction(
            None, "log_policy_decision", account_id="a", tool_name="t", action="deny"
        )
        service.enqueue_in_transaction(
            None, "log_policy_decision", account_id="a", tool_name="t", action="deny"
        )
    assert [name for name, _ in calls] == ["log_policy_decision"] * 2
    assert calls[0][1]["allow_without_loop"] is True
    warnings = [r for r in caplog.records if "not atomic" in r.getMessage()]
    assert len(warnings) == 1


def test_flag_off_paths_do_not_import_the_outbox(monkeypatch, service):
    import sys

    monkeypatch.delenv(outbox.FLAG_ENV, raising=False)
    monkeypatch.delitem(sys.modules, "preloop.services.audit.outbox")
    monkeypatch.setattr(async_writer.writer, "submit", lambda *a, **k: None)
    async_writer.schedule_audit_write(
        lambda: None,
        service.log_policy_decision,
        allow_without_loop=True,
        account_id="a",
        tool_name="t",
        action="allow",
    )
    service.shutdown()
    assert "preloop.services.audit.outbox" not in sys.modules
    async_writer.start_audit_writer()


def test_second_relay_cannot_claim_a_held_intent(db_engine, service, flag_on):
    """Two relays on separate connections: exactly one delivery."""
    from sqlalchemy.orm import Session as OrmSession

    setup = OrmSession(bind=db_engine)
    acct = crud_account.create(
        setup, obj_in={"organization_name": "Relay Race", "is_active": True}
    )
    crud_audit_outbox.enqueue(
        setup,
        account_id=acct.id,
        operation="log_policy_decision",
        payload=outbox.encode_payload(_policy_fields(acct)),
    )
    setup.commit()
    holder = OrmSession(bind=db_engine)
    relay = outbox.AuditOutboxRelay(
        session_factory=lambda: OrmSession(bind=db_engine),
        service_getter=lambda: service,
    )
    try:
        assert crud_audit_outbox.claim_next(holder) is not None
        assert relay.deliver_one() is None
        holder.rollback()
        assert relay.deliver_one() is True
        assert relay.deliver_one() is None
        logs = setup.query(AuditLog).filter_by(account_id=acct.id).all()
        assert len(logs) == 1
    finally:
        holder.close()
        setup.rollback()
        setup.query(AuditLog).filter_by(account_id=acct.id).delete()
        setup.query(AuditOutbox).filter_by(account_id=acct.id).delete()
        setup.delete(setup.get(type(acct), acct.id))
        setup.commit()
        setup.close()


def test_session_search_query_opt_out_is_honoured_in_the_intent(
    db_session, account, service, flag_on
):
    account.meta_data = {}
    db_session.flush()
    service.enqueue_in_transaction(
        db_session,
        "log_session_search",
        account_id=account.id,
        surface="console",
        status="ok",
        query="customer secret phrase",
    )
    row = db_session.query(AuditOutbox).filter_by(account_id=account.id).one()
    assert row.payload["query"] is None
    assert row.payload["query_hash"].startswith("sha256:")
    db_session.commit()
    assert _relay(db_session, service).deliver_one() is True
    log = crud_audit_log.get_by_account(db_session, account_id=account.id)[0]
    assert "query" not in log.details
    assert log.details["query_recorded"] is False
    assert log.details["query_length"] == len("customer secret phrase")


def test_session_search_query_is_bounded_in_the_intent(
    db_session, account, service, flag_on
):
    account.meta_data = {"session_search_audit_store_query_text": True}
    db_session.flush()
    service.enqueue_in_transaction(
        db_session,
        "log_session_search",
        account_id=account.id,
        surface="console",
        status="ok",
        query="q" * (audit.MAX_QUERY_TEXT_CHARS + 50),
    )
    row = db_session.query(AuditOutbox).filter_by(account_id=account.id).one()
    assert len(row.payload["query"]) == audit.MAX_QUERY_TEXT_CHARS
    db_session.commit()
    assert _relay(db_session, service).deliver_one() is True
    log = crud_audit_log.get_by_account(db_session, account_id=account.id)[0]
    # Same as the direct path: bounded text, true length.
    assert log.details["query_length"] == audit.MAX_QUERY_TEXT_CHARS + 50
    assert len(log.details["query"]) == audit.MAX_QUERY_TEXT_CHARS
