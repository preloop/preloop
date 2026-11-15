"""Status must survive restarts and refuse unauthenticated fleet reads."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints.health import router


def test_plugin_status_requires_operator_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary/anonymous request never gets fleet operational data."""
    monkeypatch.setenv("PLUGIN_STATUS_OPERATOR_TOKEN", "example-operator-token")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/health/plugins").status_code == 401
    assert (
        client.get(
            "/health/plugins", headers={"Authorization": "Bearer ordinary"}
        ).status_code
        == 401
    )
    with patch(
        "preloop.sync.plugin_status.read_status",
        new=AsyncMock(return_value={"workers": []}),
    ):
        response = client.get(
            "/health/plugins",
            headers={"Authorization": "Bearer example-operator-token"},
        )
    assert response.status_code == 200


def test_pending_deadline_survives_scheduler_restart() -> None:
    """Restart offsets cannot erase an overdue scheduled task."""
    from preloop.sync.plugin_status import expected_run

    assert expected_run(previous_due=100, next_due=500, last_success=90) == 100
    assert expected_run(previous_due=100, next_due=500, last_success=110) == 500


@pytest.mark.asyncio
async def test_failed_or_skipped_task_does_not_advance_success() -> None:
    """Normal returns from a swallowed failure must not satisfy the probe."""
    from preloop.sync import plugin_status

    @plugin_status.tracked_plugin_task
    def task() -> None:
        return None

    task()
    assert "task" not in plugin_status.last_runs()


@pytest.mark.asyncio
async def test_disabled_plugins_do_not_add_a_status_connection() -> None:
    """The existing disable flag keeps the original OSS startup behavior."""
    from preloop.config import settings
    from preloop.sync.plugin_status import run_with_status

    work = AsyncMock()
    with (
        patch.object(settings, "disable_rbac", True),
        patch(
            "nats.connect",
            new=AsyncMock(side_effect=AssertionError("unexpected NATS connection")),
        ),
    ):
        await run_with_status(work(), "worker")
    work.assert_awaited_once()


@pytest.mark.asyncio
async def test_report_retains_overdue_job_and_persists_completion() -> None:
    """The real report/read serialization preserves deadlines and run times."""
    import json
    from types import SimpleNamespace
    from preloop.sync import plugin_status

    records = {"due.ingest_provider_billing": {"expected_at": 100}}

    async def get(key: str) -> object:
        from nats.js.errors import KeyNotFoundError

        if key not in records:
            raise KeyNotFoundError
        return SimpleNamespace(value=json.dumps(records[key]).encode())

    async def put(key: str, value: bytes) -> None:
        records[key] = json.loads(value)

    kv = SimpleNamespace(get=get, put=put)
    manager = SimpleNamespace(_plugins={"billing": object()}, proprietary_enabled=True)
    job = SimpleNamespace(
        id="provider_billing_ingest_job",
        next_run_time=SimpleNamespace(timestamp=lambda: 500),
    )
    scheduler = SimpleNamespace(get_jobs=lambda: [job])
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=manager),
        patch("preloop.sync.cli.scheduler_commands.scheduler", scheduler),
        patch.object(plugin_status, "_runs", {}),
    ):
        await plugin_status.report(kv, "example-scheduler", "scheduler", [])
        assert (
            records["process.example-scheduler"]["jobs"]["ingest_provider_billing"][
                "expected_at"
            ]
            == 100
        )
        plugin_status._runs["ingest_provider_billing"] = 110
        await plugin_status.report(kv, "example-worker", "worker", [])
        await plugin_status.report(kv, "example-scheduler", "scheduler", [])
    assert records["run.ingest_provider_billing"]["last_success"] == 110
    assert records["due.ingest_provider_billing"]["expected_at"] == 500


def test_account_specific_run_cannot_satisfy_fleet_schedule() -> None:
    """A manual single-account sync is not evidence of the scheduled fleet pass."""
    from preloop.sync import plugin_status

    @plugin_status.tracked_plugin_task
    def example_task(account_id: str | None = None) -> dict[str, int]:
        return {"rows": 1}

    with patch.object(plugin_status, "_runs", {}):
        example_task(account_id="example-account")
        assert "example_task" not in plugin_status.last_runs()
        example_task()
        assert "example_task" in plugin_status.last_runs()
