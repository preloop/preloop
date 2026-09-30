"""An expired trial entitles nobody, in memory and in SQL.

A staging account whose Stripe trial ended in July still carried
``status == "trialing"`` because the provider webhook never arrived. Every
entitlement lookup checked the status alone, so the account kept a paid plan's
allowances indefinitely. These tests pin the shared rule and both lookups that
apply it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from preloop.models import models
from preloop.models.crud.billing import ENTITLED_STATUSES, billing
from preloop.models.crud.entitlement import (
    ACTIVE_STATUSES,
    entitlement_clause,
    grandfather_clause,
    grandfathers_withdrawn_plan,
    is_paid_subscription,
    is_entitled_subscription,
    is_expired_trial,
    is_live_trial,
    is_stale_subscription,
    trial_ended_at,
)
from preloop.models.crud.plan import (
    plan as crud_plan,
    subscription as crud_subscription,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(days=50)
FUTURE = NOW + timedelta(days=13)


def _row(status, period_end):
    return models.Subscription(
        status=status,
        current_period_start=period_end - timedelta(days=14),
        current_period_end=period_end,
    )


@pytest.mark.parametrize(
    "status, period_end, entitled",
    [
        # The bug: a trial whose provider-side end has passed.
        ("trialing", PAST, False),
        ("trialing", FUTURE, True),
        # A paid subscription we simply have not re-read is still paid.
        ("active", PAST, True),
        ("active", FUTURE, True),
        # Dunning keeps working; Stripe is still retrying the card.
        ("past_due", PAST, True),
        ("past_due", FUTURE, True),
        ("canceled", FUTURE, False),
        ("paused", FUTURE, False),
    ],
)
def test_entitlement_rule(status, period_end, entitled):
    assert is_entitled_subscription(_row(status, period_end), now=NOW) is entitled


def test_no_subscription_is_never_entitled():
    assert is_entitled_subscription(None, now=NOW) is False
    assert is_expired_trial(None, now=NOW) is False
    assert is_live_trial(None, now=NOW) is False
    assert trial_ended_at(None) is None
    assert is_stale_subscription(None, now=NOW) is False


@pytest.mark.parametrize(
    "status, period_end, live",
    [
        ("trialing", PAST, False),
        ("trialing", FUTURE, True),
        ("active", FUTURE, False),
        ("past_due", FUTURE, False),
        (None, None, False),
    ],
)
def test_is_live_trial(status, period_end, live):
    subscription = None if status is None else _row(status, period_end)
    assert is_live_trial(subscription, now=NOW) is live


def test_trial_expiry_is_exact_and_timezone_naive_rows_read_as_utc():
    assert is_expired_trial(_row("trialing", NOW), now=NOW) is False
    assert is_expired_trial(_row("trialing", NOW - timedelta(seconds=1)), now=NOW)
    naive = _row("trialing", PAST.replace(tzinfo=None))
    assert is_expired_trial(naive, now=NOW) is True
    assert trial_ended_at(naive) == PAST


def test_trial_ended_at_only_reports_trial_rows():
    assert trial_ended_at(_row("trialing", PAST)) == PAST
    assert trial_ended_at(_row("active", PAST)) is None


@pytest.mark.parametrize(
    "status, period_end, stale",
    [
        ("trialing", PAST, True),
        ("trialing", FUTURE, False),
        ("active", NOW - timedelta(days=8), True),
        ("active", NOW - timedelta(days=6), False),
        ("past_due", NOW - timedelta(days=30), True),
        ("canceled", PAST, False),
    ],
)
def test_stale_detection(status, period_end, stale):
    assert is_stale_subscription(_row(status, period_end), now=NOW) is stale


def test_active_status_set_excludes_dunning():
    row = _row("past_due", FUTURE)
    assert is_entitled_subscription(row, now=NOW, statuses=ENTITLED_STATUSES) is True
    assert is_entitled_subscription(row, now=NOW, statuses=ACTIVE_STATUSES) is False


def test_sql_clause_matches_the_in_memory_rule():
    clause = str(entitlement_clause(models.Subscription, now=NOW))
    assert "subscription.status IN" in clause
    assert "subscription.status !=" in clause
    assert "subscription.current_period_end >=" in clause


def _seed(db_session, account_id, *, status, period_end, stripe_id):
    if crud_plan.get(db_session, id="teams") is None:
        crud_plan.create(
            db_session,
            obj_in={
                "id": "teams",
                "name": "Teams",
                "price_monthly": 29.0,
                "price_annually": 290.0,
                "features": {"max_users": 10},
                "is_active": True,
                "is_custom": False,
            },
        )
    return crud_subscription.create(
        db_session,
        obj_in={
            "account_id": account_id,
            "plan_id": "teams",
            "stripe_subscription_id": stripe_id,
            "status": status,
            "current_period_start": period_end - timedelta(days=14),
            "current_period_end": period_end,
        },
    )


@pytest.mark.parametrize(
    "status, offset_days, entitled",
    [
        ("trialing", -50, False),
        ("trialing", 13, True),
        ("active", -50, True),
        ("past_due", -50, True),
        ("canceled", 13, False),
    ],
)
def test_entitled_subscription_query_applies_the_rule(
    db_session, test_user, status, offset_days, entitled
):
    now = datetime.now(timezone.utc)
    _seed(
        db_session,
        test_user.account_id,
        status=status,
        period_end=now + timedelta(days=offset_days),
        stripe_id=f"sub_{status}_{offset_days}",
    )
    found = billing.entitled_subscription(db_session, str(test_user.account_id))
    assert (found is not None) is entitled


@pytest.mark.parametrize(
    "status, offset_days, live",
    [
        ("trialing", -50, False),
        ("trialing", 13, True),
        ("active", -50, True),
        # past_due is entitled but not "live" for checkout/quota callers.
        ("past_due", 13, False),
    ],
)
def test_get_active_for_account_applies_the_rule(
    db_session, test_user, status, offset_days, live
):
    now = datetime.now(timezone.utc)
    _seed(
        db_session,
        test_user.account_id,
        status=status,
        period_end=now + timedelta(days=offset_days),
        stripe_id=f"sub_live_{status}_{offset_days}",
    )
    found = crud_subscription.get_active_for_account(
        db_session, account_id=str(test_user.account_id)
    )
    assert (found is not None) is live


def test_expired_trial_row_is_still_readable_and_untouched(db_session, test_user):
    """Falling back to Free must not delete or rewrite provider history."""
    now = datetime.now(timezone.utc)
    row = _seed(
        db_session,
        test_user.account_id,
        status="trialing",
        period_end=now - timedelta(days=50),
        stripe_id="sub_expired_history",
    )
    assert billing.entitled_subscription(db_session, str(test_user.account_id)) is None
    latest = crud_subscription.get_latest_for_account(
        db_session, account_id=str(test_user.account_id)
    )
    assert latest is not None
    assert str(latest.id) == str(row.id)
    assert latest.status == "trialing"


# --- Withdrawn plans are grandfathered only for paying customers -------------
#
# The legacy per-seat "teams" plan is withdrawn from sale (`is_active = False`)
# and kept only so live subscriptions still resolve their terms. A staging
# account whose per-seat trial ended in July resolved to that plan anyway,
# because entitlement asked about status and expiry and nothing asked whether
# the plan was still sold or whether anybody was paying for it.


def _withdrawn_plan(db_session, plan_id="withdrawn_seats", *, is_active=False):
    existing = crud_plan.get(db_session, id=plan_id)
    if existing is not None:
        return existing
    return crud_plan.create(
        db_session,
        obj_in={
            "id": plan_id,
            "name": "Teams",
            "price_monthly": 29.0,
            "price_annually": 290.0,
            "features": {"max_users": -1},
            "is_active": is_active,
            "is_custom": False,
        },
    )


def _seed_on(db_session, account_id, plan_id, *, status, offset_days, stripe_id):
    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=offset_days)
    return crud_subscription.create(
        db_session,
        obj_in={
            "account_id": account_id,
            "plan_id": plan_id,
            "stripe_subscription_id": stripe_id,
            "status": status,
            "current_period_start": period_end - timedelta(days=30),
            "current_period_end": period_end,
        },
    )


@pytest.mark.parametrize(
    "status, stripe_id, paid",
    [
        ("active", "sub_1", True),
        ("past_due", "sub_1", True),
        # Free by construction, whatever the period end says.
        ("trialing", "sub_1", False),
        ("canceled", "sub_1", False),
        ("paused", "sub_1", False),
        # No provider subscription is no evidence of payment: this is the
        # shape a plan change that was written locally and never completed at
        # Stripe leaves behind.
        ("active", None, False),
        ("active", "", False),
        ("active", "   ", False),
    ],
)
def test_is_paid_subscription(status, stripe_id, paid):
    row = _row(status, FUTURE)
    row.stripe_subscription_id = stripe_id
    assert is_paid_subscription(row) is paid


def test_nothing_is_paid_without_a_subscription():
    assert is_paid_subscription(None) is False


@pytest.mark.parametrize(
    "status, period_end, grandfathered",
    [
        # The reported bug, and its neighbours.
        ("trialing", PAST, False),  # trial expired
        ("canceled", PAST, False),  # trial cancelled / subscription cancelled
        ("paused", FUTURE, False),
        ("active", FUTURE, True),  # paying: grandfathering is for these
        ("past_due", FUTURE, True),  # dunning is still a paying customer
        # A promise already made, with a provider-set end date on it. It
        # cannot be renewed on a plan nobody can buy, so it resolves itself.
        ("trialing", FUTURE, True),
    ],
)
def test_grandfathering_a_withdrawn_plan_needs_a_paid_row(
    status, period_end, grandfathered
):
    withdrawn = models.Plan(id="teams", name="Teams", features={}, is_active=False)
    row = _row(status, period_end)
    row.stripe_subscription_id = "sub_provider"
    assert grandfathers_withdrawn_plan(row, plan=withdrawn, now=NOW) is grandfathered


@pytest.mark.parametrize("status", ["trialing", "active", "past_due"])
def test_a_plan_still_on_sale_is_not_subject_to_grandfathering(status):
    """The gate is scoped to withdrawn plans; ordinary plans decide alone."""
    on_sale = models.Plan(id="team", name="Team", features={}, is_active=True)
    row = _row(status, FUTURE)
    row.stripe_subscription_id = None
    assert grandfathers_withdrawn_plan(row, plan=on_sale) is True


def test_an_unresolvable_plan_is_treated_as_withdrawn():
    """A plan row we cannot load gets the strict branch, not the lenient one."""
    ended = _row("trialing", PAST)
    ended.stripe_subscription_id = "sub_provider"
    assert grandfathers_withdrawn_plan(ended, plan=None) is False
    unpaid = _row("active", FUTURE)
    unpaid.stripe_subscription_id = None
    assert grandfathers_withdrawn_plan(unpaid, plan=None) is False
    paid = _row("active", FUTURE)
    paid.stripe_subscription_id = "sub_provider"
    assert grandfathers_withdrawn_plan(paid, plan=None) is True


def test_grandfather_sql_clause_names_every_half_of_the_rule():
    clause = str(grandfather_clause(models.Subscription, models.Plan))
    assert "subscription.plan_id IN (SELECT plan.id" in clause
    assert "plan.is_active IS true" in clause
    assert "subscription.status IN" in clause
    assert "subscription.stripe_subscription_id IS NOT NULL" in clause
    assert "subscription.current_period_end >=" in clause


def test_grandfather_sql_clause_rejects_a_blank_provider_id_like_the_helper():
    """The two forms of the rule must agree on a blank id, not only on NULL."""
    clause = str(grandfather_clause(models.Subscription, models.Plan))
    assert "length(trim(subscription.stripe_subscription_id)) >" in clause


@pytest.mark.parametrize(
    "status, offset_days, stripe_id, entitled",
    [
        # Trial expired: the founder's staging account before reconciliation.
        ("trialing", -50, "sub_expired_trial", False),
        # Trial still running: kept until the date the provider set.
        ("trialing", 13, "sub_live_trial", True),
        # Trial cancelled, and an ordinary cancellation.
        ("canceled", -50, "sub_cancelled_trial", False),
        ("canceled", 13, "sub_cancelled", False),
        # Paying, so genuinely grandfathered.
        ("active", 13, "sub_paying", True),
        ("past_due", 13, "sub_dunning", True),
        # Locally written "active" with no provider subscription behind it.
        ("active", 13, None, False),
        # Same thing written as a blank id: SQL must agree with the helper.
        ("active", 13, "", False),
        ("active", 13, "   ", False),
    ],
)
def test_withdrawn_plan_resolution(
    db_session, test_user, status, offset_days, stripe_id, entitled
):
    plan = _withdrawn_plan(db_session)
    _seed_on(
        db_session,
        test_user.account_id,
        plan.id,
        status=status,
        offset_days=offset_days,
        stripe_id=stripe_id,
    )

    found = billing.entitled_subscription(db_session, str(test_user.account_id))

    assert (found is not None) is entitled
    if entitled:
        assert found.plan_id == plan.id


def test_the_same_row_on_a_plan_still_on_sale_stays_entitled(db_session, test_user):
    """Proof the new clause keys on the plan, not on trials in general."""
    on_sale = _withdrawn_plan(db_session, plan_id="on_sale_seats", is_active=True)
    _seed_on(
        db_session,
        test_user.account_id,
        on_sale.id,
        status="trialing",
        offset_days=13,
        stripe_id="sub_on_sale_trial",
    )

    found = billing.entitled_subscription(db_session, str(test_user.account_id))

    assert found is not None
    assert found.plan_id == on_sale.id


def test_a_hand_provisioned_grant_on_a_custom_plan_keeps_its_plan(
    db_session, test_user
):
    """The payment test only reaches rows whose plan was withdrawn from sale.

    A negotiated or enterprise grant is modelled as a custom plan row, and a
    plan row is on sale unless somebody set ``is_active = False`` on it. Such
    a grant therefore resolves with no provider subscription id behind it,
    exactly as it did before this rule, and the withdrawn-plan branch is
    never reached for it.
    """
    custom = crud_plan.create(
        db_session,
        obj_in={
            "id": "custom_negotiated",
            "name": "Acme contract",
            "price_monthly": 0.0,
            "price_annually": 0.0,
            "features": {"max_users": 300},
            "is_custom": True,
        },
    )
    assert custom.is_active is True

    _seed_on(
        db_session,
        test_user.account_id,
        custom.id,
        status="active",
        offset_days=13,
        stripe_id=None,
    )

    found = billing.entitled_subscription(db_session, str(test_user.account_id))

    assert found is not None
    assert found.plan_id == custom.id


def test_an_unpaid_withdrawn_row_does_not_block_a_new_purchase(db_session, test_user):
    """Resolved to Free, so checkout de-duplication must let the sale through.

    The row is ``active`` with no provider subscription behind it, which is
    what a plan change written locally and abandoned at Stripe leaves. It is
    entitled by status and inside its period, so only the grandfathering rule
    can keep it from blocking the customer's next purchase.
    """
    plan = _withdrawn_plan(db_session)
    _seed_on(
        db_session,
        test_user.account_id,
        plan.id,
        status="active",
        offset_days=13,
        stripe_id=None,
    )

    assert (
        crud_subscription.get_active_for_account(
            db_session, account_id=str(test_user.account_id)
        )
        is None
    )


def test_a_paid_withdrawn_row_still_blocks_a_second_purchase(db_session, test_user):
    plan = _withdrawn_plan(db_session)
    _seed_on(
        db_session,
        test_user.account_id,
        plan.id,
        status="active",
        offset_days=13,
        stripe_id="sub_paid_blocking",
    )

    found = crud_subscription.get_active_for_account(
        db_session, account_id=str(test_user.account_id)
    )

    assert found is not None
    assert found.plan_id == plan.id


def test_the_withdrawn_row_is_preserved_when_it_stops_entitling(db_session, test_user):
    """The account falls back to Free; the provider history is not rewritten."""
    plan = _withdrawn_plan(db_session)
    row = _seed_on(
        db_session,
        test_user.account_id,
        plan.id,
        status="trialing",
        offset_days=-50,
        stripe_id="sub_withdrawn_history",
    )

    assert billing.entitled_subscription(db_session, str(test_user.account_id)) is None
    latest = crud_subscription.get_latest_for_account(
        db_session, account_id=str(test_user.account_id)
    )
    assert latest is not None
    assert str(latest.id) == str(row.id)
    assert latest.plan_id == plan.id
    assert latest.status == "trialing"
