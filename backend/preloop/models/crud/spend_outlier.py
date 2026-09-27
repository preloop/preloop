"""CRUD operations for spend outlier settings, findings and their inputs."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import String, cast, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from preloop.models import models

from .api_usage import exclude_replay_usage_condition
from .base import CRUDBase

ApiUsage = models.ApiUsage
SpendOutlierFinding = models.SpendOutlierFinding
SpendOutlierSettings = models.SpendOutlierSettings


@dataclass(frozen=True)
class UserModelDaySpend:
    """Gateway spend for one user, one model alias and one UTC day."""

    user_id: UUID
    day: date
    model: str
    cost_usd: float


@dataclass(frozen=True)
class SessionSpend:
    """Total gateway spend of one runtime session."""

    runtime_session_id: UUID
    user_id: Optional[UUID]
    cost_usd: float
    last_request_at: datetime


def _day_start(day: date) -> datetime:
    """Naive UTC midnight, matching ``api_usage.timestamp``."""
    return datetime.combine(day, time.min)


class CRUDSpendOutlierSettings(CRUDBase[SpendOutlierSettings]):
    """CRUD operations for per-account spend outlier thresholds."""

    def get_for_account(
        self, db: Session, *, account_id: UUID
    ) -> Optional[SpendOutlierSettings]:
        """The account's settings row, or None when it uses the defaults."""
        return (
            db.query(SpendOutlierSettings)
            .filter(SpendOutlierSettings.account_id == account_id)
            .first()
        )

    def upsert(
        self,
        db: Session,
        *,
        account_id: UUID,
        values: Dict[str, Any],
        commit: bool = True,
    ) -> SpendOutlierSettings:
        """Create or update the account's settings row in one statement.

        ``INSERT ... ON CONFLICT DO UPDATE`` on the account's unique key, so
        two first-time saves racing each other both succeed (the later write
        wins) instead of one failing on the constraint.

        Args:
            db: Database session.
            account_id: Owning account.
            values: Column values to write. Keys not present keep their
                current (or default) value.
            commit: Commit when True, flush otherwise.

        Returns:
            The stored settings row.
        """
        statement = pg_insert(SpendOutlierSettings).values(
            account_id=account_id, **values
        )
        if values:
            statement = statement.on_conflict_do_update(
                constraint="uq_spend_outlier_settings_account",
                set_={**values, "updated_at": func.now()},
            )
        else:
            statement = statement.on_conflict_do_nothing(
                constraint="uq_spend_outlier_settings_account"
            )
        db.execute(statement)
        if commit:
            db.commit()
        else:
            db.flush()
        row = self.get_for_account(db, account_id=account_id)
        assert row is not None  # the statement above guarantees the row
        db.refresh(row)
        return row

    def list_account_ids_with_session_threshold(self, db: Session) -> List[UUID]:
        """Accounts that turned the per-session rule on."""
        rows = (
            db.query(SpendOutlierSettings.account_id)
            .filter(SpendOutlierSettings.session_cost_threshold_usd.is_not(None))
            .all()
        )
        return [row.account_id for row in rows]


