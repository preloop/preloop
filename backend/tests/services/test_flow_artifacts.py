"""Adversarial archive checks and immutable recovery references."""

import io
import tarfile

import pytest

from preloop.agents.checkpoint_client import capture, restore
from preloop.services.flow_artifacts import (
    artifact_thread_id,
    manifest_digest,
    validate_archive,
)


@pytest.fixture(autouse=True)
def admit_unhalted_mock_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    # These unit tests isolate checkpoint policy. Halt admission and ordering
    # are exercised against PostgreSQL in test_kill_switch_durability.
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.admit_runtime_start",
        lambda *args, **kwargs: True,
    )


def archive_with(
    name: str, data: bytes = b"source", kind: bytes = tarfile.REGTYPE
) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/etc/passwd" if kind != tarfile.REGTYPE else ""
        info.size = len(data) if kind == tarfile.REGTYPE else 0
        archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return stream.getvalue()


def test_artifact_thread_id_prefers_persisted_session_over_resume() -> None:
    assert (
        artifact_thread_id(
            {
                "_session_thread_id": "session",
                "_resume": {"thread_id": "resume", "execution_id": "prior"},
            },
            "execution",
        )
        == "session"
    )
    assert (
        artifact_thread_id({"_resume": {"thread_id": "resume"}}, "execution")
        == "resume"
    )
    assert artifact_thread_id({}, "execution") == "execution"


@pytest.mark.parametrize(
    "name", ["../escape", "/etc/passwd", "workspace/../../escape", "workspace\\escape"]
)
def test_rejects_traversal(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe_path"):
        validate_archive(archive_with(name), max_bytes=10000, max_expanded_bytes=10000)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]
)
def test_rejects_links_and_devices(kind: bytes) -> None:
    with pytest.raises(ValueError, match="unsafe_member"):
        validate_archive(
            archive_with("workspace/file", kind=kind),
            max_bytes=10000,
            max_expanded_bytes=10000,
        )


def test_compressed_bomb_is_rejected_before_expansion() -> None:
    body = archive_with("workspace/large", b"x" * 100000)
    assert len(body) < 1000
    with pytest.raises(ValueError, match="expansion_limit"):
        validate_archive(body, max_bytes=1000, max_expanded_bytes=100)


def test_corrupt_archive_rejected() -> None:
    with pytest.raises(ValueError, match="corrupt"):
        validate_archive(b"not gzip", max_bytes=1000, max_expanded_bytes=100)


def test_manifest_canonical_identity() -> None:
    assert manifest_digest({"a": 1, "b": 2}) == manifest_digest({"b": 2, "a": 1})
    assert manifest_digest({"a": 1}) != manifest_digest({"a": 2})


def test_checkpoint_roundtrip_preserves_unpushed_and_dirty_state(
    tmp_path, monkeypatch
) -> None:
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
        subprocess.run(["git", "-C", str(source), "config", key, value], check=True)
    (source / "tracked.txt").write_text("committed")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "unpublished"],
        check=True,
        capture_output=True,
    )
    original = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"])
    (source / "tracked.txt").write_text("dirty")
    (source / "untracked.txt").write_text("needed")
    (source / ".env").write_text("SECRET=hidden")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "huge").write_bytes(b"z" * 100000)
    (source / ".preloop-native-session").mkdir()
    (source / ".preloop-native-session" / "other-session").write_text("private")
    body = capture(source, max_bytes=100000)
    target = tmp_path / "restored"
    restore(body, target)
    restored = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"])
    assert restored == original
    assert (target / "tracked.txt").read_text() == "dirty"
    assert (target / "untracked.txt").read_text() == "needed"
    assert not (target / ".env").exists()
    assert not (target / "node_modules").exists()
    assert not (target / ".preloop-native-session").exists()


def test_restore_refuses_overwriting_existing_work(tmp_path) -> None:
    destination = tmp_path / "workspace"
    destination.mkdir()
    (destination / "work").write_text("existing")
    with pytest.raises(ValueError, match="not_empty"):
        restore(archive_with("workspace/work", b"new"), destination)
    assert (destination / "work").read_text() == "existing"


