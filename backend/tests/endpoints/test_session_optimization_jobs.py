"""Endpoint and worker tests for async session-optimization jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from preloop.models.crud import (
    crud_account,
    crud_optimization_job,
    crud_runtime_session,
)
from preloop.models.models.optimization_job import (
    OptimizationJob,
    OptimizationJobStatus,
)
from preloop.schemas.gateway_usage import (
    RuntimeSessionOptimizationRequest,
    RuntimeSessionOptimizationResponse,
)
from preloop.services.session_optimization import SessionOptimizationService
from preloop.services.session_optimization_jobs import (
    USER_FACING_JOB_ERROR,
    _execute_job,
    run_optimization_job_sweep,
)


@pytest.fixture(autouse=True)
def no_dispatch():
    """Keep worker threads out of endpoint tests.

    The real worker opens its own DB session against the engine, which cannot
    see rows inside the test transaction; job execution is tested directly via
    :func:`_execute_job` with an injected session instead.
    """
    with patch("preloop.services.session_optimization_jobs._dispatch_job") as dispatch:
        yield dispatch


@pytest.fixture
def cost_client(app, db_session, test_user):
    """Test client bound to the test account for the cost routes."""
    from fastapi.testclient import TestClient

    from preloop.api.common import get_account_for_user

    account = crud_account.get(db_session, id=test_user.account_id)
    app.dependency_overrides[get_account_for_user] = lambda: account
    with TestClient(app) as test_client:
        yield test_client


def make_runtime_session(db_session, account_id, source_id: str):
    """Create one runtime session owned by the given account."""
    now = datetime.now(UTC)
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=source_id,
        session_reference=f"claude-{source_id}",
        runtime_principal_type="claude_code",
        runtime_principal_id=source_id,
        runtime_principal_name="Claude Workspace",
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()
    return runtime_session


@pytest.fixture
def runtime_session(db_session, test_user):
    """A runtime session owned by the test user's account."""
    return make_runtime_session(db_session, test_user.account_id, "workspace-opt-jobs")


def jobs_url(session_id) -> str:
    return f"/api/v1/billing/cost/runtime-sessions/{session_id}/optimizations/jobs"


MINIMAL_RESULT = {"generated_by": "local", "suggestions": []}


class TestSubmitJob:
    def test_post_creates_pending_job_and_returns_202(
        self, cost_client, db_session, test_user, runtime_session
    ) -> None:
        response = cost_client.post(jobs_url(runtime_session.id), json={})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "pending"
        job = crud_optimization_job.get(db_session, id=body["job_id"])
        assert job is not None
        assert str(job.account_id) == str(test_user.account_id)
        assert str(job.runtime_session_id) == str(runtime_session.id)
        assert job.status == OptimizationJobStatus.PENDING

    def test_double_post_returns_same_active_job(
        self, cost_client, db_session, runtime_session, no_dispatch
    ) -> None:
        """A double-click converges on one job — no second model spend."""
        first = cost_client.post(jobs_url(runtime_session.id), json={})
        second = cost_client.post(jobs_url(runtime_session.id), json={})

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["job_id"] == first.json()["job_id"]
        assert no_dispatch.call_count == 1
        assert (
            db_session.query(OptimizationJob)
            .filter(OptimizationJob.runtime_session_id == str(runtime_session.id))
            .count()
            == 1
        )

    def test_post_unknown_session_404s(self, cost_client) -> None:
        response = cost_client.post(
            jobs_url("00000000-0000-0000-0000-000000000000"), json={}
        )
        assert response.status_code == 404


