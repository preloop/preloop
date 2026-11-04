"""Allowance/credit splits and settlement in real PostgreSQL transactions."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from time import monotonic, sleep
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import hosted_credit as credit
from preloop.models.crud import hosted_spend as spend

NOW = datetime(2026, 11, 4, tzinfo=UTC)


@pytest.fixture
def account_id(db_engine: Engine) -> Iterator[UUID]:
    identifier = uuid4()
    with Session(db_engine) as db:
        db.add(models.Account(id=identifier, organization_name="split-test"))
        db.flush()
        spend.establish_baseline(
            db,
            account_id=identifier,
            lifetime_spent=0,
            month_spent=0,
            now=NOW,
            evidence="isolated test account",
        )
        db.commit()
    yield identifier
    with Session(db_engine) as db:
        db.execute(
            delete(models.BillingOperation).where(
                models.BillingOperation.account_id == identifier
            )
        )
        db.execute(delete(models.Account).where(models.Account.id == identifier))
        db.commit()


def grant(
    db: Session, account_id: UUID, amount: str = "10", now: datetime = NOW
) -> models.HostedCreditLedger:
    return credit.grant(
        db,
        account_id=account_id,
        amount=amount,
        operation_key=str(uuid4()),
        kind="purchase",
        actor="system",
        now=now,
    )


def reserve(
    db: Session,
    account_id: UUID,
    amount: str = "3",
    monthly: str | None = "2",
    **kwargs: Any,
) -> models.HostedSpendReservation:
    return spend.reserve(
        db,
        account_id=account_id,
        amount=amount,
        monthly_limit=monthly,
        lifetime_limit=kwargs.pop("lifetime_limit", None),
        now=kwargs.pop("now", NOW),
        operation_key=kwargs.pop("operation_key", str(uuid4())),
        credit_available=kwargs.pop("credit_available", Decimal("10")),
        price_multiplier=kwargs.pop("price_multiplier", Decimal("1.10")),
        **kwargs,
    )


def consumption(db: Session, account_id: UUID) -> list[models.HostedCreditLedger]:
    return list(
        db.scalars(
            select(models.HostedCreditLedger)
            .where(
                models.HostedCreditLedger.account_id == account_id,
                models.HostedCreditLedger.kind == "consumption",
            )
            .order_by(models.HostedCreditLedger.operation_key)
        )
    )


def assert_reconciled(db: Session, account_id: UUID) -> None:
    balance = credit.balance(db, account_id=account_id)["balance"]
    remaining = db.scalar(
        select(
            func.coalesce(func.sum(models.HostedCreditPurchase.remaining_usd), 0)
        ).where(models.HostedCreditPurchase.account_id == account_id)
    )
    assert remaining == max(balance, 0)
    assert credit.recompute(db, account_id=account_id)["status"] == "ok"


@pytest.mark.parametrize(
    "monthly,allowance,held",
    [
        ("5", "3", "0"),
        ("2", "2", "1.1"),
        ("0", "0", "3.3"),
        (None, "3", "0"),
    ],
)
def test_reserve_and_settle_record_the_split(
    db_engine: Engine, account_id: UUID, monthly: str | None, allowance: str, held: str
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id)
        before_entries = len(credit.entries(db, account_id=account_id))
        row = reserve(db, account_id, monthly=monthly)
        assert row.reserved == Decimal(allowance)
        assert row.credit_reserved == Decimal(held)
        assert row.price_multiplier == Decimal("1.10")
        assert row.reserved + row.credit_reserved / row.price_multiplier == 3
        assert credit.balance(db, account_id=account_id)["reserved"] == Decimal(held)
        assert_reconciled(db, account_id)
        if held == "0":
            assert len(credit.entries(db, account_id=account_id)) == before_entries
        db.commit()
        spend.settle(db, account_id=account_id, reservation_id=row.id, actual="3")
        db.commit()
        state = spend.snapshot(db, account_id=account_id, now=NOW)
        assert state["month_spent"] == Decimal(allowance)
        assert state["lifetime_spent"] == Decimal(allowance)
        assert state["month_reserved"] == state["lifetime_reserved"] == 0
        wallet = credit.balance(db, account_id=account_id)
        assert wallet["consumed"] == Decimal(held)
        assert wallet["balance"] == 10 - Decimal(held)
        assert wallet["reserved"] == 0
        assert row.actual == 3
        if held == "0":
            assert len(credit.entries(db, account_id=account_id)) == before_entries
        assert_reconciled(db, account_id)


@pytest.mark.parametrize(
    "actual,allowance,charged",
    [
        ("1", "1", "0"),
        ("2.5", "2", "0.55"),
        ("3", "2", "1.10"),
        ("3.5", "2", "1.65"),
        ("4", "2", "2.20"),
        ("6", "2", "4.40"),
    ],
)
def test_settlement_under_equal_and_over_with_floor(
    db_engine: Engine, account_id: UUID, actual: str, allowance: str, charged: str
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id, "1.10")
        row = reserve(db, account_id)
        db.commit()
        spend.settle(db, account_id=account_id, reservation_id=row.id, actual=actual)
        db.commit()
        expected = max(Decimal("1.10") - Decimal(charged), Decimal("-1"))
        wallet = credit.balance(db, account_id=account_id)
        assert wallet["balance"] == expected
        assert wallet["reserved"] == 0
        assert wallet["consumed"] == Decimal(charged)
        assert spend.snapshot(db, account_id=account_id, now=NOW)[
            "month_spent"
        ] == Decimal(allowance)
        entries = consumption(db, account_id)
        assert sum((entry.amount for entry in entries), Decimal(0)) == Decimal(charged)
        assert sum((entry.provider_cost for entry in entries), Decimal(0)) == max(
            Decimal(actual) - 2, 0
        )
        adjustments = [
            e
            for e in credit.entries(db, account_id=account_id)
            if e["note"] == "below credit floor, absorbed"
        ]
        absorbed = max(Decimal(charged) - Decimal("2.10"), 0)
        assert len(adjustments) == (1 if absorbed else 0)
        if absorbed:
            assert Decimal(adjustments[0]["amount"]) == absorbed
            assert adjustments[0]["actor"] == "system"
            assert Decimal(adjustments[0]["balance_after"]) == -1
        assert_reconciled(db, account_id)
        before = credit.entries(db, account_id=account_id)
        spend.settle(db, account_id=account_id, reservation_id=row.id, actual=actual)
        assert credit.entries(db, account_id=account_id) == before
        if expected < 0:
            with pytest.raises(spend.HostedSpendExceededError):
                reserve(db, account_id, amount="0.1")
            db.rollback()
            grant(db, account_id, "10")
            assert credit.balance(db, account_id=account_id)["balance"] == 10 + expected
            assert_reconciled(db, account_id)


def test_fractional_fifo_costs_sum_exactly(db_engine: Engine, account_id: UUID) -> None:
    with Session(db_engine) as db:
        for index, amount in enumerate(("0.0000000001", "0.0000000002", "1")):
            grant(db, account_id, amount, now=NOW + timedelta(seconds=index))
        row = reserve(db, account_id, amount="0.0000000003", monthly="0")
        spend.settle(
            db, account_id=account_id, reservation_id=row.id, actual="0.0000000003"
        )
        entries = consumption(db, account_id)
        assert len(entries) == 3
        assert sum(entry.amount for entry in entries) == Decimal("0.0000000004")
        assert sum(entry.provider_cost for entry in entries) == Decimal("0.0000000003")
        assert all(entry.provider_cost >= 0 for entry in entries)
        assert_reconciled(db, account_id)


def test_fifo_ties_and_metadata_on_each_lot(
    db_engine: Engine, account_id: UUID
) -> None:
    with Session(db_engine) as db:
        # Insert newest first so insertion order cannot accidentally pass FIFO.
        newest = grant(db, account_id, "4", now=NOW + timedelta(days=1)).purchase_id
        tied = [grant(db, account_id, "1.1").purchase_id for _ in range(2)]
        oldest, second = sorted(tied)
        row = reserve(db, account_id, amount="1.5", monthly="0")
        spend.settle(db, account_id=account_id, reservation_id=row.id, actual="1.5")
        db.commit()
        entries = consumption(db, account_id)
        assert [e.purchase_id for e in entries] == [oldest, second]
        assert [e.amount for e in entries] == [Decimal("1.1"), Decimal("0.55")]
        assert [e.provider_cost for e in entries] == [Decimal("1"), Decimal("0.5")]
        assert all(e.price_multiplier == Decimal("1.10") for e in entries)
        assert all(e.reservation_id == row.id for e in entries)
        assert db.get(models.HostedCreditPurchase, newest).remaining_usd == 4
        assert db.get(models.HostedCreditPurchase, second).remaining_usd == Decimal(
            "0.55"
        )
        assert_reconciled(db, account_id)


def test_unknown_cost_holds_both_buckets_until_recovery(
    db_engine: Engine, account_id: UUID
) -> None:
    with Session(db_engine) as db:
        lot = grant(db, account_id, "1.1").purchase_id
        row = reserve(db, account_id)
        spend.mark_dispatched(db, account_id=account_id, reservation_id=row.id)
        db.commit()
        spend.settle(db, account_id=account_id, reservation_id=row.id, actual=None)
        db.commit()
        assert row.status == "recovery_required"
        assert row.credit_reserved == Decimal("1.1")
        assert spend.snapshot(db, account_id=account_id, now=NOW)["month_reserved"] == 2
        assert credit.balance(db, account_id=account_id)["available"] == 0
        assert credit.expire(db, purchase_id=lot) is None
        with pytest.raises(spend.HostedSpendExceededError):
            credit.debit(
                db,
                account_id=account_id,
                amount="0.1",
                operation_key="other",
                actor="system",
            )
        db.rollback()
        spend.recover_verified_cost(
            db,
            account_id=account_id,
            reservation_id=row.id,
            actual="2.5",
            evidence="verified provider receipt",
            apply=True,
        )
        db.commit()
        assert credit.balance(db, account_id=account_id)["available"] == Decimal("0.55")
        assert_reconciled(db, account_id)


def test_proven_non_dispatch_releases_both_holds(
    db_engine: Engine, account_id: UUID
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id)
        row = reserve(db, account_id)
        spend.settle(
            db,
            account_id=account_id,
            reservation_id=row.id,
            actual=None,
            proven_not_dispatched=True,
        )
        db.commit()
        assert row.status == "released"
        assert credit.balance(db, account_id=account_id)["available"] == 10
        assert consumption(db, account_id) == []
        assert_reconciled(db, account_id)


def test_insufficient_credit_and_duplicate_reserve_write_nothing(
    db_engine: Engine, account_id: UUID
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id, "1")
        db.commit()
        before = credit.entries(db, account_id=account_id)
        with pytest.raises(spend.HostedSpendExceededError):
            reserve(db, account_id, now=NOW + timedelta(days=31))
        # Even a caller that catches the refusal and commits writes nothing.
        db.commit()
        assert credit.entries(db, account_id=account_id) == before
        assert (
            db.scalar(
                select(func.count())
                .select_from(models.HostedSpendMonth)
                .where(models.HostedSpendMonth.account_id == account_id)
            )
            == 1
        )
        row = reserve(db, account_id, amount="2.5", operation_key="once")
        db.commit()
        with pytest.raises(spend.HostedSpendUnavailableError, match="already exists"):
            reserve(db, account_id, amount="2.5", operation_key="once")
        db.rollback()
        assert credit.balance(db, account_id=account_id)["reserved"] == Decimal("0.55")
        spend.settle(db, account_id=account_id, reservation_id=row.id, actual="2.5")
        assert_reconciled(db, account_id)


@pytest.mark.parametrize("credit_available", [None, Decimal(0), Decimal("10")])
@pytest.mark.parametrize("empty_wallet", [False, True])
def test_no_credit_preserves_legacy_exhaustion_and_overage(
    db_engine: Engine,
    account_id: UUID,
    credit_available: Decimal | None,
    empty_wallet: bool,
) -> None:
    with Session(db_engine) as db:
        if empty_wallet:
            db.add(models.HostedCreditAccount(account_id=account_id))
            db.commit()
        with pytest.raises(spend.HostedSpendExceededError) as exc:
            reserve(db, account_id, credit_available=credit_available)
        assert (
            str(exc.value)
            == "Monthly hosted allowance is exhausted; extra spending is disabled"
        )
        db.rollback()
        row = reserve(db, account_id, amount="1", credit_available=credit_available)
        assert row.credit_reserved == 0
        assert row.price_multiplier is None
        spend.settle(db, account_id=account_id, reservation_id=row.id, actual="4")
        assert spend.snapshot(db, account_id=account_id, now=NOW)["month_spent"] == 4
        assert credit.entries(db, account_id=account_id) == []


def test_free_lifetime_check_precedes_credit_access(
    db_engine: Engine, account_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id)

        def unexpected(*args: Any, **kwargs: Any) -> None:
            pytest.fail("Free credit refusal must precede prepaid lookup")

        monkeypatch.setattr(credit, "reservation_available", unexpected)
        with pytest.raises(
            spend.HostedSpendExceededError, match="One-time hosted credit is exhausted"
        ):
            reserve(db, account_id, lifetime_limit="0.5")


def test_written_off_lot_cannot_cover_a_reservation(
    db_engine: Engine, account_id: UUID
) -> None:
    with Session(db_engine) as db:
        lot = grant(db, account_id, now=NOW - timedelta(days=366)).purchase_id
        credit.expire(db, purchase_id=lot)
        db.commit()
        with pytest.raises(spend.HostedSpendExceededError):
            reserve(db, account_id, monthly="0")
        assert consumption(db, account_id) == []
        assert_reconciled(db, account_id)


@pytest.mark.parametrize(
    "price", ["0", "-1", "NaN", "Infinity", "100", "0.00001", "1.10001", None]
)
def test_invalid_multiplier_cannot_create_a_hold(
    db_engine: Engine, account_id: UUID, price: str | None
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id)
        db.commit()
        with pytest.raises(spend.HostedSpendUnavailableError):
            reserve(db, account_id, price_multiplier=price)
        db.commit()
        assert credit.balance(db, account_id=account_id)["reserved"] == 0
        assert spend.snapshot(db, account_id=account_id, now=NOW)["month_reserved"] == 0


def test_allowance_only_reservation_overage_uses_recorded_multiplier(
    db_engine: Engine, account_id: UUID
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id)
        row = reserve(db, account_id, amount="1")
        assert row.credit_reserved == 0
        spend.settle(db, account_id=account_id, reservation_id=row.id, actual="3")
        assert credit.balance(db, account_id=account_id)["consumed"] == Decimal("2.2")
        assert spend.snapshot(db, account_id=account_id, now=NOW)["month_spent"] == 1
        assert_reconciled(db, account_id)


def test_settlement_failure_rolls_back_both_buckets_and_lots(
    db_engine: Engine, account_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id, "1.1")
        row = reserve(db, account_id)
        reservation_id = row.id
        db.commit()
        original = credit._append

        def fail_adjustment(*args: Any, **kwargs: Any) -> models.HostedCreditLedger:
            if kwargs.get("note") == "below credit floor, absorbed":
                raise RuntimeError("injected failure before compensation")
            return original(*args, **kwargs)

        monkeypatch.setattr(credit, "_append", fail_adjustment)
        with pytest.raises(RuntimeError, match="injected failure"):
            spend.settle(
                db, account_id=account_id, reservation_id=reservation_id, actual="6"
            )
        db.rollback()
        assert credit.balance(db, account_id=account_id)["reserved"] == Decimal("1.1")
        assert credit.balance(db, account_id=account_id)["balance"] == Decimal("1.1")
        assert spend.snapshot(db, account_id=account_id, now=NOW)["month_reserved"] == 2
        assert (
            db.get(models.HostedSpendReservation, reservation_id).status == "reserved"
        )
        assert consumption(db, account_id) == []
        assert_reconciled(db, account_id)


def test_out_of_order_settlements_preserve_other_holds_and_floor(
    db_engine: Engine, account_id: UUID
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id, "2.2")
        first = reserve(db, account_id, amount="1", monthly="0")
        second = reserve(db, account_id, amount="1", monthly="0")
        db.commit()
        spend.settle(db, account_id=account_id, reservation_id=second.id, actual="4")
        db.commit()
        wallet = credit.balance(db, account_id=account_id)
        assert wallet["balance"] == -1
        assert wallet["reserved"] == Decimal("1.1")
        spend.settle(db, account_id=account_id, reservation_id=first.id, actual="1")
        db.commit()
        wallet = credit.balance(db, account_id=account_id)
        assert wallet["balance"] == -1
        assert wallet["reserved"] == 0
        assert wallet["consumed"] == Decimal("5.5")
        assert_reconciled(db, account_id)


def test_flag_off_never_accesses_credit_tables_even_with_a_grant(
    db_engine: Engine, account_id: UUID
) -> None:
    with Session(db_engine) as db:
        grant(db, account_id)
        db.commit()
        statements = []

        def record(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(db_engine, "before_cursor_execute", record)
        try:
            row = reserve(db, account_id, amount="1", credit_available=None)
            spend.settle(db, account_id=account_id, reservation_id=row.id, actual="1")
            with pytest.raises(spend.HostedSpendExceededError):
                reserve(db, account_id, credit_available=None)
        finally:
            event.remove(db_engine, "before_cursor_execute", record)
        assert not any("hosted_credit_" in statement for statement in statements)


def test_real_account_lock_blocks_second_reserve_and_refreshes_stale_balance(
    db_engine: Engine, account_id: UUID
) -> None:
    assert db_engine.dialect.name == "postgresql"
    with Session(db_engine) as db:
        grant(db, account_id, "1.1")
        db.commit()
    waiting = Event()
    backend_pid = []

    def second_attempt() -> str:
        with Session(db_engine) as db:
            # Load the balance into the identity map before the first commit.
            stale = db.scalar(
                select(models.HostedCreditAccount).where(
                    models.HostedCreditAccount.account_id == account_id
                )
            )
            assert stale.reserved == 0
            backend_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
            waiting.set()
            try:
                reserve(db, account_id, amount="1", monthly="0")
                db.commit()
                return "reserved"
            except spend.HostedSpendExceededError:
                db.rollback()
                return "denied"

    with Session(db_engine) as first, ThreadPoolExecutor(max_workers=1) as pool:
        reserve(first, account_id, amount="1", monthly="0")
        future = pool.submit(second_attempt)
        try:
            assert waiting.wait(5)
            deadline = monotonic() + 5
            with db_engine.connect() as observer:
                while monotonic() < deadline:
                    blocked = observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                        {"pid": backend_pid[0]},
                    )
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked, "second session never waited on the account lock"
                assert not future.done()
        finally:
            first.commit()
        assert future.result(timeout=5) == "denied"
    with Session(db_engine) as db:
        assert credit.balance(db, account_id=account_id)["reserved"] == Decimal("1.1")
        assert_reconciled(db, account_id)
