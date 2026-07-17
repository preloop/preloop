"""Account-level budget headroom reads for pre-call feasibility checks.

The replay-verification path projects a replay's cost BEFORE spending anything
and aborts when the projection cannot fit the account's remaining hard-cap
headroom. This module holds that coarse read; per-request enforcement of the
full policy set stays with the gateway's budget enforcer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from sqlalchemy.orm import Session

from preloop.models.crud.budget import (
    crud_budget_policy,
    crud_budget_spend,
    get_period_start,
    spend_bucket_for_policy,
)
from preloop.models.models.budget import BudgetPolicy


def remaining_account_budget_headroom(
    db: Session, account_id: uuid.UUID
) -> Optional[float]:
    """Return the tightest remaining account/global hard-cap headroom in USD.

    Considers only account- and global-scoped hard-limit budget policies — the
    caps that apply to any request regardless of which agent or user drives it —
    and returns the smallest remaining amount across them (clamped at 0). This
    is a coarse pre-call bound; the gateway still enforces the full set of
    per-agent/user/model policies on every request via its budget enforcer.

    Args:
        db: Database session.
        account_id: Owning account id.

    Returns:
        The smallest remaining USD headroom across account/global hard caps, or
        ``None`` when no such hard cap is configured (unbounded at this level).
    """
    now = datetime.now(timezone.utc)
    # One call is enough: get_policies_for_subject merges account- and
    # global-scoped rows when subject_type is either and subject_id is None
    # (see ACCOUNT_LEVEL_SUBJECT_TYPES in preloop.models.crud.budget). The
    # enforcer still iterates both types for other subject scopes; headroom
    # only needs the account-level merge.
    policies = crud_budget_policy.get_policies_for_subject(
        db, account_id=account_id, subject_type="account", subject_id=None
    )
    hard_policies = [p for p in policies if p.hard_limit_usd]
    if not hard_policies:
        return None

    evaluations: List[Tuple[BudgetPolicy, Tuple[Any, ...]]] = []
    buckets: List[Tuple[Any, ...]] = []
    seen: set[Tuple[Any, ...]] = set()
    for policy in hard_policies:
        p_start = get_period_start(now, policy.period)
        spend_type, spend_id, spend_alias = spend_bucket_for_policy(policy)
        bucket = (spend_type, spend_id, spend_alias, policy.period, p_start)
        evaluations.append((policy, bucket))
        if bucket not in seen:
            seen.add(bucket)
            buckets.append(bucket)

    spend_map = crud_budget_spend.get_spend_multi(
        db=db, account_id=account_id, buckets=buckets
    )
    remaining = [
        max(0.0, float(policy.hard_limit_usd) - float(spend_map.get(bucket, 0.0)))
        for policy, bucket in evaluations
    ]
    return min(remaining) if remaining else None
