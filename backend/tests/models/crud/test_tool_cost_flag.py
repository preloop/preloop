"""Tests for the tool cost flag CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from preloop.models.crud import crud_tool_cost_flag
from preloop.models.models.tool_cost_flag import ToolCostFlag


def _window() -> tuple[datetime, datetime]:
    end = datetime.now(UTC).replace(tzinfo=None)
    return end - timedelta(days=7), end


def _evidence() -> dict[str, object]:
    return {"tokens": 1234, "claim": "Defining tool foo costs ~$4.20/week."}


def test_upsert_inserts_then_updates_without_duplicate(
    db_session, create_account
) -> None:
    """Re-running detection for the same window updates in place, no duplicate."""
    account = create_account()
    window_start, window_end = _window()

    created = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="foo",
        tool_source="mcp",
        window_start=window_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence=_evidence(),
        estimated_weekly_cost=4.20,
    )
    updated = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="foo",
        tool_source="payload",
        window_start=window_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence={"tokens": 2000, "claim": "updated"},
        estimated_weekly_cost=6.50,
    )

    assert created.id == updated.id
    assert updated.tool_source == "payload"
    assert updated.estimated_weekly_cost == 6.50
    assert updated.evidence == {"tokens": 2000, "claim": "updated"}

    rows = (
        db_session.query(ToolCostFlag)
        .filter(ToolCostFlag.account_id == account.id)
        .all()
    )
    assert len(rows) == 1


def test_upsert_preserves_status_across_redetection(db_session, create_account) -> None:
    """A dismissed flag stays dismissed when detection re-runs for the window."""
    account = create_account()
    window_start, window_end = _window()

    flag = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="foo",
        tool_source="mcp",
        window_start=window_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence=_evidence(),
        estimated_weekly_cost=4.20,
    )
    crud_tool_cost_flag.set_status(
        db_session, account_id=account.id, flag_id=flag.id, status="dismissed"
    )

    refreshed = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="foo",
        tool_source="mcp",
        window_start=window_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence={"tokens": 9999, "claim": "new"},
        estimated_weekly_cost=9.99,
    )

    assert refreshed.id == flag.id
    assert refreshed.status == "dismissed"


def test_list_for_account_excludes_dismissed_by_default(
    db_session, create_account
) -> None:
    """The default listing hides dismissed flags but keeps open and snoozed."""
    account = create_account()
    base_start, window_end = _window()

    open_flag = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="open_tool",
        tool_source="mcp",
        window_start=base_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence=_evidence(),
        estimated_weekly_cost=1.0,
    )
    dismissed_flag = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="dismissed_tool",
        tool_source="mcp",
        window_start=base_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence=_evidence(),
        estimated_weekly_cost=2.0,
    )
    snoozed_flag = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="snoozed_tool",
        tool_source="mcp",
        window_start=base_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence=_evidence(),
        estimated_weekly_cost=3.0,
    )
    crud_tool_cost_flag.set_status(
        db_session, account_id=account.id, flag_id=dismissed_flag.id, status="dismissed"
    )
    crud_tool_cost_flag.set_status(
        db_session, account_id=account.id, flag_id=snoozed_flag.id, status="snoozed"
    )

    default_ids = {
        f.id
        for f in crud_tool_cost_flag.list_for_account(db_session, account_id=account.id)
    }
    assert open_flag.id in default_ids
    assert snoozed_flag.id in default_ids
    assert dismissed_flag.id not in default_ids

    all_ids = {
        f.id
        for f in crud_tool_cost_flag.list_for_account(
            db_session, account_id=account.id, include_dismissed=True
        )
    }
    assert dismissed_flag.id in all_ids

    dismissed_only = crud_tool_cost_flag.list_for_account(
        db_session, account_id=account.id, status="dismissed"
    )
    assert [f.id for f in dismissed_only] == [dismissed_flag.id]


def test_set_status_transitions(db_session, create_account) -> None:
    """Flags move through open -> dismissed -> open and open -> snoozed."""
    account = create_account()
    window_start, window_end = _window()
    flag = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="foo",
        tool_source="mcp",
        window_start=window_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence=_evidence(),
        estimated_weekly_cost=4.20,
    )
    assert flag.status == "open"

    dismissed = crud_tool_cost_flag.set_status(
        db_session, account_id=account.id, flag_id=flag.id, status="dismissed"
    )
    assert dismissed is not None
    assert dismissed.status == "dismissed"

    reopened = crud_tool_cost_flag.set_status(
        db_session, account_id=account.id, flag_id=flag.id, status="open"
    )
    assert reopened is not None
    assert reopened.status == "open"

    snoozed = crud_tool_cost_flag.set_status(
        db_session, account_id=account.id, flag_id=flag.id, status="snoozed"
    )
    assert snoozed is not None
    assert snoozed.status == "snoozed"


def test_set_status_rejects_invalid_status(db_session, create_account) -> None:
    """An unrecognized status is rejected before touching the row."""
    account = create_account()
    window_start, window_end = _window()
    flag = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=account.id,
        tool_name="foo",
        tool_source="mcp",
        window_start=window_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence=_evidence(),
        estimated_weekly_cost=4.20,
    )
    with pytest.raises(ValueError):
        crud_tool_cost_flag.set_status(
            db_session, account_id=account.id, flag_id=flag.id, status="bogus"
        )


def test_account_scoping_prevents_cross_account_mutation(
    db_session, create_account
) -> None:
    """A flag cannot be mutated or listed under a different account."""
    owner = create_account()
    other = create_account()
    window_start, window_end = _window()
    flag = crud_tool_cost_flag.upsert_flag(
        db_session,
        account_id=owner.id,
        tool_name="foo",
        tool_source="mcp",
        window_start=window_start,
        window_end=window_end,
        flag_kind="definition_cost",
        evidence=_evidence(),
        estimated_weekly_cost=4.20,
    )

    result = crud_tool_cost_flag.set_status(
        db_session, account_id=other.id, flag_id=flag.id, status="dismissed"
    )
    assert result is None

    db_session.refresh(flag)
    assert flag.status == "open"

    other_flags = crud_tool_cost_flag.list_for_account(db_session, account_id=other.id)
    assert other_flags == []
