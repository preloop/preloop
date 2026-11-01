"""Durable exception digest ledger: quiet weeks and recipient-only acknowledgment."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_user
from preloop.models.crud import exception_digest as ledger

END = datetime(2026, 10, 26, tzinfo=timezone.utc)
KEY = "a" * 64


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Keep the test account when the ledger rolls back a refused reservation."""
    with db_engine.connect() as connection:
        outer = connection.begin()
        with Session(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as session:
            yield session
        outer.rollback()


def _card(fingerprint: str = "f1") -> dict[str, Any]:
    return {"key": KEY, "fingerprint": fingerprint, "source_scope": "account:model"}


def _sent(db: Session, user: models.User, card: dict[str, Any], end: datetime) -> Any:
    row = ledger.claim_delivery(
        db,
        account_id=user.account_id,
        user_id=user.id,
        window_end=end,
        candidates=[card],
        sections=["cost"],
    )
    assert row is not None and row.status == "preparing"
    assert ledger.begin_dispatch(
        db, account_id=user.account_id, delivery_id=row.id, snapshot=row.snapshot
    )
    ledger.finish_delivery(
        db, account_id=user.account_id, delivery_id=row.id, status="sent"
    )
    return row


def _member(db: Session, user: models.User) -> models.User:
    return crud_user.create(
        db,
        obj_in={
            "account_id": user.account_id,
            "email": f"{uuid4().hex}@example.com",
            "username": uuid4().hex[:12],
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )


def test_unchanged_material_state_is_suppressed_next_week(
    db_session: Session, test_user: models.User
) -> None:
    _sent(db_session, test_user, _card(), END)
    quiet = ledger.claim_delivery(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        window_end=END + timedelta(days=7),
        candidates=[_card()],
        sections=["cost"],
    )
    assert quiet is not None
    assert quiet.status == "suppressed"
    assert quiet.reason == "no_actionable_change"


def test_changed_material_state_is_selected_again(
    db_session: Session, test_user: models.User
) -> None:
    _sent(db_session, test_user, _card(), END)
    changed = ledger.claim_delivery(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        window_end=END + timedelta(days=7),
        candidates=[_card("f2")],
        sections=["cost"],
    )
    assert changed is not None and changed.status == "preparing"


def test_second_claim_in_the_same_week_is_refused(
    db_session: Session, test_user: models.User
) -> None:
    args: dict[str, Any] = dict(
        account_id=test_user.account_id,
        user_id=test_user.id,
        window_end=END,
        candidates=[_card()],
        sections=["cost"],
    )
    assert ledger.claim_delivery(db_session, **args) is not None
    assert ledger.claim_delivery(db_session, **args) is None


def test_only_the_recipient_can_acknowledge_a_sent_fact(
    db_session: Session, test_user: models.User
) -> None:
    row = _sent(db_session, test_user, _card(), END)
    other = _member(db_session, test_user)
    for account_id, user_id in (
        (test_user.account_id, other.id),
        (uuid4(), test_user.id),
    ):
        assert not ledger.acknowledge(
            db_session,
            account_id=account_id,
            user_id=user_id,
            delivery_id=row.id,
            exception_key=KEY,
        )
    assert ledger.acknowledge(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        delivery_id=row.id,
        exception_key=KEY,
    )


def test_unsent_or_outdated_fact_cannot_be_acknowledged(
    db_session: Session, test_user: models.User
) -> None:
    pending = ledger.claim_delivery(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        window_end=END,
        candidates=[_card()],
        sections=["cost"],
    )
    assert pending is not None
    assert not ledger.acknowledge(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        delivery_id=pending.id,
        exception_key=KEY,
    )
    ledger.finish_delivery(
        db_session,
        account_id=test_user.account_id,
        delivery_id=pending.id,
        status="sent",
    )
    _sent(db_session, test_user, _card("f2"), END + timedelta(days=7))
    assert not ledger.acknowledge(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        delivery_id=pending.id,
        exception_key=KEY,
    )


@pytest.mark.parametrize("key", [KEY, "short"])
def test_acknowledge_endpoint_hides_foreign_or_malformed_requests(
    client: TestClient, db_session: Session, test_user: models.User, key: str
) -> None:
    response = client.post(
        f"/api/v1/weekly-report/deliveries/{uuid4()}/acknowledge/{key}"
    )
    assert response.status_code == 404


def test_acknowledge_endpoint_accepts_the_recipient(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    row = _sent(db_session, test_user, _card(), END)
    response = client.post(
        f"/api/v1/weekly-report/deliveries/{row.id}/acknowledge/{KEY}"
    )
    assert response.status_code == 200
    assert response.json() == {"acknowledged": True}


def _reserve(
    db: Session, account_id: UUID, usd: str, tokens: int = 100, seconds: int = 0
) -> datetime | None:
    return ledger.reserve_synthesis(
        db,
        account_id=account_id,
        window_end=END,
        estimated_usd=Decimal(usd),
        input_tokens=tokens,
        output_tokens=tokens,
        now=END + timedelta(seconds=seconds),
    )


def test_synthesis_reservation_stops_at_the_account_week_cost_cap(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    assert _reserve(db_session, account_id, "0.015") is not None
    assert _reserve(db_session, account_id, "0.006") is None
    assert _reserve(db_session, account_id, "0.005") is not None


def test_synthesis_reservation_caps_calls_tokens_and_deadline(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    assert _reserve(db_session, account_id, "0.001", tokens=6001) is None
    assert _reserve(db_session, account_id, "0.001") is not None
    assert _reserve(db_session, account_id, "0.001", seconds=21) is None
    assert _reserve(db_session, account_id, "0.001") is not None
    assert _reserve(db_session, account_id, "0.001") is None


@pytest.mark.parametrize("usd", ["0", "-1", "NaN", "Infinity"])
def test_synthesis_reservation_refuses_unknown_prices(
    db_session: Session, test_user: models.User, usd: str
) -> None:
    account_id = test_user.account_id
    assert _reserve(db_session, account_id, usd) is None


def _claim_next_week(
    db: Session, user: models.User, scopes: set[str]
) -> models.ExceptionDigestDelivery | None:
    return ledger.claim_delivery(
        db,
        account_id=user.account_id,
        user_id=user.id,
        window_end=END + timedelta(days=7),
        candidates=[],
        sections=["cost"],
        active_keys=set(),
        observed_scopes=scopes,
    )


def test_complete_observation_resolves_so_the_fact_can_recur(
    db_session: Session, test_user: models.User
) -> None:
    _sent(db_session, test_user, _card(), END)
    resolved = _claim_next_week(db_session, test_user, {"account:model"})
    assert resolved is not None and resolved.status == "suppressed"
    again = ledger.claim_delivery(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        window_end=END + timedelta(days=14),
        candidates=[_card()],
        sections=["cost"],
    )
    assert again is not None and again.status == "preparing"


def test_unobserved_scope_does_not_resolve_history(
    db_session: Session, test_user: models.User
) -> None:
    _sent(db_session, test_user, _card(), END)
    _claim_next_week(db_session, test_user, {"account:approval"})
    still = ledger.claim_delivery(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        window_end=END + timedelta(days=14),
        candidates=[_card()],
        sections=["cost"],
    )
    assert still is not None and still.status == "suppressed"


def test_a_delivery_is_handed_to_smtp_once(
    db_session: Session, test_user: models.User
) -> None:
    row = ledger.claim_delivery(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        window_end=END,
        candidates=[_card()],
        sections=["cost"],
    )
    assert row is not None
    args: dict[str, Any] = dict(
        account_id=test_user.account_id, delivery_id=row.id, snapshot=row.snapshot
    )
    assert ledger.begin_dispatch(db_session, **args)
    assert not ledger.begin_dispatch(db_session, **args)
    assert not ledger.begin_dispatch(db_session, **{**args, "account_id": uuid4()})
