"""Acceptance gaps for durable workspace recovery (preloop/preloop#386).

Each test here stands for one line of the issue's required behavior that the
landed transport did not yet prove:

* the restore log reports how old the recovered checkpoint is, so the lost
  write window is a number rather than an inference;
* a checkpoint records the base commit its unpushed work sits on;
* every git config file is redacted, not only the top-level one;
* the five-minute recovery interval actually reaches the container;
* a private continuation stays on the runner that holds its local workspace,
  waits visibly while that runner is offline, and blocks for an operator
  instead of moving hosts.
"""

import subprocess
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import pytest

from preloop.agents.checkpoint_client import capture, restore, restored_age_report
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.services.runner_service import (
    lease_job,
    runner_blocked_notice,
    runner_wait_notice,
    unwrap_agent_config,
    workspace_owner_runner_id,
)


@pytest.fixture(autouse=True)
def runtime_admission_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.admit_runtime_start",
        lambda *args, **kwargs: True,
    )


def git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "Jane Doe")
    git(path, "config", "user.email", "jane@example.com")
    return path


# --- Required behavior 3: report the age of what was recovered -------------


def test_restore_reports_the_age_of_the_checkpoint_it_recovered(tmp_path) -> None:
    source = init_repo(tmp_path / "source")
    (source / "tracked.txt").write_text("committed")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "unpublished")
    body = capture(source, max_bytes=200_000)

    metadata = restore(body, tmp_path / "restored")

    assert metadata["version"] == 1
    report = restored_age_report(metadata)
    assert report.startswith("age_seconds=")
    age = int(report.split("age_seconds=")[1].split(" ")[0])
    assert 0 <= age < 120
    assert "created_at=" in report and "unknown" not in report


def test_a_five_minute_old_checkpoint_does_not_report_as_fresh() -> None:
    report = restored_age_report({"created_at": time.time() - 300})
    assert int(report.split("age_seconds=")[1].split(" ")[0]) >= 299


@pytest.mark.parametrize("metadata", [{}, {"created_at": None}, {"created_at": "x"}])
def test_an_unreadable_creation_stamp_reports_unknown_not_zero(metadata) -> None:
    # Reporting zero would read as "nothing was lost", which is the one
    # thing an archive without a stamp cannot support.
    assert restored_age_report(metadata) == "age_seconds=unknown created_at=unknown"


# --- Required behavior 1: base/head SHA and credential exclusion ------------


def test_a_checkpoint_records_the_base_commit_its_unpushed_work_sits_on(
    tmp_path,
) -> None:
    upstream = init_repo(tmp_path / "upstream")
    (upstream / "tracked.txt").write_text("published")
    git(upstream, "add", "tracked.txt")
    git(upstream, "commit", "-m", "published")
    base = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()

    source = tmp_path / "source"
    subprocess.run(
        ["git", "clone", str(upstream), str(source)], check=True, capture_output=True
    )
    git(source, "config", "user.name", "Jane Doe")
    git(source, "config", "user.email", "jane@example.com")
    (source / "tracked.txt").write_text("never pushed")
    git(source, "commit", "-am", "unpushed")
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()

    metadata = restore(capture(source, max_bytes=2_000_000), tmp_path / "restored")

    repository = metadata["repositories"][0]
    assert repository["head_sha"] == head
    assert repository["base_sha"] == base
    assert repository["base_sha"] != repository["head_sha"]


def test_a_submodule_git_config_cannot_carry_a_clone_credential_out(tmp_path) -> None:
    source = init_repo(tmp_path / "source")
    module = source / ".git" / "modules" / "vendor"
    module.mkdir(parents=True)
    module.joinpath("config").write_text(
        '[remote "origin"]\n\turl = https://user:token@example.com/team/vendor.git\n'
    )
    worktree = source / ".git" / "worktrees" / "review"
    worktree.mkdir(parents=True)
    worktree.joinpath("config.worktree").write_text("[core]\n\tbare = false\n")
    (source / "app-config").write_text("keep me")
    (source / "src").mkdir()
    (source / "src" / "config").write_text("keep me too")

    restore(capture(source, max_bytes=2_000_000), tmp_path / "restored")

    restored = tmp_path / "restored"
    assert not (restored / ".git" / "modules" / "vendor" / "config").exists()
    assert not (restored / ".git" / "worktrees" / "review" / "config.worktree").exists()
    # Redaction is scoped to git metadata; ordinary source keeps its name.
    assert (restored / "app-config").read_text() == "keep me"
    assert (restored / "src" / "config").read_text() == "keep me too"


