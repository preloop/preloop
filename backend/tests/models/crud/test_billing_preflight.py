"""Fleet preflight aggregates and the keyset walk the reconcile task rides on.

Both are read-only fleet-wide queries, so every assertion here is a delta
against the rows the test itself created: the aggregates deliberately count the
whole database and another fixture's account is a legitimate part of that
answer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.models.crud.billing import billing
from preloop.models.crud import billing_preflight


def _account(db, name_hint: str = "preflight"):
    return crud_account.create(
        db,
        obj_in={
            "organization_name": f"{name_hint} {uuid.uuid4().hex[:8]}",
            "is_active": True,
        },
    )


def _user(db, account, *, is_active: bool = True):
    unique = uuid.uuid4().hex[:8]
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": f"preflight_{unique}@example.com",
            "username": f"preflight_{unique}",
            "hashed_password": "x",
            "is_active": is_active,
        },
    )


def _agent(db, account, *, lifecycle_state: str = "active"):
    now = datetime.now(timezone.utc)
    agent = models.ManagedAgent(
        account_id=account.id,
        agent_kind="cli",
        session_source_type="test",
        session_source_id=uuid.uuid4().hex,
        display_name="preflight agent",
        lifecycle_state=lifecycle_state,
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db.add(agent)
    db.flush()
    return agent


def _plan(db, plan_id: str):
    plan = models.Plan(
        id=plan_id,
        name=f"Plan {plan_id}",
        price_monthly=10.0,
        price_annually=100.0,
        features={},
        is_active=True,
    )
    db.add(plan)
    db.flush()
    return plan


def _subscription(db, account, plan_id: str, **overrides):
    now = datetime.now(timezone.utc)
    values = {
        "account_id": account.id,
        "plan_id": plan_id,
        "status": "active",
        "current_period_start": now - timedelta(days=1),
        "current_period_end": now + timedelta(days=29),
        "stripe_subscription_id": f"sub_{uuid.uuid4().hex[:16]}",
        "billing_state": {"revision": "rev-1"},
    }
    values.update(overrides)
    subscription = models.Subscription(**values)
    db.add(subscription)
    db.flush()
    return subscription


def _price_override(db, account, *, created_at: datetime):
    override = models.ModelPriceOverride(
        account_id=account.id,
        model_alias=f"model-{uuid.uuid4().hex[:8]}",
        currency="USD",
        input_price_per_1k=1.0,
        created_at=created_at,
    )
    db.add(override)
    db.flush()
    return override


class TestStripeSubscriptionPage:
    """The reconcile task walks the fleet with this; it must not lose rows."""

    def test_keyset_paging_visits_every_linked_row_exactly_once(self, db_session):
        plan_id = _plan(db_session, f"paging-{uuid.uuid4().hex[:8]}").id
        linked = {
            str(_subscription(db_session, _account(db_session), plan_id).id)
            for _ in range(5)
        }

        seen: list[str] = []
        cursor = None
        for _ in range(20):
            page = billing.stripe_subscription_page(
                db_session, after_id=cursor, limit=2
            )
            if not page:
                break
            seen.extend(row["id"] for row in page)
            cursor = page[-1]["id"]

        assert len(seen) == len(set(seen))
        assert linked <= set(seen)
        assert seen == sorted(seen)

    def test_rows_without_a_stripe_id_are_never_returned(self, db_session):
        plan_id = _plan(db_session, f"paging-{uuid.uuid4().hex[:8]}").id
        unlinked = _subscription(
            db_session, _account(db_session), plan_id, stripe_subscription_id=None
        )

        cursor = None
        while True:
            page = billing.stripe_subscription_page(
                db_session, after_id=cursor, limit=50
            )
            if not page:
                break
            assert str(unlinked.id) not in {row["id"] for row in page}
            cursor = page[-1]["id"]

    @pytest.mark.parametrize("limit", [0, -1, 501])
    def test_an_unbounded_page_is_refused(self, db_session, limit):
        with pytest.raises(ValueError):
            billing.stripe_subscription_page(db_session, limit=limit)


class TestFreeCapOverages:
    def test_only_unentitled_accounts_over_a_cap_are_counted(self, db_session):
        before = billing_preflight.free_cap_overages(
            db_session, max_users=1, max_agents=3
        )

        crowded = _account(db_session)
        _user(db_session, crowded)
        _user(db_session, crowded)
        # Deactivated seats do not block growth, so they do not count.
        _user(db_session, crowded, is_active=False)
        paying = _account(db_session)
        _user(db_session, paying)
        _user(db_session, paying)
        plan_id = f"preflight-paid-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        _subscription(db_session, paying, plan_id)

        after = billing_preflight.free_cap_overages(
            db_session, max_users=1, max_agents=3
        )

        assert after["over_user_cap"] == before["over_user_cap"] + 1
        assert after["over_agent_cap"] == before["over_agent_cap"]
        assert after["over_any_cap"] == before["over_any_cap"] + 1

    def test_agents_over_the_cap_are_counted_once_per_account(self, db_session):
        before = billing_preflight.free_cap_overages(
            db_session, max_users=1, max_agents=3
        )

        account = _account(db_session)
        for _ in range(4):
            _agent(db_session, account)
        _agent(db_session, account, lifecycle_state="retired")

        after = billing_preflight.free_cap_overages(
            db_session, max_users=1, max_agents=3
        )

        assert after["over_agent_cap"] == before["over_agent_cap"] + 1
        assert after["over_any_cap"] == before["over_any_cap"] + 1

    def test_an_expired_trial_over_the_free_cap_is_counted(self, db_session):
        """A trialing row past current_period_end is not entitled, so Free caps apply."""
        before = billing_preflight.free_cap_overages(
            db_session, max_users=1, max_agents=3
        )

        account = _account(db_session)
        _user(db_session, account)
        _user(db_session, account)
        plan_id = f"preflight-expired-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        now = datetime.now(timezone.utc)
        _subscription(
            db_session,
            account,
            plan_id,
            status="trialing",
            current_period_start=now - timedelta(days=30),
            current_period_end=now - timedelta(days=1),
        )

        after = billing_preflight.free_cap_overages(
            db_session, max_users=1, max_agents=3
        )

        assert after["over_user_cap"] == before["over_user_cap"] + 1
        assert after["over_any_cap"] == before["over_any_cap"] + 1


class TestSubscriptionHealth:
    def test_an_entitled_subscription_without_a_revision_is_counted(self, db_session):
        plan_id = f"preflight-rev-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        before = billing_preflight.entitled_without_revision(db_session)

        _subscription(db_session, _account(db_session), plan_id, billing_state={})
        _subscription(db_session, _account(db_session), plan_id)

        assert billing_preflight.entitled_without_revision(db_session) == before + 1

    def test_a_trial_past_its_period_end_is_counted(self, db_session):
        plan_id = f"preflight-trial-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        now = datetime.now(timezone.utc)
        before = billing_preflight.expired_trials(db_session, now=now)

        _subscription(
            db_session,
            _account(db_session),
            plan_id,
            status="trialing",
            current_period_start=now - timedelta(days=30),
            current_period_end=now - timedelta(days=1),
        )
        _subscription(db_session, _account(db_session), plan_id, status="trialing")

        assert billing_preflight.expired_trials(db_session, now=now) == before + 1


class TestGatedCapabilityUsage:
    def test_usage_is_grouped_by_the_plan_the_account_holds_today(self, db_session):
        """The regression: the group key must render as one expression."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=90)
        before = billing_preflight.gated_capability_usage(db_session, since=since)

        free_account = _account(db_session)
        _price_override(db_session, free_account, created_at=now - timedelta(days=1))
        paid_account = _account(db_session)
        plan_id = f"preflight-gated-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        _subscription(db_session, paid_account, plan_id)
        _price_override(db_session, paid_account, created_at=now - timedelta(days=1))

        after = billing_preflight.gated_capability_usage(db_session, since=since)

        default_plan = billing_preflight.DEFAULT_PLAN_ID
        assert after["price_overrides"].get(default_plan, 0) == (
            before["price_overrides"].get(default_plan, 0) + 1
        )
        assert after["price_overrides"][plan_id] == 1

    def test_usage_outside_the_window_is_ignored(self, db_session):
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=90)
        before = billing_preflight.gated_capability_usage(db_session, since=since)

        _price_override(
            db_session, _account(db_session), created_at=now - timedelta(days=200)
        )

        after = billing_preflight.gated_capability_usage(db_session, since=since)

        assert after["price_overrides"] == before["price_overrides"]


