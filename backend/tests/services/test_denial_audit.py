"""Durable denial aggregation, isolation and immutable hash-chain coverage."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, literal, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import denial_audit
from preloop.services import audit_chain
from preloop.services.denial_audit import write_denial
from tests.models.test_audit_chain_state_concurrency import (
    committed_account as create_account,
)


@pytest.fixture
def denial_account(db_engine: Engine) -> Iterator[UUID]:
    """An isolated account visible to independently committed worker sessions."""
    yield from create_account.__wrapped__(db_engine)


def _write(db: Session, account_id: UUID, principal_id: UUID, **kwargs: Any) -> None:
    """Write one synthetic permission refusal."""
    write_denial(
        db,
        account_id=account_id,
        principal_id=principal_id,
        principal_type="managed_agent",
        action="agent_permission_denied",
        target=kwargs.get("target", "Bash"),
        reason_code=kwargs.get("reason_code", "tool_blocked"),
        rule_message=kwargs.get("rule_message"),
    )


def test_parallel_denials_count_once_and_cannot_seal_open_minute(
    db_engine: Engine, denial_account: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent workers share one durable counter, including a fresh head."""
    principal = uuid4()
    minute = datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0)
    monkeypatch.setattr(
        denial_audit, "_current_minute_expression", lambda: literal(minute)
    )

    def refuse(_: int) -> None:
        with Session(db_engine) as db:
            _write(db, denial_account, principal)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(refuse, range(40)))
    with Session(db_engine) as db:
        rows = db.scalars(
            select(models.AuditLog).where(models.AuditLog.account_id == denial_account)
        ).all()
        assert len(rows) == 1
        assert rows[0].details["count"] == 40
        assert (
            audit_chain.seal_account(
                db, account_id=denial_account, lag=timedelta(0)
            ).sealed
            == 0
        )
        assert rows[0].chain_seq is None
        monkeypatch.setattr(
            denial_audit,
            "_current_minute_expression",
            lambda: literal(minute + timedelta(minutes=1)),
        )
        assert (
            audit_chain.seal_account(
                db, account_id=denial_account, lag=timedelta(0)
            ).sealed
            == 1
        )
        db.refresh(rows[0])
        assert rows[0].chain_seq is not None


def test_closed_count_is_hashed_and_next_minute_never_rewrites_it(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The minute boundary creates a new record; changing old counts breaks proof."""
    minute = datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0)
    monkeypatch.setattr(
        denial_audit, "_current_minute", lambda db: minute - timedelta(minutes=1)
    )
    for _ in range(5):
        _write(db_session, test_user.account_id, test_user.id)
    audit_chain.seal_account(
        db_session, account_id=test_user.account_id, lag=timedelta(0)
    )
    old = db_session.scalars(
        select(models.AuditLog).where(
            models.AuditLog.account_id == test_user.account_id
        )
    ).one()
    assert old.details["count"] == 5
    assert old.row_hash
    old_hash = old.row_hash
    monkeypatch.setattr(denial_audit, "_current_minute", lambda db: minute)
    _write(db_session, test_user.account_id, test_user.id)
    db_session.refresh(old)
    assert old.row_hash == old_hash
    assert old.details["count"] == 5
    assert (
        audit_chain.verify_chain(db_session, account_id=test_user.account_id)["status"]
        == "ok"
    )
    old.details = {**old.details, "count": 999}
    db_session.flush()
    assert (
        audit_chain.verify_chain(db_session, account_id=test_user.account_id)["status"]
        == "broken"
    )


def test_reason_principal_and_target_are_separate_counters(
    db_session: Session, test_user: models.User
) -> None:
    """Only identical refusals share a bucket; strings are bounded and redacted."""
    _write(db_session, test_user.account_id, test_user.id)
    _write(db_session, test_user.account_id, uuid4())
    _write(db_session, test_user.account_id, test_user.id, target="Read")
    _write(db_session, test_user.account_id, test_user.id, reason_code="key_expired")
    _write(
        db_session, test_user.account_id, test_user.id, target="Bearer " + "a" * 2000
    )
    rows = db_session.scalars(
        select(models.AuditLog).where(
            models.AuditLog.account_id == test_user.account_id
        )
    ).all()
    assert len(rows) == 5
    assert all(row.details["count"] == 1 for row in rows)
    assert all(len(row.resource_id) <= 200 for row in rows)
    assert "a" * 100 not in str([row.details for row in rows])


def test_rule_explanations_are_redacted_bounded_and_counted_separately(
    db_session: Session, test_user: models.User
) -> None:
    """Different policy reasons cannot disappear into one generic counter."""
    explanation = (
        "Writes outside the workspace are denied. Bearer synthetic-secret " + "x" * 2000
    )
    for _ in range(2):
        _write(
            db_session,
            test_user.account_id,
            test_user.id,
            reason_code="permission_denied",
            rule_message=explanation,
        )
    _write(
        db_session,
        test_user.account_id,
        test_user.id,
        reason_code="permission_denied",
        rule_message="Only read commands are allowed.",
    )
    rows = db_session.scalars(
        select(models.AuditLog).where(
            models.AuditLog.account_id == test_user.account_id
        )
    ).all()
    assert sorted(row.details["count"] for row in rows) == [1, 2]
    message = next(row.details["message"] for row in rows if row.details["count"] == 2)
    assert message.startswith("Writes outside the workspace are denied.")
    assert "synthetic-secret" not in message
    assert len(message) == 500