class CRUDSpendOutlierFinding(CRUDBase[SpendOutlierFinding]):
    """CRUD operations for recorded spend outlier findings."""

    def record(
        self,
        db: Session,
        *,
        account_id: UUID,
        rule: str,
        user_id: Optional[UUID],
        day: date,
        item_id: str,
        fingerprint: str,
        details: Dict[str, Any],
        detected_at: datetime,
        runtime_session_id: Optional[UUID] = None,
        commit: bool = True,
    ) -> Optional[SpendOutlierFinding]:
        """Store a finding unless one with this fingerprint already exists.

        A single ``INSERT ... ON CONFLICT DO NOTHING`` so two evaluations
        racing on the same day cannot both record the same alert.

        Returns:
            The new finding, or None when the fingerprint was already known.
        """
        statement = (
            pg_insert(SpendOutlierFinding)
            .values(
                account_id=account_id,
                rule=rule,
                user_id=user_id,
                runtime_session_id=runtime_session_id,
                day=day,
                item_id=item_id,
                fingerprint=fingerprint,
                details=details,
                detected_at=detected_at,
            )
            .on_conflict_do_nothing(
                constraint="uq_spend_outlier_finding_fingerprint",
            )
            .returning(SpendOutlierFinding.id)
        )
        finding_id = db.execute(statement).scalar_one_or_none()
        if commit:
            db.commit()
        else:
            db.flush()
        if finding_id is None:
            return None
        return db.get(SpendOutlierFinding, finding_id)

    def list_detected_since(
        self, db: Session, *, account_id: UUID, since: datetime
    ) -> List[SpendOutlierFinding]:
        """Every finding detected at or after ``since``, oldest first."""
        return (
            db.query(SpendOutlierFinding)
            .filter(SpendOutlierFinding.account_id == account_id)
            .filter(SpendOutlierFinding.detected_at >= since)
            .order_by(
                SpendOutlierFinding.detected_at.asc(),
                SpendOutlierFinding.fingerprint.asc(),
            )
            .all()
        )

    def set_dismissed(
        self,
        db: Session,
        *,
        account_id: UUID,
        item_id: str,
        fingerprint: str,
        dismissed_at: Optional[datetime],
        commit: bool = True,
    ) -> int:
        """Record (or, with None, clear) the dismissal of one finding.

        Returns:
            The number of findings updated (0 or 1).
        """
        updated = (
            db.query(SpendOutlierFinding)
            .filter(SpendOutlierFinding.account_id == account_id)
            .filter(SpendOutlierFinding.item_id == item_id)
            .filter(SpendOutlierFinding.fingerprint == fingerprint)
            .update(
                {SpendOutlierFinding.dismissed_at: dismissed_at},
                synchronize_session=False,
            )
        )
        if commit:
            db.commit()
        return int(updated)

    def list_account_ids_with_gateway_spend(
        self, db: Session, *, day: date
    ) -> List[UUID]:
        """Accounts with any priced gateway spend on the given UTC day.

        Uses the same filters as the spend queries the rules read, replay
        validation excluded, so an account whose only traffic was replay
        validation is not evaluated.
        """
        rows = (
            db.query(ApiUsage.account_id)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id.is_not(None),
                ApiUsage.estimated_cost > 0,
                exclude_replay_usage_condition(),
                ApiUsage.timestamp >= _day_start(day),
                ApiUsage.timestamp < _day_start(day + timedelta(days=1)),
            )
            .distinct()
            .all()
        )
        return [row.account_id for row in rows]

    def gateway_spend_by_user_model_day(
        self,
        db: Session,
        *,
        account_id: UUID,
        start_day: date,
        end_day: date,
        user_ids: Optional[Sequence[UUID]] = None,
    ) -> List[UserModelDaySpend]:
        """Gateway ``estimated_cost`` per user, model alias and UTC day.

        Covers ``[start_day, end_day]`` inclusive. Rows without a user, without
        a cost, or tagged as replay validation traffic are left out. The day
        bucket is ``date_trunc('day')`` of the naive UTC timestamp, the same
        bucket the Cost page's timeseries uses.
        """
        bucket = func.date_trunc("day", ApiUsage.timestamp)
        model = func.coalesce(ApiUsage.model_alias, "")
        query = db.query(
            ApiUsage.user_id.label("user_id"),
            bucket.label("day"),
            model.label("model"),
            func.sum(ApiUsage.estimated_cost).label("cost"),
        ).filter(
            ApiUsage.action_type == "model_gateway",
            ApiUsage.account_id == account_id,
            ApiUsage.user_id.is_not(None),
            ApiUsage.estimated_cost > 0,
            exclude_replay_usage_condition(),
            ApiUsage.timestamp >= _day_start(start_day),
            ApiUsage.timestamp < _day_start(end_day + timedelta(days=1)),
        )
        if user_ids is not None:
            query = query.filter(ApiUsage.user_id.in_(list(user_ids)))
        rows = query.group_by(ApiUsage.user_id, bucket, model).all()
        return [
            UserModelDaySpend(
                user_id=row.user_id,
                day=row.day.date(),
                model=row.model,
                cost_usd=float(row.cost or 0.0),
            )
            for row in rows
        ]

    def session_spend_over(
        self,
        db: Session,
        *,
        account_id: UUID,
        threshold_usd: float,
        active_since: datetime,
    ) -> List[SessionSpend]:
        """Sessions active since ``active_since`` whose total cost exceeds it.

        Only sessions with a gateway request in the window are summed, so a
        periodic check reads recent sessions and not the account's history.
        """
        naive_since = (
            active_since.astimezone(timezone.utc).replace(tzinfo=None)
            if active_since.tzinfo
            else active_since
        )
        active = (
            db.query(ApiUsage.runtime_session_id)
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                ApiUsage.runtime_session_id.is_not(None),
                ApiUsage.timestamp >= naive_since,
            )
            .distinct()
            .subquery()
        )
        total = func.sum(ApiUsage.estimated_cost)
        rows = (
            db.query(
                ApiUsage.runtime_session_id.label("runtime_session_id"),
                func.min(cast(ApiUsage.user_id, String)).label("user_id"),
                total.label("cost"),
                func.max(ApiUsage.timestamp).label("last_request_at"),
            )
            .filter(
                ApiUsage.action_type == "model_gateway",
                ApiUsage.account_id == account_id,
                ApiUsage.runtime_session_id.in_(db.query(active.c.runtime_session_id)),
                exclude_replay_usage_condition(),
            )
            .group_by(ApiUsage.runtime_session_id)
            .having(total > threshold_usd)
            .all()
        )
        return [
            SessionSpend(
                runtime_session_id=row.runtime_session_id,
                user_id=UUID(row.user_id) if row.user_id else None,
                cost_usd=float(row.cost or 0.0),
                last_request_at=row.last_request_at,
            )
            for row in rows
        ]

    def user_display_names(
        self, db: Session, *, account_id: UUID, user_ids: Sequence[UUID]
    ) -> Dict[UUID, str]:
        """Map user id to the name a card shows, in one query."""
        if not user_ids:
            return {}
        user = models.User
        rows = (
            db.query(user.id, user.full_name, user.username, user.email)
            .filter(user.account_id == account_id)
            .filter(user.id.in_(list(set(user_ids))))
            .all()
        )
        return {
            row.id: (row.full_name or row.username or row.email or str(row.id))
            for row in rows
        }

    def session_titles(
        self, db: Session, *, account_id: UUID, session_ids: Sequence[UUID]
    ) -> Dict[UUID, str]:
        """Map runtime session id to its title, where it has one."""
        if not session_ids:
            return {}
        session_model = models.RuntimeSession
        rows = (
            db.query(session_model.id, session_model.title)
            .filter(session_model.account_id == account_id)
            .filter(session_model.id.in_(list(set(session_ids))))
            .all()
        )
        return {row.id: row.title for row in rows if row.title}


crud_spend_outlier_settings = CRUDSpendOutlierSettings(SpendOutlierSettings)
crud_spend_outlier_finding = CRUDSpendOutlierFinding(SpendOutlierFinding)
