"""Tests for runtime session optimization action CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from preloop.models.crud import crud_runtime_session_optimization_action
from preloop.models.crud.runtime_session import crud_runtime_session
from preloop.models.models.runtime_session_optimization_action import (
    RuntimeSessionOptimizationAction,
)


def _create_runtime_session(db_session, account_id) -> object:
    now = datetime.now(UTC)
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"opt-action-{uuid4()}",
        session_reference="opt-action",
        runtime_principal_type="claude_code",
        runtime_principal_id="opt-action",
        runtime_principal_name="Claude Workspace",
        started_at=now,
        last_activity_at=now,
    )


def test_create_applied_persists_action(db_session, create_account) -> None:
    """create_applied should store a fully populated applied action row."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()

    action = crud_runtime_session_optimization_action.create_applied(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        suggestion_id="scope-tools",
        suggestion_title="Scope unused tools",
        action_type="scope_tools",
        params={"tools": ["search_issues"]},
        applied_by="alice",
        runtime_principal_id="agent-1",
        baseline={"requests": 10, "avg_cost_per_request": 0.05},
        result={"scoped_tools": 1},
    )

    assert action.id is not None
    assert action.status == "applied"
    assert action.suggestion_id == "scope-tools"
    assert action.result == {"scoped_tools": 1}
    stored = db_session.get(RuntimeSessionOptimizationAction, action.id)
    assert stored is not None
    assert stored.applied_by == "alice"


def test_list_for_session_orders_latest_first_and_caps_limit(
    db_session, create_account
) -> None:
    """list_for_session should return newest actions first and honor the cap."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()

    first = crud_runtime_session_optimization_action.create_applied(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        suggestion_id="first",
        suggestion_title="First",
        action_type="scope_tools",
        params={},
        applied_by="alice",
        runtime_principal_id="agent-1",
        baseline=None,
        result={},
    )
    second = crud_runtime_session_optimization_action.create_applied(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        suggestion_id="second",
        suggestion_title="Second",
        action_type="set_budget",
        params={},
        applied_by="alice",
        runtime_principal_id="agent-1",
        baseline=None,
        result={},
    )
    first.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    second.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    db_session.commit()

    rows = crud_runtime_session_optimization_action.list_for_session(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        limit=1,
    )

    assert len(rows) == 1
    assert rows[0].id == second.id


def test_exists_for_suggestion_detects_matching_action(
    db_session, create_account
) -> None:
    """exists_for_suggestion should match suggestion id and action type only."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()

    crud_runtime_session_optimization_action.create_applied(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        suggestion_id="scope-tools",
        suggestion_title="Scope unused tools",
        action_type="scope_tools",
        params={},
        applied_by="alice",
        runtime_principal_id="agent-1",
        baseline=None,
        result={},
    )

    assert (
        crud_runtime_session_optimization_action.exists_for_suggestion(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            suggestion_id="scope-tools",
            action_type="scope_tools",
        )
        is True
    )
    assert (
        crud_runtime_session_optimization_action.exists_for_suggestion(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            suggestion_id="scope-tools",
            action_type="set_budget",
        )
        is False
    )
