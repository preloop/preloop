"""Durable worker outcomes, redelivery fencing, and abandoned-claim recovery."""

from datetime import datetime, timedelta, timezone

import pytest

from preloop.models.crud import crud_repricing_job
from preloop.services.usage_repricing import RepriceResult
from preloop.sync import tasks


@pytest.fixture
def job(db_session, test_user):
    now = datetime.now(timezone.utc)
    return crud_repricing_job.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "status": "queued",
            "request": {
                "start_date": (now - timedelta(days=30)).isoformat(),
                "end_date": now.isoformat(),
            },
        },
    )


@pytest.fixture
def worker(db_session, monkeypatch):
    def session():
        yield db_session

    monkeypatch.setattr(tasks, "get_db_session", session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "preloop.services.model_price_catalog.load_catalog", lambda: None
    )
    return tasks.reprice_gateway_usage_task


def _kwargs(job):
    return dict(
        job_id=str(job.id),
        account_id=str(job.account_id),
        start="ignored",
        end="ignored",
    )


def test_worker_persists_result_and_terminal_delivery_is_noop(
    db_session, job, worker, monkeypatch
):
    calls = []

    def reprice(*args, **kwargs):
        calls.append(kwargs)
        return RepriceResult(rows_examined=180, rows_updated=175)

    monkeypatch.setattr(
        "preloop.services.usage_repricing.reprice_gateway_usage", reprice
    )
    kwargs = _kwargs(job)
    assert worker(**kwargs)["rows_updated"] == 175
    db_session.refresh(job)
    assert job.status == "succeeded"
    assert job.result["rows_examined"] == 180
    assert worker(**kwargs) is None
    assert len(calls) == 1


def test_worker_failure_visible_and_propagates(db_session, job, worker, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("private diagnostic")

    monkeypatch.setattr("preloop.services.usage_repricing.reprice_gateway_usage", fail)
    kwargs = _kwargs(job)
    with pytest.raises(ValueError):
        worker(**kwargs)
    db_session.refresh(job)
    assert job.status == "failed"
    assert "Some usage may already" in job.error
    assert "private diagnostic" not in job.error
    assert worker(**kwargs) is None


def test_live_duplicate_remains_unacknowledged(db_session, job, worker):
    kwargs = _kwargs(job)
    assert crud_repricing_job.claim(
        db_session, job_id=kwargs["job_id"], account_id=kwargs["account_id"]
    )
    with pytest.raises(RuntimeError, match="already being processed"):
        worker(**kwargs)
    db_session.refresh(job)
    assert job.status == "running"


def test_stale_claim_recovers_and_fences_old_worker(db_session, job):
    kwargs = dict(job_id=str(job.id), account_id=str(job.account_id))
    assert crud_repricing_job.claim(db_session, **kwargs).attempts == 1
    db_session.refresh(job)
    job.heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=6
    )
    db_session.commit()
    assert crud_repricing_job.claim(db_session, **kwargs).attempts == 2
    assert not crud_repricing_job.heartbeat(db_session, **kwargs, attempt=1)
    assert not crud_repricing_job.finish(db_session, **kwargs, attempt=1, result={})
    assert crud_repricing_job.finish(
        db_session, **kwargs, attempt=2, result={"rows_updated": 1}
    )


def test_exhausted_crashed_worker_becomes_terminal(db_session, job, worker):
    job.status = "running"
    job.attempts = 3
    job.heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=6
    )
    db_session.commit()
    assert worker(**_kwargs(job)) is None
    db_session.refresh(job)
    assert job.status == "failed"


def test_submission_failure_does_not_clobber_claim(db_session, job):
    kwargs = dict(job_id=str(job.id), account_id=str(job.account_id))
    crud_repricing_job.claim(db_session, **kwargs)
    crud_repricing_job.fail_submission(
        db_session, job_id=job.id, account_id=job.account_id
    )
    db_session.refresh(job)
    assert job.status == "running"


def test_lost_finish_lease_does_not_return_success(
    db_session, job, worker, monkeypatch
):
    monkeypatch.setattr(
        "preloop.services.usage_repricing.reprice_gateway_usage",
        lambda *a, **k: RepriceResult(),
    )
    monkeypatch.setattr(crud_repricing_job, "finish", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="lease was superseded"):
        worker(**_kwargs(job))


def test_failed_heartbeat_rolls_back_pending_edits(db_session, job):
    kwargs = dict(job_id=str(job.id), account_id=str(job.account_id))
    crud_repricing_job.claim(db_session, **kwargs)
    db_session.refresh(job)
    job.error = "uncommitted edit"
    assert not crud_repricing_job.heartbeat(db_session, **kwargs, attempt=2)
    db_session.refresh(job)
    assert job.error is None
