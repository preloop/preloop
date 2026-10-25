"""CRUD rules for the governance review tables."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import subprocess
import sys
import textwrap
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account
from preloop.models.crud.governance_review import (
    GovernanceReviewValidationError,
    crud_governance_finding,
    crud_governance_policy_document,
    crud_governance_review_run,
    crud_governance_review_settings,
    crud_governance_session_digest,
    validate_digest,
    validate_observations,
)
from preloop.models.models.governance_review import (
    DIGEST_MAX_BYTES,
    OBSERVATION_EXCERPT_MAX_CHARS,
    POLICY_DOCUMENT_BODY_MAX_BYTES,
    GovernancePolicyDocument,
    GovernanceReviewSettings,
)
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.user import User


def _window() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture(autouse=True)
def fixed_digest_review_week(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CRUD fixtures independent of the wall clock."""
    monkeypatch.setattr(
        "preloop.models.crud.governance_review._digest_review_start",
        lambda: _window()[0],
    )


def _session(db_session: Session, account_id: UUID) -> RuntimeSession:
    row = models.RuntimeSession(
        account_id=account_id,
        session_source_type="cli",
        session_source_id=str(uuid4()),
        started_at=datetime(2026, 10, 13, 10, 0),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _finding(run_id: UUID, fingerprint: str = "unused-tools") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "kind": "inefficiency",
        "category": "unused_tools",
        "severity": "low",
        "title": "Unused tool",
        "suggestion": "Detach the tool.",
        "fingerprint": fingerprint,
        "evidence": [{"excerpt": "attached, never called", "source": "metadata"}],
        "rule_ref": "prefer.tools",
    }


def test_two_versions_leave_one_active_row(
    db_session: Session, test_user: User
) -> None:
    first = crud_governance_policy_document.create_version(
        db_session,
        account_id=test_user.account_id,
        body_markdown="First",
        source="default_template",
        commit=False,
    )
    second = crud_governance_policy_document.create_version(
        db_session,
        account_id=test_user.account_id,
        body_markdown="Second",
        source="user",
        created_by_id=test_user.id,
        commit=False,
    )

    active = crud_governance_policy_document.get_active(
        db_session, account_id=test_user.account_id
    )
    history = crud_governance_policy_document.list_history(
        db_session, account_id=test_user.account_id
    )

    assert first.version == 1
    assert second.version == 2
    assert active is not None
    assert active.id == second.id
    assert [row.version for row in history] == [2, 1]
    assert history[1].is_active is False


def test_partial_unique_index_rejects_a_second_active_row(
    db_session: Session, test_user: User
) -> None:
    crud_governance_policy_document.create_version(
        db_session,
        account_id=test_user.account_id,
        body_markdown="Active",
        source="user",
        commit=False,
    )
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "INSERT INTO governance_policy_document "
                    "(id, created_at, updated_at, account_id, version, "
                    "body_markdown, is_active, source) "
                    "VALUES (:id, now(), now(), :account_id, 2, 'other', "
                    "true, 'user')"
                ),
                {"id": uuid4(), "account_id": test_user.account_id},
            )


def test_concurrent_new_versions_leave_one_active_row(db_engine: Engine) -> None:
    """Two callers serialise on the advisory lock and the partial unique index."""
    account_id = uuid4()
    with Session(db_engine) as db:
        db.add(models.Account(id=account_id, organization_name="Governance race"))
        db.commit()
    barrier = Barrier(2)

    def create() -> int:
        barrier.wait(timeout=5)
        with Session(db_engine) as db:
            row = crud_governance_policy_document.create_version(
                db,
                account_id=account_id,
                body_markdown="Concurrent policy",
                source="user",
            )
            return row.version

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            versions = sorted(pool.map(lambda _: create(), range(2)))
        assert versions == [1, 2]
        with Session(db_engine) as db:
            active = (
                db.query(GovernancePolicyDocument)
                .filter(
                    GovernancePolicyDocument.account_id == account_id,
                    GovernancePolicyDocument.is_active.is_(True),
                )
                .all()
            )
            assert len(active) == 1
            assert active[0].version == 2
    finally:
        with Session(db_engine) as db:
            db.execute(delete(models.Account).where(models.Account.id == account_id))
            db.commit()


