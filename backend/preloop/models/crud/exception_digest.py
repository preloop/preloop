"""Atomic weekly exception snapshots, material state and synthesis reservations."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models

MAX_SYNTHESIS_USD = Decimal("0.02")
MAX_SYNTHESIS_CALLS = 2
MAX_SYNTHESIS_TOKENS = 12000
SYNTHESIS_DEADLINE_SECONDS = 20


def lock_week(
    db: Session, *, account_id: UUID, window_end: datetime
) -> models.ExceptionDigestWeek:
    """Serialize all recipient claims and reservations for this account/week."""
    db.execute(
        insert(models.ExceptionDigestWeek)
        .values(id=uuid4(), account_id=account_id, window_end=window_end)
        .on_conflict_do_nothing(constraint="uq_exception_digest_week")
    )
    return db.scalars(
        select(models.ExceptionDigestWeek)
        .where(
            models.ExceptionDigestWeek.account_id == account_id,
            models.ExceptionDigestWeek.window_end == window_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def state_map(
    db: Session, *, account_id: UUID, user_id: UUID, keys: list[str]
) -> dict[str, models.ExceptionDigestState]:
    """Return only this exact recipient's material-history rows."""
    return {
        row.exception_key: row
        for row in db.scalars(
            select(models.ExceptionDigestState).where(
                models.ExceptionDigestState.account_id == account_id,
                models.ExceptionDigestState.user_id == user_id,
                models.ExceptionDigestState.exception_key.in_(keys),
            )
        )
    }


