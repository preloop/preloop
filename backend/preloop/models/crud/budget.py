"""CRUD operations for Budget models."""

from typing import Optional, Sequence
from datetime import datetime, timedelta
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from .base import CRUDBase
from ..models.budget import BudgetPolicy, BudgetSpendActivity, BudgetPeriod

ACCOUNT_LEVEL_SUBJECT_TYPES = frozenset({"account", "global"})


class CRUDBudgetPolicy(CRUDBase[BudgetPolicy]):
    """CRUD operations for BudgetPolicy model."""

    def get_policies_for_subject(
        self,
        db: Session,
        account_id: uuid.UUID,
        subject_type: str,
        subject_id: Optional[uuid.UUID] = None,
    ) -> Sequence[BudgetPolicy]:
        """Get all budget policies configured for a specific subject.

        Account-level lookups (``subject_type`` in
        :data:`ACCOUNT_LEVEL_SUBJECT_TYPES` with ``subject_id=None``) return
        both ``account`` and ``global`` policies — they share the same spend
        bucket and either alias is sufficient; a second call is not required.
        """
        if subject_type in ACCOUNT_LEVEL_SUBJECT_TYPES and subject_id is None:
            query = select(self.model).where(
                self.model.account_id == account_id,
                self.model.subject_type.in_(tuple(ACCOUNT_LEVEL_SUBJECT_TYPES)),
                self.model.subject_id.is_(None),
            )
            return db.execute(query).scalars().all()

        query = select(self.model).where(
            self.model.account_id == account_id,
            self.model.subject_type == subject_type,
            self.model.subject_id == subject_id,
        )
        return db.execute(query).scalars().all()

    def remove(
        self, db: Session, *, id: uuid.UUID, account_id: str
    ) -> Optional[BudgetPolicy]:
        """Delete a budget policy strictly enforcing account ownership."""
        obj = (
            db.query(self.model)
            .filter(self.model.id == id, self.model.account_id == account_id)
            .first()
        )
        if obj:
            db.delete(obj)
            db.commit()
        return obj


