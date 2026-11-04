"""Atomic hosted balances; callers own commit boundaries and provider dispatch.

Launch helpers support the account-balance-baseline reconciliation required
before hosted-spend activation. Edition operator tooling consumes these helpers;
they are not automatic startup routines. Recovery helpers likewise require an
operator's verified evidence. Unknown provider charges stay reserved until verified.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models

ZERO = Decimal(0)


class HostedSpendUnavailableError(ValueError):
    """The available balance or operation cannot be established safely."""


class HostedSpendExceededError(ValueError):
    """The verified included balance cannot cover a requested reservation."""


def money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HostedSpendUnavailableError("Hosted cost is unavailable") from exc
    if not amount.is_finite() or amount < ZERO:
        raise HostedSpendUnavailableError("Hosted cost is invalid")
    return amount.quantize(Decimal("0.0000000001"), rounding="ROUND_CEILING")


def month_start(now: datetime) -> date:
    if now.tzinfo is None:
        raise HostedSpendUnavailableError(
            "Hosted accounting requires an aware timestamp"
        )
    return now.astimezone(UTC).date().replace(day=1)


def lock_account(db: Session, account_id: Any) -> models.Account:
    account = db.execute(
        select(models.Account)
        .where(models.Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if account is None:
        raise HostedSpendUnavailableError("Hosted account is unavailable")
    return account


def _wallet(db: Session, account_id: Any) -> models.HostedSpendAccount | None:
    return db.execute(
        select(models.HostedSpendAccount)
        .where(models.HostedSpendAccount.account_id == account_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _month(
    db: Session, account_id: Any, period: date
) -> models.HostedSpendMonth | None:
    return db.execute(
        select(models.HostedSpendMonth)
        .where(
            models.HostedSpendMonth.account_id == account_id,
            models.HostedSpendMonth.period == period,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def establish_baseline(
    db: Session,
    *,
    account_id: Any,
    lifetime_spent: Any,
    month_spent: Any,
    now: datetime,
    evidence: str,
) -> None:
    """Initialize once from a proven baseline, never from retained usage scans.

    Runtime only calls this inside the newly created account transaction.
    Existing accounts require reviewed reconciliation or an explicitly approved
    zero-consumption launch through initialize_zero_launch. Neither path resets
    existing wallets; there is no public reset endpoint.
    """
    if not evidence or len(evidence) > 500:
        raise HostedSpendUnavailableError("A verified baseline needs evidence")
    lock_account(db, account_id)
    if _wallet(db, account_id) is not None:
        raise HostedSpendUnavailableError("Hosted baseline already exists")
    db.add(
        models.HostedSpendAccount(
            account_id=account_id,
            lifetime_spent=money(lifetime_spent)
            if lifetime_spent is not None
            else None,
            lifetime_reserved=ZERO,
            coverage_start=now,
            baseline_evidence=evidence,
        )
    )
    db.add(
        models.HostedSpendMonth(
            account_id=account_id,
            period=month_start(now),
            spent=money(month_spent),
            reserved=ZERO,
        )
    )
    db.flush()


def snapshot(
    db: Session, *, account_id: Any, now: datetime
) -> dict[str, Decimal | None]:
    wallet = _wallet(db, account_id)
    period = _month(db, account_id, month_start(now))
    known = wallet is not None and wallet.coverage_start is not None
    covered = known and month_start(wallet.coverage_start) < month_start(now)
    return {
        "lifetime_spent": wallet.lifetime_spent if known else None,
        "lifetime_reserved": wallet.lifetime_reserved if known else None,
        "month_spent": period.spent if period else ZERO if covered else None,
        "month_reserved": period.reserved if period else ZERO if covered else None,
    }


def reserve(
    db: Session,
    *,
    account_id: Any,
    operation_key: str,
    amount: Any,
    lifetime_limit: Any | None,
    monthly_limit: Any | None,
    now: datetime,
    credit_available: Decimal | None = None,
    price_multiplier: Decimal = Decimal("1"),
) -> models.HostedSpendReservation:
    """Reserve allowance then credit; repeated keys never permit redispatch.

    ``credit_available=None`` preserves the allowance-only path without reading
    credit tables. Otherwise the caller's snapshot opts in, but availability is
    refreshed under the account lock. Empty credit wallets retain legacy behavior.
    Callers commit the complete reservation before dispatch, or roll back on error.
    """
    amount = money(amount)
    if not operation_key or len(operation_key) > 128:
        raise HostedSpendUnavailableError("Hosted operation key is invalid")
    lock_account(db, account_id)
    existing = db.execute(
        select(models.HostedSpendReservation.id).where(
            models.HostedSpendReservation.account_id == account_id,
            models.HostedSpendReservation.operation_key == operation_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HostedSpendUnavailableError(
            "Hosted operation already exists; recover it without redispatch"
        )
    wallet = _wallet(db, account_id)
    if wallet is None or wallet.coverage_start is None:
        raise HostedSpendUnavailableError(
            "Historical hosted balance has not been verified"
        )
    period_key = month_start(now)
    period = _month(db, account_id, period_key)
    if period is None:
        if period_key <= month_start(wallet.coverage_start):
            raise HostedSpendUnavailableError(
                "This month's hosted balance has not been verified"
            )
        period = models.HostedSpendMonth(
            account_id=account_id, period=period_key, spent=ZERO, reserved=ZERO
        )
    if lifetime_limit is not None:
        if wallet.lifetime_spent is None:
            raise HostedSpendUnavailableError(
                "Lifetime hosted balance has not been verified"
            )
        if wallet.lifetime_spent + wallet.lifetime_reserved + amount > money(
            lifetime_limit
        ):
            raise HostedSpendExceededError("One-time hosted credit is exhausted")
    allowance_part = amount
    credit_part = ZERO
    multiplier = None
    available = None
    if credit_available is not None:
        from preloop.models.crud import hosted_credit

        available = hosted_credit.reservation_available(db, account_id=account_id)
    if available is not None:
        multiplier = hosted_credit.multiplier(price_multiplier)
        if multiplier is None:
            raise HostedSpendUnavailableError("Hosted credit price is unavailable")
        if monthly_limit is not None:
            remaining = max(money(monthly_limit) - period.spent - period.reserved, ZERO)
            allowance_part = min(amount, remaining)
        credit_part = money((amount - allowance_part) * multiplier)
        # The floor is an explicit hard stop. Above it, a negative credit
        # balance cannot block a request covered entirely by its allowance.
        if available <= hosted_credit.CREDIT_FLOOR or (
            credit_part > ZERO and credit_part > available
        ):
            raise HostedSpendExceededError("Prepaid credit is exhausted")
    elif monthly_limit is not None and period.spent + period.reserved + amount > money(
        monthly_limit
    ):
        raise HostedSpendExceededError(
            "Monthly hosted allowance is exhausted; extra spending is disabled"
        )
    db.add(period)
    wallet.lifetime_reserved += allowance_part
    period.reserved += allowance_part
    reservation = models.HostedSpendReservation(
        account_id=account_id,
        operation_key=operation_key,
        period=period_key,
        reserved=allowance_part,
        price_multiplier=multiplier,
        status="reserved",
    )
    db.add(reservation)
    db.flush()
    if credit_part > ZERO:
        hosted_credit.hold(
            db,
            account_id=account_id,
            amount=credit_part,
            operation_key=f"hosted-hold:{reservation.id}",
            actor="system",
            reservation_id=reservation.id,
            price_multiplier=multiplier,
        )
    return reservation


def _locked_reservation(
    db: Session, *, account_id: Any, reservation_id: Any
) -> models.HostedSpendReservation:
    lock_account(db, account_id)
    row = db.execute(
        select(models.HostedSpendReservation)
        .where(
            models.HostedSpendReservation.id == reservation_id,
            models.HostedSpendReservation.account_id == account_id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise HostedSpendUnavailableError("Hosted reservation is unavailable")
    return row


def mark_dispatched(db: Session, *, account_id: Any, reservation_id: Any) -> None:
    row = _locked_reservation(db, account_id=account_id, reservation_id=reservation_id)
    if row.status != "reserved":
        raise HostedSpendUnavailableError(
            "Hosted reservation cannot be dispatched again"
        )
    row.status = "dispatched"
    db.flush()


def settle(
    db: Session,
    *,
    account_id: Any,
    reservation_id: Any,
    actual: Any | None,
    proven_not_dispatched: bool = False,
) -> None:
    """Idempotent settlement; unknown dispatched cost retains its reservation.

    Never release on timeout/cancellation merely because no response arrived.
    A provider may have charged. Recovery must supply actual verified cost.
    """
    row = _locked_reservation(db, account_id=account_id, reservation_id=reservation_id)
    if row.status in {"settled", "released"}:
        if actual is not None and row.actual != money(actual):
            raise HostedSpendUnavailableError("Conflicting hosted settlement")
        return
    if actual is None and not (proven_not_dispatched and row.status == "reserved"):
        row.status = "recovery_required"
        db.flush()
        return
    if proven_not_dispatched and row.status != "reserved":
        raise HostedSpendUnavailableError(
            "A dispatched request cannot be released without settlement"
        )
    actual = ZERO if actual is None else money(actual)
    wallet, period = _wallet(db, account_id), _month(db, account_id, row.period)
    if wallet is None or period is None:
        raise HostedSpendUnavailableError("Hosted reservation balance is unavailable")
    allowance_actual = actual
    if row.price_multiplier is not None or row.credit_reserved > ZERO:
        from preloop.models.crud import hosted_credit

        allowance_actual = min(actual, row.reserved)
        hosted_credit.settle_reservation(
            db, reservation=row, provider_cost=actual - allowance_actual
        )
    wallet.lifetime_reserved -= row.reserved
    if wallet.lifetime_spent is not None:
        wallet.lifetime_spent += allowance_actual
    period.reserved -= row.reserved
    period.spent += allowance_actual
    row.actual = actual
    row.status = "released" if proven_not_dispatched else "settled"
    db.flush()


def launch_account_ids(
    db: Session, *, after_id: Any = None, limit: int = 100
) -> list[Any]:
    """Read a bounded stable page for launch initialization.

    Edition operator tooling uses this for account-balance-baseline reconciliation
    before hosted-spend activation; startup does not call it automatically.
    """
    if not 1 <= limit <= 500:
        raise ValueError("Launch batch size must be between 1 and 500")
    query = select(models.Account.id).order_by(models.Account.id).limit(limit)
    if after_id is not None:
        query = query.where(models.Account.id > after_id)
    return list(db.scalars(query))


def initialize_zero_launch(
    db: Session,
    *,
    account_id: Any,
    now: datetime,
    evidence: str,
    apply: bool = False,
) -> str:
    """Initialize missing state after an explicit zero-start decision.

    Existing balances and reservations are never reset. Caller commits each
    applied account separately. Preview is read-only and repeats checks on apply.
    Zero is consumed usage, not a replacement for the plan's included allowance.
    Edition operator tooling performs this pre-activation baseline reconciliation;
    startup does not call it automatically.
    """
    if not evidence or len(evidence) > 500:
        raise HostedSpendUnavailableError(
            "Launch evidence must contain 1-500 characters"
        )
    month_start(now)  # Refuse naive timestamps before any state change.
    if apply:
        lock_account(db, account_id)
    wallet = _wallet(db, account_id)
    if wallet is not None:
        if wallet.coverage_start is None or wallet.lifetime_spent is None:
            return "existing_state_requires_review"
        period = _month(db, account_id, month_start(now))
        if (period is not None and period.spent is None) or (
            period is None and month_start(wallet.coverage_start) >= month_start(now)
        ):
            return "existing_state_requires_review"
        return "already_initialized"
    # Even an orphan historical month or settled operation means this is not
    # an empty wallet. Do not repair it by silently giving fresh credit.
    for model in (models.HostedSpendMonth, models.HostedSpendReservation):
        present = db.scalar(
            select(model.id).where(model.account_id == account_id).limit(1)
        )
        if present is not None:
            return "existing_state_requires_review"
    if not apply:
        return "would_initialize"
    establish_baseline(
        db,
        account_id=account_id,
        lifetime_spent=ZERO,
        month_spent=ZERO,
        now=now,
        evidence=evidence,
    )
    return "initialized"


def launch_system_models(db: Session, *, limit: int = 101) -> list[models.AIModel]:
    """Read a bounded system-model set for tariff readiness checks.

    Edition operator tooling checks these before hosted-spend activation;
    startup does not attest the provider tariffs automatically.
    """
    if not 1 <= limit <= 501:
        raise ValueError("Model readiness limit must be between 1 and 501")
    return list(
        db.scalars(
            select(models.AIModel)
            .where(models.AIModel.account_id.is_(None))
            .order_by(models.AIModel.id)
            .limit(limit)
        )
    )


def recover_verified_cost(
    db: Session,
    *,
    account_id: Any,
    reservation_id: Any,
    actual: Any,
    evidence: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or settle one verified charge with an atomic durable audit.

    The caller must independently verify the provider's actual USD charge and
    commit the transaction. This helper makes no provider calls and never guesses
    zero from elapsed time or missing responses. Exact replays preserve the first
    audit; conflicting amounts are refused. Previously settled reservations are
    reported honestly without fabricating a recovery audit after the fact.
    """
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 500:
        raise HostedSpendUnavailableError("Recovery evidence requires 1-500 characters")
    actual = money(actual)
    if apply:
        row = _locked_reservation(
            db, account_id=account_id, reservation_id=reservation_id
        )
    else:
        row = db.scalar(
            select(models.HostedSpendReservation).where(
                models.HostedSpendReservation.account_id == account_id,
                models.HostedSpendReservation.id == reservation_id,
            )
        )
    if row is None:
        raise HostedSpendUnavailableError("Hosted reservation is unavailable")
    operation_key = "hosted-recovery:" + str(row.id)
    audit = db.scalar(
        select(models.BillingOperation).where(
            models.BillingOperation.account_id == account_id,
            models.BillingOperation.operation_key == operation_key,
        )
    )
    result = {
        "reservation_id": str(row.id),
        "account_id": str(row.account_id),
        "prior_status": row.status,
        "reserved_usd": str(row.reserved),
        "actual_usd": str(actual),
    }
    if row.status in {"settled", "released"}:
        if row.actual != actual:
            raise HostedSpendUnavailableError("Conflicting hosted settlement")
        if audit is not None:
            if (
                audit.kind != "hosted_recovery"
                or audit.status != "completed"
                or audit.payload.get("actual_usd") != str(actual)
            ):
                raise HostedSpendUnavailableError("Conflicting hosted recovery audit")
            return {**result, "status": "already_recovered", "audit_id": str(audit.id)}
        return {**result, "status": "already_settled_without_recovery_audit"}
    if (
        row.status not in {"reserved", "dispatched", "recovery_required"}
        or audit is not None
    ):
        raise HostedSpendUnavailableError("Hosted recovery state requires review")
    if not apply:
        return {**result, "status": "would_settle"}
    settle(db, account_id=account_id, reservation_id=row.id, actual=actual)
    audit = models.BillingOperation(
        account_id=account_id,
        operation_key=operation_key,
        kind="hosted_recovery",
        status="completed",
        payload={
            **result,
            "evidence": evidence.strip(),
            "recorded_at": datetime.now(UTC).isoformat(),
        },
        result={"status": "settled", "actual_usd": str(actual)},
    )
    db.add(audit)
    db.flush()
    return {**result, "status": "settled", "audit_id": str(audit.id)}


def unresolved_reservations(
    db: Session, *, account_id: Any, after_id: Any = None, limit: int = 100
) -> list[dict[str, str]]:
    """Read a bounded account-scoped recovery page without prompts or credentials."""
    if not 1 <= limit <= 100:
        raise ValueError("Recovery listing size must be between 1 and 100")
    query = (
        select(models.HostedSpendReservation)
        .where(
            models.HostedSpendReservation.account_id == account_id,
            models.HostedSpendReservation.status.in_(
                ("reserved", "dispatched", "recovery_required")
            ),
        )
        .order_by(models.HostedSpendReservation.id)
        .limit(limit)
    )
    if after_id is not None:
        query = query.where(models.HostedSpendReservation.id > after_id)
    return [
        {
            "reservation_id": str(row.id),
            "operation_key": row.operation_key,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "status": row.status,
            "reserved_usd": str(row.reserved),
            "period": row.period.isoformat(),
        }
        for row in db.scalars(query)
    ]
