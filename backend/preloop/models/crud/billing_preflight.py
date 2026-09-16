"""Read-only fleet aggregates for the pre-activation billing preflight.

Every function here answers one question an operator asks before entitlement
gating goes live: who is already over a cap, whose subscription state is
incomplete, and who is using a capability their plan will stop including.

All of it is aggregate and read-only. Nothing returns a customer name, email,
organization name or Stripe identifier: the answers are counts, so a preflight
report can be pasted into a change record. The EE operator script
``scripts/preflight_ladder.py`` is the only intended caller; startup does not
run any of this.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, literal, select
from sqlalchemy.orm import Session

from preloop.models import models

from .entitlement import entitlement_clause

#: Plan id assumed for an account without an entitled subscription.
DEFAULT_PLAN_ID = "free"


def _entitled_plan_subquery() -> Any:
    """Latest entitled subscription per account, as (account_id, plan_id)."""
    ranked = (
        select(
            models.Subscription.account_id.label("account_id"),
            models.Subscription.plan_id.label("plan_id"),
            func.row_number()
            .over(
                partition_by=models.Subscription.account_id,
                order_by=models.Subscription.created_at.desc(),
            )
            .label("rank"),
        )
        .where(entitlement_clause(models.Subscription))
        .subquery()
    )
    return select(ranked.c.account_id, ranked.c.plan_id).where(ranked.c.rank == 1)


def free_cap_overages(
    db: Session, *, max_users: int, max_agents: int
) -> dict[str, int]:
    """Count unentitled accounts already above the Free caps.

    These are the accounts that will meet a seat or agent refusal the first
    time they try to grow after gating goes live. Existing users and agents
    are never removed by the gate, so this is a "who gets stuck" count, not a
    "who loses access" count.

    Args:
        db: Read-only session.
        max_users: Free included users.
        max_agents: Free included agents.

    Returns:
        ``over_user_cap``, ``over_agent_cap`` and ``over_any_cap`` counts.
    """
    entitled = _entitled_plan_subquery().subquery()
    users = (
        select(
            models.User.account_id.label("account_id"),
            func.count(models.User.id).label("total"),
        )
        .where(models.User.is_active.is_(True))
        .group_by(models.User.account_id)
        .subquery()
    )
    agents = (
        select(
            models.ManagedAgent.account_id.label("account_id"),
            func.count(models.ManagedAgent.id).label("total"),
        )
        .where(models.ManagedAgent.lifecycle_state == "active")
        .group_by(models.ManagedAgent.account_id)
        .subquery()
    )
    over_users = func.coalesce(users.c.total, 0) > max_users
    over_agents = func.coalesce(agents.c.total, 0) > max_agents
    row = db.execute(
        select(
            func.count(case((over_users, 1))),
            func.count(case((over_agents, 1))),
            func.count(case(((over_users) | (over_agents), 1))),
        )
        .select_from(models.Account)
        .outerjoin(entitled, entitled.c.account_id == models.Account.id)
        .outerjoin(users, users.c.account_id == models.Account.id)
        .outerjoin(agents, agents.c.account_id == models.Account.id)
        .where(entitled.c.account_id.is_(None))
    ).one()
    return {
        "over_user_cap": int(row[0] or 0),
        "over_agent_cap": int(row[1] or 0),
        "over_any_cap": int(row[2] or 0),
    }


def entitled_accounts_by_plan(db: Session) -> dict[str, int]:
    """Count entitled accounts per plan id.

    The mirror image of :func:`free_cap_overages`, which only ever looks at
    accounts *without* an entitled subscription. The risk that section cannot
    see is a paying customer on a grandfathered plan whose persisted row
    carries limits the catalog does not: gating goes live and a customer who
    was promised "unlimited" meets a cap. Counting them by plan is the first
    half of the answer; the caller resolves each plan's effective limits.

    Args:
        db: Read-only session.

    Returns:
        ``{plan_id: account_count}``, empty when nobody is entitled.
    """
    entitled = _entitled_plan_subquery().subquery()
    rows = db.execute(
        select(entitled.c.plan_id, func.count(entitled.c.account_id)).group_by(
            entitled.c.plan_id
        )
    ).all()
    return {str(plan_id): int(count or 0) for plan_id, count in rows if plan_id}


def entitled_capacity_pressure(
    db: Session, *, plan_id: str, max_users: int, max_agents: int
) -> dict[str, int]:
    """How many entitled accounts on one plan sit at or above its caps.

    "Seats used" is active users plus pending invitations, matching what the
    seat gate actually counts, so the number here is the one a customer will
    meet. A cap of -1 means unlimited and is reported as zero pressure rather
    than skipped, so the shape of the answer never depends on the plan.

    Args:
        db: Read-only session.
        plan_id: Plan whose entitled accounts are measured.
        max_users: Effective seat ceiling, -1 for unlimited.
        max_agents: Effective agent ceiling, -1 for unlimited.

    Returns:
        ``at_or_over_user_cap``, ``over_user_cap``, ``at_or_over_agent_cap``,
        ``over_agent_cap``, ``over_any_cap`` and ``accounts``.
    """
    entitled = _entitled_plan_subquery().subquery()
    now = datetime.now(timezone.utc)
    users = (
        select(
            models.User.account_id.label("account_id"),
            func.count(models.User.id).label("total"),
        )
        .where(models.User.is_active.is_(True))
        .group_by(models.User.account_id)
        .subquery()
    )
    invites = (
        select(
            models.UserInvitation.account_id.label("account_id"),
            func.count(models.UserInvitation.id).label("total"),
        )
        .where(
            models.UserInvitation.status == models.UserInvitationStatus.PENDING,
            models.UserInvitation.expires_at > now,
        )
        .group_by(models.UserInvitation.account_id)
        .subquery()
    )
    agents = (
        select(
            models.ManagedAgent.account_id.label("account_id"),
            func.count(models.ManagedAgent.id).label("total"),
        )
        .where(models.ManagedAgent.lifecycle_state == "active")
        .group_by(models.ManagedAgent.account_id)
        .subquery()
    )
    seats = func.coalesce(users.c.total, 0) + func.coalesce(invites.c.total, 0)
    agent_total = func.coalesce(agents.c.total, 0)
    always_false = literal(1) == literal(0)
    at_users = seats >= max_users if max_users >= 0 else always_false
    over_users = seats > max_users if max_users >= 0 else always_false
    at_agents = agent_total >= max_agents if max_agents >= 0 else always_false
    over_agents = agent_total > max_agents if max_agents >= 0 else always_false
    row = db.execute(
        select(
            func.count(models.Account.id),
            func.count(case((at_users, 1))),
            func.count(case((over_users, 1))),
            func.count(case((at_agents, 1))),
            func.count(case((over_agents, 1))),
            func.count(case((over_users | over_agents, 1))),
        )
        .select_from(models.Account)
        .join(entitled, entitled.c.account_id == models.Account.id)
        .outerjoin(users, users.c.account_id == models.Account.id)
        .outerjoin(invites, invites.c.account_id == models.Account.id)
        .outerjoin(agents, agents.c.account_id == models.Account.id)
        .where(entitled.c.plan_id == plan_id)
    ).one()
    return {
        "accounts": int(row[0] or 0),
        "at_or_over_user_cap": int(row[1] or 0),
        "over_user_cap": int(row[2] or 0),
        "at_or_over_agent_cap": int(row[3] or 0),
        "over_agent_cap": int(row[4] or 0),
        "over_any_cap": int(row[5] or 0),
    }


def account_capacity_counts(db: Session, *, account_id: str) -> dict[str, int]:
    """Seat and agent counts for one account, as the gates count them.

    Args:
        db: Read-only session.
        account_id: Account to measure.

    Returns:
        ``active_users``, ``pending_invitations``, ``seats_used`` and
        ``active_agents``.
    """
    now = datetime.now(timezone.utc)
    active_users = int(
        db.execute(
            select(func.count(models.User.id)).where(
                models.User.account_id == account_id,
                models.User.is_active.is_(True),
            )
        ).scalar()
        or 0
    )
    pending = int(
        db.execute(
            select(func.count(models.UserInvitation.id)).where(
                models.UserInvitation.account_id == account_id,
                models.UserInvitation.status == models.UserInvitationStatus.PENDING,
                models.UserInvitation.expires_at > now,
            )
        ).scalar()
        or 0
    )
    agents = int(
        db.execute(
            select(func.count(models.ManagedAgent.id)).where(
                models.ManagedAgent.account_id == account_id,
                models.ManagedAgent.lifecycle_state == "active",
            )
        ).scalar()
        or 0
    )
    return {
        "active_users": active_users,
        "pending_invitations": pending,
        "seats_used": active_users + pending,
        "active_agents": agents,
    }


def entitled_without_revision(db: Session) -> int:
    """Count entitled subscriptions whose snapshot carries no revision.

    The console refuses a plan change without a revision, because a switch
    quoted from an unknown provider state cannot be verified on confirm. These
    accounts silently cannot self-serve until they reconcile once.
    """
    revision = models.Subscription.billing_state["revision"].astext
    return int(
        db.execute(
            select(func.count(models.Subscription.id)).where(
                entitlement_clause(models.Subscription),
                models.Subscription.stripe_subscription_id.isnot(None),
                (revision.is_(None)) | (revision == ""),
            )
        ).scalar()
        or 0
    )


def expired_trials(db: Session, *, now: datetime) -> int:
    """Count subscriptions still marked trialing past their period end."""
    return int(
        db.execute(
            select(func.count(models.Subscription.id)).where(
                models.Subscription.status == "trialing",
                models.Subscription.current_period_end < now,
            )
        ).scalar()
        or 0
    )


def accounts_without_hosted_wallet(db: Session) -> dict[str, int]:
    """Count accounts with no hosted balance, and the total account count.

    With the runtime zero-start baseline these accounts no longer need an
    operator pass: the wallet is created on their first built-in model call.
    The count stays in the preflight so the size of that population is known
    before activation.
    """
    wallets = select(models.HostedSpendAccount.account_id).subquery()
    total = int(db.execute(select(func.count(models.Account.id))).scalar() or 0)
    missing = int(
        db.execute(
            select(func.count(models.Account.id))
            .select_from(models.Account)
            .outerjoin(wallets, wallets.c.account_id == models.Account.id)
            .where(wallets.c.account_id.is_(None))
        ).scalar()
        or 0
    )
    return {"accounts": total, "without_hosted_wallet": missing}


def _per_plan_account_counts(db: Session, account_ids: Any) -> dict[str, int]:
    """Group a set of account ids by the plan they are entitled to today."""
    entitled = _entitled_plan_subquery().subquery()
    # One expression object, used in both the select list and the GROUP BY:
    # two equivalent copies render two bind parameters, which PostgreSQL does
    # not accept as the same grouping expression.
    plan_id = func.coalesce(entitled.c.plan_id, DEFAULT_PLAN_ID).label("plan_id")
    rows = db.execute(
        select(plan_id, func.count(models.Account.id))
        .select_from(models.Account)
        .outerjoin(entitled, entitled.c.account_id == models.Account.id)
        .where(models.Account.id.in_(account_ids))
        .group_by(plan_id)
    ).all()
    return {str(plan_id): int(count or 0) for plan_id, count in rows}


def gated_capability_usage(db: Session, *, since: datetime) -> dict[str, Any]:
    """Per-plan account counts for each capability the ladder gates.

    Each capability maps to the durable object or usage row that proves an
    account exercised it inside the window. An account appears under the plan
    it is entitled to today, so an operator can read "how many Free accounts
    are using price overrides" directly.

    Args:
        db: Read-only session.
        since: Window start; rows older than this are ignored.

    Returns:
        ``{capability: {plan_id: account_count}}``.
    """
    sources: dict[str, Any] = {
        "price_overrides": select(
            models.ModelPriceOverride.account_id.distinct()
        ).where(models.ModelPriceOverride.created_at >= since),
        "reconciliation": select(
            models.ProviderBillingSnapshot.account_id.distinct()
        ).where(models.ProviderBillingSnapshot.created_at >= since),
        "rbac": select(models.Team.account_id.distinct()).where(
            models.Team.created_at >= since
        ),
        "ai_optimization": select(
            models.RuntimeSessionOptimizationResult.account_id.distinct()
        ).where(models.RuntimeSessionOptimizationResult.created_at >= since),
    }
    return {
        capability: _per_plan_account_counts(db, query)
        for capability, query in sources.items()
    }


__all__ = [
    "account_capacity_counts",
    "accounts_without_hosted_wallet",
    "entitled_accounts_by_plan",
    "entitled_capacity_pressure",
    "entitled_without_revision",
    "expired_trials",
    "free_cap_overages",
    "gated_capability_usage",
]
