"""Match stranded hosted reservations to the usage rows that measured them.

A hosted call reserves its worst-case cost before dispatch. When settlement
could not see the provider's terminal usage, the reservation is left
``recovery_required`` with the full bound held. The gateway still wrote an
``api_usage`` row with the measured tokens, so the real cost is known; it
just never reached the ledger.

There is no stored key linking the two. ``hosted_spend_reservation.operation_key``
is a random ``uuid4`` minted inside ``HostedSpendService.prepare`` (EE billing
plugin), and the reservation id is carried only in memory by ``HostedCall``,
so neither appears on ``api_usage`` or in the gateway audit row. The join is
therefore temporal and deliberately conservative: a reservation is created
inside the request it pays for, so it must fall within
``[usage.created_at - usage.duration - slack, usage.created_at + slack]`` of
a successful usage row on the same account and hosted model. A pairing is
accepted only when it is one-to-one: that usage row contains exactly one
reservation of this account (of any status, so already-settled calls compete)
and that reservation sits inside exactly one such usage row. Anything else,
including retried attempts that share one usage row, is left for manual
review. Nothing is ever released without a measured cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import hosted_spend as ledger

DEFAULT_SLACK = timedelta(seconds=5)
MANUAL_REVIEW = "needs manual review"


@dataclass
class RecoveryRow:
    """One unresolved reservation and what recovery would do with it."""

    reservation_id: str
    created_at: str
    status: str
    reserved_usd: str
    api_usage_id: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    actual_usd: Optional[str] = None
    outcome: str = MANUAL_REVIEW
    reason: str = ""


def tariff_for(model: Any) -> dict[str, Decimal]:
    """Read the hosted tariff exactly as the billing plugin prices a call."""
    meta = model.meta_data if isinstance(model.meta_data, dict) else {}
    config = meta.get("hosted_metering")
    if not isinstance(config, dict):
        raise ValueError("Model has no hosted_metering tariff")
    tariff = {}
    for key in ("input_usd_per_million", "output_usd_per_million", "request_usd"):
        value = config.get(key)
        if value is None or isinstance(value, bool):
            raise ValueError(f"Hosted tariff is missing {key}")
        amount = Decimal(str(value))
        if amount < 0:
            raise ValueError(f"Hosted tariff {key} is negative")
        tariff[key] = amount
    return tariff


def measured_cost(
    tariff: dict[str, Decimal], prompt_tokens: int, completion_tokens: int
) -> Decimal:
    """Price one call the way ``HostedCall.finish`` does, rounded by the ledger."""
    return ledger.money(
        (
            Decimal(prompt_tokens) * tariff["input_usd_per_million"]
            + Decimal(completion_tokens) * tariff["output_usd_per_million"]
        )
        / Decimal(1_000_000)
        + tariff["request_usd"]
    )


def iter_unresolved(db: Session, *, account_id: Any) -> Iterator[dict[str, str]]:
    """Page every unresolved reservation of the account."""
    after_id = None
    while True:
        page = ledger.unresolved_reservations(
            db, account_id=account_id, after_id=after_id, limit=100
        )
        yield from page
        if len(page) < 100:
            return
        after_id = page[-1]["reservation_id"]


def _measured(usage: models.ApiUsage) -> bool:
    return all(
        type(value) is int and value >= 0
        for value in (usage.prompt_tokens, usage.completion_tokens)
    )


def plan_recovery(
    db: Session,
    *,
    account_id: Any,
    hosted_model_id: Any,
    slack: timedelta = DEFAULT_SLACK,
) -> list[RecoveryRow]:
    """Pair each unresolved reservation with its measured usage, read-only."""
    model = db.get(models.AIModel, hosted_model_id)
    if model is None or model.account_id is not None:
        raise ValueError("Hosted model must be an existing system model row")
    tariff = tariff_for(model)
    unresolved = list(iter_unresolved(db, account_id=account_id))
    if not unresolved:
        return []
    created = [
        db.get(models.HostedSpendReservation, row["reservation_id"]).created_at
        for row in unresolved
    ]
    window_start = min(created) - slack
    window_end = max(created) + timedelta(days=1)
    usages = [
        usage
        for usage in db.scalars(
            select(models.ApiUsage)
            .where(
                models.ApiUsage.account_id == account_id,
                models.ApiUsage.ai_model_id == model.id,
                models.ApiUsage.status_code == 200,
                models.ApiUsage.created_at >= window_start,
                models.ApiUsage.created_at <= window_end,
            )
            .order_by(models.ApiUsage.created_at, models.ApiUsage.id)
        )
    ]
    # Every reservation of the account competes, including settled ones, so
    # a usage row already paid for by a settled call is never reused.
    competitors = list(
        db.scalars(
            select(models.HostedSpendReservation).where(
                models.HostedSpendReservation.account_id == account_id,
                models.HostedSpendReservation.created_at
                >= window_start - timedelta(days=1),
                models.HostedSpendReservation.created_at <= window_end,
            )
        )
    )

    def covers(usage: models.ApiUsage, moment: Any) -> bool:
        start = usage.created_at - timedelta(seconds=usage.duration or 0) - slack
        return start <= moment <= usage.created_at + slack

    plan = []
    for row, moment in zip(unresolved, created, strict=False):
        entry = RecoveryRow(
            reservation_id=row["reservation_id"],
            created_at=row["created_at"],
            status=row["status"],
            reserved_usd=row["reserved_usd"],
        )
        plan.append(entry)
        if row["status"] != "recovery_required":
            # ``reserved``/``dispatched`` may still be in flight; its own
            # settlement must not race a recovery.
            entry.reason = f"status {row['status']} may still be in flight"
            continue
        containing = [usage for usage in usages if covers(usage, moment)]
        if not containing:
            entry.reason = "no successful usage row on the hosted model"
            continue
        if len(containing) > 1:
            entry.reason = f"{len(containing)} usage rows overlap this reservation"
            continue
        usage = containing[0]
        rivals = [
            other
            for other in competitors
            if str(other.id) != entry.reservation_id and covers(usage, other.created_at)
        ]
        if rivals:
            entry.api_usage_id = str(usage.id)
            entry.reason = (
                f"usage row also covers {len(rivals)} other reservation(s), "
                "e.g. a retried attempt"
            )
            continue
        entry.api_usage_id = str(usage.id)
        if not _measured(usage):
            entry.reason = "usage row has no measured token counts"
            continue
        entry.prompt_tokens = usage.prompt_tokens
        entry.completion_tokens = usage.completion_tokens
        actual = measured_cost(tariff, usage.prompt_tokens, usage.completion_tokens)
        if actual > Decimal(row["reserved_usd"]):
            entry.reason = "measured cost exceeds the reservation bound"
            continue
        entry.actual_usd = str(actual)
        entry.outcome = "matched"
    return plan


def recover(
    db: Session,
    *,
    account_id: Any,
    hosted_model_id: Any,
    apply: bool = False,
    slack: timedelta = DEFAULT_SLACK,
) -> list[RecoveryRow]:
    """Preview (default) or settle every matched reservation with evidence.

    Each settlement goes through ``recover_verified_cost``, which writes its
    own ``hosted_recovery`` billing audit. The caller commits.
    """
    plan = plan_recovery(
        db, account_id=account_id, hosted_model_id=hosted_model_id, slack=slack
    )
    for entry in plan:
        if entry.outcome != "matched":
            continue
        evidence = (
            f"measured usage from api_usage {entry.api_usage_id}: "
            f"{entry.prompt_tokens} input + {entry.completion_tokens} output "
            f"tokens at the hosted model tariff; stream settlement missed the "
            f"terminal usage chunk"
        )
        result = ledger.recover_verified_cost(
            db,
            account_id=account_id,
            reservation_id=entry.reservation_id,
            actual=entry.actual_usd,
            evidence=evidence,
            apply=apply,
        )
        entry.outcome = result["status"]
    return plan