class CRUDBudgetSpendActivity(CRUDBase[BudgetSpendActivity]):
    """CRUD operations for BudgetSpendActivity model."""

    def upsert_spend(
        self,
        db: Session,
        account_id: uuid.UUID,
        subject_type: str,
        subject_id: Optional[uuid.UUID],
        model_alias: Optional[str],
        period: BudgetPeriod,
        period_start: Optional[datetime],
        spend_increment_usd: float,
    ) -> BudgetSpendActivity:
        """Atomically upsert the spend activity logic using ON CONFLICT DO UPDATE.
        Returns the updated record.
        """
        effective_model_alias = model_alias if model_alias is not None else ""
        stmt = insert(BudgetSpendActivity).values(
            id=uuid.uuid4(),
            account_id=account_id,
            subject_type=subject_type,
            subject_id=subject_id,
            model_alias=effective_model_alias,
            period=period,
            period_start=period_start,
            spend_usd=spend_increment_usd,
        )

        # Increment spend on conflict. The bucket unique constraint is
        # NULLS NOT DISTINCT (PG >= 15, migration 20260712_budget_nnd) so
        # conflicts fire for NULL subject_id / period_start too — without it,
        # account-level buckets inserted a new row per request instead of
        # accumulating.
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "account_id",
                "subject_type",
                "subject_id",
                "model_alias",
                "period",
                "period_start",
            ],
            set_={"spend_usd": BudgetSpendActivity.spend_usd + stmt.excluded.spend_usd},
        ).returning(BudgetSpendActivity)

        result = db.execute(stmt).scalar_one()
        db.commit()
        return result

    def get_spend(
        self,
        db: Session,
        account_id: uuid.UUID,
        subject_type: str,
        subject_id: Optional[uuid.UUID],
        model_alias: Optional[str],
        period: BudgetPeriod,
        period_start: Optional[datetime],
    ) -> float:
        """Get the current spend for a specific bucket. Returns 0.0 if no spend recorded yet."""
        from sqlalchemy import func, or_

        conditions = [
            self.model.account_id == account_id,
            self.model.subject_type == subject_type,
            self.model.period == period,
        ]
        if subject_id is not None:
            conditions.append(self.model.subject_id == subject_id)
        else:
            conditions.append(self.model.subject_id.is_(None))

        if model_alias is not None:
            conditions.append(self.model.model_alias == model_alias)
        else:
            conditions.append(
                or_(self.model.model_alias.is_(None), self.model.model_alias == "")
            )

        if period_start is not None:
            conditions.append(self.model.period_start == period_start)
        else:
            conditions.append(self.model.period_start.is_(None))

        result = db.execute(
            select(func.coalesce(func.sum(self.model.spend_usd), 0.0)).where(
                *conditions
            )
        ).scalar_one()
        return float(result or 0.0)

    def get_spend_multi(
        self,
        db: Session,
        account_id: uuid.UUID,
        buckets: Sequence[
            tuple[
                str,
                Optional[uuid.UUID],
                Optional[str],
                BudgetPeriod,
                Optional[datetime],
            ]
        ],
    ) -> dict[
        tuple[
            str, Optional[uuid.UUID], Optional[str], BudgetPeriod, Optional[datetime]
        ],
        float,
    ]:
        """Fetch multiple spend buckets at once, summing duplicate rollup rows."""
        from sqlalchemy import func, or_, and_

        if not buckets:
            return {}

        conditions = []
        normalized_model_alias = func.coalesce(self.model.model_alias, "")
        for s_type, s_id, m_alias, period, p_start in buckets:
            conds = [
                self.model.subject_type == s_type,
                self.model.period == period,
            ]
            if s_id is not None:
                conds.append(self.model.subject_id == s_id)
            else:
                conds.append(self.model.subject_id.is_(None))

            if m_alias is not None:
                conds.append(self.model.model_alias == m_alias)
            else:
                conds.append(
                    or_(self.model.model_alias.is_(None), self.model.model_alias == "")
                )

            if p_start is not None:
                conds.append(self.model.period_start == p_start)
            else:
                conds.append(self.model.period_start.is_(None))

            conditions.append(and_(*conds))

        query = (
            select(
                self.model.subject_type,
                self.model.subject_id,
                normalized_model_alias.label("model_alias_key"),
                self.model.period,
                self.model.period_start,
                func.coalesce(func.sum(self.model.spend_usd), 0.0),
            )
            .where(self.model.account_id == account_id, or_(*conditions))
            .group_by(
                self.model.subject_type,
                self.model.subject_id,
                normalized_model_alias,
                self.model.period,
                self.model.period_start,
            )
        )

        rows = db.execute(query).all()
        result: dict[
            tuple[
                str,
                Optional[uuid.UUID],
                Optional[str],
                BudgetPeriod,
                Optional[datetime],
            ],
            float,
        ] = {}
        for row in rows:
            model_alias = row.model_alias_key or None
            result[
                (
                    row.subject_type,
                    row.subject_id,
                    model_alias,
                    row.period,
                    row.period_start,
                )
            ] = float(row[5] or 0.0)

        return result


crud_budget_policy = CRUDBudgetPolicy(BudgetPolicy)
crud_budget_spend = CRUDBudgetSpendActivity(BudgetSpendActivity)


def normalize_budget_subject_type(subject_type: str) -> str:
    """Map UI/API aliases to the subject types used for spend buckets."""
    if subject_type == "global":
        return "account"
    if subject_type == "api_keys":
        return "api_key"
    if subject_type == "managed_agents":
        return "managed_agent"
    if subject_type == "flows":
        return "flow"
    if subject_type == "users":
        return "user"
    return subject_type


def spend_bucket_for_policy(
    policy: BudgetPolicy,
) -> tuple[str, Optional[uuid.UUID], Optional[str]]:
    """Return the spend bucket coordinates for a configured policy."""
    if policy.subject_type in ACCOUNT_LEVEL_SUBJECT_TYPES:
        return ("account", None, policy.model_alias)
    if policy.subject_type == "ai_model":
        # Model-scoped policies aggregate spend via the model_alias bucket.
        return ("account", None, policy.model_alias)
    return (policy.subject_type, policy.subject_id, policy.model_alias)


