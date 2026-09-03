"""CRUD tests for console attention dismissals.

The interesting part is :meth:`CRUDAttentionDismissal.upsert`: it is a single
``INSERT ... ON CONFLICT DO UPDATE``, so re-dismissing an item is idempotent
even when two operators (or a retry) hit the same ``(account, item)`` at once,
rather than racing into ``uq_attention_dismissal_account_item``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_attention_dismissal
from preloop.models.models.attention_dismissal import AttentionDismissal

ITEM_ID = "flow:flow-1"


def _rows(db_session, account_id) -> list[AttentionDismissal]:
    return (
        db_session.query(AttentionDismissal)
        .filter(AttentionDismissal.account_id == account_id)
        .all()
    )


def test_upsert_twice_keeps_one_row_with_the_latest_decision(
    db_session, create_account
) -> None:
    """The second dismissal of the same item replaces the first, in place."""
    account = create_account()

    first = crud_attention_dismissal.upsert(
        db_session,
        account_id=account.id,
        item_id=ITEM_ID,
        fingerprint="run:execution-1",
        reason="expected",
    )
    second = crud_attention_dismissal.upsert(
        db_session,
        account_id=account.id,
        item_id=ITEM_ID,
        fingerprint="run:execution-2",
        reason="fixed",
    )

    rows = _rows(db_session, account.id)
    assert len(rows) == 1
    assert rows[0].id == first.id == second.id
    assert rows[0].fingerprint == "run:execution-2"
    assert rows[0].reason == "fixed"


def test_upsert_clears_a_snooze_when_the_new_reason_has_none(
    db_session, create_account
) -> None:
    """Every column of the conflicting row is replaced, not merged."""
    account = create_account()
    snooze_until = datetime.now(UTC) + timedelta(days=7)

    crud_attention_dismissal.upsert(
        db_session,
        account_id=account.id,
        item_id=ITEM_ID,
        fingerprint="run:execution-1",
        reason="snoozed",
        snooze_until=snooze_until,
    )
    updated = crud_attention_dismissal.upsert(
        db_session,
        account_id=account.id,
        item_id=ITEM_ID,
        fingerprint="run:execution-2",
        reason="expected",
    )

    assert updated.snooze_until is None
    assert len(_rows(db_session, account.id)) == 1


def test_upsert_is_scoped_to_the_account(db_session, create_account) -> None:
    """The same item id in two accounts is two rows, not a conflict."""
    account = create_account()
    other = create_account()

    crud_attention_dismissal.upsert(
        db_session,
        account_id=account.id,
        item_id=ITEM_ID,
        fingerprint="run:execution-1",
        reason="expected",
    )
    crud_attention_dismissal.upsert(
        db_session,
        account_id=other.id,
        item_id=ITEM_ID,
        fingerprint="run:execution-9",
        reason="fixed",
    )

    assert len(_rows(db_session, account.id)) == 1
    assert len(_rows(db_session, other.id)) == 1
    mine = crud_attention_dismissal.get_by_item(
        db_session, account_id=account.id, item_id=ITEM_ID
    )
    assert mine is not None
    assert mine.fingerprint == "run:execution-1"