class TestGetJob:
    def test_get_pending_job_shape(self, cost_client, runtime_session) -> None:
        job_id = cost_client.post(jobs_url(runtime_session.id), json={}).json()[
            "job_id"
        ]

        response = cost_client.get(f"{jobs_url(runtime_session.id)}/{job_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == job_id
        assert body["status"] == "pending"
        assert body["result"] is None
        assert body["error"] is None

    def test_get_succeeded_job_returns_optimization_response_shape(
        self, cost_client, db_session, runtime_session
    ) -> None:
        job_id = cost_client.post(jobs_url(runtime_session.id), json={}).json()[
            "job_id"
        ]
        assert crud_optimization_job.transition(
            db_session,
            job_id=job_id,
            from_statuses=(OptimizationJobStatus.PENDING,),
            to_status=OptimizationJobStatus.RUNNING,
        )
        assert crud_optimization_job.transition(
            db_session,
            job_id=job_id,
            from_statuses=(OptimizationJobStatus.RUNNING,),
            to_status=OptimizationJobStatus.SUCCEEDED,
            result=MINIMAL_RESULT,
        )

        response = cost_client.get(f"{jobs_url(runtime_session.id)}/{job_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["error"] is None
        # The embedded result parses as the legacy inline response schema.
        parsed = RuntimeSessionOptimizationResponse.model_validate(body["result"])
        assert parsed.generated_by == "local"
        assert parsed.suggestions == []

    def test_get_failed_job_returns_user_facing_error(
        self, cost_client, db_session, runtime_session
    ) -> None:
        job_id = cost_client.post(jobs_url(runtime_session.id), json={}).json()[
            "job_id"
        ]
        assert crud_optimization_job.transition(
            db_session,
            job_id=job_id,
            from_statuses=(OptimizationJobStatus.PENDING,),
            to_status=OptimizationJobStatus.FAILED,
            error=USER_FACING_JOB_ERROR,
        )

        response = cost_client.get(f"{jobs_url(runtime_session.id)}/{job_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["result"] is None
        assert body["error"] == USER_FACING_JOB_ERROR

    def test_cross_account_job_404s(
        self, cost_client, db_session, runtime_session
    ) -> None:
        """A job id from another account must be indistinguishable from absent."""
        other_account = crud_account.create(
            db_session,
            obj_in={"organization_name": "Other Org", "is_active": True},
        )
        other_session = make_runtime_session(
            db_session, other_account.id, "workspace-other-account"
        )
        other_job = crud_optimization_job.create_pending(
            db_session,
            account_id=other_account.id,
            runtime_session_id=other_session.id,
        )

        response = cost_client.get(f"{jobs_url(other_session.id)}/{other_job.id}")

        assert response.status_code == 404

    def test_malformed_job_id_404s(self, cost_client, runtime_session) -> None:
        """Corrupted client storage must poll into a 404, not a 500."""
        response = cost_client.get(f"{jobs_url(runtime_session.id)}/not-a-uuid")
        assert response.status_code == 404

    def test_wrong_session_job_404s(
        self, cost_client, db_session, test_user, runtime_session
    ) -> None:
        """A real job id polled under a different session id is a 404."""
        job_id = cost_client.post(jobs_url(runtime_session.id), json={}).json()[
            "job_id"
        ]
        other_session = make_runtime_session(
            db_session, test_user.account_id, "workspace-wrong-session"
        )

        response = cost_client.get(f"{jobs_url(other_session.id)}/{job_id}")

        assert response.status_code == 404


class TestRecoverySweep:
    def test_sweep_fails_stale_pending_and_stale_running(
        self, db_session, test_user
    ) -> None:
        account_id = test_user.account_id
        stale_pending_session = make_runtime_session(
            db_session, account_id, "workspace-stale-pending"
        )
        stale_running_session = make_runtime_session(
            db_session, account_id, "workspace-stale-running"
        )
        fresh_session = make_runtime_session(db_session, account_id, "workspace-fresh")

        stale_pending = crud_optimization_job.create_pending(
            db_session,
            account_id=account_id,
            runtime_session_id=stale_pending_session.id,
        )
        stale_running = crud_optimization_job.create_pending(
            db_session,
            account_id=account_id,
            runtime_session_id=stale_running_session.id,
        )
        fresh_pending = crud_optimization_job.create_pending(
            db_session,
            account_id=account_id,
            runtime_session_id=fresh_session.id,
        )
        assert crud_optimization_job.transition(
            db_session,
            job_id=stale_running.id,
            from_statuses=(OptimizationJobStatus.PENDING,),
            to_status=OptimizationJobStatus.RUNNING,
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        db_session.query(OptimizationJob).filter(
            OptimizationJob.id == stale_pending.id
        ).update({"created_at": now - timedelta(minutes=20)})
        db_session.query(OptimizationJob).filter(
            OptimizationJob.id == stale_running.id
        ).update({"heartbeat_at": now - timedelta(minutes=10)})
        db_session.commit()

        counts = run_optimization_job_sweep(db_session)

        assert counts["failed"] == 2
        db_session.expire_all()
        for job_id in (stale_pending.id, stale_running.id):
            job = crud_optimization_job.get(db_session, id=job_id)
            assert job.status == OptimizationJobStatus.FAILED
            assert job.error == USER_FACING_JOB_ERROR
            assert job.finished_at is not None
        fresh = crud_optimization_job.get(db_session, id=fresh_pending.id)
        assert fresh.status == OptimizationJobStatus.PENDING

    def test_sweep_prunes_finished_jobs_past_retention(
        self, db_session, test_user
    ) -> None:
        session = make_runtime_session(
            db_session, test_user.account_id, "workspace-prune"
        )
        old_job = crud_optimization_job.create_pending(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        assert crud_optimization_job.transition(
            db_session,
            job_id=old_job.id,
            from_statuses=(OptimizationJobStatus.PENDING,),
            to_status=OptimizationJobStatus.FAILED,
            error=USER_FACING_JOB_ERROR,
        )
        old_job_id = old_job.id
        now = datetime.now(UTC).replace(tzinfo=None)
        db_session.query(OptimizationJob).filter(
            OptimizationJob.id == old_job_id
        ).update({"finished_at": now - timedelta(days=15)})
        db_session.commit()

        counts = run_optimization_job_sweep(db_session)

        assert counts["pruned"] == 1
        db_session.expire_all()
        assert crud_optimization_job.get(db_session, id=old_job_id) is None


class TestWorker:
    def test_worker_failure_stores_user_facing_error_only(
        self, db_session, test_user, runtime_session
    ) -> None:
        job = crud_optimization_job.create_pending(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime_session.id,
        )
        with patch.object(
            SessionOptimizationService,
            "get_account_session_optimization_suggestions",
            side_effect=RuntimeError("gateway exploded: secret detail"),
        ):
            _execute_job(
                job.id,
                account_id=test_user.account_id,
                user_id=test_user.id,
                runtime_session_id=str(runtime_session.id),
                request=RuntimeSessionOptimizationRequest(),
                db=db_session,
            )

        db_session.expire_all()
        refreshed = crud_optimization_job.get(db_session, id=job.id)
        assert refreshed.status == OptimizationJobStatus.FAILED
        # The row carries ONLY the stable user-facing copy; the diagnostic
        # detail must never reach the console.
        assert refreshed.error == USER_FACING_JOB_ERROR
        assert "secret detail" not in (refreshed.error or "")
        assert refreshed.result is None
        assert refreshed.finished_at is not None

    def test_worker_success_stores_result(
        self, db_session, test_user, runtime_session
    ) -> None:
        job = crud_optimization_job.create_pending(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=runtime_session.id,
        )
        response = RuntimeSessionOptimizationResponse(
            generated_by="local", suggestions=[]
        )
        with patch.object(
            SessionOptimizationService,
            "get_account_session_optimization_suggestions",
            return_value=response,
        ):
            _execute_job(
                job.id,
                account_id=test_user.account_id,
                user_id=test_user.id,
                runtime_session_id=str(runtime_session.id),
                request=RuntimeSessionOptimizationRequest(),
                db=db_session,
            )

        db_session.expire_all()
        refreshed = crud_optimization_job.get(db_session, id=job.id)
        assert refreshed.status == OptimizationJobStatus.SUCCEEDED
        assert refreshed.error is None
        assert refreshed.started_at is not None
        assert refreshed.finished_at is not None
        parsed = RuntimeSessionOptimizationResponse.model_validate(refreshed.result)
        assert parsed.generated_by == "local"


class TestLegacyInlineEndpoint:
    def test_legacy_sync_endpoint_still_returns_inline_results(
        self, cost_client, db_session, runtime_session
    ) -> None:
        """CRITICAL regression: legacy POST stays inline, never submit-then-poll."""
        response = cost_client.post(
            f"/api/v1/billing/cost/runtime-sessions/{runtime_session.id}/optimizations",
            json={},
        )

        assert response.status_code == 200
        body = response.json()
        # Inline result body, not a job acknowledgement.
        assert "job_id" not in body
        assert body["generated_by"] in ("local", "model")
        assert isinstance(body["suggestions"], list)
        # And no background job row was created as a side effect.
        assert (
            db_session.query(OptimizationJob)
            .filter(OptimizationJob.runtime_session_id == str(runtime_session.id))
            .count()
            == 0
        )
