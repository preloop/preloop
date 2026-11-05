"""Prepaid credit lots. Callers own the transaction; this module only flushes.

Amounts go through ``hosted_spend.money``. A naive timestamp is refused by
``hosted_spend.month_start`` wherever a grant or expiry clock is set. Holds
and releases are ledger markers with notes ``credit-hold`` and
``credit-release``; they move ``reserved`` and are not purchases. Recompute
corrections use an ``adjustment`` whose note starts with ``recompute:`` and
are not themselves replayed, so a second pass sees no drift.

Expiry writes one entry per lot, ``credit-expiry:<purchase_id>``, for the
whole remainder. A live hold allocated FIFO onto that lot skips the lot:
held credit is not expirable, and a second expiry key is not used.
"""

from calendar import monthrange
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.hosted_credit import GRANT_KINDS
from preloop.models.crud.hosted_spend import (
    HostedSpendExceededError,
    HostedSpendUnavailableError,
    lock_account,
    money,
    month_start,
)

ZERO = Decimal(0)
CREDIT_FLOOR = Decimal("-1.00")
HOLD_NOTE = "credit-hold"
RELEASE_NOTE = "credit-release"
RECOMPUTE_PREFIX = "recompute:"
EXPIRY_ACTOR = "credit-expiry"
_COMPONENTS = (
    "purchased",
    "consumed",
    "reserved",
    "refunded",
    "expired",
    "balance",
)
_MULTIPLIER_QUANTUM = Decimal("0.0001")
_GRANT_PACKS = {
    "purchase": "hosted_credits_grant",
    "promotional_grant": "promotional",
    "adjustment": "hosted_credit_adjustment",
}


def _amount(value: Any) -> Decimal:
    """Coerce a credit amount through ``money`` and refuse zero."""
    amount = money(value)
    if amount == ZERO:
        raise HostedSpendUnavailableError("Hosted cost is invalid")
    return amount


