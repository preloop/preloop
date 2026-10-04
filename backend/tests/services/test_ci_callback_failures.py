"""Independent delivery-failure contracts for restricted completion callbacks."""

import socket
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.schemas.ci_principal import CiAction
from preloop.services.event_webhooks import outbox, targets, worker
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from tests.services.test_ci_callback_contract import callback as create_callback
from tests.services.test_ci_callback_contract import ci_resources as create_ci_resources


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


@pytest.fixture
def callback(db_session: Session, ci_resources: tuple[Any, ...]) -> tuple[Any, ...]:
    return create_callback.__wrapped__(db_session, ci_resources)


def queue_callback(db: Session, callback: tuple[Any, ...]) -> models.WebhookDelivery:
    *_, endpoint, _, execution = callback
    result = outbox.enqueue_event(
        db,
        account_id=endpoint.account_id,
        event_type="flow.execution.finished",
        subject_id=execution.id,
        data={},
    )
    assert len(result.delivery_ids) == 1
    row = CRUDBase(models.WebhookDelivery).get(db, id=result.delivery_ids[0])
    assert row is not None
    return row


def worker_factory(db: Session) -> Callable[[], Session]:
    connection = db.connection()

    def factory() -> Session:
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    return factory


def intercept_http(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    sent: list[bytes] = []

    class SyntheticClient:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self, url: str, *, content: bytes, headers: dict[str, str]
        ) -> httpx.Response:
            sent.append(content)
            return httpx.Response(204)

    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: SyntheticClient())
    return sent


def make_due(db: Session, delivery: models.WebhookDelivery) -> None:
    CRUDBase(models.WebhookDelivery).update(
        db,
        db_obj=delivery,
        obj_in={
            "next_attempt_at": datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=1)
        },
    )


@pytest.mark.asyncio
async def test_transient_dns_failure_retries_then_delivers_once(
    db_session: Session,
    callback: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = queue_callback(db_session, callback)
    sent = intercept_http(monkeypatch)
    monkeypatch.setattr(targets.settings, "webhook_block_private_targets", True)

    def unresolved(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(socket.EAI_AGAIN, "temporary name resolution failure")

    monkeypatch.setattr(targets.socket, "getaddrinfo", unresolved)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await worker.run_once(db_factory=worker_factory(db_session))
    db_session.refresh(delivery)
    assert sent == []
    assert delivery.status == "pending"
    assert delivery.attempt_count == 1
    assert delivery.claimed_at is None
    assert delivery.next_attempt_at > before
    make_due(db_session, delivery)
    monkeypatch.setattr(
        targets.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    assert await worker.run_once(db_factory=worker_factory(db_session)) == 1
    db_session.refresh(delivery)
    assert len(sent) == 1
    assert delivery.status == "delivered"
    assert delivery.attempt_count == 2
    assert await worker.run_once(db_factory=worker_factory(db_session)) == 0
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["private_target", "disabled", "grant"])
async def test_permanent_denial_is_dead_without_http_or_failed_attempt(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    callback: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    denial: str,
) -> None:
    principal, _, _, endpoint, _, _ = callback
    delivery = queue_callback(db_session, callback)
    sent = intercept_http(monkeypatch)
    if denial == "private_target":
        monkeypatch.setattr(targets.settings, "webhook_block_private_targets", True)
        monkeypatch.setattr(
            targets.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        )
    else:
        values: dict[str, Any] = {"enabled": False}
        if denial == "grant":
            values = {
                "grant": ci_resources[3].model_copy(
                    update={"actions": (CiAction.READ_RESULT,)}
                )
            }
        crud.crud_ci_principal.change(
            db_session,
            actor=ci_resources[0],
            principal_id=principal.id,
            **values,
        )
    assert await worker.run_once(db_factory=worker_factory(db_session)) == 0
    db_session.refresh(delivery)
    db_session.refresh(endpoint)
    assert sent == []
    assert delivery.status == "dead"
    assert delivery.claimed_at is None
    assert delivery.attempt_count == 0
    assert endpoint.consecutive_failures == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "unprepared"])
