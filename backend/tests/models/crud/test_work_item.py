"""Work item state transitions, PR upserts and assessments (#2016)."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

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


def test_ready_pr_closed_unmerged_returns_to_open(db_session, account) -> None:
    item = _item(db_session, account, "ready")
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


def _thread(db: Session, account: models.Account, tracker) -> models.FlowThread:
    flow = models.Flow(
        name=f"flow {uuid.uuid4().hex[:8]}",
        account_id=account.id,
        prompt_template="example",
        agent_config={},
    )
    db.add(flow)
    db.flush()
    now = datetime.now(UTC).replace(tzinfo=None)
    thread = models.FlowThread(
        account_id=account.id,
        flow_id=flow.id,
        tracker_id=tracker.id,
        repository_id="42",
        pr_number=uuid.uuid4().hex[:6],
        pr_url="https://example.com/o/r/pull/1",
        provider="github",
        branch="feature/example",
        context={},
        policy={},
        cursor={},
        latest_execution_id=uuid.uuid4(),
        due_at=now + timedelta(hours=1),
        expires_at=now + timedelta(days=7),
    )
    db.add(thread)
    db.flush()
    return thread


def test_get_for_thread_is_account_and_thread_scoped(
    db_session, create_account, create_tracker
) -> None:
    first, second = create_account(), create_account()
    first_tracker = create_tracker(account=first)
    thread = _thread(db_session, first, first_tracker)
    other_thread = _thread(db_session, first, first_tracker)
    foreign_thread = _thread(db_session, second, create_tracker(account=second))
    mine = crud_work_item.open(
        db_session, account_id=first.id, source="flow", flow_thread_id=thread.id
    )
    crud_work_item.open(
        db_session, account_id=first.id, source="flow", flow_thread_id=other_thread.id
    )
    crud_work_item.open(
        db_session,
        account_id=second.id,
        source="flow",
        flow_thread_id=foreign_thread.id,
    )
    found = crud_work_item.get_for_thread(
        db_session, account_id=first.id, flow_thread_id=thread.id
    )
    assert [item.id for item in found] == [mine.id]
    assert (
        crud_work_item.get_for_thread(
            db_session, account_id=second.id, flow_thread_id=thread.id
        )
        == []
    )


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


@pytest.mark.parametrize("field", ["cost", "metadata", "account_id", "id"])
def test_pr_upsert_rejects_non_forge_field_before_writing(
    db_session, account, field: str
) -> None:
    item = _item(db_session, account)
    with pytest.raises(ValueError, match="unknown work item PR field"):
        crud_work_item_pr.upsert(
            db_session,
            item=item,
            repo="o/r",
            pr_number=1,
            state="open",
            **{field: uuid.uuid4()},
        )
    assert crud_work_item_pr.list_for_work_item(db_session, work_item_id=item.id) == []


def test_concurrent_pr_upserts_converge_on_one_row(db_engine: Engine) -> None:
    """Two sessions upserting the same new PR both succeed; one row remains."""
    factory = sessionmaker(bind=db_engine)
    with factory() as setup:
        account = models.Account(organization_name=f"Org {uuid.uuid4().hex[:8]}")
        setup.add(account)
        setup.flush()
        item = crud_work_item.open(setup, account_id=account.id, source="flow")
        setup.commit()
        account_id, item_id = account.id, item.id
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def upsert(sha: str) -> None:
        try:
            with factory() as db:
                own = db.get(models.WorkItem, item_id)
                assert own is not None
                barrier.wait()
                crud_work_item_pr.upsert(
                    db, item=own, repo="o/r", pr_number=3, state="open", head_sha=sha
                )
                db.commit()
        except BaseException as error:  # surfaced below
            errors.append(error)

    workers = [threading.Thread(target=upsert, args=(c * 40,)) for c in "ab"]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    try:
        assert errors == []
        with factory() as db:
            rows = crud_work_item_pr.list_for_work_item(db, work_item_id=item_id)
            assert len(rows) == 1
            assert rows[0].head_sha in {"a" * 40, "b" * 40}
    finally:
        with factory() as db:
            db.delete(db.get(models.Account, account_id))
            db.commit()


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