def claim_delivery(
    db: Session,
    *,
    account_id: UUID,
    user_id: UUID,
    window_end: datetime,
    candidates: list[dict[str, Any]],
    sections: list[str],
    active_keys: set[str] | None = None,
    observed_scopes: set[str] | None = None,
) -> models.ExceptionDigestDelivery | None:
    """Freeze the first evaluated weekly shortlist and claim it exactly once.

    A quiet decision is durable too. Retrying later in the same week cannot
    move its window or turn the report into repeated polling notifications.
    """
    member = db.scalar(
        select(models.User.id).where(
            models.User.id == user_id,
            models.User.account_id == account_id,
            models.User.is_active.is_(True),
        )
    )
    if member is None:
        raise ValueError("Digest recipient is not an active account member")
    week = lock_week(db, account_id=account_id, window_end=window_end)
    existing = db.scalar(
        select(models.ExceptionDigestDelivery.id).where(
            models.ExceptionDigestDelivery.week_id == week.id,
            models.ExceptionDigestDelivery.user_id == user_id,
        )
    )
    if existing is not None:
        db.commit()
        return None
    if active_keys is not None and observed_scopes:
        # Clear only scopes completely observed this week. Truncated lists,
        # permission changes and unavailable sources cannot resolve history.
        for state in db.scalars(
            select(models.ExceptionDigestState).where(
                models.ExceptionDigestState.account_id == account_id,
                models.ExceptionDigestState.user_id == user_id,
                models.ExceptionDigestState.source_scope.in_(observed_scopes),
                models.ExceptionDigestState.exception_key.not_in(active_keys),
            )
        ):
            state.delivered_fingerprint = None
            state.acknowledged_fingerprint = None
    history = state_map(
        db,
        account_id=account_id,
        user_id=user_id,
        keys=[row["key"] for row in candidates],
    )
    selected = [
        row
        for row in candidates
        if row["key"] not in history
        or row["fingerprint"]
        not in {
            history[row["key"]].delivered_fingerprint,
            history[row["key"]].acknowledged_fingerprint,
        }
    ][:3]
    row = models.ExceptionDigestDelivery(
        week_id=week.id,
        user_id=user_id,
        status="preparing" if selected else "suppressed",
        reason=None if selected else "no_actionable_change",
        snapshot={"window_end": window_end.isoformat(), "cards": selected},
        sections=sections,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_delivery(
    db: Session, *, account_id: UUID, delivery_id: UUID
) -> models.ExceptionDigestDelivery | None:
    """Read a delivery only through its owning week/account."""
    return db.scalar(
        select(models.ExceptionDigestDelivery)
        .join(models.ExceptionDigestWeek)
        .where(
            models.ExceptionDigestDelivery.id == delivery_id,
            models.ExceptionDigestWeek.account_id == account_id,
        )
        .execution_options(populate_existing=True)
    )


def begin_dispatch(
    db: Session, *, account_id: UUID, delivery_id: UUID, snapshot: dict[str, Any]
) -> bool:
    """Commit the sole SMTP handoff claim before network I/O."""
    row = db.scalar(
        select(models.ExceptionDigestDelivery)
        .join(models.ExceptionDigestWeek)
        .where(
            models.ExceptionDigestDelivery.id == delivery_id,
            models.ExceptionDigestWeek.account_id == account_id,
        )
        .with_for_update(of=models.ExceptionDigestDelivery)
        .execution_options(populate_existing=True)
    )
    if row is None or row.status != "preparing":
        db.rollback()
        return False
    row.snapshot = snapshot
    row.status = "dispatching"
    db.commit()
    return True


def finish_delivery(
    db: Session,
    *,
    account_id: UUID,
    delivery_id: UUID,
    status: str,
    reason: str | None = None,
) -> None:
    """Persist one outcome; only confirmed SMTP success updates material history."""
    if status not in {"sent", "suppressed", "uncertain", "failed"}:
        raise ValueError("Invalid delivery outcome")
    row = get_delivery(db, account_id=account_id, delivery_id=delivery_id)
    if row is None or row.status not in {"preparing", "dispatching"}:
        return
    row.status, row.reason = status, reason
    if status == "sent":
        row.sent_at = datetime.now(timezone.utc)
        if row.snapshot.get("tracking_message_id"):
            message = db.scalar(
                select(models.Event).where(
                    models.Event.id == UUID(row.snapshot["tracking_message_id"]),
                    models.Event.account_id == account_id,
                    models.Event.user_id == row.user_id,
                    models.Event.event_type == "report_prepared",
                )
            )
            if message is not None:
                message.event_type = "report_sent"
        for card in row.snapshot["cards"]:
            statement = insert(models.ExceptionDigestState).values(
                id=uuid4(),
                account_id=account_id,
                user_id=row.user_id,
                exception_key=card["key"],
                source_scope=card.get("source_scope"),
                delivered_fingerprint=card["fingerprint"],
            )
            db.execute(
                statement.on_conflict_do_update(
                    constraint="uq_exception_digest_state",
                    set_={
                        "delivered_fingerprint": card["fingerprint"],
                        "source_scope": card.get("source_scope"),
                    },
                )
            )
    db.commit()


def acknowledge(
    db: Session,
    *,
    account_id: UUID,
    user_id: UUID,
    delivery_id: UUID,
    exception_key: str,
) -> bool:
    """Acknowledge only a fact actually delivered to this same member."""
    row = get_delivery(db, account_id=account_id, delivery_id=delivery_id)
    if row is None or row.user_id != user_id or row.status != "sent":
        return False
    card = next(
        (card for card in row.snapshot["cards"] if card["key"] == exception_key), None
    )
    if card is None:
        return False
    current = state_map(
        db, account_id=account_id, user_id=user_id, keys=[exception_key]
    ).get(exception_key)
    if current is None or current.delivered_fingerprint != card["fingerprint"]:
        # An old email cannot acknowledge a later recurrence or changed fact.
        return False
    statement = insert(models.ExceptionDigestState).values(
        id=uuid4(),
        account_id=account_id,
        user_id=user_id,
        exception_key=exception_key,
        acknowledged_fingerprint=card["fingerprint"],
    )
    db.execute(
        statement.on_conflict_do_update(
            constraint="uq_exception_digest_state",
            set_={"acknowledged_fingerprint": card["fingerprint"]},
        )
    )
    db.commit()
    return True


def reserve_synthesis(
    db: Session,
    *,
    account_id: UUID,
    window_end: datetime,
    estimated_usd: Decimal,
    input_tokens: int,
    output_tokens: int,
    now: datetime,
) -> datetime | None:
    """Reserve a worst-case call across every funding type, never refund ambiguity.

    Counters are pessimistic reservations, not provider invoices. Keeping each
    reservation after success also bounds restarts/repairs without a second
    reconciliation ledger. Zero/unknown/unbounded prices are refused.
    """
    if (
        not estimated_usd.is_finite()
        or estimated_usd <= 0
        or input_tokens < 1
        or output_tokens < 1
    ):
        return None
    week = lock_week(db, account_id=account_id, window_end=window_end)
    deadline = week.synthesis_deadline or now + timedelta(
        seconds=SYNTHESIS_DEADLINE_SECONDS
    )
    tokens = input_tokens + output_tokens
    if (
        now >= deadline
        or week.synthesis_calls >= MAX_SYNTHESIS_CALLS
        or week.synthesis_tokens + tokens > MAX_SYNTHESIS_TOKENS
        or week.synthesis_reserved_usd + estimated_usd > MAX_SYNTHESIS_USD
    ):
        db.rollback()
        return None
    week.synthesis_deadline = deadline
    week.synthesis_calls += 1
    week.synthesis_tokens += tokens
    week.synthesis_reserved_usd += estimated_usd
    db.commit()
    return deadline
