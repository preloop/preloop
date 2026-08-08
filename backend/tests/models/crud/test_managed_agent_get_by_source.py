"""Determinism tests for managed-agent source lookups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text

from preloop.models import models
from preloop.models.crud import crud_managed_agent


def _make_agent(
    db_session,
    test_user,
    *,
    source_id: str,
    source_type: str = "claude_code",
    lifecycle_state: str = "active",
    created_at: datetime | None = None,
) -> models.ManagedAgent:
    """Insert one managed-agent row for source-lookup tests.

    Args:
        db_session: Active database session.
        test_user: Account owner fixture.
        source_id: Durable principal id.
        source_type: Durable principal type.
        lifecycle_state: Lifecycle marker for the row.
        created_at: Explicit creation timestamp for ordering assertions.

    Returns:
        The persisted managed agent.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind=source_type,
        session_source_type=source_type,
        session_source_id=source_id,
        display_name=f"{source_type} {source_id} {lifecycle_state}",
        enrolled_via="runtime_session_token",
        lifecycle_state=lifecycle_state,
        lifecycle_updated_at=now,
        last_seen_at=now,
        created_at=created_at or now,
    )
    db_session.add(agent)
    db_session.flush()
    return agent


def _drop_source_unique_constraint(db_session) -> None:
    """Allow sibling rows sharing one source identity inside this test.

    Production guards duplicates with ``uq_managed_agent_account_source``, but
    databases restored from older dumps (and merge/rekey races) can still hold
    siblings, which is exactly the state ``get_by_source`` must resolve
    deterministically. The DDL is rolled back with the test transaction.
    """
    db_session.execute(
        text(
            "ALTER TABLE managed_agent "
            "DROP CONSTRAINT IF EXISTS uq_managed_agent_account_source"
        )
    )


def test_get_by_source_prefers_active_over_archived_siblings(db_session, test_user):
    """A stale decommissioned sibling must never shadow the active row."""
    _drop_source_unique_constraint(db_session)
    base = datetime.now(UTC).replace(tzinfo=None)
    archived = _make_agent(
        db_session,
        test_user,
        source_id="shadowed-host",
        lifecycle_state="decommissioned",
        created_at=base,
    )
    suspended = _make_agent(
        db_session,
        test_user,
        source_id="shadowed-host",
        lifecycle_state="suspended",
        created_at=base + timedelta(minutes=1),
    )
    active = _make_agent(
        db_session,
        test_user,
        source_id="shadowed-host",
        lifecycle_state="active",
        created_at=base - timedelta(days=1),
    )

    resolved = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="shadowed-host",
    )

    assert resolved is not None
    assert str(resolved.id) == str(active.id)
    assert str(resolved.id) not in {str(archived.id), str(suspended.id)}


def test_get_by_source_prefers_suspended_over_decommissioned(db_session, test_user):
    """With no active row, a resumable suspended row wins over an archived one."""
    _drop_source_unique_constraint(db_session)
    base = datetime.now(UTC).replace(tzinfo=None)
    _make_agent(
        db_session,
        test_user,
        source_id="archived-only-host",
        lifecycle_state="decommissioned",
        created_at=base,
    )
    suspended = _make_agent(
        db_session,
        test_user,
        source_id="archived-only-host",
        lifecycle_state="suspended",
        created_at=base - timedelta(days=2),
    )

    resolved = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="archived-only-host",
    )

    assert resolved is not None
    assert str(resolved.id) == str(suspended.id)


def test_get_by_source_breaks_ties_by_recency(db_session, test_user):
    """Same-lifecycle siblings resolve to the most recently created row."""
    _drop_source_unique_constraint(db_session)
    base = datetime.now(UTC).replace(tzinfo=None)
    _make_agent(
        db_session,
        test_user,
        source_id="tied-host",
        lifecycle_state="active",
        created_at=base - timedelta(days=3),
    )
    newest = _make_agent(
        db_session,
        test_user,
        source_id="tied-host",
        lifecycle_state="active",
        created_at=base,
    )

    resolved = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="tied-host",
    )

    assert resolved is not None
    assert str(resolved.id) == str(newest.id)