# --- Required behavior 3: the configured recovery interval is delivered -----


def test_the_default_recovery_interval_reaching_the_container_is_five_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.config import settings
    from preloop.services.checkpoint_runtime import checkpoint_context

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    assert settings.flow_checkpoint_interval_seconds == 300
    env = checkpoint_context(
        Mock(),
        {
            "account_id": str(uuid4()),
            "flow_id": str(uuid4()),
            "execution_id": str(uuid4()),
            "trigger_event_data": {},
        },
    )
    assert env["PRELOOP_CHECKPOINT_INTERVAL"] == "300"


# --- Required behavior 6: a private continuation stays on its owner ---------


def owned_by(execution_id, runner_id) -> SimpleNamespace:
    """A prior execution that ran on one private runner."""
    return SimpleNamespace(
        id=execution_id, runner_id=runner_id, agent_session_reference=None
    )


def rows_by_id(rows: dict):
    """One ``crud_flow_execution.get`` stand-in for several executions.

    The pin lookup and the executor read the same CRUD entry point, so a
    single-row stub would answer both questions with the same row.
    """

    def _get(db, *, id=None, **kwargs):  # noqa: A002 - matches the CRUD kwarg
        return rows.get(id)

    return _get


def persisting_payload(resume_from) -> dict:
    return {
        "resume_from": str(resume_from),
        "agent_config": {"runner": {"persist_workspace": True}},
    }


def test_a_persisted_workspace_resume_names_the_runner_that_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = uuid4()
    prior = uuid4()
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.get",
        lambda db, **kwargs: SimpleNamespace(
            id=prior, runner_id=owner, agent_session_reference=None
        ),
    )
    assert (
        workspace_owner_runner_id(MagicMock(), payload=persisting_payload(prior))
        == owner
    )


def test_an_ephemeral_private_run_is_not_pinned_to_any_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = uuid4()
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.get",
        lambda db, **kwargs: SimpleNamespace(
            id=prior, runner_id=uuid4(), agent_session_reference=None
        ),
    )
    # Without persist_workspace nothing survives on the host, so pinning
    # would only reduce the pool for no recovery benefit.
    payload = {"resume_from": str(prior), "agent_config": {"runner": {}}}
    assert workspace_owner_runner_id(MagicMock(), payload=payload) is None
    assert workspace_owner_runner_id(MagicMock(), payload={}) is None


def test_lease_refuses_an_idle_peer_when_the_workspace_lives_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = SimpleNamespace(id=uuid4(), status="online", free_slots=0)
    idle_peer = SimpleNamespace(id=uuid4(), status="online", free_slots=4)
    claimed: list = []

    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kwargs: [idle_peer, owner]
    )

    def _claim(db, *, runner_id):
        claimed.append(runner_id)
        return SimpleNamespace(id=runner_id, publication_capabilities=None)

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", _claim)
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
        payload={"prompt": "continue"},
        required_runner_id=owner.id,
    )
    assert result is None
    assert claimed == []


def test_lease_uses_the_owning_runner_once_it_has_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = SimpleNamespace(
        id=uuid4(),
        status="online",
        flow_execution_consent="always",
        free_slots=1,
        capabilities={"docker_available": True},
    )
    idle_peer = SimpleNamespace(id=uuid4(), status="online", free_slots=4)

    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kwargs: [idle_peer, owner]
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "claim_free_slot",
        lambda db, *, runner_id: SimpleNamespace(
            id=runner_id,
            flow_execution_consent="always",
            publication_capabilities=None,
            capabilities={"docker_available": True},
        ),
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kwargs: SimpleNamespace(reported_status=None, **kwargs),
    )
    monkeypatch.setattr(
        "preloop.services.runner_service.emit_runner_updated",
        lambda *args, **kwargs: None,
    )

    result = lease_job(
        MagicMock(),
        account_id=uuid4(),
        pool="local",
        execution_id=uuid4(),
        payload={"prompt": "continue"},
        required_runner_id=owner.id,
    )
    assert result is not None
    assert result.id == owner.id


def test_the_wait_and_the_block_both_name_the_owning_runner() -> None:
    runner = SimpleNamespace(name="build-box-2")
    execution = uuid4()
    waiting = runner_wait_notice(runner, execution)
    assert "build-box-2" in waiting and str(execution) in waiting
    assert "another host" in waiting
    blocked = runner_blocked_notice(runner, execution)
    assert "build-box-2" in blocked and str(execution) in blocked
    assert "does not upload private workspaces" in blocked


