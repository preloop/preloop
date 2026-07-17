"""Tests for account budget headroom used to cap the replay pre-call guard."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from preloop.services import budget_headroom as be
from preloop.models.crud.budget import (
    crud_budget_policy,
    crud_budget_spend,
    get_period_start,
)
from preloop.models.models.budget import BudgetPeriod


def _account_policy(hard_limit: Any, subject_type: str = "account") -> SimpleNamespace:
    return SimpleNamespace(
        hard_limit_usd=hard_limit,
        period=BudgetPeriod.daily,
        subject_type=subject_type,
        subject_id=None,
        model_alias=None,
    )


def test_remaining_headroom_returns_tightest_hard_cap(monkeypatch):
    """Mock mirrors CRUD: one account-level lookup returns account + global."""
    p1 = _account_policy(10.0, "account")
    p2 = _account_policy(5.0, "global")
    monkeypatch.setattr(
        be.crud_budget_policy,
        "get_policies_for_subject",
        lambda db, *, account_id, subject_type, subject_id=None: [p1, p2],
    )
    # Both account-level policies share the same spend bucket; report $3 spent.
    monkeypatch.setattr(
        be.crud_budget_spend,
        "get_spend_multi",
        lambda *, db, account_id, buckets: {b: 3.0 for b in buckets},
    )
    headroom = be.remaining_account_budget_headroom(db=None, account_id=uuid.uuid4())
    assert headroom == pytest.approx(2.0)  # min(10-3, 5-3)


def test_remaining_headroom_clamps_at_zero_when_overspent(monkeypatch):
    monkeypatch.setattr(
        be.crud_budget_policy,
        "get_policies_for_subject",
        lambda db, *, account_id, subject_type, subject_id=None: [_account_policy(5.0)],
    )
    monkeypatch.setattr(
        be.crud_budget_spend,
        "get_spend_multi",
        lambda *, db, account_id, buckets: {b: 9.0 for b in buckets},
    )
    headroom = be.remaining_account_budget_headroom(db=None, account_id=uuid.uuid4())
    assert headroom == 0.0  # 5-9 clamped to 0, never negative


def test_remaining_headroom_none_without_hard_cap(monkeypatch):
    monkeypatch.setattr(
        be.crud_budget_policy,
        "get_policies_for_subject",
        lambda db, *, account_id, subject_type, subject_id=None: [
            _account_policy(None)
        ],
    )
    headroom = be.remaining_account_budget_headroom(db=None, account_id=uuid.uuid4())
    assert headroom is None


def test_remaining_headroom_includes_global_policy_via_db(db_session, test_user):
    """Real CRUD/DB: a global hard-cap alone must constrain headroom.

    Regression guard for the false assumption that
    ``get_policies_for_subject(..., subject_type="account")`` excludes
    ``global`` rows. Account-level lookups merge both subject types.
    """
    account_id = test_user.account_id
    crud_budget_policy.create(
        db=db_session,
        obj_in={
            "account_id": account_id,
            "subject_type": "global",
            "subject_id": None,
            "model_alias": None,
            "period": BudgetPeriod.daily,
            "hard_limit_usd": 5.0,
            "soft_limit_usd": None,
            "notify_on_soft": False,
            "notify_on_hard": False,
        },
    )
    crud_budget_policy.create(
        db=db_session,
        obj_in={
            "account_id": account_id,
            "subject_type": "account",
            "subject_id": None,
            "model_alias": None,
            "period": BudgetPeriod.daily,
            "hard_limit_usd": 10.0,
            "soft_limit_usd": None,
            "notify_on_soft": False,
            "notify_on_hard": False,
        },
    )
    period_start = get_period_start(datetime.now(timezone.utc), BudgetPeriod.daily)
    crud_budget_spend.upsert_spend(
        db=db_session,
        account_id=account_id,
        subject_type="account",
        subject_id=None,
        model_alias=None,
        period=BudgetPeriod.daily,
        period_start=period_start,
        spend_increment_usd=3.0,
    )

    headroom = be.remaining_account_budget_headroom(
        db=db_session, account_id=account_id
    )
    # Tightest remaining is the global $5 cap with $3 spent → $2, not the
    # looser account $10 cap ($7 remaining).
    assert headroom == pytest.approx(2.0)


def test_remaining_headroom_global_only_via_db(db_session, test_user):
    """A sole global hard-cap must still be visible through an account lookup."""
    account_id = test_user.account_id
    crud_budget_policy.create(
        db=db_session,
        obj_in={
            "account_id": account_id,
            "subject_type": "global",
            "subject_id": None,
            "model_alias": None,
            "period": BudgetPeriod.daily,
            "hard_limit_usd": 5.0,
            "soft_limit_usd": None,
            "notify_on_soft": False,
            "notify_on_hard": False,
        },
    )
    period_start = get_period_start(datetime.now(timezone.utc), BudgetPeriod.daily)
    crud_budget_spend.upsert_spend(
        db=db_session,
        account_id=account_id,
        subject_type="account",
        subject_id=None,
        model_alias=None,
        period=BudgetPeriod.daily,
        period_start=period_start,
        spend_increment_usd=1.5,
    )

    headroom = be.remaining_account_budget_headroom(
        db=db_session, account_id=account_id
    )
    assert headroom == pytest.approx(3.5)