def test_accounts_without_a_hosted_wallet_are_counted_against_the_fleet(db_session):
    before = billing_preflight.accounts_without_hosted_wallet(db_session)

    _account(db_session)

    after = billing_preflight.accounts_without_hosted_wallet(db_session)

    assert after["accounts"] == before["accounts"] + 1
    assert after["without_hosted_wallet"] == before["without_hosted_wallet"] + 1


def _invitation(db, account, inviter, *, status=None, expires_in_days: int = 7):
    now = datetime.now(timezone.utc)
    invitation = models.UserInvitation(
        account_id=account.id,
        email=f"invite_{uuid.uuid4().hex[:8]}@example.com",
        invited_by=inviter.id,
        token=uuid.uuid4().hex,
        status=status or models.UserInvitationStatus.PENDING,
        expires_at=now + timedelta(days=expires_in_days),
    )
    db.add(invitation)
    db.flush()
    return invitation


class TestEntitledAccounts:
    """The population ``free_cap_overages`` cannot see: the paying one.

    An account on a grandfathered plan can be entitled and still be over the
    limit its persisted plan row carries, which is exactly the case an
    operator needs counted before gating is switched on.
    """

    def test_entitled_accounts_are_counted_under_their_current_plan(self, db_session):
        plan_id = f"preflight-entitled-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        account = _account(db_session)
        _subscription(db_session, account, plan_id)

        counts = billing_preflight.entitled_accounts_by_plan(db_session)

        assert counts[plan_id] == 1

    def test_an_unentitled_account_is_never_counted(self, db_session):
        plan_id = f"preflight-stale-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        now = datetime.now(timezone.utc)
        _subscription(
            db_session,
            _account(db_session),
            plan_id,
            status="trialing",
            current_period_start=now - timedelta(days=30),
            current_period_end=now - timedelta(days=1),
        )

        counts = billing_preflight.entitled_accounts_by_plan(db_session)

        assert plan_id not in counts

    def test_pressure_counts_seats_the_way_the_seat_gate_counts_them(self, db_session):
        """Seats are active users plus live invitations, not users alone."""
        plan_id = f"preflight-seats-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        account = _account(db_session)
        _subscription(db_session, account, plan_id)
        inviter = _user(db_session, account)
        _user(db_session, account)
        _invitation(db_session, account, inviter)

        pressure = billing_preflight.entitled_capacity_pressure(
            db_session, plan_id=plan_id, max_users=2, max_agents=-1
        )

        assert pressure["accounts"] == 1
        assert pressure["over_user_cap"] == 1
        assert pressure["at_or_over_user_cap"] == 1
        assert pressure["over_any_cap"] == 1

    def test_an_account_exactly_at_the_cap_is_not_over_it(self, db_session):
        plan_id = f"preflight-at-cap-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        account = _account(db_session)
        _subscription(db_session, account, plan_id)
        _user(db_session, account)
        _user(db_session, account)

        pressure = billing_preflight.entitled_capacity_pressure(
            db_session, plan_id=plan_id, max_users=2, max_agents=-1
        )

        assert pressure["at_or_over_user_cap"] == 1
        assert pressure["over_user_cap"] == 0
        assert pressure["over_any_cap"] == 0

    def test_an_unlimited_cap_puts_nobody_under_pressure(self, db_session):
        plan_id = f"preflight-unlimited-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        account = _account(db_session)
        _subscription(db_session, account, plan_id)
        for _ in range(4):
            _user(db_session, account)
            _agent(db_session, account)

        pressure = billing_preflight.entitled_capacity_pressure(
            db_session, plan_id=plan_id, max_users=-1, max_agents=-1
        )

        assert pressure["accounts"] == 1
        assert pressure["at_or_over_user_cap"] == 0
        assert pressure["over_user_cap"] == 0
        assert pressure["at_or_over_agent_cap"] == 0
        assert pressure["over_agent_cap"] == 0

    def test_agents_over_the_cap_are_counted(self, db_session):
        plan_id = f"preflight-agents-{uuid.uuid4().hex[:8]}"
        _plan(db_session, plan_id)
        account = _account(db_session)
        _subscription(db_session, account, plan_id)
        for _ in range(3):
            _agent(db_session, account)
        _agent(db_session, account, lifecycle_state="retired")

        pressure = billing_preflight.entitled_capacity_pressure(
            db_session, plan_id=plan_id, max_users=-1, max_agents=2
        )

        assert pressure["over_agent_cap"] == 1
        assert pressure["over_any_cap"] == 1

    def test_only_the_named_plan_is_measured(self, db_session):
        measured = f"preflight-measured-{uuid.uuid4().hex[:8]}"
        other = f"preflight-other-{uuid.uuid4().hex[:8]}"
        _plan(db_session, measured)
        _plan(db_session, other)
        crowded = _account(db_session)
        _subscription(db_session, crowded, other)
        for _ in range(5):
            _user(db_session, crowded)

        pressure = billing_preflight.entitled_capacity_pressure(
            db_session, plan_id=measured, max_users=1, max_agents=1
        )

        assert pressure["accounts"] == 0
        assert pressure["over_any_cap"] == 0


