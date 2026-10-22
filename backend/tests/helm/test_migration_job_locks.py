"""The pre-upgrade migration hook must be safe against a live deployment.

The hook runs while the previous API pods and sync workers keep serving, so
the rendered Job has to use the wrapper that commits one transaction per
revision, hold a short per-session lock timeout, and retry a revision that
loses a lock race. A chart that renders bare `alembic upgrade head` again
would silently restore the failure mode.
"""

from __future__ import annotations

import yaml

from .chart_helpers import helm_template, load_values, resolve_values_path


def _job(overrides: list[str] | None = None) -> dict:
    rendered = helm_template("templates/migration-job.yaml", overrides)
    return yaml.safe_load(rendered)


def _container(job: dict) -> dict:
    return job["spec"]["template"]["spec"]["containers"][0]


def _env(container: dict) -> dict[str, str]:
    return {
        item["name"]: item.get("value")
        for item in container.get("env", [])
        if "value" in item
    }


def test_defaults_are_short_and_bounded() -> None:
    values = load_values()
    assert resolve_values_path(values, "migrationJob.lockTimeout") == "5s"
    assert resolve_values_path(values, "migrationJob.maxAttempts") == 20
    assert resolve_values_path(values, "migrationJob.retryMinSeconds") == 3
    assert resolve_values_path(values, "migrationJob.retryMaxSeconds") == 15


def test_cluster_wide_timeouts_are_untouched() -> None:
    """The short timeout belongs to the migration session, not the cluster."""
    values = load_values()
    resilience = resolve_values_path(values, "database.cnpg.resilience")
    assert resilience["lock_timeout"] == "60000"
    assert resilience["statement_timeout"] == "300000"


def test_the_job_runs_the_wrapper_not_bare_alembic() -> None:
    container = _container(_job())
    args = " ".join(container["args"])
    assert "python -m preloop.models.migrate" in args
    assert "alembic upgrade head" not in args


def test_the_job_carries_the_lock_and_retry_settings() -> None:
    env = _env(_container(_job()))
    assert env["PRELOOP_MIGRATION_LOCK_TIMEOUT"] == "5s"
    assert env["PRELOOP_MIGRATION_MAX_ATTEMPTS"] == "20"
    assert env["PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS"] == "60"
    assert env["PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS"] == "120"
    assert env["PRELOOP_MIGRATION_RETRY_MIN_SECONDS"] == "3"
    assert env["PRELOOP_MIGRATION_RETRY_MAX_SECONDS"] == "15"


def test_an_operator_can_retune_without_a_new_image() -> None:
    env = _env(
        _container(
            _job(
                [
                    "migrationJob.lockTimeout=2s",
                    "migrationJob.maxAttempts=40",
                    "migrationJob.retryMinSeconds=1",
                    "migrationJob.retryMaxSeconds=5",
                ]
            )
        )
    )
    assert env["PRELOOP_MIGRATION_LOCK_TIMEOUT"] == "2s"
    assert env["PRELOOP_MIGRATION_MAX_ATTEMPTS"] == "40"
    assert env["PRELOOP_MIGRATION_RETRY_MIN_SECONDS"] == "1"
    assert env["PRELOOP_MIGRATION_RETRY_MAX_SECONDS"] == "5"


def test_the_job_keeps_its_pre_upgrade_hook_and_bounds_pod_retries() -> None:
    job = _job()
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-upgrade"
    # The container retries lock contention itself, so the pod-level budget
    # only has to cover a crash.
    assert job["spec"]["backoffLimit"] == 2
    assert _job(["migrationJob.backoffLimit=5"])["spec"]["backoffLimit"] == 5


def test_concurrent_index_budgets_can_be_tuned_without_relaxing_blocking_ddl() -> None:
    env = _env(
        _container(
            _job(
                [
                    "migrationJob.concurrentLockTimeoutSeconds=45",
                    "migrationJob.concurrentStatementTimeoutSeconds=180",
                ]
            )
        )
    )
    assert env["PRELOOP_MIGRATION_LOCK_TIMEOUT"] == "5s"
    assert env["PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS"] == "45"
    assert env["PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS"] == "180"


def test_migration_job_uses_the_same_secret_keys_as_the_application() -> None:
    """Credential backfills must not encrypt with a different process default."""
    for overrides in ([], ["security.encryptionKey=synthetic-render-only-value"]):
        migration = _container(_job(overrides))
        application = yaml.safe_load(
            helm_template("templates/api-deployment.yaml", overrides)
        )
        app_container = application["spec"]["template"]["spec"]["containers"][0]
        migration_env = {item["name"]: item for item in migration["env"]}
        application_env = {item["name"]: item for item in app_container["env"]}
        assert migration_env["SECRET_KEY"] == application_env["SECRET_KEY"]
        if overrides:
            assert (
                migration_env["SECURITY__ENCRYPTION_KEY"]
                == application_env["SECURITY__ENCRYPTION_KEY"]
            )
        else:
            assert "SECURITY__ENCRYPTION_KEY" not in migration_env
