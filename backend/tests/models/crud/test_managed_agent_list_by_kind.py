"""Tests for managed-agent list_by_kind lifecycle filtering (#112 part a)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from preloop.models import models
from preloop.models.crud import crud_managed_agent


def _make_agent(
    db_session,
    test_user,
    *,
    source_id: str,
    agent_kind: str = "cursor",
    lifecycle_state: str = "active",
) -> models.ManagedAgent:
    """Insert one managed agent row for list_by_kind tests."""
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind=agent_kind,
        session_source_type=agent_kind,
        session_source_id=source_id,
        display_name=f"{agent_kind} {source_id}",
        enrolled_via="runtime_session_token",
        lifecycle_state=lifecycle_state,
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


def test_list_by_kind_excludes_decommissioned_by_default(db_session, test_user):
    """Default attribution lookup must ignore archived duplicates."""
    active = _make_agent(db_session, test_user, source_id="cursor-active")
    _make_agent(
        db_session,
        test_user,
        source_id="cursor-archived",
        lifecycle_state="decommissioned",
    )
    _make_agent(
        db_session,
        test_user,
        source_id="cursor-suspended",
        lifecycle_state="suspended",
    )

    rows = crud_managed_agent.list_by_kind(
        db_session,
        account_id=str(test_user.account_id),
        agent_kind="cursor",
    )

    assert [str(row.id) for row in rows] == [str(active.id)]


def test_list_by_kind_active_only_false_includes_archived(db_session, test_user):
    """Operators can opt into seeing archived rows when needed."""
    active = _make_agent(db_session, test_user, source_id="cursor-active-2")
    archived = _make_agent(
        db_session,
        test_user,
        source_id="cursor-archived-2",
        lifecycle_state="decommissioned",
    )

    rows = crud_managed_agent.list_by_kind(
        db_session,
        account_id=str(test_user.account_id),
        agent_kind="cursor",
        active_only=False,
    )
    ids = {str(row.id) for row in rows}
    assert str(active.id) in ids
    assert str(archived.id) in ids


def test_list_by_kind_default_attribution_ignores_archived_duplicate(
    db_session, test_user
):
    """One active + one decommissioned Cursor agent must not be ambiguous."""
    active = _make_agent(db_session, test_user, source_id="cursor-primary")
    _make_agent(
        db_session,
        test_user,
        source_id="cursor-stale",
        lifecycle_state="decommissioned",
    )

    candidates = crud_managed_agent.list_by_kind(
        db_session,
        account_id=str(test_user.account_id),
        agent_kind="cursor",
    )
    assert len(candidates) == 1
    assert str(candidates[0].id) == str(active.id)
