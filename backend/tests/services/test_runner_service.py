"""Tests for self-hosted runner pool resolution and queue timeout."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.models.crud.flow_runner import crud_flow_runner, runner_matches_pool
from preloop.services.runner_service import (
    AUTO_RUNNER_POOL,
    DEFAULT_QUEUE_TIMEOUT,
    emit_runner_updated,
    hash_runner_token,
    lease_job,
    mark_queued_or_fail,
    persistable_job_payload,
    resolve_runner_pool,
    runner_console_payload,
)


@pytest.fixture(autouse=True)
def runtime_admission_allowed(monkeypatch):
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.admit_runtime_start",
        lambda *args, **kwargs: True,
    )


def test_resolve_runner_pool_prefers_trigger_override() -> None:
    flow = SimpleNamespace(runner_pool="default")
    pool = resolve_runner_pool(
        flow,
        {"trigger_event_data": {"_runner": "office", "ref": "main"}},
    )
    assert pool == "office"


def test_resolve_runner_pool_reads_nested_payload() -> None:
    flow = SimpleNamespace(runner_pool="default")
    pool = resolve_runner_pool(
        flow,
        {"trigger_event_data": {"payload": {"_runner": "gpu"}}},
    )
    assert pool == "gpu"


def test_resolve_runner_pool_falls_back_to_flow() -> None:
    flow = SimpleNamespace(runner_pool="nightly")
    assert resolve_runner_pool(flow, {"trigger_event_data": {}}) == "nightly"
    assert resolve_runner_pool(SimpleNamespace(runner_pool=None), {}) is None


def test_resolve_runner_pool_server_sentinel_uses_hosted() -> None:
    flow = SimpleNamespace(runner_pool="server", account_id=uuid4())
    assert resolve_runner_pool(flow, {}) is None
    assert (
        resolve_runner_pool(
            SimpleNamespace(runner_pool="nightly"),
            {"trigger_event_data": {"_runner": "server"}},
        )
        is None
    )


def test_resolve_runner_pool_uses_account_default() -> None:
    flow = SimpleNamespace(
        runner_pool=None,
        account_id=uuid4(),
        account=SimpleNamespace(default_runner_pool="office-mac"),
    )
    assert resolve_runner_pool(flow, {}) == "office-mac"


def test_resolve_runner_pool_account_server_beats_online_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    flow = SimpleNamespace(
        runner_pool=None,
        account_id=account_id,
        account=SimpleNamespace(default_runner_pool="server"),
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [SimpleNamespace(id=uuid4())],
    )
    assert resolve_runner_pool(flow, {}, db=MagicMock()) is None


def test_resolve_runner_pool_auto_when_online_private_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    flow = SimpleNamespace(
        runner_pool=None,
        account_id=account_id,
        account=SimpleNamespace(default_runner_pool=None),
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [
            SimpleNamespace(id=uuid4(), status="online", pending_job=None)
        ],
    )
    assert resolve_runner_pool(flow, {}, db=MagicMock()) == AUTO_RUNNER_POOL


def test_resolve_runner_pool_hosted_when_only_private_runner_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    flow = SimpleNamespace(
        runner_pool=None,
        account_id=account_id,
        account=SimpleNamespace(default_runner_pool=None),
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [
            SimpleNamespace(id=uuid4(), status="busy", pending_job={"id": "job"})
        ],
    )
    assert resolve_runner_pool(flow, {}, db=MagicMock()) is None


def test_resolve_runner_pool_hosted_when_no_online_private_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = SimpleNamespace(
        runner_pool=None,
        account_id=uuid4(),
        account=SimpleNamespace(default_runner_pool=None),
    )
    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda db, **kwargs: [])
    assert resolve_runner_pool(flow, {}, db=MagicMock()) is None


def test_resolve_runner_pool_flow_beats_account_default() -> None:
    flow = SimpleNamespace(
        runner_pool="gpu",
        account_id=uuid4(),
        account=SimpleNamespace(default_runner_pool="office-mac"),
    )
    assert resolve_runner_pool(flow, {}) == "gpu"


def test_resolve_runner_pool_loads_account_default_from_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    flow = SimpleNamespace(runner_pool=None, account_id=account_id)
    monkeypatch.setattr(
        "preloop.services.runner_service.crud_account.get",
        lambda db, id: SimpleNamespace(default_runner_pool="nightly"),
    )
    assert resolve_runner_pool(flow, {}, db=MagicMock()) == "nightly"


def test_mark_queued_or_fail_times_out() -> None:
    now = datetime.now(timezone.utc)
    assert mark_queued_or_fail(queued_since=now) == "PENDING"
    assert (
        mark_queued_or_fail(
            queued_since=now - DEFAULT_QUEUE_TIMEOUT - timedelta(seconds=1)
        )
        == "FAILED"
    )


def test_hash_runner_token_is_stable() -> None:
    assert hash_runner_token("abc") == hash_runner_token("abc")
    assert hash_runner_token("abc") != hash_runner_token("def")


def test_runner_matches_pool_id_name_or_label() -> None:
    runner_id = uuid4()
    row = SimpleNamespace(id=runner_id, name="Office Mac", labels=["local", "gpu"])
    assert runner_matches_pool(row, str(runner_id))
    assert runner_matches_pool(row, "office mac")
    assert runner_matches_pool(row, "GPU")
    assert runner_matches_pool(row, "auto")
    assert runner_matches_pool(row, "")
    assert not runner_matches_pool(row, "server")
    assert not runner_matches_pool(row, "missing")


def test_crud_runner_counts_and_heartbeat_methods_are_reachable() -> None:
    assert hasattr(crud_flow_runner, "counts_for_instance")
    assert hasattr(crud_flow_runner, "counts_for_account")
    assert hasattr(crud_flow_runner, "touch_heartbeat")


def test_persistable_job_payload_strips_account_api_token() -> None:
    payload = {
        "execution_id": "exec-1",
        "prompt": "do work",
        "account_api_token": "live-secret",
    }
    stored = persistable_job_payload(payload)
    assert "account_api_token" not in stored
    assert stored["prompt"] == "do work"
    assert payload["account_api_token"] == "live-secret"


def test_lease_job_claims_row_and_does_not_persist_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SimpleNamespace(
        id=uuid4(),
        status="online",
        pending_job=None,
        current_execution_id=None,
        halt_requested=False,
        reported_status=None,
    )
    claimed: list = []

    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [runner],
    )

    def _claim_idle(db, *, runner_id):
        claimed.append(runner_id)
        return runner

    monkeypatch.setattr(crud_flow_runner, "claim_idle", _claim_idle)

    db = MagicMock()
    execution_id = uuid4()
    result = lease_job(
        db,
        account_id=uuid4(),
        pool="local",
        execution_id=execution_id,
        payload={
            "execution_id": str(execution_id),
            "account_api_token": "live-secret",
            "prompt": "do work",
        },
    )
    assert result is runner
    assert claimed == [runner.id]
    assert runner.pending_job is not None
    assert "account_api_token" not in runner.pending_job
    assert runner.pending_job["prompt"] == "do work"
    assert runner.status == "busy"
    assert runner.current_execution_id == execution_id
    db.commit.assert_called()


def test_lease_job_skips_locked_runner_and_claims_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = SimpleNamespace(id=uuid4(), status="online", pending_job=None)
    next_runner = SimpleNamespace(
        id=uuid4(),
        status="online",
        pending_job=None,
        current_execution_id=None,
        halt_requested=False,
        reported_status=None,
    )

    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [locked, next_runner],
    )

    def _claim_idle(db, *, runner_id):
        if runner_id == locked.id:
            return None
        return next_runner

    monkeypatch.setattr(crud_flow_runner, "claim_idle", _claim_idle)

    result = lease_job(
        MagicMock(),
        account_id=uuid4(),
        pool="local",
        execution_id=uuid4(),
        payload={"prompt": "do work"},
    )
    assert result is next_runner
    assert next_runner.status == "busy"


def test_lease_job_skips_runner_missing_host_exec_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = SimpleNamespace(
        id=uuid4(),
        status="online",
        pending_job=None,
        capabilities={"host_exec_profiles": []},
    )
    matching = SimpleNamespace(
        id=uuid4(),
        status="online",
        pending_job=None,
        current_execution_id=None,
        halt_requested=False,
        reported_status=None,
        capabilities={
            "host_exec_profiles": [
                {"name": "cursor-ask", "capabilities": ["host_exec", "cursor_cli"]}
            ]
        },
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [missing, matching],
    )

    def _claim_idle(db, *, runner_id):
        if runner_id == matching.id:
            return matching
        raise AssertionError("must not claim a runner missing the advertised profile")

    monkeypatch.setattr(crud_flow_runner, "claim_idle", _claim_idle)
    result = lease_job(
        MagicMock(),
        account_id=uuid4(),
        pool="local",
        execution_id=uuid4(),
        payload={"host_exec_profile": "cursor-ask", "prompt": "ask"},
    )
    assert result is matching


def test_lease_job_queues_when_no_runner_advertises_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SimpleNamespace(
        id=uuid4(),
        status="online",
        pending_job=None,
        capabilities={"host_exec_profiles": [{"name": "other"}]},
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [runner],
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "claim_idle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not claim unmatched host-exec runner")
        ),
    )
    result = lease_job(
        MagicMock(),
        account_id=uuid4(),
        pool="local",
        execution_id=uuid4(),
        payload={"host_exec_profile": "cursor-ask"},
    )
    assert result is None
    runner_id = uuid4()
    user_id = uuid4()
    runner = SimpleNamespace(
        id=runner_id,
        name="box",
        hostname="mac.local",
        os="darwin",
        arch="arm64",
        labels=["local"],
        status="online",
        last_heartbeat=datetime(2026, 9, 3, tzinfo=timezone.utc),
        current_execution_id=None,
        registered_by_user_id=user_id,
        token="runner-token",
        token_hash="hashed",
    )
    payload = runner_console_payload(runner)
    assert payload["id"] == str(runner_id)
    assert payload["status"] == "online"
    assert payload["labels"] == ["local"]
    assert payload["registered_by_email"] is None
    assert payload["capabilities"] == {}
    assert "token" not in payload
    assert "token_hash" not in payload


def test_emit_runner_updated_includes_registered_by_email(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        "preloop.services.runner_service.emit_account_event",
        lambda event: captured.update(event),
    )
    monkeypatch.setattr(
        "preloop.services.runner_service.crud_user.get",
        lambda db, id: SimpleNamespace(email="ops@example.com"),
    )
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        name="box",
        hostname="mac.local",
        os="darwin",
        arch="arm64",
        labels=["local"],
        status="online",
        last_heartbeat=datetime(2026, 9, 3, tzinfo=timezone.utc),
        current_execution_id=None,
        registered_by_user_id=uuid4(),
    )
    emit_runner_updated(runner, db=MagicMock())
    assert captured["payload"]["registered_by_email"] == "ops@example.com"


def test_emit_runner_updated_publishes_runners_topic(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        "preloop.services.runner_service.emit_account_event",
        lambda event: captured.update(event),
    )
    runner = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        name="box",
        hostname="mac.local",
        os="darwin",
        arch="arm64",
        labels=["local"],
        status="online",
        last_heartbeat=datetime(2026, 9, 3, tzinfo=timezone.utc),
        current_execution_id=None,
        registered_by_user_id=None,
    )
    emit_runner_updated(runner)
    assert captured["topic"] == "runners"
    assert captured["type"] == "runner_updated"
    assert captured["payload"]["name"] == "box"
    assert captured["payload"]["status"] == "online"