def test_get_or_default_is_on_platform_for_cloud(
    db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRELOOP_HOSTED", "true")
    monkeypatch.delenv("PRELOOP_GOVERNANCE_ANALYSIS_MODEL", raising=False)

    row = crud_governance_review_settings.get_or_default(
        db_session, account_id=test_user.account_id
    )

    assert row.mode == "on_platform"
    assert (
        db_session.query(GovernanceReviewSettings)
        .filter(GovernanceReviewSettings.account_id == test_user.account_id)
        .count()
        == 0
    )


def test_get_or_default_is_off_for_self_hosted_without_a_model(
    db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRELOOP_HOSTED", "")
    monkeypatch.delenv("PRELOOP_GOVERNANCE_ANALYSIS_MODEL", raising=False)

    row = crud_governance_review_settings.get_or_default(
        db_session, account_id=test_user.account_id
    )

    assert row.mode == "off"


def test_get_or_default_uses_own_model_when_self_hosted_model_is_configured(
    db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRELOOP_HOSTED", "")
    monkeypatch.setenv("PRELOOP_GOVERNANCE_ANALYSIS_MODEL", "example-flash")

    row = crud_governance_review_settings.get_or_default(
        db_session, account_id=test_user.account_id
    )

    assert row.mode == "on_own_model"


def test_importing_governance_review_crud_does_not_import_httpx() -> None:
    """A worker that only loads the CRUD package must not import the HTTP client.

    ``preloop.models.crud`` imports this module. ``instance_service`` imports
    ``httpx`` at module level, so a top-level import of that service here would
    pull the client into every process that registers a stored spend source.
    """
    script = textwrap.dedent(
        """
        import sys
        import preloop.models.crud.governance_review
        assert "httpx" not in sys.modules
        assert "preloop.services.instance_service" not in sys.modules
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("ok")


def test_set_mode_persists_and_overrides_the_default(
    db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRELOOP_HOSTED", "true")
    crud_governance_review_settings.set_mode(
        db_session,
        account_id=test_user.account_id,
        mode="off",
        mode_changed_by_id=test_user.id,
        commit=False,
    )

    row = crud_governance_review_settings.get_or_default(
        db_session, account_id=test_user.account_id
    )

    assert row.mode == "off"
    assert row.mode_changed_by_id == test_user.id


def test_set_thresholds_keeps_mode_and_round_trips(
    db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRELOOP_HOSTED", "true")
    crud_governance_review_settings.set_mode(
        db_session,
        account_id=test_user.account_id,
        mode="off",
        commit=False,
    )

    row = crud_governance_review_settings.set_thresholds(
        db_session,
        account_id=test_user.account_id,
        detector_thresholds={"retry_limit": {"count": 7}},
        commit=False,
    )

    assert row.mode == "off"
    assert row.detector_thresholds == {"retry_limit": {"count": 7}}


def test_set_thresholds_inserts_the_default_mode_for_a_new_account(
    db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRELOOP_HOSTED", "true")

    row = crud_governance_review_settings.set_thresholds(
        db_session,
        account_id=test_user.account_id,
        detector_thresholds=None,
        commit=False,
    )

    assert row.mode == "on_platform"
    assert row.detector_thresholds is None


def test_set_thresholds_rejects_a_non_object_or_oversized_value(
    db_session: Session, test_user: User
) -> None:
    with pytest.raises(GovernanceReviewValidationError):
        crud_governance_review_settings.set_thresholds(
            db_session,
            account_id=test_user.account_id,
            detector_thresholds=["retry_limit"],  # type: ignore[arg-type]
            commit=False,
        )
    with pytest.raises(GovernanceReviewValidationError):
        crud_governance_review_settings.set_thresholds(
            db_session,
            account_id=test_user.account_id,
            detector_thresholds={"x": "y" * 5000},
            commit=False,
        )


def test_upsert_digest_is_per_segment(db_session: Session, test_user: User) -> None:
    session = _session(db_session, test_user.account_id)
    start, end = _window()
    closed = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    first = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        segment_index=0,
        window_start=start,
        window_end=closed,
        policy_document_version=1,
        digest={"intent": "first"},
        observations=[
            {"rule_ref": "must.secrets", "excerpt": "none", "confidence": 0.2}
        ],
        rolling_summary="Started a parser change.",
        model_alias="example-model",
        input_tokens=12,
        output_tokens=4,
        estimated_cost=1.5,
        funding="account",
        truncated=True,
        commit=False,
    )
    replaced = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        segment_index=0,
        window_start=start,
        window_end=closed,
        policy_document_version=2,
        summary={"intent": "revised"},
        force_resummarize=True,
        override_caller="operator/test",
        override_reason="Explicitly revise the fixture",
        commit=False,
    )
    second = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        segment_index=1,
        window_start=closed,
        window_end=datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc),
        digest={"intent": "next week"},
        commit=False,
    )

    assert first.id == replaced.id
    assert replaced.policy_document_version == 2
    assert replaced.summary == {"intent": "revised"}
    assert replaced.digest == {"intent": "revised"}
    assert replaced.observations == [
        {"rule_ref": "must.secrets", "excerpt": "none", "confidence": 0.2}
    ]
    assert replaced.rolling_summary == "Started a parser change."
    assert replaced.model_alias == "example-model"
    assert replaced.input_tokens == 12
    assert replaced.output_tokens == 4
    assert replaced.estimated_cost == 1.5
    assert replaced.funding == "account"
    assert replaced.truncated is True
    assert replaced.account_id == test_user.account_id
    listed = crud_governance_session_digest.list_for_window(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
    )
    assert [row.segment_index for row in listed] == [0]
    assert second.segment_index == 1


def test_digest_over_four_kibibytes_is_rejected(
    db_session: Session, test_user: User
) -> None:
    session = _session(db_session, test_user.account_id)
    start, end = _window()
    digest = {"intent": "x" * (DIGEST_MAX_BYTES + 1)}
    with pytest.raises(ValueError, match="4"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            window_start=start,
            window_end=end,
            digest=digest,
            commit=False,
        )


def test_recreating_a_run_for_the_same_week_returns_the_existing_run(
    db_session: Session, test_user: User
) -> None:
    start, end = _window()
    first = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        mode_before="on_platform",
        commit=False,
    )
    finished = crud_governance_review_run.finish_run(
        db_session,
        account_id=test_user.account_id,
        run_id=first.id,
        status="completed",
        findings_count=3,
        mode_after="on_platform",
        commit=False,
    )
    second = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        mode_before="off",
        commit=False,
    )

    assert second.id == first.id
    assert finished is not None
    assert finished.status == "completed"
    assert second.status == "completed"
    assert second.findings_count == 3
    assert second.mode_before == "on_platform"


def test_inserting_an_existing_fingerprint_is_a_no_op(
    db_session: Session, test_user: User
) -> None:
    start, end = _window()
    run = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        commit=False,
    )
    payload = _finding(run.id)
    inserted = crud_governance_finding.insert_findings(
        db_session,
        account_id=test_user.account_id,
        findings=[payload, payload],
        commit=False,
    )
    again = crud_governance_finding.insert_findings(
        db_session,
        account_id=test_user.account_id,
        findings=[payload],
        commit=False,
    )
    rows = crud_governance_finding.list_findings(
        db_session, account_id=test_user.account_id, run_id=run.id
    )

    assert len(inserted) == 1
    assert again == []
    assert len(rows) == 1
    assert rows[0].rule_ref == "prefer.tools"


def test_list_findings_filters_and_dismiss(
    db_session: Session, test_user: User
) -> None:
    start, end = _window()
    run = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        commit=False,
    )
    crud_governance_finding.insert_findings(
        db_session,
        account_id=test_user.account_id,
        findings=[
            _finding(run.id, "open"),
            {
                **_finding(run.id, "closed"),
                "kind": "violation",
                "severity": "high",
                "user_id": test_user.id,
            },
        ],
        commit=False,
    )
    closed = crud_governance_finding.list_findings(
        db_session,
        account_id=test_user.account_id,
        kind="violation",
        severity="high",
        user_id=test_user.id,
    )[0]
    crud_governance_finding.dismiss(
        db_session,
        account_id=test_user.account_id,
        finding_id=closed.id,
        dismissed_by_id=test_user.id,
        reason="Expected",
        commit=False,
    )

    open_rows = crud_governance_finding.list_findings(
        db_session, account_id=test_user.account_id, dismissed=False
    )
    dismissed = crud_governance_finding.list_findings(
        db_session, account_id=test_user.account_id, dismissed=True, run_id=run.id
    )

    assert [row.fingerprint for row in open_rows] == ["open"]
    assert [row.fingerprint for row in dismissed] == ["closed"]
    assert dismissed[0].dismissed_reason == "Expected"
    assert dismissed[0].dismissed_by_id == test_user.id


def _other_account(db_session: Session) -> UUID:
    account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
        commit=False,
    )
    return account.id


def test_validate_digest_rejects_known_field_mistakes() -> None:
    with pytest.raises(GovernanceReviewValidationError, match="outcome"):
        validate_digest({"outcome": "nope"})
    with pytest.raises(GovernanceReviewValidationError, match="triviality"):
        validate_digest({"triviality": "huge"})
    with pytest.raises(GovernanceReviewValidationError, match="notable"):
        validate_digest({"notable": ["ok"] * 6})
    with pytest.raises(GovernanceReviewValidationError, match="notable"):
        validate_digest({"notable": ["x" * 281]})
    validate_digest({"outcome": "done", "triviality": "routine", "notable": ["ok"]})


def test_validate_observations_rejects_bad_items() -> None:
    with pytest.raises(GovernanceReviewValidationError, match="list"):
        validate_observations({"rule_ref": "must.secrets"})
    with pytest.raises(GovernanceReviewValidationError, match="rule_ref"):
        validate_observations([{"excerpt": "x", "confidence": 1}])
    with pytest.raises(GovernanceReviewValidationError, match="excerpt"):
        validate_observations(
            [
                {
                    "rule_ref": "must.secrets",
                    "excerpt": "x" * (OBSERVATION_EXCERPT_MAX_CHARS + 1),
                    "confidence": "low",
                }
            ]
        )
    with pytest.raises(GovernanceReviewValidationError, match="confidence"):
        validate_observations(
            [{"rule_ref": "must.secrets", "excerpt": "x", "confidence": None}]
        )


def test_create_version_rejects_invalid_body_and_source(
    db_session: Session, test_user: User
) -> None:
    with pytest.raises(GovernanceReviewValidationError, match="source"):
        crud_governance_policy_document.create_version(
            db_session,
            account_id=test_user.account_id,
            body_markdown="ok",
            source="imported",
            commit=False,
        )
    with pytest.raises(GovernanceReviewValidationError, match="body_markdown"):
        crud_governance_policy_document.create_version(
            db_session,
            account_id=test_user.account_id,
            body_markdown="x" * (POLICY_DOCUMENT_BODY_MAX_BYTES + 1),
            source="user",
            commit=False,
        )


def test_upsert_rejects_a_session_from_another_account(
    db_session: Session, test_user: User
) -> None:
    session = _session(db_session, test_user.account_id)
    start, end = _window()
    with pytest.raises(GovernanceReviewValidationError, match="account"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=_other_account(db_session),
            runtime_session_id=session.id,
            window_start=start,
            window_end=end,
            digest={"intent": "cross"},
            commit=False,
        )


def test_finish_run_rejects_unknown_fields_and_foreign_accounts(
    db_session: Session, test_user: User
) -> None:
    start, end = _window()
    run = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        commit=False,
    )
    with pytest.raises(GovernanceReviewValidationError, match="unknown finish"):
        crud_governance_review_run.finish_run(
            db_session,
            account_id=test_user.account_id,
            run_id=run.id,
            status="completed",
            not_a_column=1,
            commit=False,
        )
    missing = crud_governance_review_run.finish_run(
        db_session,
        account_id=_other_account(db_session),
        run_id=run.id,
        status="failed",
        commit=False,
    )

    assert missing is None
    assert run.status == "running"


def test_insert_findings_checks_uuid_and_run_owner(
    db_session: Session, test_user: User
) -> None:
    start, end = _window()
    run = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        commit=False,
    )
    with pytest.raises(GovernanceReviewValidationError, match="run_id"):
        crud_governance_finding.insert_findings(
            db_session,
            account_id=test_user.account_id,
            findings=[{**_finding(run.id), "run_id": "not-a-uuid"}],
            commit=False,
        )
    with pytest.raises(GovernanceReviewValidationError, match="account"):
        crud_governance_finding.insert_findings(
            db_session,
            account_id=_other_account(db_session),
            findings=[_finding(run.id)],
            commit=False,
        )


def test_dismiss_returns_none_for_a_foreign_account(
    db_session: Session, test_user: User
) -> None:
    start, end = _window()
    run = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        commit=False,
    )
    finding = crud_governance_finding.insert_findings(
        db_session,
        account_id=test_user.account_id,
        findings=[_finding(run.id)],
        commit=False,
    )[0]
    missing = crud_governance_finding.dismiss(
        db_session,
        account_id=_other_account(db_session),
        finding_id=finding.id,
        dismissed_by_id=None,
        commit=False,
    )

    assert missing is None
    assert finding.dismissed_at is None


def test_run_history_and_segment_coverage_are_account_scoped(
    db_session: Session, test_user: User
) -> None:
    """Empty runs stay visible; boundary segments belong to the next week."""
    from datetime import timedelta

    start, end = _window()
    account_id = test_user.account_id
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}, commit=False
    )
    runs = [
        crud_governance_review_run.create_run(
            db_session,
            account_id=owner,
            window_start=window,
            window_end=window + timedelta(days=7),
            commit=False,
        )
        for owner, window in [(account_id, start), (account_id, end), (other.id, start)]
    ]
    assert (
        crud_governance_review_run.summaries_started_at(
            db_session, account_id=account_id
        )
        is None
    )
    assert crud_governance_review_run.segment_counts(
        db_session, account_id=account_id, run_ids=[row.id for row in runs]
    ) == {runs[0].id: 0, runs[1].id: 0}
    session = _session(db_session, account_id)
    for index, window_end in enumerate([start, end - timedelta(seconds=1), end]):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=account_id,
            runtime_session_id=session.id,
            segment_index=index,
            window_start=start,
            window_end=window_end,
            digest={"intent": "Review an example change"},
            commit=False,
        )
    counts = crud_governance_review_run.segment_counts(
        db_session, account_id=account_id, run_ids=[row.id for row in runs]
    )
    assert counts == {runs[0].id: 2, runs[1].id: 1}
    assert (
        crud_governance_review_run.segment_counts(
            db_session, account_id=account_id, run_ids=[]
        )
        == {}
    )
    assert (
        crud_governance_review_run.summaries_started_at(
            db_session, account_id=account_id
        )
        is not None
    )
    page = crud_governance_review_run.list_runs(
        db_session, account_id=account_id, limit=1, offset=0
    )
    assert [row.id for row in page] == [runs[1].id]
    page = crud_governance_review_run.list_runs(
        db_session, account_id=account_id, limit=1, offset=1
    )
    assert [row.id for row in page] == [runs[0].id]


def test_finding_kind_permissions_apply_before_pagination(
    db_session: Session, test_user: User
) -> None:
    """A page does not shrink or expose rows from a forbidden kind or tenant."""
    start, end = _window()
    run = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        commit=False,
    )
    rows = [
        {**_finding(run.id, f"example-{index}"), "kind": kind}
        for index, kind in enumerate(["violation", "inefficiency", "anomaly"])
    ]
    crud_governance_finding.insert_findings(
        db_session,
        account_id=test_user.account_id,
        findings=rows,
        commit=False,
    )
    for row in crud_governance_finding.list_findings(
        db_session, account_id=test_user.account_id
    ):
        row.created_at = start.replace(
            hour={"violation": 0, "inefficiency": 1, "anomaly": 2}[row.kind]
        )
    db_session.flush()
    page = crud_governance_finding.list_findings(
        db_session,
        account_id=test_user.account_id,
        allowed_kinds=["violation", "anomaly"],
        limit=1,
        offset=1,
    )
    assert [row.kind for row in page] == ["anomaly"]
    assert (
        crud_governance_finding.list_findings(
            db_session, account_id=test_user.account_id, allowed_kinds=[]
        )
        == []
    )
    assert (
        crud_governance_finding.list_findings(
            db_session, account_id=uuid4(), allowed_kinds=["violation", "anomaly"]
        )
        == []
    )


def test_finding_member_names_exclude_other_accounts(
    db_session: Session, test_user: User
) -> None:
    assert crud_governance_finding.member_names(
        db_session, account_id=test_user.account_id, user_ids=[test_user.id]
    ) == {test_user.id: test_user.full_name or test_user.username}
    assert (
        crud_governance_finding.member_names(
            db_session, account_id=uuid4(), user_ids=[test_user.id]
        )
        == {}
    )
    assert (
        crud_governance_finding.member_names(
            db_session, account_id=test_user.account_id, user_ids=[]
        )
        == {}
    )


def test_category_filter_precedes_paging_and_keeps_scope(
    db_session: Session, test_user: User
) -> None:
    start, end = _window()
    run = crud_governance_review_run.create_run(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        commit=False,
    )
    crud_governance_finding.insert_findings(
        db_session,
        account_id=test_user.account_id,
        findings=[
            {**_finding(run.id, f"category-{i}"), "category": category}
            for i, category in enumerate(
                ["first_seen", "unused_tools", "spend_outlier"]
            )
        ],
        commit=False,
    )
    page = crud_governance_finding.list_findings(
        db_session,
        account_id=test_user.account_id,
        category="tool_hygiene",
        allowed_kinds=["inefficiency"],
        limit=1,
    )
    assert len(page) == 1 and page[0].category == "unused_tools"
    assert (
        crud_governance_finding.list_findings(
            db_session,
            account_id=test_user.account_id,
            category="tool_hygiene",
            allowed_kinds=["anomaly"],
            limit=1,
        )
        == []
    )
    assert (
        crud_governance_finding.list_findings(
            db_session, account_id=uuid4(), category="tool_hygiene"
        )
        == []
    )


@pytest.mark.parametrize(
    "status", ["complete", "deterministic_only", "deterministic_only_small", None]
)
def test_completed_segment_cannot_be_overwritten(
    db_session: Session, test_user: User, status: str | None
) -> None:
    runtime = _session(db_session, test_user.account_id)
    start, end = _window()
    stored = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=start,
        window_end=end,
        digest={"summary_status": status, "intent": "Preserve this"},
        commit=False,
    )
    with pytest.raises(GovernanceReviewValidationError, match="complete"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime.id,
            window_start=start,
            window_end=end,
            summary={"intent": "Overwrite through summary alias"},
            commit=False,
        )
    db_session.refresh(stored)
    assert stored.digest["intent"] == "Preserve this"


def test_historical_segment_requires_attributed_override(
    db_session: Session, test_user: User
) -> None:
    from datetime import timedelta

    runtime = _session(db_session, test_user.account_id)
    end = _window()[0] - timedelta(days=30)
    with pytest.raises(GovernanceReviewValidationError, match="review window"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime.id,
            window_start=end,
            window_end=end,
            digest={},
            commit=False,
        )
    with pytest.raises(GovernanceReviewValidationError, match="caller and reason"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime.id,
            window_start=end,
            window_end=end,
            digest={},
            include_history=True,
            commit=False,
        )


def test_generic_update_cannot_bypass_completed_guard(
    db_session: Session, test_user: User
) -> None:
    runtime = _session(db_session, test_user.account_id)
    start, end = _window()
    stored = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=start,
        window_end=end,
        digest={"summary_status": "complete"},
        commit=False,
    )
    with pytest.raises(GovernanceReviewValidationError, match="complete"):
        crud_governance_session_digest.update(
            db_session,
            db_obj=stored,
            obj_in={"digest": {"summary_status": "pending"}},
            commit=False,
        )


def test_explicit_crud_override_records_caller_reason_in_audit(
    db_session: Session, test_user: User, caplog: pytest.LogCaptureFixture
) -> None:
    from preloop.models.crud import crud_audit_log

    runtime = _session(db_session, test_user.account_id)
    start, end = _window()
    stored = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=start,
        window_end=end,
        digest={"summary_status": "complete"},
        commit=False,
    )
    with pytest.raises(GovernanceReviewValidationError, match="caller and reason"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime.id,
            window_start=start,
            window_end=end,
            digest={"summary_status": "pending"},
            force_resummarize=True,
            commit=False,
        )
    replaced = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=start,
        window_end=end,
        digest={"summary_status": "pending"},
        force_resummarize=True,
        override_caller="operator/test",
        override_reason="Approved repair",
        commit=False,
    )
    assert replaced.id == stored.id
    assert replaced.digest["summary_status"] == "pending"
    audits = crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        action="governance_review.summary_override",
        resource_id=str(stored.id),
    )
    assert len(audits) == 1
    assert audits[0].details["caller"] == "operator/test"
    assert audits[0].details["reason"] == "Approved repair"
    assert audits[0].details["force_resummarize"] is True
    assert str(audits[0].id) in caplog.text
    assert "operator/test" not in caplog.text
    assert "Approved repair" not in caplog.text


def test_parse_failure_enters_retry_queue_without_complete_segments(
    db_session: Session, test_user: User
) -> None:
    from datetime import timedelta
    from preloop.models.crud.governance_session_candidates import (
        list_session_digest_retries,
    )

    start, end = _window()
    identifiers = {}
    for status in ("complete", "deterministic_only", "parse_failed"):
        runtime = _session(db_session, test_user.account_id)
        row = crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime.id,
            window_start=start,
            window_end=end,
            digest={"summary_status": status},
            commit=False,
        )
        identifiers[status] = row.id
    found = list_session_digest_retries(
        db_session,
        account_id=test_user.account_id,
        stale_before=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    assert found == [identifiers["parse_failed"]]


def test_older_review_boundary_cannot_implicitly_allow_history(
    db_session: Session, test_user: User
) -> None:
    from datetime import timedelta

    runtime = _session(db_session, test_user.account_id)
    old = _window()[0] - timedelta(days=30)
    with pytest.raises(GovernanceReviewValidationError, match="review window"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime.id,
            window_start=old,
            window_end=old,
            digest={},
            review_window_start=old,
            commit=False,
        )


def test_failed_segment_attempt_counter_cannot_be_reset_implicitly(
    db_session: Session, test_user: User
) -> None:
    runtime = _session(db_session, test_user.account_id)
    start, end = _window()
    stored = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=start,
        window_end=end,
        digest={"summary_status": "provider_failed", "summary_attempts": 3},
        commit=False,
    )
    with pytest.raises(GovernanceReviewValidationError, match="summary attempts"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime.id,
            window_start=start,
            window_end=end,
            digest={"summary_status": "pending", "summary_attempts": 0},
            commit=False,
        )
    db_session.refresh(stored)
    assert stored.digest["summary_attempts"] == 3


@pytest.mark.parametrize("same_claim", [True, False])
@pytest.mark.parametrize("format_rejected", [True, False])
def test_active_format_rejection_can_refund_only_its_own_attempt(
    db_session: Session, test_user: User, same_claim: bool, format_rejected: bool
) -> None:
    runtime = _session(db_session, test_user.account_id)
    start, end = _window()
    claim = {
        "summary_status": "summarizing",
        "summary_attempts": 2,
        "summary_attempted_at": end.isoformat(),
    }
    stored = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=start,
        window_end=end,
        digest=claim,
        commit=False,
    )
    refunded = {
        **claim,
        "summary_attempts": 1,
        "provider_status_code": 400,
        "provider_error_class": "BadRequestError",
    }
    if format_rejected:
        refunded["summary_stop_reason"] = "response_format_rejected"
    if not same_claim:
        refunded["summary_attempted_at"] = start.isoformat()
    if not same_claim or not format_rejected:
        with pytest.raises(GovernanceReviewValidationError, match="summary attempts"):
            crud_governance_session_digest.upsert(
                db_session,
                account_id=test_user.account_id,
                runtime_session_id=runtime.id,
                window_start=start,
                window_end=end,
                digest=refunded,
                commit=False,
            )
        db_session.refresh(stored)
        assert stored.digest["summary_attempts"] == 2
        return
    crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=start,
        window_end=end,
        digest=refunded,
        commit=False,
    )
    db_session.refresh(stored)
    assert stored.digest["summary_attempts"] == 1


@pytest.mark.parametrize("alias", ["digest", "summary"])
@pytest.mark.parametrize("status", ["provider_failed", "budget_limited"])
def test_historical_retirement_preserves_attempts(
    db_session: Session, test_user: User, alias: str, status: str
) -> None:
    """Retirement needs no history override and cannot reset spent attempts."""
    from datetime import timedelta

    runtime = _session(db_session, test_user.account_id)
    old = _window()[0] - timedelta(days=30)
    stored = crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=old,
        window_end=old,
        digest={"summary_status": status, "summary_attempts": 3},
        include_history=True,
        override_caller="test",
        override_reason="Seed a failed historical segment",
        commit=False,
    )
    crud_governance_session_digest.upsert(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime.id,
        window_start=old,
        window_end=old,
        **{alias: {"summary_status": "outside_review_window"}},
        commit=False,
    )
    db_session.refresh(stored)
    assert (
        stored.digest
        == stored.summary
        == {
            "summary_status": "outside_review_window",
            "summary_attempts": 3,
        }
    )
    with pytest.raises(GovernanceReviewValidationError, match="summary attempts"):
        crud_governance_session_digest.upsert(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime.id,
            window_start=old,
            window_end=old,
            digest={"summary_status": "pending"},
            include_history=True,
            override_caller="test",
            override_reason="Attempt to restart with a stale counter",
            commit=False,
        )
