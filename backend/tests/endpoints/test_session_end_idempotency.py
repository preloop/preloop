"""Operator session-end retries preserve one lifecycle fact and webhook intent."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Iterator
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from preloop.api.endpoints import account
from preloop.models import crud, models
from tests.endpoints.test_agent_permission_session import (
    permission_credential as make_credential,
)


def test_repeated_end_has_one_audit_row_and_webhook_delivery(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten requests have identical responses, with no repeated side effects."""
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="synthetic-session",
        started_at=datetime.now(UTC),
    )
    endpoint = models.WebhookEndpoint(
        account_id=test_user.account_id,
        url="https://example.com/hook",
        secret_encrypted="synthetic-encrypted-secret",
        secret_hint="test",
        event_types=["session.ended"],
        active=True,
        source="account",
    )
    db_session.add_all([session, endpoint])
    db_session.flush()
    emit = Mock(wraps=account.emit_session_ended)
    monkeypatch.setattr(account, "emit_session_ended", emit)
    events = Mock()
    monkeypatch.setattr(account, "emit_account_event", events)
    responses = [
        client.patch(f"/api/v1/runtime-sessions/{session.id}", json={"action": "end"})
        for _ in range(10)
    ]
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    audits = db_session.scalars(
        select(models.AuditLog).where(
            models.AuditLog.account_id == test_user.account_id,
            models.AuditLog.action == "runtime_session_ended",
        )
    ).all()
    deliveries = db_session.scalars(
        select(models.WebhookDelivery).where(
            models.WebhookDelivery.endpoint_id == endpoint.id
        )
    ).all()
    assert len(audits) == len(deliveries) == 1
    emit.assert_called_once()
    assert events.call_count == 2


def test_failed_end_transaction_can_be_retried(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state transition rolls back with an audit persistence failure."""
    import asyncio

    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="retry-session",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit write failure")

    monkeypatch.setattr(account.crud_audit_log, "log_action", fail)
    with pytest.raises(RuntimeError), db_session.begin_nested():
        asyncio.run(
            account.update_account_runtime_session(
                runtime_session_id=str(session.id),
                update=account.RuntimeSessionUpdateRequest(action="end"),
                account=test_user.account,
                current_user=test_user,
                db=db_session,
            )
        )
    db_session.refresh(session)
    assert session.ended_at is None


@pytest.fixture
def committed_identity(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    """Use independent transactions for concurrent session-end requests."""
    yield from make_credential.__wrapped__(db_engine, monkeypatch)


def test_concurrent_session_end_calls_have_one_winner(
    db_engine: Engine,
    committed_identity: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent request sessions cannot both emit the lifecycle audit."""
    account_id = committed_identity["account_id"]
    with Session(db_engine) as db:
        session = models.RuntimeSession(
            account_id=account_id,
            session_source_type="claude_code",
            session_source_id="concurrent-end",
            started_at=datetime.now(UTC),
        )
        db.add(session)
        db.commit()
        session_id = session.id
    events = Mock()
    monkeypatch.setattr(account, "emit_account_event", events)

    def end(_: int) -> dict[str, Any]:
        with Session(db_engine) as db:
            user = crud.crud_user.get(db, id=committed_identity["user_id"])
            owner = crud.crud_account.get(db, id=account_id)
            return asyncio.run(
                account.update_account_runtime_session(
                    runtime_session_id=str(session_id),
                    update=account.RuntimeSessionUpdateRequest(action="end"),
                    account=owner,
                    current_user=user,
                    db=db,
                )
            ).model_dump(mode="json")

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(end, range(12)))
    assert all(response == responses[0] for response in responses)
    with Session(db_engine) as db:
        rows = db.scalars(
            select(models.AuditLog).where(
                models.AuditLog.account_id == account_id,
                models.AuditLog.action == "runtime_session_ended",
            )
        ).all()
        assert len(rows) == 1
    assert events.call_count == 2