def get_period_start(ts: datetime, period: BudgetPeriod) -> Optional[datetime]:
    """Return the inclusive start of the budget period containing ``ts``.

    Args:
        ts: Timestamp to truncate.
        period: Budget period granularity.

    Returns:
        Period start aligned to the period boundary, or ``None`` for
        :data:`BudgetPeriod.all_time`.
    """
    if period == BudgetPeriod.hourly:
        return ts.replace(minute=0, second=0, microsecond=0)
    elif period == BudgetPeriod.daily:
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == BudgetPeriod.weekly:
        # Monday is 0, Sunday is 6. Subtracting weekday() gets us back to Monday.
        dt = ts - timedelta(days=ts.weekday())
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == BudgetPeriod.monthly:
        return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == BudgetPeriod.yearly:
        return ts.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # BudgetPeriod.all_time
        return None


def get_period_end(ts: datetime, period: BudgetPeriod) -> Optional[datetime]:
    """Return the exclusive end of the budget period containing ``ts``.

    Args:
        ts: Timestamp whose period end should be computed.
        period: Budget period granularity.

    Returns:
        Period end (start of the next period), or ``None`` when the period
        is :data:`BudgetPeriod.all_time`.
    """
    start = get_period_start(ts, period)
    if start is None:
        return None
    from dateutil.relativedelta import relativedelta

    if period == BudgetPeriod.hourly:
        return start + relativedelta(hours=1)
    elif period == BudgetPeriod.daily:
        return start + relativedelta(days=1)
    elif period == BudgetPeriod.weekly:
        return start + relativedelta(weeks=1)
    elif period == BudgetPeriod.monthly:
        return start + relativedelta(months=1)
    elif period == BudgetPeriod.yearly:
        return start + relativedelta(years=1)
    return None


def record_spend_for_request(
    db: Session,
    account_id: uuid.UUID,
    subject_type: Optional[str],
    subject_id: Optional[str],
    model_alias: Optional[str],
    estimated_cost: float,
    timestamp: datetime,
    *,
    subject_scopes: Optional[Sequence[tuple[str, Optional[str]]]] = None,
) -> None:
    """Record gateway spend into budget buckets for every applicable scope.

    Upserts spend for the account and each configured subject scope (API key,
    managed agent, etc.) across all :class:`BudgetPeriod` values and, when
    ``model_alias`` is set, both the model-specific and account-wide buckets.

    Args:
        db: Database session.
        account_id: Owning account id.
        subject_type: Primary subject type for this request, if any.
        subject_id: Primary subject id for this request, if any.
        model_alias: Gateway model alias, when the spend is model-specific.
        estimated_cost: Estimated request cost in USD; non-positive values are
            ignored.
        timestamp: Request timestamp used to derive period boundaries.
        subject_scopes: Additional ``(subject_type, subject_id)`` pairs to
            record against (e.g. managed agent plus API key).
    """
    if estimated_cost <= 0:
        return

    periods = list(BudgetPeriod)

    # Always record at account level.
    subjects: list[tuple[str, Optional[uuid.UUID]]] = [("account", None)]

    scope_candidates: list[tuple[str, Optional[str]]] = list(subject_scopes or [])
    if subject_type and subject_id:
        scope_candidates.append((subject_type, subject_id))

    seen_scopes: set[tuple[str, Optional[uuid.UUID]]] = set()
    for raw_subject_type, raw_subject_id in scope_candidates:
        normalized_type = normalize_budget_subject_type(raw_subject_type)
        parsed_id: Optional[uuid.UUID] = None
        if raw_subject_id:
            try:
                parsed_id = uuid.UUID(str(raw_subject_id))
            except ValueError:
                continue
        scope = (normalized_type, parsed_id)
        if scope in seen_scopes or normalized_type == "account":
            continue
        seen_scopes.add(scope)
        subjects.append(scope)

    models = [None]  # All models
    if model_alias:
        models.append(model_alias)

    for s_type, s_id in subjects:
        for m_alias in models:
            for p in periods:
                crud_budget_spend.upsert_spend(
                    db=db,
                    account_id=account_id,
                    subject_type=s_type,
                    subject_id=s_id,
                    model_alias=m_alias,
                    period=p,
                    period_start=get_period_start(timestamp, p),
                    spend_increment_usd=estimated_cost,
                )
