"""Durable purchase transitions against PostgreSQL, including replay and races."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import credit_purchase as purchase
from preloop.models.crud import hosted_credit as credit

NOW = datetime(2028, 2, 29, 12, tzinfo=UTC)


@pytest.fixture
def lot(db_session, create_account):
    account = create_account()
    return purchase.create_pending(
        db_session,
        account_id=account.id,
        pack_id="hosted_credits_10",
        amount="10",
        now=NOW - timedelta(hours=1),
    )


def complete(db, lot, event="evt_complete", **kwargs):
    return purchase.apply_event(
        db,
        purchase_id=lot.id,
        account_id=lot.account_id,
        event_id=event,
        kind="completed",
        session_id="cs_test",
        payment_intent_id="pi_test",
        now=NOW,
        **kwargs,
    )


def reverse(db, lot, amount="10", kind="refund", event="evt_refund", now=NOW):
    return purchase.apply_event(
        db,
        purchase_id=lot.id,
        account_id=lot.account_id,
        event_id=event,
        kind=kind,
        payment_intent_id="pi_test",
        amount=amount,
        now=now,
    )


def test_grant_replay_and_calendar_clock(db_session, lot):
    for event in ["evt_complete"] * 3 + ["evt_redelivery"]:
        complete(db_session, lot, event)
    assert lot.status == "granted"
    assert lot.granted_at == NOW
    assert lot.expires_at == datetime(2029, 2, 28, 12, tzinfo=UTC)
    assert lot.remaining_usd == Decimal("10")
    assert len(credit.entries(db_session, account_id=lot.account_id)) == 1
    assert credit.balance(db_session, account_id=lot.account_id)["balance"] == 10


def test_negative_is_cleared_before_lot(db_session, lot):
    db_session.add(
        models.HostedCreditAccount(
            account_id=lot.account_id,
            purchased=0,
            consumed=Decimal(".31"),
            balance=Decimal("-.31"),
            reserved=0,
            refunded=0,
            expired=0,
        )
    )
    db_session.flush()
    complete(db_session, lot)
    assert lot.remaining_usd == Decimal("9.69")
    assert (
        credit.entries(db_session, account_id=lot.account_id)[0]["amount"]
        == "10.0000000000"
    )
    credit.hold(
        db_session,
        account_id=lot.account_id,
        amount="1",
        operation_key="resume",
        actor="system",
    )


def test_refund_before_grant_and_stale_expiry(db_session, lot):
    reverse(db_session, lot)
    complete(db_session, lot)
    purchase.apply_event(
        db_session,
        purchase_id=lot.id,
        account_id=lot.account_id,
        event_id="evt_expired",
        kind="expired",
        session_id="cs_test",
    )
    assert lot.status == "refunded"
    assert lot.remaining_usd == 0
    assert credit.balance(db_session, account_id=lot.account_id)["balance"] == 0
    assert not credit.entries(db_session, account_id=lot.account_id)


def test_refund_policy_and_cumulative_replay(db_session, lot):
    complete(db_session, lot)
    credit.debit(
        db_session,
        account_id=lot.account_id,
        amount="4",
        operation_key="usage",
        actor="system",
    )
    assert purchase.refundable_amount(lot, now=NOW + timedelta(days=60)) == 6
    assert purchase.refundable_amount(lot, now=NOW + timedelta(days=60, seconds=1)) == 0
    reverse(db_session, lot, "2")
    reverse(db_session, lot, "6", event="evt_refund_6")
    reverse(db_session, lot, "2", event="evt_old_refund")
    assert lot.remaining_usd == 0
    assert lot.status == "refunded"
    rows = credit.entries(db_session, account_id=lot.account_id)
    assert sum(Decimal(r["amount"]) for r in rows if r["kind"] == "refund") == 6
    assert credit.balance(db_session, account_id=lot.account_id)["balance"] == 0


def test_late_external_refund_is_accounted_and_floor_absorbed(db_session, lot):
    complete(db_session, lot)
    credit.debit(
        db_session,
        account_id=lot.account_id,
        amount="9",
        operation_key="usage",
        actor="system",
    )
    reverse(db_session, lot, now=NOW + timedelta(days=61))
    rows = credit.entries(db_session, account_id=lot.account_id)
    assert [Decimal(r["amount"]) for r in rows if r["kind"] == "refund"] == [10]
    assert [Decimal(r["amount"]) for r in rows if r["kind"] == "adjustment"] == [8]
    assert lot.remaining_usd == 0
    assert credit.balance(db_session, account_id=lot.account_id)["balance"] == -1


def test_expired_lot_not_refundable(db_session, lot):
    complete(db_session, lot)
    assert purchase.refundable_amount(lot, now=lot.expires_at) == 0
    lot.status = "expired"
    lot.remaining_usd = 0
    assert purchase.refundable_amount(lot, now=NOW) == 0


def test_chargeback_disables_topup_and_replays_once(db_session, lot):
    complete(db_session, lot)
    setting = models.HostedCreditTopupSetting(account_id=lot.account_id, enabled=True)
    db_session.add(setting)
    db_session.flush()
    for event in ["evt_dispute", "evt_dispute", "evt_other_delivery"]:
        reverse(db_session, lot, kind="chargeback", event=event)
    assert lot.status == "charged_back"
    assert not setting.enabled
    assert setting.disabled_reason == "chargeback"
    assert (
        len(
            [
                r
                for r in credit.entries(db_session, account_id=lot.account_id)
                if r["kind"] == "chargeback"
            ]
        )
        == 1
    )


def test_expired_session_and_binding_conflict(db_session, lot):
    purchase.apply_event(
        db_session,
        purchase_id=lot.id,
        account_id=lot.account_id,
        event_id="evt_expired",
        kind="expired",
        session_id="cs_test",
    )
    assert (lot.status, lot.failure_code) == ("failed", "expired")
    complete(db_session, lot)
    assert lot.remaining_usd == 0
    with pytest.raises(ValueError, match="session"):
        purchase.bind_session(
            db_session,
            purchase_id=lot.id,
            account_id=lot.account_id,
            session_id="cs_other",
        )


def test_failed_transaction_rolls_back_receipt_and_grant(db_session, lot):
    with pytest.raises(RuntimeError), db_session.begin_nested():
        complete(db_session, lot)
        raise RuntimeError("crash before commit")
    db_session.refresh(lot)
    assert lot.status == "pending"
    complete(db_session, lot)
    assert lot.remaining_usd == 10


def test_concurrent_pending_check_and_grants(db_engine):
    account_id = uuid4()
    with Session(db_engine) as db:
        db.add(models.Account(id=account_id, organization_name="Purchase race"))
        db.commit()
    barrier = Barrier(2)

    def attempt(_):
        with Session(db_engine) as db:
            barrier.wait()
            try:
                lot = purchase.create_pending(
                    db,
                    account_id=account_id,
                    pack_id="hosted_credits_10",
                    amount="10",
                    now=NOW,
                )
                identifier = lot.id
                db.commit()
                return identifier
            except purchase.PendingPurchaseError:
                db.rollback()
                return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(attempt, range(2)))
        assert len([i for i in ids if i]) == 1
        lot_id = next(i for i in ids if i)

        def grant(index):
            with Session(db_engine) as db:
                barrier.wait()
                purchase.apply_event(
                    db,
                    purchase_id=lot_id,
                    account_id=account_id,
                    event_id=f"evt_race{index}",
                    kind="completed",
                    session_id=f"cs_{account_id}",
                    payment_intent_id=f"pi_{account_id}",
                    now=NOW,
                )
                db.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(grant, range(2)))
        with Session(db_engine) as db:
            assert credit.balance(db, account_id=account_id)["balance"] == 10
    finally:
        with Session(db_engine) as db:
            for model in (
                models.BillingOperation,
                models.HostedCreditLedger,
                models.HostedCreditPurchase,
                models.HostedCreditAccount,
            ):
                for row in db.scalars(
                    select(model).where(model.account_id == account_id)
                ):
                    db.delete(row)
                db.flush()
            db.delete(db.get(models.Account, account_id))
            db.commit()


def test_dispute_revokes_full_pack_net_of_previous_refunds(db_session, lot):
    complete(db_session, lot)
    reverse(db_session, lot, "2")
    reverse(db_session, lot, "8", kind="chargeback", event="evt_dispute")
    reverse(db_session, lot, "2", event="evt_old_refund")
    assert lot.status == "charged_back"
    assert lot.remaining_usd == 0
    rows = credit.entries(db_session, account_id=lot.account_id)
    assert sum(Decimal(r["amount"]) for r in rows if r["kind"] == "chargeback") == 8
    assert credit.balance(db_session, account_id=lot.account_id)["balance"] == 0
    assert credit.recompute(db_session, account_id=lot.account_id)["status"] == "ok"


def test_stripe_key_underscores_cannot_be_used_as_debit_wildcards(db_session, lot):
    from preloop.models.crud.hosted_spend import HostedSpendUnavailableError

    complete(db_session, lot)
    with pytest.raises(HostedSpendUnavailableError, match="operation key"):
        credit.debit(
            db_session,
            account_id=lot.account_id,
            amount="1",
            operation_key="credit-purchase:pi_test",
            actor="system",
        )
    assert credit.balance(db_session, account_id=lot.account_id)["balance"] == 10


@pytest.mark.parametrize("seconds,blocked", [(59, True), (60, False), (61, False)])
def test_pending_window_boundary(db_session, create_account, seconds, blocked):
    account = create_account()
    purchase.create_pending(
        db_session,
        account_id=account.id,
        pack_id="hosted_credits_10",
        amount="10",
        now=NOW,
    )
    args = dict(
        account_id=account.id,
        pack_id="hosted_credits_10",
        amount="10",
        now=NOW + timedelta(seconds=seconds),
    )
    if blocked:
        with pytest.raises(purchase.PendingPurchaseError):
            purchase.create_pending(db_session, **args)
    else:
        assert purchase.create_pending(db_session, **args).status == "pending"


def test_purchase_lookup_and_binding_are_account_scoped(
    db_session, lot, create_account
):
    other = create_account()
    assert purchase.get(db_session, purchase_id=lot.id, account_id=other.id) is None
    with pytest.raises(ValueError, match="not found"):
        purchase.bind_session(
            db_session, purchase_id=lot.id, account_id=other.id, session_id="cs_other"
        )
    assert (
        purchase.get(
            db_session, purchase_id=lot.id, account_id=lot.account_id
        ).stripe_checkout_session_id
        is None
    )


@pytest.mark.parametrize("pack_id", [None, "", " ", "x" * 65, " pack"])
def test_invalid_pack_identifier_refused_before_writing(
    db_session, create_account, pack_id
):
    account = create_account()
    with pytest.raises(ValueError, match="pack"):
        purchase.create_pending(
            db_session, account_id=account.id, pack_id=pack_id, amount="10", now=NOW
        )
    assert not list(
        db_session.scalars(
            select(models.HostedCreditPurchase).where(
                models.HostedCreditPurchase.account_id == account.id
            )
        )
    )
