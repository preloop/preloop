"""Purchase lifecycle for prepaid credit. Callers commit or roll back.

Every transition locks the account before the purchase, like the credit wallet.
Provider event receipts and money movements flush in the same transaction.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import hosted_credit as credit
from preloop.models.crud.hosted_spend import lock_account, money

ZERO = Decimal(0)
FLOOR = Decimal("-1")


class PendingPurchaseError(ValueError):
    """An identical purchase is already being opened."""


def create_pending(
    db: Session,
    *,
    account_id: Any,
    pack_id: str,
    amount: Any,
    now: datetime | None = None,
) -> models.HostedCreditPurchase:
    """Persist a purchase attempt, serializing the 60-second duplicate check."""
    stamp = credit._aware(now) if now is not None else datetime.now(UTC)
    amount = credit._amount(amount)
    lock_account(db, account_id)
    recent = db.scalar(
        select(models.HostedCreditPurchase.id)
        .where(
            models.HostedCreditPurchase.account_id == account_id,
            models.HostedCreditPurchase.pack_id == pack_id,
            models.HostedCreditPurchase.status == "pending",
            models.HostedCreditPurchase.created_at > stamp - timedelta(seconds=60),
        )
        .limit(1)
    )
    if recent is not None:
        raise PendingPurchaseError(
            "A checkout for this pack is already pending. Please wait a minute."
        )
    row = models.HostedCreditPurchase(
        account_id=account_id,
        pack_id=pack_id,
        amount_usd=amount,
        source="checkout",
        status="pending",
        remaining_usd=ZERO,
        created_at=stamp,
    )
    db.add(row)
    db.flush()
    return row


def get(
    db: Session,
    *,
    purchase_id: Any,
    account_id: Any,
) -> models.HostedCreditPurchase | None:
    """Resolve only the purchase belonging to this account."""
    return db.scalar(
        select(models.HostedCreditPurchase)
        .where(
            models.HostedCreditPurchase.id == purchase_id,
            models.HostedCreditPurchase.account_id == account_id,
        )
        .execution_options(populate_existing=True)
    )


def _locked(
    db: Session, purchase_id: Any, account_id: Any
) -> models.HostedCreditPurchase:
    lock_account(db, account_id)
    row = get(db, purchase_id=purchase_id, account_id=account_id)
    if row is None:
        raise ValueError("Credit purchase was not found.")
    return row


def _bind(row: models.HostedCreditPurchase, field: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError(f"Invalid {field}.")
    current = getattr(row, field)
    if current is not None and current != value:
        raise ValueError(f"Conflicting {field}.")
    setattr(row, field, value)


def bind_session(
    db: Session,
    *,
    purchase_id: Any,
    account_id: Any,
    session_id: str,
) -> None:
    """Bind the provider response without overwriting an early webhook."""
    row = _locked(db, purchase_id, account_id)
    _bind(row, "stripe_checkout_session_id", session_id)
    db.flush()


def refundable_amount(
    row: models.HostedCreditPurchase,
    *,
    now: datetime | None = None,
) -> Decimal:
    """Maximum policy refund; external refunds must still be accounted for."""
    stamp = credit._aware(now) if now is not None else datetime.now(UTC)
    if (
        row.status != "granted"
        or row.granted_at is None
        or row.expires_at is None
        or stamp > row.granted_at + timedelta(days=60)
        or stamp >= row.expires_at
    ):
        return ZERO
    return row.remaining_usd


def _reversed(db: Session, row: models.HostedCreditPurchase) -> Decimal:
    return sum(
        (
            entry.amount
            for entry in db.scalars(
                select(models.HostedCreditLedger).where(
                    models.HostedCreditLedger.purchase_id == row.id,
                    models.HostedCreditLedger.kind.in_(("refund", "chargeback")),
                )
            )
        ),
        ZERO,
    )


def _reverse(
    db: Session,
    row: models.HostedCreditPurchase,
    *,
    kind: str,
    total: Decimal,
    key: str,
    stamp: datetime,
) -> None:
    """Apply cumulative provider totals, including late/excess policy refunds."""
    if kind == "chargeback":
        setting = db.scalar(
            select(models.HostedCreditTopupSetting).where(
                models.HostedCreditTopupSetting.account_id == row.account_id
            )
        )
        if setting is not None:
            setting.enabled = False
            setting.disabled_reason = "chargeback"
            setting.disabled_at = stamp
    if row.granted_at is None:
        # A refund or dispute delivered before payment completion prevents a sale.
        if row.status != "charged_back":
            row.status = "charged_back" if kind == "chargeback" else "refunded"
        row.remaining_usd = ZERO
        return
    delta = max(ZERO, total - _reversed(db, row))
    if delta > ZERO:
        wallet = credit._require_credit(db, row.account_id)
        wallet.refunded += delta
        credit._rebalance(wallet)
        row.remaining_usd = max(ZERO, row.remaining_usd - delta)
        credit._append(
            db,
            wallet,
            kind=kind,
            amount=delta,
            operation_key=key,
            actor="stripe",
            purchase_id=row.id,
            note=f"Provider {kind}; cumulative amount {total} USD.",
        )
        if wallet.balance < FLOOR:
            absorbed = FLOOR - wallet.balance
            wallet.purchased += absorbed
            credit._rebalance(wallet)
            credit._append(
                db,
                wallet,
                kind="adjustment",
                amount=absorbed,
                operation_key=f"{key}:floor",
                actor="system",
                purchase_id=row.id,
                note="Reversal beyond the -1 USD floor absorbed by the service.",
            )
    if kind == "chargeback":
        row.status = "charged_back"
    elif row.status != "charged_back" and row.remaining_usd == ZERO:
        row.status = "refunded"


def apply_event(
    db: Session,
    *,
    purchase_id: Any,
    account_id: Any,
    event_id: str,
    kind: str,
    session_id: str | None = None,
    payment_intent_id: str | None = None,
    amount: Any = None,
    now: datetime | None = None,
) -> models.HostedCreditPurchase:
    """Atomically receipt an event and grant, expire, refund or charge back.

    Refund amount is the charge's cumulative refunded total. Dispute amount is
    the disputed total. Duplicate or older totals cannot debit twice. Stripe
    identifiers retain their underscores; unlike user debit keys these keys
    are exact lookups, never LIKE expressions.
    """
    stamp = credit._aware(now) if now is not None else datetime.now(UTC)
    if kind not in {"completed", "expired", "refund", "chargeback"}:
        raise ValueError("Unsupported credit purchase event.")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 80:
        raise ValueError("Invalid provider event identifier.")
    if kind != "expired" and not payment_intent_id:
        raise ValueError("Credit purchase requires a payment intent.")
    if kind in {"completed", "expired"} and not session_id:
        raise ValueError("Credit checkout requires a session.")
    total = money(amount) if kind in {"refund", "chargeback"} else ZERO
    row = _locked(db, purchase_id, account_id)
    _bind(row, "stripe_checkout_session_id", session_id)
    _bind(row, "stripe_payment_intent_id", payment_intent_id)
    key = f"credit-event:{event_id}"
    prior = db.scalar(
        select(models.BillingOperation).where(
            models.BillingOperation.account_id == account_id,
            models.BillingOperation.operation_key == key,
        )
    )
    payload = {"purchase_id": str(row.id), "kind": kind, "amount": str(total)}
    if prior is not None:
        if prior.payload != payload:
            raise ValueError("Conflicting provider event.")
        return row
    if kind == "completed" and row.status == "pending":
        credit.grant(
            db,
            account_id=account_id,
            amount=row.amount_usd,
            operation_key=f"credit-purchase:{payment_intent_id}",
            kind="purchase",
            purchase_id=row.id,
            actor="stripe",
            now=stamp,
        )
    elif kind == "expired" and row.status == "pending":
        row.status = "failed"
        row.failure_code = "expired"
    elif kind in {"refund", "chargeback"}:
        if total > row.amount_usd:
            raise ValueError("Provider reversal exceeds the purchase amount.")
        # A dispute revokes the whole pack, net of any previous refunds.
        # Leaving a remainder on a terminal charged_back lot would strand it.
        target = row.amount_usd if kind == "chargeback" else total
        _reverse(db, row, kind=kind, total=target, key=key, stamp=stamp)
    db.add(
        models.BillingOperation(
            account_id=account_id,
            operation_key=key,
            kind="credit_event",
            status="completed",
            payload=payload,
            result={},
        )
    )
    db.flush()
    return row
