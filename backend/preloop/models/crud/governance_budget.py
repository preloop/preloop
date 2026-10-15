"""Funding-aware governance spend and atomic budget finalization primitives."""

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator
from uuid import UUID, uuid4

from sqlalchemy import Numeric, and_, cast, func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.governance_review import default_review_mode

PURPOSES = ("governance_session_digest", "governance_review")


def run_spend(
    db: Session,
    *,
    account_id: UUID,
    window_start: datetime,
    window_end: datetime,
    run_id: UUID,
) -> dict[str, float]:
    """Sum explicitly funded governance usage, including this run's later calls.

    Weekly stage B runs after the reporting window closes, so its calls must
    carry ``meta_data.governance_run_id``. Stage A calls are selected by their
    timestamp. The OR predicate counts a row once even if both selectors match.
    Unknown funding is deliberately excluded rather than charged to the platform.
    """
    usage = models.ApiUsage
    funding = usage.meta_data["funding"].astext
    rows = (
        db.query(
            funding,
            func.coalesce(func.sum(cast(usage.estimated_cost, Numeric)), 0.0),
        )
        .filter(
            usage.account_id == account_id,
            usage.action_type == "model_gateway",
            usage.meta_data["purpose"].astext.in_(PURPOSES),
            funding.in_(("platform", "account")),
            or_(
                and_(
                    usage.timestamp >= window_start,
                    usage.timestamp < window_end,
                    usage.meta_data["governance_run_id"].astext.is_(None),
                ),
                usage.meta_data["governance_run_id"].astext == str(run_id),
            ),
        )
        .group_by(funding)
        .all()
    )
    return {"platform": 0.0, "account": 0.0, **{k: float(v) for k, v in rows}}


@contextmanager
def finalization_transaction(
    db: Session, *, account_id: UUID, run_id: UUID
) -> Iterator[tuple[models.GovernanceReviewSettings, models.GovernanceReviewRun]]:
    """Lock settings and run, committing mode, costs and audit together.

    Seeding with ON CONFLICT also handles two first-time finalizers. A settings
    upsert from the console waits for the row lock. Refresh locked rows so a
    session's identity map cannot turn an old mode into a second transition.
    The caller must use CRUD writes with ``commit=False`` inside this context.
    """
    settings = models.GovernanceReviewSettings
    run = models.GovernanceReviewRun
    try:
        db.execute(
            insert(settings)
            .values(id=uuid4(), account_id=account_id, mode=default_review_mode())
            .on_conflict_do_nothing(constraint="uq_governance_review_settings_account")
        )
        locked_settings = (
            db.query(settings)
            .filter(settings.account_id == account_id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        locked_run = (
            db.query(run)
            .filter(run.id == run_id, run.account_id == account_id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        yield locked_settings, locked_run
        db.commit()
    except Exception:
        db.rollback()
        raise
