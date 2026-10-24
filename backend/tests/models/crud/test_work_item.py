"""Work item state transitions, PR upserts and assessments (#2016)."""

from decimal import Decimal
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_assessment, crud_work_item, crud_work_item_pr
from preloop.models.crud.work_item import (
    WORK_ITEM_TRANSITIONS,
    InvalidWorkItemTransitionError,
)
from preloop.models.models.work_item import WORK_ITEM_STATES


@pytest.fixture
def account(create_account) -> models.Account:
    return create_account()


def _item(db: Session, account: models.Account, state: str = "open"):
    item = crud_work_item.open(
        db, account_id=account.id, source="flow", expected_artifact="pull_request"
    )
    item.state = state
    db.flush()
    return item


def test_open_creates_open_item_with_nullable_links(db_session, account) -> None:
    item = crud_work_item.open(db_session, account_id=account.id, source="tracker")
    assert item.state == "open"
    assert item.issue_id is None
    assert item.flow_execution_id is None
    assert item.flow_thread_id is None
    assert item.latest_assessment_id is None
    assert not hasattr(item, "head_sha")
    assert not hasattr(item, "cost")
    assert not hasattr(item, "merged_at")


def test_happy_path_open_pr_open_ready_merged(db_session, account) -> None:
    item = _item(db_session, account)
    assert crud_work_item.mark_pr_open(db_session, item=item).state == "pr_open"
    assert crud_work_item.mark_ready(db_session, item=item).state == "ready"
    assert crud_work_item.mark_merged(db_session, item=item).state == "merged"


def test_pr_closed_unmerged_returns_to_open(db_session, account) -> None:
    item = _item(db_session, account, "pr_open")
    assert crud_work_item.reopen(db_session, item=item).state == "open"


def test_new_push_moves_ready_back_to_pr_open(db_session, account) -> None:
    item = _item(db_session, account, "ready")
    assert crud_work_item.mark_pr_open(db_session, item=item).state == "pr_open"


def test_no_change_from_open(db_session, account) -> None:
    item = _item(db_session, account)
    assert crud_work_item.mark_no_change(db_session, item=item).state == "no_change"


def test_close_records_reason_and_actor(db_session, account) -> None:
    item = _item(db_session, account, "ready")
    crud_work_item.close(db_session, item=item, reason="superseded", actor="system")
    db_session.expire(item)
    assert (item.state, item.closed_reason, item.closed_by) == (
        "closed",
        "superseded",
        "system",
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in WORK_ITEM_STATES
        for target in WORK_ITEM_STATES
        if (current, target) not in WORK_ITEM_TRANSITIONS
    ],
)
def test_every_unlisted_transition_is_rejected(
    db_session, account, current: str, target: str
) -> None:
    item = _item(db_session, account, current)
    with pytest.raises(InvalidWorkItemTransitionError):
        crud_work_item.transition(db_session, item=item, state=target)
    assert item.state == current


@pytest.mark.parametrize(("current", "target"), WORK_ITEM_TRANSITIONS)
def test_every_listed_transition_is_accepted(
    db_session, account, current: str, target: str
) -> None:
    item = _item(db_session, account, current)
    crud_work_item.transition(db_session, item=item, state=target)
    assert item.state == target


def test_terminal_states_have_no_exits() -> None:
    sources = {current for current, _ in WORK_ITEM_TRANSITIONS}
    assert sources.isdisjoint({"merged", "no_change", "closed"})


def test_unknown_state_is_rejected(db_session, account) -> None:
    item = _item(db_session, account)
    with pytest.raises(ValueError, match="unknown work item state"):
        crud_work_item.transition(db_session, item=item, state="verified")


def test_record_reconciliation_sets_and_clears_error(db_session, account) -> None:
    item = _item(db_session, account)
    crud_work_item.record_reconciliation(db_session, item=item, error="rate limited")
    assert item.last_error == "rate limited"
    assert item.last_reconciled_at is not None
    crud_work_item.record_reconciliation(db_session, item=item)
    assert item.last_error is None