def test_capture_defers_while_git_transaction_is_open(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index.lock").write_text("write in progress")
    with pytest.raises(ValueError, match="workspace_busy"):
        capture(tmp_path, max_bytes=100000)


def test_missing_checkpoint_blocks_resume_before_cold_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4
    from unittest.mock import Mock

    from preloop.config import settings
    from preloop.models.crud import flow_artifact
    from preloop.services.checkpoint_runtime import checkpoint_context

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    monkeypatch.setattr(flow_artifact, "latest", lambda *args, **kwargs: None)
    context = {
        "account_id": str(uuid4()),
        "flow_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "checkpoint_resume_authorized": True,
        "trigger_event_data": {"_resume": {"execution_id": str(uuid4())}},
    }
    with pytest.raises(ValueError, match="workspace_checkpoint_missing"):
        checkpoint_context(Mock(), context)
    context["checkpoint_resume_authorized"] = False
    with pytest.raises(ValueError, match="checkpoint_resume_not_authorized"):
        checkpoint_context(Mock(), context)


def test_pr_comment_resume_without_thread_id_requires_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4
    from unittest.mock import Mock

    from preloop.config import settings
    from preloop.services.checkpoint_runtime import checkpoint_context

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    context = {
        "account_id": str(uuid4()),
        "flow_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "trigger_event_data": {
            "_resume": {
                "execution_id": str(uuid4()),
                "pr_url": "https://example.com/merge_requests/1",
                "source_branch": "fix",
            }
        },
    }
    with pytest.raises(ValueError, match="checkpoint_resume_not_authorized"):
        checkpoint_context(Mock(), context)


@pytest.mark.asyncio
async def test_private_executor_never_receives_hosted_artifact_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, Mock

    from preloop.agents.remote_runner import RemoteRunnerExecutor
    from preloop.services import checkpoint_runtime, flow_orchestrator

    orchestrator = object.__new__(flow_orchestrator.FlowExecutionOrchestrator)
    orchestrator.flow = Mock()
    orchestrator.execution_log = Mock()
    orchestrator.db = Mock()
    runner = object.__new__(RemoteRunnerExecutor)
    runner.start = AsyncMock(return_value="runner:local:execution")
    runner.cleanup = AsyncMock()
    monkeypatch.setattr(
        flow_orchestrator, "create_executor_for_execution", lambda *a, **k: runner
    )
    hosted = Mock(side_effect=AssertionError("private capability must not be minted"))
    monkeypatch.setattr(checkpoint_runtime, "checkpoint_context", hosted)
    context = {"agent_type": "codex", "agent_config": {}}
    await orchestrator._start_agent_session(context)
    hosted.assert_not_called()
    assert context["checkpoint_env"] == {}


def test_artifact_capability_covers_longest_execution() -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    import jwt

    from preloop.api.endpoints.flow_artifacts import (
        ARTIFACT_CAPABILITY_TTL,
        mint_artifact_capability,
    )
    from preloop.config import settings

    token = mint_artifact_capability(
        account_id=uuid4(),
        flow_id=uuid4(),
        thread_id="thread",
        execution_id=uuid4(),
        kind="workspace",
        operation="put",
    )
    claims = jwt.decode(
        token,
        settings.security.secret_key,
        algorithms=["HS256"],
        audience="flow-artifact",
    )
    expiry = datetime.fromtimestamp(claims["exp"], UTC)
    assert ARTIFACT_CAPABILITY_TTL >= timedelta(hours=24)
    assert expiry >= datetime.now(UTC) + timedelta(hours=23)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_native_session", [False, True])
async def test_unreserved_resume_never_starts_a_hosted_agent_on_a_cold_workspace(
    monkeypatch: pytest.MonkeyPatch, has_native_session: bool
) -> None:
    """A legacy PR binding cannot authorize loss of unpublished workspace state."""
    from unittest.mock import AsyncMock, Mock
    from uuid import uuid4

    from preloop.api.endpoints import flow_artifacts
    from preloop.config import settings
    from preloop.services import flow_orchestrator

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    executor = Mock()
    executor.start = AsyncMock(return_value="unexpected-agent")
    executor.cleanup = AsyncMock()
    monkeypatch.setattr(
        flow_orchestrator, "create_executor_for_execution", lambda *a, **k: executor
    )
    mint = Mock()
    monkeypatch.setattr(flow_artifacts, "mint_artifact_capability", mint)
    orchestrator = object.__new__(flow_orchestrator.FlowExecutionOrchestrator)
    orchestrator.flow = Mock()
    orchestrator.execution_log = Mock()
    orchestrator.db = Mock()
    resume = {
        "execution_id": str(uuid4()),
        "pr_url": "https://example.test/pull/1",
        "source_branch": "implementation",
    }
    if has_native_session:
        resume["cli_session"] = {"agent_type": "codex", "session_id": str(uuid4())}
    context = {
        "agent_type": "codex",
        "agent_config": {},
        "trigger_event_data": {"_resume": resume},
        "cli_session_restore_archive": b"synthetic legacy archive",
    }
    with pytest.raises(ValueError, match="checkpoint_resume_not_authorized"):
        await orchestrator._start_agent_session(context)
    executor.start.assert_not_awaited()
    executor.cleanup.assert_awaited_once()
    mint.assert_not_called()
