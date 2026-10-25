"""Whether one account can switch to one plan, and what the switch costs it.

The console used to answer this with adjectives. It offered every catalog
plan as a target, then hedged with "not enough evidence to confirm a fit" and
a permanent retention warning shown to accounts with no data old enough to be
deleted. A reader could not tell from that whether Pro was a mistake for a
four person account (it is: Pro includes one user) or whether switching would
delete anything (usually it would not).

This module answers with numbers instead, so the console renders text it did
not compose:

Refusals (``eligible`` false)
    Seats and agents, because those two are gates the platform actually
    enforces. An account with four members cannot hold a one member plan: the
    seat gate refuses the next invitation, and the switch would strand it.

Consequences (``eligible`` stays true, a warning carries the number)
    The monthly ingestion quota and analytics retention. Exhausting the
    ingestion quota degrades analytics detail and never blocks agent traffic,
    and a shorter retention window deletes old analytics, which a reader may
    legitimately accept when downgrading. Refusing either would trap an
    account that wants a cheaper plan; saying nothing would delete data by
    surprise. So they are stated, with the date and the count, and the reader
    decides.

Nothing here reads the plan catalog: limits arrive as :class:`PlanLimits`
from whoever owns the catalog (the EE billing plugin at runtime, a fixture in
tests), so a price or limit change in ``plans.yaml`` never needs a code change
here and no test can pin yesterday's numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from preloop.models import models

from .api_usage import exclude_customer_overhead_condition
from .history_policy import (
    HISTORY_RETENTION_KEY,
    longest_history_promise,
    subscription_history_retention,
)

#: A limit of -1 means "no limit", as in ``plans.yaml``.
UNLIMITED = -1

#: Analytics record classes a plan's ``retention_days`` governs, with the
#: column that dates a row. Audit, approvals and evidence are deliberately
#: absent: they follow the account retention policy and the legal floor in
#: ``preloop.services.retention_policy``, not the plan.
CLASS_USAGE = "usage"
CLASS_RUNTIME_SESSIONS = "runtime_sessions"

#: Labels used in the sentence shown to the reader.
CLASS_LABELS = {
    CLASS_USAGE: "usage records",
    CLASS_RUNTIME_SESSIONS: "runtime sessions",
}

#: Only gateway traffic counts against an ingestion quota.
INGEST_ACTION_TYPE = "model_gateway"


@dataclass(frozen=True)
class PlanLimits:
    """One candidate plan's limits, as the caller's catalog states them.

    Args:
        plan_id: Catalog id.
        name: Plan name as shown to the reader.
        included_users: Users the flat price includes, -1 for unlimited.
        max_users: Hard seat ceiling including any seat add-on, -1 for
            unlimited. Equal to ``included_users`` for a plan with no add-on.
        max_agents: Active agent ceiling, -1 for unlimited.
        ingest_tokens_monthly: Monthly ingestion quota in tokens, -1 for
            unlimited, None when the plan does not state one.
        retention_days: Analytics history window in days, -1 for unlimited.
        purchasable: False for a quote-only plan, which is never a checkout
            target.
    """

    plan_id: str
    name: str
    included_users: int = UNLIMITED
    max_users: int = UNLIMITED
    max_agents: int = UNLIMITED
    ingest_tokens_monthly: Optional[int] = None
    retention_days: int = UNLIMITED
    purchasable: bool = True


@dataclass(frozen=True)
class AccountState:
    """What the account is today, measured once and compared to every plan.

    Args:
        active_users: Active user rows.
        pending_invitations: Unexpired pending invitations. These hold a seat
            already, which is why the seat gate counts them, so they are
            counted here too.
        active_agents: Agents in lifecycle state ``active``.
        ingest_tokens_this_month: Tokens ingested since the start of the
            current UTC month, None when it was not measured.
        oldest_record_at: Oldest retained analytics record over every class,
            None when the account has no analytics at all.
        oldest_record_class: Which class that record belongs to.
        retention_floor_days: Longest retention promise the account already
            holds, -1 for unlimited, None for no promise. A floor protects
            records a shorter plan would otherwise delete.
        legal_hold: True when at least one analytics record is under a legal
            hold, which survives any plan change.
        current_retention_days: The current plan's window, used only to phrase
            a longer window as a benefit rather than a warning.
    """

    active_users: int = 0
    pending_invitations: int = 0
    active_agents: int = 0
    ingest_tokens_this_month: Optional[int] = None
    oldest_record_at: Optional[datetime] = None
    oldest_record_class: Optional[str] = None
    retention_floor_days: Optional[int] = None
    legal_hold: bool = False
    current_retention_days: Optional[int] = None

    @property
    def members(self) -> int:
        """Seats held: active users plus unexpired pending invitations."""
        return self.active_users + self.pending_invitations


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    return f"{count:,} {singular if count == 1 else (plural or singular + 's')}"


def _utc(value: datetime) -> datetime:
    """A naive column and an aware one cannot be compared; make both aware."""
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _day(value: datetime) -> str:
    """One UTC calendar day, the only precision a retention date deserves."""
    return _utc(value).strftime("%Y-%m-%d")


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else _utc(value).isoformat()


def _members_blocker(limits: PlanLimits, state: AccountState) -> dict[str, Any]:
    """Say the count, the cap and the arithmetic between them."""
    breakdown = (
        ""
        if not state.pending_invitations
        else " ({} and {})".format(
            _plural(state.active_users, "active user"),
            _plural(state.pending_invitations, "pending invitation"),
        )
    )
    if limits.max_users > limits.included_users >= 0:
        capacity = (
            f"{limits.name} includes {limits.included_users:,} "
            f"and allows up to {limits.max_users:,} with the seat add-on"
        )
    else:
        capacity = f"{limits.name} includes {limits.included_users:,}"
    excess = state.members - limits.max_users
    return {
        "kind": "members",
        "current": state.members,
        "limit": limits.max_users,
        "message": (
            f"You have {_plural(state.members, 'member')}{breakdown}; "
            f"{capacity}. Remove {_plural(excess, 'member')} to switch."
        ),
    }


def _agents_blocker(limits: PlanLimits, state: AccountState) -> dict[str, Any]:
    excess = state.active_agents - limits.max_agents
    return {
        "kind": "agents",
        "current": state.active_agents,
        "limit": limits.max_agents,
        "message": (
            f"You have {_plural(state.active_agents, 'active agent')}; "
            f"{limits.name} includes {limits.max_agents:,}. "
            f"Deactivate {_plural(excess, 'agent')} to switch. "
            "A plan change never deletes an agent."
        ),
    }


def _ingest_warning(limits: PlanLimits, state: AccountState) -> dict[str, Any]:
    quota = limits.ingest_tokens_monthly or 0
    return {
        "kind": "ingest",
        "current": state.ingest_tokens_this_month,
        "limit": quota,
        "message": (
            f"You have ingested {state.ingest_tokens_this_month:,} tokens so far "
            f"this month; {limits.name} includes {quota:,} per month. "
            "Above the quota analytics detail is reduced; the gateway, "
            "firewall, approvals and budgets keep running."
        ),
    }


def retention_effect(
    limits: PlanLimits, state: AccountState, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """What this plan's history window does to the records the account has.

    ``affected`` is true only when a record that exists today falls outside
    the target window and no floor protects it. A window that is longer than
    the current one produces ``benefit_message`` and no warning: an upgrade
    cannot delete anything, and telling a reader that upgrading might lose
    data (which the console did) is simply false.

    Args:
        limits: Target plan limits.
        state: Measured account state.
        now: Evaluation instant, defaulting to now in UTC.

    Returns:
        ``oldest_record_at``, ``target_days``, ``cutoff_at``, ``affected``,
        ``protected_by_floor``, ``floor_days``, ``message`` and
        ``benefit_message``. ``target_days`` and ``cutoff_at`` are None when
        the plan keeps history without limit.
    """
    now = now or datetime.now(timezone.utc)
    days = limits.retention_days
    floor = state.retention_floor_days
    result: dict[str, Any] = {
        "oldest_record_at": _iso(state.oldest_record_at),
        "oldest_record_class": state.oldest_record_class,
        "target_days": None if days == UNLIMITED else days,
        "cutoff_at": None,
        "affected": False,
        "protected_by_floor": False,
        "floor_days": floor,
        "legal_hold": state.legal_hold,
        "message": None,
        "benefit_message": None,
    }
    current = state.current_retention_days
    if days == UNLIMITED:
        if current is not None and current != UNLIMITED:
            result["benefit_message"] = (
                f"{limits.name} keeps analytics history without a time limit, "
                f"up from {current:,} days."
            )
        return result
    if current is not None and current != UNLIMITED and current <= days:
        # The window does not shrink, so this switch deletes nothing. A
        # longer window is stated as a benefit; an identical one is silent.
        # An unlimited current window is the opposite case and falls through:
        # every finite target is shorter than it.
        if current < days:
            result["benefit_message"] = (
                f"Analytics history extends from {current:,} to {days:,} days. "
                "Records already deleted are not restored."
            )
        return result
    cutoff = _utc(now) - timedelta(days=days)
    result["cutoff_at"] = _iso(cutoff)
    oldest = state.oldest_record_at
    if oldest is None:
        return result
    oldest = _utc(oldest)
    if oldest >= cutoff:
        return result
    protected = floor is not None and (floor == UNLIMITED or floor > days)
    result["protected_by_floor"] = protected
    hold = (
        " Records under a legal hold are kept for as long as the hold lasts."
        if state.legal_hold
        else ""
    )
    label = CLASS_LABELS.get(state.oldest_record_class or "", "analytics records")
    if protected:
        promise = (
            "without a time limit" if floor == UNLIMITED else f"for {floor:,} days"
        )
        result["message"] = (
            f"Your {label} go back to {_day(oldest)}. {limits.name} keeps "
            f"{days:,} days, but your account already keeps analytics "
            f"{promise}, so this switch deletes nothing.{hold}"
        )
        return result
    result["affected"] = True
    result["message"] = (
        f"Your {label} go back to {_day(oldest)}. {limits.name} keeps "
        f"{days:,} days, so records before {_day(cutoff)} will be deleted "
        f"after the switch.{hold}"
    )
    return result


def evaluate_plan(
    limits: PlanLimits, state: AccountState, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Compare one account to one plan.

    Args:
        limits: Target plan limits, from the catalog.
        state: Measured account state.
        now: Evaluation instant, defaulting to now in UTC.

    Returns:
        ``plan_id``, ``eligible``, ``blockers``, ``warnings`` and
        ``retention``. Every blocker and warning carries ``kind``,
        ``current``, ``limit`` and a ``message`` already written for a reader.
        An empty ``blockers`` and ``warnings`` means there is nothing to say,
        and the console says nothing.
    """
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if limits.max_users != UNLIMITED and state.members > limits.max_users:
        blockers.append(_members_blocker(limits, state))
    if limits.max_agents != UNLIMITED and state.active_agents > limits.max_agents:
        blockers.append(_agents_blocker(limits, state))
    quota = limits.ingest_tokens_monthly
    if (
        quota is not None
        and quota != UNLIMITED
        and state.ingest_tokens_this_month is not None
        and state.ingest_tokens_this_month > quota
    ):
        warnings.append(_ingest_warning(limits, state))
    retention = retention_effect(limits, state, now=now)
    if retention["affected"]:
        warnings.append(
            {
                "kind": "retention",
                "current": retention["oldest_record_at"],
                "limit": retention["target_days"],
                "message": retention["message"],
            }
        )
    return {
        "plan_id": limits.plan_id,
        "eligible": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "retention": retention,
    }


