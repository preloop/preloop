"""CRUD operations for Plan and Subscription models."""

from datetime import date
from typing import List, Optional

from sqlalchemy.orm import Session

from preloop.plugins.account_hooks import billing_account_id

from ..models.plan import Plan, Subscription, MonthlyUsage
from .base import CRUDBase
from .entitlement import (
    ACTIVE_STATUSES,
    entitlement_clause,
    grandfather_clause,
)


class CRUDPlan(CRUDBase[Plan]):
    """CRUD operations for Plan model."""

    def get_active_public_plans(self, db: Session) -> List[Plan]:
        """Get all active public plans."""
        return (
            db.query(Plan)
            .filter(Plan.is_active, Plan.is_custom.is_(False))
            .order_by(Plan.created_at.asc())
            .all()
        )

    def get_active_custom_plans_for_account(
        self, db: Session, *, account_id: str
    ) -> List[Plan]:
        """Get all active custom plans for a specific account."""
        return (
            db.query(Plan)
            .filter(
                Plan.is_active,
                Plan.is_custom,
                Plan.account_id == account_id,
            )
            .all()
        )


class CRUDSubscription(CRUDBase[Subscription]):
    """CRUD operations for Subscription model."""

    def get_latest_for_account(
        self, db: Session, *, account_id: str
    ) -> Optional[Subscription]:
        """Get the latest subscription for an account.

        Reads the billing account's rows when account hook H7 names one
        (for example the parent that pays for this account).
        """
        account_id = billing_account_id(db, account_id)
        return (
            db.query(Subscription)
            .filter(Subscription.account_id == account_id)
            .order_by(Subscription.created_at.desc())
            .first()
        )

    def get_active_for_account(
        self, db: Session, *, account_id: str
    ) -> Optional[Subscription]:
        """Get the live subscription for an account, if it still is one.

        ``active`` or ``trialing`` only (never ``past_due``): callers are
        checkout de-duplication, the trial hosted-model cap and the ingestion
        quota, none of which should treat a dunning subscription as a live
        trial. A trial whose ``current_period_end`` has passed is not live and
        returns None, so those callers fall back to Free. The expiry rule is
        shared with every other entitlement lookup; see
        :mod:`preloop.models.crud.entitlement`.

        The withdrawn-plan grandfathering rule applies here too, and it has
        to: this lookup also decides whether a second checkout is refused. An
        account holding an unpaid row on a retired plan resolves to Free, so
        refusing its purchase would leave it on Free with no way off. With
        the rule applied, the row that no longer grants the retired plan's
        terms also stops blocking the sale.

        Account hook H7 may name another account whose subscription applies
        (for example a paying parent); its rows are read instead.
        """
        account_id = billing_account_id(db, account_id)
        return (
            db.query(Subscription)
            .filter(
                Subscription.account_id == account_id,
                entitlement_clause(Subscription, statuses=ACTIVE_STATUSES),
                grandfather_clause(Subscription, Plan),
            )
            .order_by(Subscription.created_at.desc())
            .first()
        )

    def get_by_stripe_subscription_id(
        self, db: Session, *, stripe_subscription_id: str
    ) -> Optional[Subscription]:
        """Get subscription by Stripe subscription ID."""
        return (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == stripe_subscription_id)
            .first()
        )


class CRUDMonthlyUsage(CRUDBase[MonthlyUsage]):
    """CRUD operations for MonthlyUsage model."""

    def get_for_current_cycle(
        self, db: Session, *, subscription_id: str, today: date
    ) -> Optional[MonthlyUsage]:
        """Get usage record for the current billing cycle."""
        return (
            db.query(MonthlyUsage)
            .filter(
                MonthlyUsage.subscription_id == subscription_id,
                MonthlyUsage.billing_cycle_start <= today,
                MonthlyUsage.billing_cycle_end >= today,
            )
            .first()
        )


plan = CRUDPlan(Plan)
subscription = CRUDSubscription(Subscription)
monthly_usage = CRUDMonthlyUsage(MonthlyUsage)