@pytest.mark.asyncio
async def test_a_queued_continuation_says_which_offline_runner_it_waits_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.agents.remote_runner import RemoteRunnerExecutor

    owner = uuid4()
    prior = uuid4()
    execution_id = uuid4()
    execution = SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        runner_id=None,
        agent_session_reference=None,
        start_time=datetime.now(timezone.utc),
        status="PENDING",
        error_message=None,
        end_time=None,
        resolved_input_prompt="continue",
        model_output_summary=None,
        result=None,
        trigger_event_details={"_resume": {"execution_id": str(prior)}},
    )
    leases: list = []

    def _lease(db, **kwargs):
        leases.append(kwargs.get("required_runner_id"))
        return None

    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", _lease)
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.get",
        rows_by_id({execution_id: execution, prior: owned_by(prior, owner)}),
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get_stop_request",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "get_fresh",
        lambda db, *, runner_id: SimpleNamespace(id=runner_id, name="build-box-2"),
    )

    executor = RemoteRunnerExecutor(
        "codex",
        {"runner": {"persist_workspace": True}},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
        flow=SimpleNamespace(
            agent_config={"runner": {"persist_workspace": True}},
            ai_model=None,
            git_clone_config=None,
            custom_commands=None,
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            agent_type="codex",
        ),
    )
    reference = await executor.start(
        {
            "execution_id": str(execution_id),
            "flow_id": str(execution.flow_id),
            "trigger_event_data": {"_resume": {"execution_id": str(prior)}},
        }
    )
    assert reference.startswith("runner:queued:local:")
    assert leases == [owner]
    assert "build-box-2" in (execution.error_message or "")