def test_list_by_state_is_account_scoped(db_session, create_account) -> None:
    first, second = create_account(), create_account()
    mine = _item(db_session, first, "ready")
    _item(db_session, first, "merged")
    _item(db_session, second, "ready")
    found = crud_work_item.list_by_state(
        db_session, account_id=first.id, states=["ready"]
    )
    assert [item.id for item in found] == [mine.id]


def test_pr_upsert_updates_one_row_per_number(db_session, account) -> None:
    item = _item(db_session, account, "pr_open")
    first = crud_work_item_pr.upsert(
        db_session,
        item=item,
        repo="Example/Repo",
        pr_number=7,
        state="open",
        head_sha="a" * 40,
        etag='W/"1"',
    )
    second = crud_work_item_pr.upsert(
        db_session,
        item=item,
        repo="example/repo",
        pr_number=7,
        state="merged",
        merged=True,
        merger="jane-doe",
    )
    assert first.id == second.id
    assert (second.repo, second.state, second.merged, second.head_sha) == (
        "example/repo",
        "merged",
        True,
        "a" * 40,
    )
    rows = crud_work_item_pr.list_for_work_item(db_session, work_item_id=item.id)
    assert [row.id for row in rows] == [first.id]


def test_pr_upsert_rejects_unknown_field(db_session, account) -> None:
    item = _item(db_session, account)
    with pytest.raises(ValueError, match="unknown work item PR field"):
        crud_work_item_pr.upsert(
            db_session, item=item, repo="o/r", pr_number=1, state="open", cost=1
        )


def test_pr_number_is_unique_per_work_item(db_session, account) -> None:
    item = _item(db_session, account)
    savepoint = db_session.begin_nested()
    for _ in range(2):
        db_session.add(
            models.WorkItemPR(
                account_id=account.id,
                work_item_id=item.id,
                repo="o/r",
                pr_number=1,
                state="open",
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()
    savepoint.rollback()


def test_assessment_becomes_latest_on_work_item(db_session, account) -> None:
    item = _item(db_session, account, "merged")
    crud_assessment.record(
        db_session, account_id=account.id, work_item=item, purpose_verdict="partial"
    )
    latest = crud_assessment.record(
        db_session,
        account_id=account.id,
        work_item=item,
        purpose_verdict="covered",
        risk_flags=["hot_path_without_tests"],
        evidence={"files": ["backend/example.py"]},
        cost=Decimal("0.1234"),
    )
    assert item.latest_assessment_id == latest.id
    db_session.expire(latest)
    assert latest.risk_flags == ["hot_path_without_tests"]
    assert latest.cost == Decimal("0.1234")


def test_assessment_without_work_item(db_session, account) -> None:
    row = crud_assessment.record(
        db_session, account_id=account.id, purpose_verdict="no_criteria"
    )
    assert row.work_item_id is None
    assert (row.risk_flags, row.evidence) == ([], {})


def test_assessment_rejects_unknown_verdict_and_foreign_item(
    db_session, create_account
) -> None:
    first, second = create_account(), create_account()
    item = _item(db_session, first)
    with pytest.raises(ValueError, match="unknown purpose verdict"):
        crud_assessment.record(
            db_session, account_id=first.id, purpose_verdict="verified"
        )
    with pytest.raises(ValueError, match="another account"):
        crud_assessment.record(
            db_session,
            account_id=second.id,
            work_item=item,
            purpose_verdict="covered",
        )


def test_deleting_assessment_clears_latest_pointer(db_session, account) -> None:
    item = _item(db_session, account)
    row = crud_assessment.record(
        db_session, account_id=account.id, work_item=item, purpose_verdict="covered"
    )
    db_session.delete(row)
    db_session.flush()
    db_session.expire(item)
    assert item.latest_assessment_id is None
    assert db_session.get(models.WorkItem, item.id) is not None
