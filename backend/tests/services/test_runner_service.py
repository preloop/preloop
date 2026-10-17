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
    derive_execution_runner,
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
            SimpleNamespace(
                id=uuid4(),
                status="online",
                flow_execution_consent="always",
                free_slots=2,
                capabilities={"docker_available": True},
                accept_untagged=True,
            )
        ],
    )
    assert resolve_runner_pool(flow, {}, db=MagicMock()) == AUTO_RUNNER_POOL


def test_resolve_runner_pool_auto_when_private_runner_has_a_second_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner already working still has capacity when a slot is free."""
    flow = SimpleNamespace(
        runner_pool=None,
        account_id=uuid4(),
        account=SimpleNamespace(default_runner_pool=None),
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [
            SimpleNamespace(
                id=uuid4(),
                status="busy",
                free_slots=1,
                capabilities={"docker_available": True},
                accept_untagged=True,
            )
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
        lambda db, **kwargs: [SimpleNamespace(id=uuid4(), status="busy", free_slots=0)],
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
        flow_execution_consent="always",
        free_slots=2,
        capabilities={"docker_available": True},
        accept_untagged=True,
    )
    claimed: list = []
    created: list = []

    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [runner],
    )

    def _claim_free_slot(db, *, runner_id):
        claimed.append(runner_id)
        return runner

    def _create_assignment(db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(reported_status=None, **kwargs)

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", _claim_free_slot)
    monkeypatch.setattr(crud_flow_runner, "create_assignment", _create_assignment)

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
    assert len(created) == 1
    assert created[0]["execution_id"] == execution_id
    assert "account_api_token" not in created[0]["pending_job"]
    assert created[0]["pending_job"]["prompt"] == "do work"
    db.commit.assert_called()


def test_lease_job_skips_locked_runner_and_claims_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = SimpleNamespace(
        id=uuid4(),
        status="online",
        flow_execution_consent="always",
        free_slots=1,
        capabilities={"docker_available": True},
        accept_untagged=True,
    )
    next_runner = SimpleNamespace(
        id=uuid4(),
        status="online",
        flow_execution_consent="always",
        free_slots=1,
        capabilities={"docker_available": True},
        accept_untagged=True,
    )

    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [locked, next_runner],
    )

    def _claim_free_slot(db, *, runner_id):
        if runner_id == locked.id:
            return None
        return next_runner

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", _claim_free_slot)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kwargs: SimpleNamespace(reported_status=None, **kwargs),
    )

    result = lease_job(
        MagicMock(),
        account_id=uuid4(),
        pool="local",
        execution_id=uuid4(),
        payload={"prompt": "do work"},
    )
    assert result is next_runner


def test_lease_job_skips_runner_missing_host_exec_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = SimpleNamespace(
        id=uuid4(),
        status="online",
        flow_execution_consent="always",
        free_slots=1,
        capabilities={"host_exec_profiles": []},
    )
    matching = SimpleNamespace(
        id=uuid4(),
        status="online",
        flow_execution_consent="always",
        free_slots=1,
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

    def _claim_free_slot(db, *, runner_id):
        if runner_id == matching.id:
            return matching
        raise AssertionError("must not claim a runner missing the advertised profile")

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", _claim_free_slot)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kwargs: SimpleNamespace(reported_status=None, **kwargs),
    )
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
        flow_execution_consent="always",
        free_slots=1,
        capabilities={"host_exec_profiles": [{"name": "other"}]},
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [runner],
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "claim_free_slot",
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
        flow_execution_consent="always",
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
        flow_execution_consent="always",
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
        flow_execution_consent="always",
        last_heartbeat=datetime(2026, 9, 3, tzinfo=timezone.utc),
        current_execution_id=None,
        registered_by_user_id=None,
    )
    emit_runner_updated(runner)
    assert captured["topic"] == "runners"
    assert captured["type"] == "runner_updated"
    assert captured["payload"]["name"] == "box"
    assert captured["payload"]["status"] == "online"


def test_execution_without_assignment_does_not_claim_hosted() -> None:
    assert derive_execution_runner() == {
        "kind": "unknown",
        "id": None,
        "name": "Not recorded",
        "pool": None,
    }


def test_execution_runner_preserves_known_destinations() -> None:
    assert (
        derive_execution_runner(agent_session_reference="agent-example")["kind"]
        == "hosted"
    )
    queued = derive_execution_runner(
        agent_session_reference="runner:queued:office:example"
    )
    assert queued["kind"] == "private"
    assert queued["pool"] == "office"


def test_lease_job_matches_copilot_lease_to_copilot_profile_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same_name_cursor = SimpleNamespace(
        id=uuid4(),
        status="online",
        flow_execution_consent="always",
        free_slots=1,
        capabilities={
            "host_exec_profiles": [
                {"name": "seat", "capabilities": ["host_exec", "cursor_cli"]}
            ]
        },
    )
    copilot = SimpleNamespace(
        id=uuid4(),
        status="online",
        flow_execution_consent="always",
        free_slots=1,
        capabilities={
            "host_exec_profiles": [
                {
                    "name": "seat",
                    "capabilities": ["host_exec", "copilot_cli"],
                    "models": ["team-default"],
                }
            ]
        },
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kwargs: [same_name_cursor, copilot],
    )

    def _claim_free_slot(db, *, runner_id):
        if runner_id == copilot.id:
            return copilot
        raise AssertionError("must not claim a Cursor profile for a Copilot lease")

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", _claim_free_slot)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kwargs: SimpleNamespace(reported_status=None, **kwargs),
    )
    result = lease_job(
        MagicMock(),
        account_id=uuid4(),
        pool="local",
        execution_id=uuid4(),
        payload={
            "agent_type": "copilot",
            "host_exec_profile": "seat",
            "model_identifier": "team-default",
            "prompt": "review",
        },
    )
    assert result is copilot


@pytest.mark.parametrize(
    "docker,native,paused,untagged,pool,expected",
    [
        (False, False, False, False, "docker", False),
        (False, False, False, True, "local", False),
        (True, False, False, False, "docker", True),
        (True, False, False, False, "auto", False),
        (True, False, False, True, "auto", True),
        (False, True, False, False, "auto", True),
        (True, True, True, True, "cursor_cli", False),
    ],
)
def test_runner_accepts_only_usable_and_authorized_work(
    docker: bool,
    native: bool,
    paused: bool,
    untagged: bool,
    pool: str,
    expected: bool,
) -> None:
    from preloop.services.runner_service import runner_accepts_job

    runner = SimpleNamespace(
        capabilities={"docker_available": docker},
        paused=paused,
        accept_untagged=untagged,
    )
    payload = (
        {"host_exec_profile": "cursor"} if native else {"docker_image": "example/agent"}
    )
    assert runner_accepts_job(runner, pool, payload) is expected


def test_runner_capability_tags_follow_current_inventory() -> None:
    from preloop.models.crud.flow_runner import runner_labels

    runner = SimpleNamespace(
        id=uuid4(),
        name="workstation",
        labels=["office", "Docker", "devin_cli"],
        capabilities={"docker_available": True},
        accept_untagged=True,
        harness_inventory={
            "entries": [
                {
                    "harness": "cursor_cli",
                    "enabled": True,
                    "login_state": "signed_in",
                    "support_level": "flows_and_sessions",
                },
                {
                    "harness": "copilot_cli",
                    "enabled": False,
                    "login_state": "signed_in",
                    "support_level": "flows_and_sessions",
                },
                {
                    "harness": "codex_cli",
                    "enabled": True,
                    "login_state": "signed_out",
                    "support_level": "flows_only",
                },
            ]
        },
    )
    assert runner_labels(runner) == ["cursor_cli", "docker", "office"]
    assert runner_matches_pool(runner, "cursor_cli")
    assert not runner_matches_pool(runner, "copilot_cli")
    runner.capabilities["docker_available"] = False
    assert not runner_matches_pool(runner, "docker")


@pytest.mark.parametrize("docker", [True, False])
def test_explicit_container_trigger_requires_docker(
    monkeypatch: pytest.MonkeyPatch, docker: bool
) -> None:
    from preloop.services.runner_service import (
        RunnerCapabilityError,
        validate_runner_capabilities,
    )

    flow = SimpleNamespace(
        account_id=uuid4(),
        runner_pool="workstation",
        agent_type="codex",
        agent_config={},
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda *args, **kwargs: [
            SimpleNamespace(capabilities={"docker_available": docker})
        ],
    )
    if docker:
        validate_runner_capabilities(MagicMock(), flow, {})
    else:
        with pytest.raises(RunnerCapabilityError, match="working Docker daemon"):
            validate_runner_capabilities(MagicMock(), flow, {})


def test_native_trigger_does_not_require_docker() -> None:
    from preloop.services.runner_service import validate_runner_capabilities

    flow = SimpleNamespace(
        account_id=uuid4(),
        runner_pool="workstation",
        agent_type="cursor",
        agent_config={"host_exec_profile": "cursor"},
    )
    validate_runner_capabilities(MagicMock(), flow, {})


def test_default_runner_does_not_capture_untagged_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = SimpleNamespace(
        account_id=uuid4(), runner_pool=None, agent_type="codex", agent_config={}
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda *args, **kwargs: [
            SimpleNamespace(
                status="online",
                flow_execution_consent="always",
                free_slots=2,
                accept_untagged=False,
                capabilities={"docker_available": True},
            )
        ],
    )
    assert resolve_runner_pool(flow, {}, db=MagicMock()) is None


@pytest.mark.parametrize("pool", ["workstation", "auto"])
def test_native_publication_does_not_require_docker(
    monkeypatch: pytest.MonkeyPatch, pool: str
) -> None:
    from preloop.services.runner_service import (
        runner_accepts_job,
        validate_runner_capabilities,
    )

    flow = SimpleNamespace(
        account_id=uuid4(),
        runner_pool=pool,
        agent_type="copilot",
        agent_config={"host_exec_profile": "copilot"},
        git_clone_config={"create_pull_request": True},
    )
    runner = SimpleNamespace(
        capabilities={"docker_available": False}, paused=False, accept_untagged=False
    )
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda *args, **kwargs: [runner]
    )
    validate_runner_capabilities(MagicMock(), flow, {})
    assert runner_accepts_job(
        runner,
        pool,
        {
            "host_exec_profile": "copilot",
            "host_exec_publication": {"publication_mode": "legacy"},
        },
    )
    assert not runner_accepts_job(runner, pool, {"_publication": True})


def test_runner_labels_coerce_legacy_json_values() -> None:
    from preloop.models.crud.flow_runner import runner_labels

    runner = SimpleNamespace(
        id=uuid4(),
        name="legacy",
        labels=[1, None, "office", "Docker"],
        capabilities={},
        harness_inventory={},
    )
    assert runner_labels(runner) == ["1", "None", "office"]
    assert runner_matches_pool(runner, "1")
    assert not runner_matches_pool(runner, "docker")
