"""Status must survive restarts and refuse unauthenticated fleet reads."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints.health import router


def test_plugin_status_requires_operator_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary/anonymous request never gets fleet operational data."""
    from pydantic import SecretStr
    from preloop.config import settings

    monkeypatch.setattr(
        settings, "plugin_status_operator_token", SecretStr("example-operator-token")
    )
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


@pytest.mark.asyncio
async def test_reader_bounds_unavailable_nats_and_closes_client() -> None:
    """A stalled handshake must not strand an authenticated health request."""
    import asyncio
    from types import SimpleNamespace
    from preloop.sync import plugin_status

    async def stalled(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    client = SimpleNamespace(connect=stalled, close=AsyncMock())
    with (
        patch("nats.NATS", return_value=client),
        patch.object(plugin_status, "READ_TIMEOUT_SECONDS", 0.01),
    ):
        with pytest.raises(TimeoutError):
            await plugin_status.read_status()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_failure_does_not_prevent_work() -> None:
    """A broker/KV status failure is visible without taking processing down."""
    import asyncio
    from types import SimpleNamespace
    from preloop.sync import plugin_status

    started = asyncio.Event()

    async def work() -> None:
        started.set()
        await asyncio.sleep(0.02)

    manager = SimpleNamespace(proprietary_enabled=True)
    client = SimpleNamespace(
        connect=AsyncMock(side_effect=OSError("unavailable")), close=AsyncMock()
    )
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=manager),
        patch("nats.NATS", return_value=client),
    ):
        await plugin_status.run_with_status(work(), "worker")
    assert started.is_set()
    client.close.assert_awaited()


def test_intentional_noop_advances_completion() -> None:
    """An installed handler's explicit no-work result satisfies its schedule."""
    from preloop.sync import plugin_status, tasks

    manager = AsyncMock()
    manager.get_service = lambda _: (
        lambda db, account_id=None: {"skipped": "stripe_not_configured"}
    )
    with (
        patch("preloop.plugins.base.get_plugin_manager", return_value=manager),
        patch.object(tasks, "get_db_session", return_value=iter([MagicMock()])),
        patch.object(plugin_status, "_runs", {}),
    ):
        assert tasks.reconcile_stripe_subscriptions() == {
            "skipped": "stripe_not_configured"
        }
        assert "reconcile_stripe_subscriptions" in plugin_status.last_runs()


def test_operator_token_loads_from_native_env_file(tmp_path: "Path") -> None:
    """Native installs load the same secret setting as compose containers."""
    from preloop.config import Settings, settings

    config = tmp_path / ".env"
    config.write_text("PLUGIN_STATUS_OPERATOR_TOKEN=example-native-token\n")
    setting = Settings(
        database=settings.database,
        security=settings.security,
        server=settings.server,
        _env_file=config,
    ).plugin_status_operator_token
    assert setting.get_secret_value() == "example-native-token"
    assert "example-native-token" not in repr(setting)


@pytest.mark.asyncio
async def test_process_records_expire_without_expiring_job_evidence() -> None:
    """The writer configures separate bounded stores with different lifetimes."""
    import asyncio
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator
    from types import SimpleNamespace
    from nats.js.errors import BucketNotFoundError
    from preloop.sync import plugin_status

    written = asyncio.Event()
    js = SimpleNamespace(
        key_value=AsyncMock(side_effect=BucketNotFoundError),
        create_key_value=AsyncMock(return_value=object()),
    )

    @asynccontextmanager
    async def connection() -> AsyncIterator[SimpleNamespace]:
        yield js

    async def report(*args: object, **kwargs: object) -> None:
        written.set()

    with (
        patch.object(plugin_status, "_connection", connection),
        patch.object(plugin_status, "report", report),
    ):
        task = asyncio.create_task(
            plugin_status._heartbeat("example-worker", "worker", [])
        )
        await asyncio.wait_for(written.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    configurations = {
        c.kwargs["config"].bucket: c.kwargs["config"]
        for c in js.create_key_value.call_args_list
    }
    assert (
        configurations[plugin_status.PROCESS_BUCKET].ttl == plugin_status.STALE_SECONDS
    )
    assert configurations[plugin_status.BUCKET].ttl == 0
    assert all(config.max_bytes > 0 for config in configurations.values())


@pytest.mark.asyncio
async def test_missing_process_bucket_is_an_empty_fleet() -> None:
    """Before the first heartbeat, a reachable broker has an empty fleet."""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from nats.js.errors import BucketNotFoundError
    from preloop.sync import plugin_status

    js = SimpleNamespace(key_value=AsyncMock(side_effect=BucketNotFoundError))

    @asynccontextmanager
    async def connection() -> AsyncIterator[SimpleNamespace]:
        yield js

    with patch.object(plugin_status, "_connection", connection):
        result = await plugin_status.read_status()
    assert result["workers"] == []
    assert result["timestamp"] > 0