def evaluate_plans(
    plans: Sequence[PlanLimits],
    state: AccountState,
    *,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Evaluate every candidate plan against one measured account state."""
    return [evaluate_plan(plan, state, now=now) for plan in plans]


def month_start(now: datetime) -> datetime:
    """Start of ``now``'s UTC calendar month, the ingestion quota window."""
    moment = (
        now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    )
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def ingest_tokens_this_month(
    db: Session,
    *,
    account_id: str,
    now: Optional[datetime] = None,
    model_ids: Optional[Sequence[str]] = None,
) -> int:
    """Tokens ingested since the start of the current UTC month.

    Counts the same rows the quota counts: gateway traffic, replay-validation and governance
    measurement excluded, negative or missing token counts ignored rather than
    guessed.

    Args:
        db: Read-only session.
        account_id: Account to measure.
        now: Evaluation instant, defaulting to now in UTC.
        model_ids: Restrict to these models (the caller's BYOK models). None
            counts every gateway row, which is what a deployment with no
            hosted models wants.

    Returns:
        Token count, zero when there is nothing to count.
    """
    usage = models.ApiUsage
    conditions = [
        usage.account_id == account_id,
        usage.timestamp >= month_start(now or datetime.now(timezone.utc)),
        usage.action_type == INGEST_ACTION_TYPE,
        usage.total_tokens.is_not(None),
        usage.total_tokens >= 0,
        exclude_customer_overhead_condition(),
    ]
    if model_ids is not None:
        if not model_ids:
            return 0
        conditions.append(usage.ai_model_id.in_(list(model_ids)))
    return int(
        db.execute(
            select(func.coalesce(func.sum(usage.total_tokens), 0)).where(*conditions)
        ).scalar()
        or 0
    )


def analytics_data_age(db: Session, *, account_id: str) -> dict[str, Any]:
    """Oldest retained analytics record per class, and whether a hold covers one.

    A runtime session is dated by when it started, matching the purge, which
    keeps a session alive until its last timestamp passes the cutoff. Dating
    it by ``ended_at`` would report an abandoned session as older than the
    purge treats it.

    Args:
        db: Read-only session.
        account_id: Account to measure.

    Returns:
        ``per_class`` (ISO timestamp or None per class), ``oldest_record_at``,
        ``oldest_record_class`` and ``legal_hold``.
    """
    oldest_usage = db.execute(
        select(func.min(models.ApiUsage.timestamp)).where(
            models.ApiUsage.account_id == account_id
        )
    ).scalar()
    oldest_session = db.execute(
        select(func.min(models.RuntimeSession.started_at)).where(
            models.RuntimeSession.account_id == account_id
        )
    ).scalar()
    held = bool(
        db.execute(
            select(func.count(models.RuntimeSession.id)).where(
                models.RuntimeSession.account_id == account_id,
                models.RuntimeSession.legal_hold.is_(True),
            )
        ).scalar()
        or 0
    )
    per_class = {CLASS_USAGE: oldest_usage, CLASS_RUNTIME_SESSIONS: oldest_session}
    dated = [(value, name) for name, value in per_class.items() if value is not None]
    oldest, oldest_class = min(dated, default=(None, None))
    return {
        "per_class": {name: _iso(value) for name, value in per_class.items()},
        "oldest_record_at": _iso(oldest),
        "oldest_record_class": oldest_class,
        "legal_hold": held,
    }


def protected_floor_days(db: Session, account: models.Account) -> Optional[int]:
    """Longest analytics retention this account is already promised.

    Reads the durable account floor, its metadata mirror and every persisted
    subscription's plan terms, exactly as
    :func:`preloop.services.analytics_history.storage_history_days` does, so
    the console cannot warn about deleting records the purge is forbidden to
    delete.

    Args:
        db: Read-only session.
        account: Account whose promises are read.

    Returns:
        Days, -1 for unlimited, or None when no promise exists.
    """
    promised = longest_history_promise(
        account.subscription_history_retention_days,
        (account.meta_data or {}).get(HISTORY_RETENTION_KEY),
        subscription_history_retention(db, account_id=account.id),
    )
    return None if promised == 0 else promised


def account_state(
    db: Session,
    account: models.Account,
    *,
    counts: dict[str, int],
    now: Optional[datetime] = None,
    model_ids: Optional[Sequence[str]] = None,
    current_retention_days: Optional[int] = None,
) -> AccountState:
    """Measure one account once, for comparison against every candidate plan.

    Args:
        db: Read-only session.
        account: Account to measure.
        counts: ``active_users``, ``pending_invitations`` and ``active_agents``
            as the seat and agent gates count them.
        now: Evaluation instant, defaulting to now in UTC.
        model_ids: BYOK model ids for the ingestion measurement.
        current_retention_days: The current plan's analytics window.

    Returns:
        The :class:`AccountState` the evaluation functions take.
    """
    age = analytics_data_age(db, account_id=str(account.id))
    oldest = age["oldest_record_at"]
    return AccountState(
        active_users=int(counts.get("active_users") or 0),
        pending_invitations=int(counts.get("pending_invitations") or 0),
        active_agents=int(counts.get("active_agents") or 0),
        ingest_tokens_this_month=ingest_tokens_this_month(
            db, account_id=str(account.id), now=now, model_ids=model_ids
        ),
        oldest_record_at=datetime.fromisoformat(oldest) if oldest else None,
        oldest_record_class=age["oldest_record_class"],
        retention_floor_days=protected_floor_days(db, account),
        legal_hold=age["legal_hold"],
        current_retention_days=current_retention_days,
    )


__all__ = [
    "AccountState",
    "PlanLimits",
    "UNLIMITED",
    "account_state",
    "analytics_data_age",
    "evaluate_plan",
    "evaluate_plans",
    "ingest_tokens_this_month",
    "month_start",
    "protected_floor_days",
    "retention_effect",
]
