"""Assessment history reads and the extended pull request record (#2016)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_assessment, crud_issue_cost

T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def account(create_account) -> models.Account:
    return create_account()


@pytest.fixture
def issue(create_issue) -> models.Issue:
    return create_issue()


def _pr(db: Session, account: models.Account, n: int = 1):
    return crud_issue_cost.get_or_create_pull_request(
        db, account_id=account.id, pr_key=f"https://example.com/org/repo/pull/{n}"
    )


def _record(db, account, minutes, revision, verdict="covered", **subject):
    return crud_assessment.record(
        db,
        account_id=account.id,
        source="audit",
        purpose_verdict=verdict,
        revision=revision,
        assessed_at=T0 + timedelta(minutes=minutes),
        **subject,
    )


def test_record_keeps_provenance(db_session, account, issue) -> None:
    row = crud_assessment.record(
        db_session,
        account_id=account.id,
        source="report",
        purpose_verdict="partial",
        issue_id=issue.id,
        revision="a" * 40,
        criteria_snapshot=["returns 404 for unknown ids"],
        evaluator_version="audit-v3",
    )
    assert row.criteria_snapshot == ["returns 404 for unknown ids"]
    assert row.evaluator_version == "audit-v3"
    assert row.assessed_at is not None
    assert row.flow_execution_id is None
    assert row.risk_flags == [] and row.evidence == {}


def test_record_rejects_bad_input(db_session, account, issue) -> None:
    with pytest.raises(ValueError):
        _record(db_session, account, 0, "a", verdict="great", issue_id=issue.id)
    with pytest.raises(ValueError):
        crud_assessment.record(
            db_session,
            account_id=account.id,
            source="vibes",
            purpose_verdict="covered",
            issue_id=issue.id,
        )
    with pytest.raises(ValueError):
        crud_assessment.record(
            db_session, account_id=account.id, source="audit", purpose_verdict="covered"
        )


def test_history_is_append_only_newest_per_revision(db_session, account, issue) -> None:
    _record(db_session, account, 0, "aaa", "unverified", issue_id=issue.id)
    first_b = _record(db_session, account, 1, "bbb", "partial", issue_id=issue.id)
    newest_a = _record(db_session, account, 2, "aaa", "covered", issue_id=issue.id)
    newest_b = _record(db_session, account, 3, "bbb", "covered", issue_id=issue.id)

    rows = crud_assessment.newest_per_revision(
        db_session, account_id=account.id, issue_id=issue.id
    )
    assert [r.id for r in rows] == [newest_b.id, newest_a.id]
    assert (
        crud_assessment.newest(
            db_session, account_id=account.id, issue_id=issue.id, revision="bbb"
        ).id
        == newest_b.id
    )
    assert db_session.get(models.Assessment, first_b.id).purpose_verdict == "partial"


def test_history_reads_are_scoped_to_subject_and_account(
    db_session, account, issue, create_account
) -> None:
    pr = _pr(db_session, account)
    on_pr = _record(db_session, account, 0, "aaa", issue_cost_pull_request_id=pr.id)
    _record(db_session, account, 1, "aaa", issue_id=issue.id)
    other = create_account()
    assert crud_assessment.newest_per_revision(
        db_session, account_id=account.id, issue_cost_pull_request_id=pr.id
    ) == [on_pr]
    assert (
        crud_assessment.newest_per_revision(
            db_session, account_id=other.id, issue_id=issue.id
        )
        == []
    )
    with pytest.raises(ValueError):
        crud_assessment.newest_per_revision(db_session, account_id=account.id)


def test_pull_request_identity_defaults_and_updates(db_session, account) -> None:
    pr = _pr(db_session, account)
    assert pr.origin == "unknown"
    assert pr.author_id is None and pr.head_sha is None and pr.state is None
    crud_issue_cost.record_pull_request_identity(
        db_session,
        row=pr,
        origin="copilot",
        author_id="198982749",
        head_sha="c" * 40,
        state="open",
    )
    crud_issue_cost.record_pull_request_identity(db_session, row=pr, state="merged")
    pr = crud_issue_cost.get_pull_request(
        db_session, account_id=account.id, pr_key=pr.pr_key
    )
    assert (pr.origin, pr.author_id, pr.head_sha, pr.state) == (
        "copilot",
        "198982749",
        "c" * 40,
        "merged",
    )


def test_pull_request_identity_rejects_bad_values(db_session, account) -> None:
    pr = _pr(db_session, account)
    crud_issue_cost.record_pull_request_identity(db_session, row=pr, author_id="1")
    with pytest.raises(ValueError):
        crud_issue_cost.record_pull_request_identity(db_session, row=pr, author_id="2")
    with pytest.raises(ValueError):
        crud_issue_cost.record_pull_request_identity(db_session, row=pr, origin="bot")
    with pytest.raises(ValueError):
        crud_issue_cost.record_pull_request_identity(db_session, row=pr, state="draft")


def test_pull_request_links_several_issues_without_touching_rollup(
    db_session, account, create_issue
) -> None:
    first, second = create_issue(), create_issue()
    pr, other_pr = _pr(db_session, account, 1), _pr(db_session, account, 2)
    for issue in (first, second, first):
        crud_issue_cost.link_pull_request_issue(db_session, row=pr, issue_id=issue.id)
    crud_issue_cost.link_pull_request_issue(db_session, row=other_pr, issue_id=first.id)
    linked = crud_issue_cost.issue_ids_for_pull_request(
        db_session, account_id=account.id, pull_request_id=pr.id
    )
    assert sorted(linked) == sorted([first.id, second.id])
    assert {
        row.id
        for row in crud_issue_cost.pull_requests_for_issue(
            db_session, account_id=account.id, issue_id=first.id
        )
    } == {pr.id, other_pr.id}
    assert pr.rollup_id is None and pr.ambiguous is False