def _operation_key(value: Any, *, limit: int, allow_stripe_id: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise HostedSpendUnavailableError("Hosted operation key is invalid")
    if any(char in value for char in "/%") or (
        "_" in value and not (allow_stripe_id and value.startswith("credit-purchase:"))
    ):
        raise HostedSpendUnavailableError("Hosted operation key is invalid")
    if value.startswith(("credit-expiry:", "credit-release:", "credit-recompute:")):
        raise HostedSpendUnavailableError("Hosted operation key is invalid")
    return value


def _actor(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise HostedSpendUnavailableError("Hosted credit actor is invalid")
    return value.strip()


def _note(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2000:
        raise HostedSpendUnavailableError("Hosted credit note is invalid")
    if value.startswith(
        (HOLD_NOTE, RELEASE_NOTE, RECOMPUTE_PREFIX, "credit-recompute:")
    ):
        raise HostedSpendUnavailableError("Hosted credit note is invalid")
    return value


def _aware(value: datetime) -> datetime:
    """Refuse a naive clock. ``month_start`` is the single period check."""
    month_start(value)
    return value.astimezone(UTC)


def _add_months(value: datetime, months: int) -> datetime:
    index = value.month - 1 + months
    year = value.year + index // 12
    month = index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def multiplier(value: Any) -> Decimal | None:
    """Validate the exact four-decimal multiplier stored on reservations."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HostedSpendUnavailableError("Hosted cost is unavailable") from exc
    if not amount.is_finite() or amount <= ZERO or amount >= Decimal("100"):
        raise HostedSpendUnavailableError("Hosted cost is invalid")
    stored = amount.quantize(_MULTIPLIER_QUANTUM)
    if stored <= ZERO or stored != amount:
        raise HostedSpendUnavailableError("Hosted credit price is invalid")
    return stored


def _provider_cost(value: Any) -> Decimal | None:
    if value is None:
        return None
    return money(value)


def _rebalance(account: models.HostedCreditAccount) -> None:
    account.balance = (
        account.purchased - account.consumed - account.refunded - account.expired
    )


def _credit_row(
    db: Session, account_id: Any, *, lock: bool
) -> models.HostedCreditAccount | None:
    query = select(models.HostedCreditAccount).where(
        models.HostedCreditAccount.account_id == account_id
    )
    if lock:
        query = query.with_for_update()
    return db.execute(
        query.execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _require_credit(db: Session, account_id: Any) -> models.HostedCreditAccount:
    row = _credit_row(db, account_id, lock=True)
    if row is None:
        row = models.HostedCreditAccount(
            account_id=account_id,
            purchased=ZERO,
            consumed=ZERO,
            reserved=ZERO,
            refunded=ZERO,
            expired=ZERO,
            balance=ZERO,
        )
        db.add(row)
        db.flush()
    return row


def reservation_available(db: Session, *, account_id: Any) -> Decimal | None:
    """Refresh available credit after the account lock, never create a wallet.

    Missing or empty wallets keep the legacy allowance path, including overages.
    The caller's pre-lock balance must never authorize a stale reservation.
    """
    lock_account(db, account_id)
    row = _credit_row(db, account_id, lock=True)
    if row is None or (row.balance == ZERO and row.reserved == ZERO):
        return None
    return row.balance - row.reserved


def _entry(
    db: Session, account_id: Any, operation_key: str
) -> models.HostedCreditLedger | None:
    return db.execute(
        select(models.HostedCreditLedger)
        .where(
            models.HostedCreditLedger.account_id == account_id,
            models.HostedCreditLedger.operation_key == operation_key,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _append(
    db: Session,
    account: models.HostedCreditAccount,
    *,
    kind: str,
    amount: Decimal,
    operation_key: str,
    actor: str,
    purchase_id: Any = None,
    reservation_id: Any = None,
    provider_cost: Decimal | None = None,
    price_multiplier: Decimal | None = None,
    note: str | None = None,
) -> models.HostedCreditLedger:
    entry = models.HostedCreditLedger(
        account_id=account.account_id,
        kind=kind,
        amount=amount,
        balance_after=account.balance,
        operation_key=operation_key,
        purchase_id=purchase_id,
        reservation_id=reservation_id,
        provider_cost=provider_cost,
        price_multiplier=price_multiplier,
        actor=actor,
        note=note,
    )
    db.add(entry)
    db.flush()
    return entry


def _open_lot(
    db: Session,
    *,
    account_id: Any,
    amount: Decimal,
    kind: str,
    purchase_id: Any,
    now: datetime | None,
) -> models.HostedCreditPurchase:
    """Open the lot a grant draws down.

    ``source`` is only the ingestion channel (``checkout`` or
    ``auto_topup``). A lot created here has no Stripe session, so the
    channel is ``checkout``. The ledger ``kind`` is what distinguishes a
    paid purchase, a promotional grant, and an adjustment.
    """
    stamp = _aware(now) if now is not None else datetime.now(UTC)
    lot: models.HostedCreditPurchase | None
    if purchase_id is None:
        lot = models.HostedCreditPurchase(
            id=uuid4(),
            account_id=account_id,
            pack_id=_GRANT_PACKS[kind],
            amount_usd=amount,
            source="checkout",
            status="pending",
            remaining_usd=ZERO,
        )
        db.add(lot)
        db.flush()
    else:
        lot = db.execute(
            select(models.HostedCreditPurchase)
            .where(
                models.HostedCreditPurchase.id == purchase_id,
                models.HostedCreditPurchase.account_id == account_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if lot is None:
            raise HostedSpendUnavailableError("Hosted credit purchase is unavailable")
        if lot.amount_usd != amount:
            raise HostedSpendUnavailableError("Hosted credit purchase amount conflicts")
    if lot.status != "pending":
        raise HostedSpendUnavailableError("Hosted credit purchase is already granted")
    granted_at = lot.granted_at
    if granted_at is None:
        granted_at = stamp
    else:
        granted_at = _aware(granted_at)
    expires_at = lot.expires_at
    if expires_at is None:
        expires_at = _add_months(granted_at, 12)
    else:
        expires_at = _aware(expires_at)
    lot.granted_at = granted_at
    lot.expires_at = expires_at
    return lot


def _spendable(amount: Decimal, prior: Decimal) -> Decimal:
    """Clear a negative balance before any of this grant becomes a lot."""
    if prior < ZERO:
        remainder = amount + prior
        return remainder if remainder > ZERO else ZERO
    return amount


def grant(
    db: Session,
    *,
    account_id: Any,
    amount: Any,
    operation_key: str,
    kind: str,
    purchase_id: Any = None,
    actor: str,
    note: str | None = None,
    now: datetime | None = None,
) -> models.HostedCreditLedger:
    """Grant credit once per ``(account_id, operation_key)``.

    When the balance is negative the lot keeps only ``amount + balance``
    (or zero, when the grant does not cover the deficit). The ledger entry
    is still the full amount. A repeated key returns that same entry only
    when kind, amount, and note match. A hold or release marker that reused
    the key is a conflict, not a replay.
    """
    amount = _amount(amount)
    operation_key = _operation_key(operation_key, limit=128, allow_stripe_id=True)
    actor = _actor(actor)
    note = _note(note)
    if now is not None:
        _aware(now)
    if kind not in GRANT_KINDS:
        raise HostedSpendUnavailableError("Hosted credit grant kind is invalid")
    lock_account(db, account_id)
    existing = _entry(db, account_id, operation_key)
    if existing is not None:
        # Hold and release share this key space and are stored as
        # adjustments. Matching kind and amount is not enough to replay one
        # of those markers as a grant.
        marker = (existing.note or "").startswith((HOLD_NOTE, RELEASE_NOTE))
        if (
            marker
            or existing.kind != kind
            or existing.amount != amount
            or existing.note != note
        ):
            raise HostedSpendUnavailableError("Conflicting hosted credit grant")
        return existing
    account = _credit_row(db, account_id, lock=True)
    prior = account.balance if account is not None else ZERO
    if account is None:
        account = _require_credit(db, account_id)
    lot = _open_lot(
        db,
        account_id=account_id,
        amount=amount,
        kind=kind,
        purchase_id=purchase_id,
        now=now,
    )
    account.purchased += amount
    _rebalance(account)
    lot.remaining_usd = _spendable(amount, prior)
    lot.status = "granted"
    return _append(
        db,
        account,
        kind=kind,
        amount=amount,
        operation_key=operation_key,
        actor=actor,
        purchase_id=lot.id,
        note=note,
    )


def _lots_for_update(db: Session, account_id: Any) -> list[models.HostedCreditPurchase]:
    return list(
        db.scalars(
            select(models.HostedCreditPurchase)
            .where(
                models.HostedCreditPurchase.account_id == account_id,
                models.HostedCreditPurchase.status == "granted",
                models.HostedCreditPurchase.remaining_usd > 0,
                models.HostedCreditPurchase.granted_at.is_not(None),
            )
            .order_by(
                models.HostedCreditPurchase.granted_at.asc(),
                models.HostedCreditPurchase.id.asc(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _held_by_lot(
    lots: list[models.HostedCreditPurchase], reserved: Decimal
) -> dict[Any, Decimal]:
    """Allocate live holds onto lots oldest-first, the same order debit uses."""
    left = reserved
    held: dict[Any, Decimal] = {}
    for lot in lots:
        take = min(lot.remaining_usd, left)
        held[lot.id] = take
        left -= take
        if left == ZERO:
            break
    return held


def _debit_entries(
    db: Session, account_id: Any, operation_key: str
) -> list[models.HostedCreditLedger]:
    prefix = operation_key + "/"
    return list(
        db.scalars(
            select(models.HostedCreditLedger)
            .where(
                models.HostedCreditLedger.account_id == account_id,
                models.HostedCreditLedger.operation_key.startswith(prefix),
            )
            .order_by(models.HostedCreditLedger.operation_key)
            .execution_options(populate_existing=True)
        )
    )


def debit(
    db: Session,
    *,
    account_id: Any,
    amount: Any,
    operation_key: str,
    actor: str,
    note: str | None = None,
    reservation_id: Any = None,
    provider_cost: Any = None,
    price_multiplier: Any = None,
) -> list[models.HostedCreditLedger]:
    """Consume lots oldest ``granted_at`` first. Equal clocks break on ``id``.

    One ledger row is written per lot touched. Held credit is not spendable.
    The same operation key returns the original rows.
    """
    amount = _amount(amount)
    operation_key = _operation_key(operation_key, limit=80)
    actor = _actor(actor)
    note = _note(note)
    cost = _provider_cost(provider_cost)
    price = multiplier(price_multiplier)
    lock_account(db, account_id)
    existing = _debit_entries(db, account_id, operation_key)
    if existing:
        if sum((row.amount for row in existing), ZERO) != amount:
            raise HostedSpendUnavailableError("Conflicting hosted credit debit")
        return existing
    account = _credit_row(db, account_id, lock=True)
    available = (account.balance - account.reserved) if account is not None else ZERO
    if account is None or amount > available:
        raise HostedSpendExceededError("Prepaid credit is exhausted")
    lots = _lots_for_update(db, account_id)
    held = _held_by_lot(lots, account.reserved)
    plan: list[tuple[models.HostedCreditPurchase, Decimal]] = []
    left = amount
    for lot in lots:
        free = lot.remaining_usd - held.get(lot.id, ZERO)
        if free <= ZERO:
            continue
        take = min(free, left)
        plan.append((lot, take))
        left -= take
        if left == ZERO:
            break
    if left > ZERO:
        raise HostedSpendExceededError("Prepaid credit is exhausted")
    return _consume(
        db,
        account,
        plan=plan,
        operation_key=operation_key,
        actor=actor,
        reservation_id=reservation_id,
        provider_cost=cost,
        price_multiplier=price,
        note=note,
    )


def _consume(
    db: Session,
    account: models.HostedCreditAccount,
    *,
    plan: Sequence[tuple[models.HostedCreditPurchase | None, Decimal]],
    operation_key: str,
    actor: str,
    reservation_id: Any,
    provider_cost: Decimal | None,
    price_multiplier: Decimal | None,
    note: str | None = None,
) -> list[models.HostedCreditLedger]:
    """Write each FIFO movement with its share of cost, including lotless debt."""
    written: list[models.HostedCreditLedger] = []
    total = sum((take for _, take in plan), ZERO)
    cost_left = provider_cost if provider_cost is not None else ZERO
    for index, (lot, take) in enumerate(plan):
        cost = None
        if provider_cost is not None:
            # Round intermediate shares down and put the residual on the last
            # entry so provider costs sum exactly even across tiny lots.
            cost = (
                cost_left
                if index == len(plan) - 1
                else (provider_cost * take / total).quantize(
                    Decimal("0.0000000001"), rounding=ROUND_DOWN
                )
            )
            cost_left -= cost
        if lot is not None:
            lot.remaining_usd -= take
        account.consumed += take
        _rebalance(account)
        written.append(
            _append(
                db,
                account,
                kind="consumption",
                amount=take,
                operation_key=f"{operation_key}/{index:04d}/{lot.id if lot else 'overage'}",
                actor=actor,
                purchase_id=lot.id if lot else None,
                reservation_id=reservation_id,
                provider_cost=cost,
                price_multiplier=price_multiplier,
                note=note,
            )
        )
    return written


def settle_reservation(
    db: Session,
    *,
    reservation: models.HostedSpendReservation,
    provider_cost: Decimal,
) -> None:
    """Settle credit under hosted_spend's lock and transaction, without commit.

    Only hosted_spend.settle calls this, after its status/idempotency checks.
    A final provider charge consumes FIFO even when other requests still hold
    credit. Those holds remain intact; debt has no purchase lot. The full charge
    and any floor compensation are both recorded before the transaction commits.
    """
    account_id = reservation.account_id
    lock_account(db, account_id)
    account = _credit_row(db, account_id, lock=True)
    if account is None:
        raise HostedSpendUnavailableError("Hosted credit balance is unavailable")
    cost = money(provider_cost)
    # No credit charge needs no historical tariff. This also lets an unpriced
    # legacy hold release after proven non-dispatch or allowance-only usage.
    price = multiplier(reservation.price_multiplier) if cost > ZERO else None
    if cost > ZERO and price is None:
        raise HostedSpendUnavailableError("Hosted credit price is unavailable")
    if reservation.credit_reserved > ZERO:
        standard_key = f"hosted-hold:{reservation.id}"
        standard = _entry(db, account_id, standard_key)
        if (
            standard is not None
            and standard.amount == reservation.credit_reserved
            and _entry(db, account_id, f"credit-release:{standard_key}") is None
        ):
            # The normal reserve path needs only indexed operation-key lookups.
            live = [standard]
        else:
            holds = list(
                db.scalars(
                    select(models.HostedCreditLedger).where(
                        models.HostedCreditLedger.account_id == account_id,
                        models.HostedCreditLedger.reservation_id == reservation.id,
                        models.HostedCreditLedger.kind == "adjustment",
                        models.HostedCreditLedger.note.startswith(HOLD_NOTE),
                    )
                )
            )
            live = [
                held
                for held in holds
                if _entry(db, account_id, f"credit-release:{held.operation_key}")
                is None
            ]
        if sum((held.amount for held in live), ZERO) != reservation.credit_reserved:
            raise HostedSpendUnavailableError(
                "Hosted credit reservation is unavailable"
            )
        # Public hold() permits caller-chosen keys and multiple holds per
        # reservation. Release every live marker, including those older callers
        # created, without releasing any other reservation's credit.
        for held in live:
            release(
                db,
                account_id=account_id,
                operation_key=held.operation_key,
                actor="system",
            )
    if price is None:
        return
    amount = money(cost * price)
    plan: list[tuple[models.HostedCreditPurchase | None, Decimal]] = []
    left = amount
    for lot in _lots_for_update(db, account_id):
        take = min(lot.remaining_usd, left)
        plan.append((lot, take))
        left -= take
        if left == ZERO:
            break
    if left > ZERO:
        plan.append((None, left))
    _consume(
        db,
        account,
        plan=plan,
        operation_key=f"hosted-settle:{reservation.id}",
        actor="system",
        reservation_id=reservation.id,
        provider_cost=cost,
        price_multiplier=price,
    )
    if account.balance < CREDIT_FLOOR:
        absorbed = CREDIT_FLOOR - account.balance
        # This compensation clears debt, so it creates no spendable purchase lot.
        account.purchased += absorbed
        _rebalance(account)
        _append(
            db,
            account,
            kind="adjustment",
            amount=absorbed,
            operation_key=f"hosted-floor:{reservation.id}",
            actor="system",
            reservation_id=reservation.id,
            note="below credit floor, absorbed",
        )


def _touch_reservation(
    db: Session,
    *,
    account_id: Any,
    reservation_id: Any,
    delta: Decimal,
    multiplier: Decimal | None,
    release: bool,
) -> None:
    row = db.execute(
        select(models.HostedSpendReservation)
        .where(
            models.HostedSpendReservation.id == reservation_id,
            models.HostedSpendReservation.account_id == account_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise HostedSpendUnavailableError("Hosted reservation is unavailable")
    if not release and row.status in {"settled", "released"}:
        raise HostedSpendUnavailableError("Hosted reservation is terminal")
    if multiplier is not None:
        if row.price_multiplier is None:
            row.price_multiplier = multiplier
        elif row.price_multiplier != multiplier:
            raise HostedSpendUnavailableError("Conflicting hosted credit price")
    if release:
        if row.credit_reserved < delta:
            raise HostedSpendUnavailableError(
                "Hosted credit reservation is unavailable"
            )
        row.credit_reserved -= delta
        return
    row.credit_reserved += delta


def hold(
    db: Session,
    *,
    account_id: Any,
    amount: Any,
    operation_key: str,
    actor: str,
    note: str | None = None,
    reservation_id: Any = None,
    price_multiplier: Any = None,
) -> models.HostedCreditLedger:
    """Reserve available credit under the account lock. Same key holds once.

    A reservation-linked hold must record a multiplier for eventual settlement.
    """
    amount = _amount(amount)
    operation_key = _operation_key(operation_key, limit=100)
    actor = _actor(actor)
    user_note = _note(note)
    price = multiplier(price_multiplier)
    if reservation_id is not None and price is None:
        raise HostedSpendUnavailableError("Hosted credit price is unavailable")
    held_note = HOLD_NOTE if user_note is None else f"{HOLD_NOTE} {user_note}"
    lock_account(db, account_id)
    existing = _entry(db, account_id, operation_key)
    if existing is not None:
        if (
            existing.note != held_note
            or existing.amount != amount
            or existing.reservation_id != reservation_id
        ):
            raise HostedSpendUnavailableError("Conflicting hosted credit hold")
        return existing
    account = _credit_row(db, account_id, lock=True)
    available = (account.balance - account.reserved) if account is not None else ZERO
    if account is None or amount > available:
        raise HostedSpendExceededError("Prepaid credit is exhausted")
    if reservation_id is not None:
        _touch_reservation(
            db,
            account_id=account_id,
            reservation_id=reservation_id,
            delta=amount,
            multiplier=price,
            release=False,
        )
    account.reserved += amount
    return _append(
        db,
        account,
        kind="adjustment",
        amount=amount,
        operation_key=operation_key,
        actor=actor,
        reservation_id=reservation_id,
        price_multiplier=price,
        note=held_note,
    )


def release(
    db: Session,
    *,
    account_id: Any,
    operation_key: str,
    actor: str,
    note: str | None = None,
) -> models.HostedCreditLedger:
    """Release one hold in full. A second call returns the same entry."""
    operation_key = _operation_key(operation_key, limit=100)
    actor = _actor(actor)
    user_note = _note(note)
    release_key = f"credit-release:{operation_key}"
    lock_account(db, account_id)
    existing = _entry(db, account_id, release_key)
    if existing is not None:
        return existing
    held = _entry(db, account_id, operation_key)
    if held is None or not (held.note or "").startswith(HOLD_NOTE):
        raise HostedSpendUnavailableError("Hosted credit hold is unavailable")
    account = _credit_row(db, account_id, lock=True)
    if account is None or account.reserved < held.amount:
        raise HostedSpendUnavailableError("Hosted credit reservation is unavailable")
    if held.reservation_id is not None:
        _touch_reservation(
            db,
            account_id=account_id,
            reservation_id=held.reservation_id,
            delta=held.amount,
            multiplier=None,
            release=True,
        )
    account.reserved -= held.amount
    released_note = RELEASE_NOTE if user_note is None else f"{RELEASE_NOTE} {user_note}"
    return _append(
        db,
        account,
        kind="adjustment",
        amount=held.amount,
        operation_key=release_key,
        actor=actor,
        reservation_id=held.reservation_id,
        note=released_note,
    )


def _replay(db: Session, account_id: Any) -> dict[str, Decimal]:
    totals = {name: ZERO for name in _COMPONENTS}
    rows = db.scalars(
        select(models.HostedCreditLedger)
        .where(models.HostedCreditLedger.account_id == account_id)
        .order_by(models.HostedCreditLedger.created_at, models.HostedCreditLedger.id)
    )
    for row in rows:
        note = row.note or ""
        if row.kind == "adjustment" and note.startswith(RECOMPUTE_PREFIX):
            continue
        if row.kind == "adjustment" and note.startswith(HOLD_NOTE):
            totals["reserved"] += row.amount
            continue
        if row.kind == "adjustment" and note.startswith(RELEASE_NOTE):
            totals["reserved"] -= row.amount
            continue
        if row.kind in {"purchase", "promotional_grant", "adjustment"}:
            totals["purchased"] += row.amount
        elif row.kind == "consumption":
            totals["consumed"] += row.amount
        elif row.kind in {"refund", "chargeback"}:
            totals["refunded"] += row.amount
        elif row.kind == "expiry":
            totals["expired"] += row.amount
        else:
            raise HostedSpendUnavailableError("Hosted credit ledger kind is invalid")
    totals["balance"] = (
        totals["purchased"]
        - totals["consumed"]
        - totals["refunded"]
        - totals["expired"]
    )
    return totals


def expire(db: Session, *, purchase_id: Any) -> models.HostedCreditLedger | None:
    """Write off one lot's remainder, or skip it when a hold covers any of it.

    Idempotent: a second call returns the expiry entry. A skipped lot writes
    nothing, so a later call after the hold is released can still expire it.
    """
    lot = db.get(models.HostedCreditPurchase, purchase_id)
    if lot is None:
        raise HostedSpendUnavailableError("Hosted credit purchase is unavailable")
    account_id = lot.account_id
    lock_account(db, account_id)
    key = f"credit-expiry:{purchase_id}"
    existing = _entry(db, account_id, key)
    if existing is not None:
        return existing
    account = _credit_row(db, account_id, lock=True)
    if account is None:
        raise HostedSpendUnavailableError("Hosted credit balance is unavailable")
    # Lock spendable lots in granted_at, id order before the target row so a
    # debit walking the same order cannot deadlock with this expiry.
    lots = _lots_for_update(db, account_id)
    current = next((item for item in lots if item.id == purchase_id), None)
    if current is None:
        current = db.execute(
            select(models.HostedCreditPurchase)
            .where(models.HostedCreditPurchase.id == purchase_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
    if current.remaining_usd <= ZERO:
        return None
    if current.status != "granted":
        raise HostedSpendUnavailableError("Hosted credit purchase is unavailable")
    held = _held_by_lot(lots, account.reserved).get(current.id, ZERO)
    if held > ZERO:
        return None
    lot = current
    amount = lot.remaining_usd
    lot.remaining_usd = ZERO
    lot.status = "expired"
    lot.expired_at = datetime.now(UTC)
    account.expired += amount
    _rebalance(account)
    return _append(
        db,
        account,
        kind="expiry",
        amount=amount,
        operation_key=key,
        actor=EXPIRY_ACTOR,
        purchase_id=lot.id,
    )


def _next_expiry(db: Session, account_id: Any) -> tuple[Decimal, datetime | None]:
    lots = list(
        db.scalars(
            select(models.HostedCreditPurchase)
            .where(
                models.HostedCreditPurchase.account_id == account_id,
                models.HostedCreditPurchase.status == "granted",
                models.HostedCreditPurchase.remaining_usd > 0,
                models.HostedCreditPurchase.expires_at.is_not(None),
            )
            .order_by(
                models.HostedCreditPurchase.expires_at.asc(),
                models.HostedCreditPurchase.id.asc(),
            )
        )
    )
    if not lots:
        return ZERO, None
    soonest = lots[0].expires_at
    amount = sum(
        (lot.remaining_usd for lot in lots if lot.expires_at == soonest),
        ZERO,
    )
    return amount, soonest


def balance(db: Session, *, account_id: Any) -> dict[str, Any]:
    """Read the cache. An account with no row is all zeros and does not raise."""
    row = _credit_row(db, account_id, lock=False)
    expiring, expires_at = _next_expiry(db, account_id)
    if row is None:
        return {
            "purchased": ZERO,
            "consumed": ZERO,
            "reserved": ZERO,
            "refunded": ZERO,
            "expired": ZERO,
            "balance": ZERO,
            "available": ZERO,
            "next_expiring_amount": expiring,
            "next_expires_at": expires_at,
        }
    return {
        "purchased": row.purchased,
        "consumed": row.consumed,
        "reserved": row.reserved,
        "refunded": row.refunded,
        "expired": row.expired,
        "balance": row.balance,
        "available": row.balance - row.reserved,
        "next_expiring_amount": expiring,
        "next_expires_at": expires_at,
    }


def entries(
    db: Session, *, account_id: Any, after_id: Any = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Read a bounded ledger page ordered by id, without prompts or secrets."""
    if not 1 <= limit <= 100:
        raise ValueError("Credit listing size must be between 1 and 100")
    query = (
        select(models.HostedCreditLedger)
        .where(models.HostedCreditLedger.account_id == account_id)
        .order_by(models.HostedCreditLedger.id)
        .limit(limit)
    )
    if after_id is not None:
        query = query.where(models.HostedCreditLedger.id > after_id)
    return [
        {
            "id": str(row.id),
            "kind": row.kind,
            "amount": str(row.amount),
            "balance_after": str(row.balance_after),
            "operation_key": row.operation_key,
            "purchase_id": None if row.purchase_id is None else str(row.purchase_id),
            "reservation_id": (
                None if row.reservation_id is None else str(row.reservation_id)
            ),
            "actor": row.actor,
            "note": row.note,
            "created_at": row.created_at.isoformat(),
        }
        for row in db.scalars(query)
    ]


def _stored(account: models.HostedCreditAccount | None) -> dict[str, Decimal]:
    if account is None:
        return {name: ZERO for name in _COMPONENTS}
    return {name: getattr(account, name) for name in _COMPONENTS}


def recompute(db: Session, *, account_id: Any, apply: bool = False) -> dict[str, Any]:
    """Replay the ledger and report drift against the balance cache.

    ``apply=True`` appends one ``adjustment`` and sets the cache to the
    replay. Prior ledger rows are not updated or deleted.
    """
    if apply:
        lock_account(db, account_id)
    account = _credit_row(db, account_id, lock=apply)
    replay = _replay(db, account_id)
    stored = _stored(account)
    drift = {name: stored[name] - replay[name] for name in _COMPONENTS}
    drifted = {name: delta for name, delta in drift.items() if delta != ZERO}
    result: dict[str, Any] = {
        "status": "ok" if not drifted else "drift",
        "drift": {name: str(delta) for name, delta in drifted.items()},
        "replay": {name: str(value) for name, value in replay.items()},
        "stored": {name: str(value) for name, value in stored.items()},
    }
    if not drifted or not apply:
        return result
    if account is None:
        account = _require_credit(db, account_id)
    for name in _COMPONENTS:
        setattr(account, name, replay[name])
    magnitude = sum((abs(delta) for delta in drifted.values()), ZERO)
    note = RECOMPUTE_PREFIX + ",".join(
        f"{name}={drift[name]}" for name in sorted(drifted)
    )
    entry = _append(
        db,
        account,
        kind="adjustment",
        amount=_amount(magnitude),
        operation_key=f"credit-recompute:{uuid4()}",
        actor="recompute",
        note=note,
    )
    result["status"] = "adjusted"
    result["adjustment_id"] = str(entry.id)
    result["stored"] = {name: str(replay[name]) for name in _COMPONENTS}
    result["drift"] = {}
    return result