@pytest.mark.parametrize("previous_attempts", [0, 5])
async def test_unexpected_prepost_failure_is_accounted_safely(
    db_session: Session,
    callback: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    previous_attempts: int,
) -> None:
    *_, endpoint, secret, _ = callback
    delivery = queue_callback(db_session, callback)
    CRUDBase(models.WebhookDelivery).update(
        db_session, db_obj=delivery, obj_in={"attempt_count": previous_attempts}
    )
    sent = intercept_http(monkeypatch)
    monkeypatch.setattr(worker.settings, "webhook_circuit_failure_threshold", 1)
    sentinel = "synthetic-private-exception-content"
    if failure == "exception":

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(sentinel)

        monkeypatch.setattr(crud.crud_ci_subscription, "prepare_delivery", broken)
    else:
        original = worker.prepare_claimed
        calls = 0

        def unprepared(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return original(*args, **kwargs) if calls == 1 else None

        monkeypatch.setattr(worker, "prepare_claimed", unprepared)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await worker.run_once(db_factory=worker_factory(db_session))
    db_session.refresh(delivery)
    db_session.refresh(endpoint)
    assert sent == []
    assert delivery.attempt_count == previous_attempts + 1
    assert delivery.claimed_at is None
    assert endpoint.consecutive_failures == 1
    assert endpoint.circuit_opened_at is not None
    assert delivery.status == ("dead" if previous_attempts == 5 else "pending")
    if previous_attempts == 0:
        assert delivery.next_attempt_at > before
    assert sentinel not in (delivery.last_error or "")
    assert secret not in (delivery.last_error or "")
    assert sentinel not in caplog.text
    assert secret not in caplog.text
    if failure == "exception":
        assert any(
            "RuntimeError" in record.getMessage()
            or "RuntimeError" in str(record.__dict__.values())
            for record in caplog.records
        )
        assert any(
            str(delivery.id) in record.getMessage()
            or str(delivery.id) in str(record.__dict__.values())
            for record in caplog.records
        )


@pytest.mark.asyncio
async def test_prepost_session_open_failure_records_attempt_with_recovered_session(
    db_session: Session,
    callback: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    delivery = queue_callback(db_session, callback)
    *_, endpoint, secret, _ = callback
    sent = intercept_http(monkeypatch)
    monkeypatch.setattr(worker.settings, "webhook_circuit_failure_threshold", 1)
    healthy_factory = worker_factory(db_session)
    calls = 0
    sentinel = "synthetic-private-session-error"

    def factory() -> Session:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError(sentinel)
        return healthy_factory()

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await worker.run_once(db_factory=factory)
    db_session.refresh(delivery)
    db_session.refresh(endpoint)
    assert sent == []
    assert delivery.status == "pending"
    assert delivery.attempt_count == 1
    assert delivery.claimed_at is None
    assert delivery.next_attempt_at > before
    assert endpoint.consecutive_failures == 1
    assert endpoint.circuit_opened_at is not None
    assert calls >= 3
    assert sentinel not in caplog.text
    assert secret not in caplog.text
    assert sentinel not in (delivery.last_error or "")
    assert any(
        "RuntimeError" in str(record.__dict__.values()) for record in caplog.records
    )
    assert any(
        str(delivery.id) in str(record.__dict__.values()) for record in caplog.records
    )


@pytest.mark.asyncio
async def test_authorization_database_failure_retries_without_permanent_denial(
    db_session: Session,
    callback: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    delivery = queue_callback(db_session, callback)
    *_, endpoint, secret, _ = callback
    sent = intercept_http(monkeypatch)
    sentinel = "synthetic-private-database-error"

    def database_unavailable(*args: Any, **kwargs: Any) -> Any:
        raise SQLAlchemyError(sentinel)

    monkeypatch.setattr(
        crud.crud_ci_principal, "authorize_principal", database_unavailable
    )
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    await worker.run_once(db_factory=worker_factory(db_session))
    db_session.refresh(delivery)
    db_session.refresh(endpoint)
    assert sent == []
    assert delivery.status == "pending"
    assert delivery.attempt_count == 1
    assert delivery.claimed_at is None
    assert delivery.next_attempt_at > before
    assert endpoint.consecutive_failures == 1
    assert sentinel not in caplog.text
    assert secret not in caplog.text
    assert sentinel not in (delivery.last_error or "")
    assert any(
        "SQLAlchemyError" in str(record.__dict__.values())
        and str(delivery.id) in str(record.__dict__.values())
        for record in caplog.records
    )