class TestAccountCapacityCounts:
    def test_one_account_reports_the_numbers_behind_its_seats(self, db_session):
        account = _account(db_session)
        inviter = _user(db_session, account)
        _user(db_session, account)
        _user(db_session, account, is_active=False)
        _invitation(db_session, account, inviter)
        _agent(db_session, account)
        _agent(db_session, account, lifecycle_state="retired")

        counts = billing_preflight.account_capacity_counts(
            db_session, account_id=str(account.id)
        )

        assert counts["active_users"] == 2
        assert counts["pending_invitations"] == 1
        assert counts["seats_used"] == 3
        assert counts["active_agents"] == 1

    def test_an_expired_invitation_does_not_hold_a_seat(self, db_session):
        account = _account(db_session)
        inviter = _user(db_session, account)
        _invitation(db_session, account, inviter, expires_in_days=-1)

        counts = billing_preflight.account_capacity_counts(
            db_session, account_id=str(account.id)
        )

        assert counts["pending_invitations"] == 0
        assert counts["seats_used"] == 1

    def test_another_account_never_leaks_into_the_counts(self, db_session):
        account = _account(db_session)
        neighbour = _account(db_session)
        _user(db_session, neighbour)
        _agent(db_session, neighbour)

        counts = billing_preflight.account_capacity_counts(
            db_session, account_id=str(account.id)
        )

        assert counts == {
            "active_users": 0,
            "pending_invitations": 0,
            "seats_used": 0,
            "active_agents": 0,
        }