@pytest.mark.asyncio
async def test_the_queue_deadline_blocks_for_an_operator_and_keeps_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.agents.base import AgentStatus
    from preloop.agents.remote_runner import RemoteRunnerExecutor

    owner = uuid4()
    prior = uuid4()
    execution_id = uuid4()
    execution = SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        runner_id=None,
        agent_session_reference=f"runner:queued:local:{execution_id}",
        start_time=datetime.now(timezone.utc) - timedelta(minutes=16),
        status="PENDING",
        error_message=None,
        end_time=None,
        resolved_input_prompt="continue",
        model_output_summary=None,
        result=None,
        trigger_event_details={"_resume": {"execution_id": str(prior)}},
    )
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.get",
        rows_by_id({execution_id: execution, prior: owned_by(prior, owner)}),
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get_stop_request",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "get_fresh",
        lambda db, *, runner_id: SimpleNamespace(id=runner_id, name="build-box-2"),
    )

    def _must_not_lease(*args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("a timed out continuation must not move hosts")

    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", _must_not_lease)

    executor = RemoteRunnerExecutor(
        "codex",
        {"runner": {"persist_workspace": True}},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
        flow=SimpleNamespace(agent_config={"runner": {"persist_workspace": True}}),
    )
    status = await executor.get_status(execution.agent_session_reference)

    assert status is AgentStatus.FAILED
    assert execution.status == "FAILED"
    assert "build-box-2" in execution.error_message
    assert "does not upload private workspaces" in execution.error_message
    assert str(prior) in execution.error_message


def test_a_doubly_wrapped_config_still_pins_the_continuation() -> None:
    """Some stored flow configs nest agent_config inside agent_config.

    The lease payload builder unwraps that shape before a runner sees it. If
    this lookup did not agree, a host-bound continuation would read as free
    and an idle peer could lease work it cannot serve.
    """
    owner = uuid4()
    prior = uuid4()
    db = MagicMock()
    with patch(
        "preloop.models.crud.crud_flow_execution.get",
        rows_by_id({prior: owned_by(prior, owner)}),
    ):
        pinned = workspace_owner_runner_id(
            db,
            payload={
                "resume_from": str(prior),
                "agent_config": {
                    "agent_config": {"runner": {"persist_workspace": True}}
                },
            },
        )
    assert pinned == owner


@pytest.mark.asyncio
async def test_a_poll_pins_from_the_payload_the_runner_will_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row alone can understate the pin; the delivered payload cannot."""
    from preloop.agents.remote_runner import RemoteRunnerExecutor

    owner = uuid4()
    prior = uuid4()
    execution_id = uuid4()
    wrapped = {"agent_config": {"runner": {"persist_workspace": True}}}
    execution = SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        runner_id=None,
        agent_session_reference=f"runner:queued:local:{execution_id}",
        start_time=datetime.now(timezone.utc),
        status="PENDING",
        error_message=None,
        end_time=None,
        resolved_input_prompt="continue",
        model_output_summary=None,
        result=None,
        trigger_event_details={"_resume": {"execution_id": str(prior)}},
    )
    leases: list = []

    def _lease(db, **kwargs):
        leases.append(kwargs.get("required_runner_id"))
        return None

    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", _lease)
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.get",
        rows_by_id({execution_id: execution, prior: owned_by(prior, owner)}),
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get_stop_request",
        lambda *args, **kwargs: None,
    )

    executor = RemoteRunnerExecutor(
        "codex",
        wrapped,
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
        flow=SimpleNamespace(
            agent_config=wrapped,
            ai_model=None,
            git_clone_config=None,
            custom_commands=None,
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            agent_type="codex",
        ),
        execution=execution,
    )
    await executor.get_status(execution.agent_session_reference)

    assert leases == [owner]


@pytest.mark.asyncio
async def test_a_failed_owner_lookup_waits_instead_of_leasing_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not knowing the owner is not the same answer as having no owner.

    Leasing unpinned on a lookup error would consume a peer's slot and end
    the continuation on a host that does not hold its workspace. The next
    poll retries the lookup, so waiting costs one interval.
    """
    from preloop.agents.base import AgentStatus
    from preloop.agents.remote_runner import RemoteRunnerExecutor

    prior = uuid4()
    execution_id = uuid4()
    execution = SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        runner_id=None,
        agent_session_reference=f"runner:queued:local:{execution_id}",
        start_time=datetime.now(timezone.utc),
        status="PENDING",
        error_message=None,
        end_time=None,
        resolved_input_prompt="continue",
        model_output_summary=None,
        result=None,
        trigger_event_details={"_resume": {"execution_id": str(prior)}},
    )

    def _must_not_lease(*args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("an unresolved owner must not lease anywhere")

    def _explode(*args, **kwargs):
        raise RuntimeError("database is unreachable")

    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", _must_not_lease)
    monkeypatch.setattr(
        "preloop.agents.remote_runner.workspace_owner_runner_id", _explode
    )
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.get",
        rows_by_id({execution_id: execution}),
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get_stop_request",
        lambda *args, **kwargs: None,
    )

    executor = RemoteRunnerExecutor(
        "codex",
        {"runner": {"persist_workspace": True}},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
        flow=SimpleNamespace(agent_config={"runner": {"persist_workspace": True}}),
    )
    status = await executor.get_status(execution.agent_session_reference)

    assert status is AgentStatus.PENDING
    assert execution.status == "PENDING"


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"agent_config": {"runner": {}}}, {"runner": {}}),
        ({"runner": {}}, {"runner": {}}),
        ({"agent_config": {}, "image": "x"}, {"agent_config": {}, "image": "x"}),
        ({"agent_config": "not-a-dict"}, {"agent_config": "not-a-dict"}),
        (None, None),
    ],
)
def test_one_unwrapping_serves_the_lease_payload_and_the_owner_lookup(
    config: object, expected: object
) -> None:
    """Two readers of the same stored shape must never disagree.

    Drift between a payload-side unwrap and a lookup-side unwrap is exactly
    how a host-bound continuation reads as free on one path and pinned on
    the other, so both now call this.
    """
    assert unwrap_agent_config(config) == expected


def test_checkpoint_keeps_base_after_restore_redacts_git_config(tmp_path) -> None:
    upstream = init_repo(tmp_path / "upstream")
    (upstream / "tracked").write_text("base")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "base")
    source = tmp_path / "source"
    subprocess.run(
        ["git", "clone", str(upstream), str(source)], check=True, capture_output=True
    )
    git(source, "config", "user.name", "Jane Doe")
    git(source, "config", "user.email", "jane@example.com")
    git(source, "checkout", "-b", "implementation/new")
    (source / "tracked").write_text("unpushed")
    git(source, "commit", "-am", "local work")
    expected_base = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    restored = tmp_path / "restored"
    first = restore(capture(source, max_bytes=2_000_000), restored)
    assert first["repositories"][0]["base_sha"] == expected_base
    second = restore(capture(restored, max_bytes=2_000_000), tmp_path / "again")
    assert second["repositories"][0]["base_sha"] == expected_base
