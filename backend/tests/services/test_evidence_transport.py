"""Direct evidence transport: packing, receipts, wrapper, hosted/private env."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from preloop.agents.checkpoint_client import _read_bounded, pack_evidence
from preloop.agents.container import ContainerAgentExecutor, K8S_ARTIFACT_WRAPPER_SCRIPT
from preloop.config import settings
from preloop.services.checkpoint_runtime import evidence_transport_env
from preloop.services.flow_artifacts import (
    EvidenceUnavailableError,
    evidence_receipt,
    extract_result_json,
    inspect_evidence,
    public_evidence_status,
    sanitize_captured_result,
    validate_archive,
)
from backend.tests.services.test_flow_artifacts import archive_with


def _evidence_archive(
    tmp_path: Path, payload: bytes = b'{"id":"CVE-0000-0000"}'
) -> bytes:
    evidence = tmp_path / "workspace"
    (evidence / "evidence").mkdir(parents=True)
    (evidence / "evidence" / "findings.json").write_bytes(payload)
    (evidence / "result.json").write_text(
        json.dumps({"schema": "preloop.cra.vulnscan/v1", "verdict": "fail"})
    )
    return pack_evidence(
        evidence, max_bytes=32 * 1024 * 1024, max_expanded_bytes=64 * 1024 * 1024
    )


def test_pack_evidence_includes_result_and_rejects_unsafe_members(
    tmp_path: Path,
) -> None:
    body = _evidence_archive(tmp_path)
    assert len(body) < 2 * 1024 * 1024
    validate_archive(body, max_bytes=32 * 1024 * 1024, max_expanded_bytes=1024 * 1024)
    result = extract_result_json(body)
    assert result is not None and result["verdict"] == "fail"
    (tmp_path / "escape" / "evidence").mkdir(parents=True)
    (tmp_path / "escape" / "evidence" / "ok.json").write_text("{}")
    with pytest.raises(ValueError, match="evidence_absent"):
        pack_evidence(tmp_path / "missing", max_bytes=1000, max_expanded_bytes=1000)


def test_pack_evidence_accepts_payload_above_legacy_log_cap(tmp_path: Path) -> None:
    payload = os.urandom(2 * 1024 * 1024 + 4096)
    body = _evidence_archive(tmp_path, payload)
    assert len(body) > 2 * 1024 * 1024
    validate_archive(
        body,
        max_bytes=settings.flow_evidence_max_bytes,
        max_expanded_bytes=settings.flow_artifact_expanded_max_bytes,
    )


def test_receipt_never_claims_legal_hold() -> None:
    receipt = evidence_receipt(
        status="available",
        execution_id=uuid4(),
        transport="direct",
        archive=b"abc",
    )
    assert receipt["object_lock"] is False
    assert receipt["legal_hold"] is False
    assert receipt["sha256"]
    assert receipt["digest"] == receipt["sha256"]
    assert receipt["kind"] == "evidence"
    assert receipt["integrity_verified"] is False
    assert receipt["status"] == "available"


def test_inspect_evidence_prefers_live_artifact_over_stale_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = Mock()
    execution.id = uuid4()
    execution.flow_id = uuid4()
    execution.trigger_event_details = {"_session_thread_id": "thread"}
    execution.evidence_archive = None
    execution.evidence_receipt = {"status": "available", "transport": "direct"}
    monkeypatch.setattr(
        "preloop.services.flow_artifacts.crud.latest", lambda *args, **kwargs: None
    )
    receipt = inspect_evidence(Mock(), account_id=uuid4(), execution=execution)
    assert receipt["status"] == "missing"


def test_evidence_unavailable_http_codes() -> None:
    assert EvidenceUnavailableError("missing", {"status": "missing"}).status_code == 404
    assert EvidenceUnavailableError("expired", {"status": "expired"}).status_code == 410
    assert EvidenceUnavailableError("failed", {"status": "failed"}).status_code == 409


def test_evidence_transport_env_hosted_and_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    context = {
        "account_id": str(uuid4()),
        "flow_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "trigger_event_data": {},
    }
    env = evidence_transport_env(context)
    assert env["PRELOOP_EVIDENCE_PUT_TOKEN"]
    assert env["PRELOOP_EVIDENCE_URL"].endswith("/artifacts")
    assert "PRELOOP_CHECKPOINT_PUT_TOKEN" not in env
    monkeypatch.setattr(settings, "flow_artifact_direct_upload", False)
    assert evidence_transport_env(context) == {}


@pytest.mark.asyncio
async def test_private_runner_receives_evidence_capability_not_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.agents.remote_runner import RemoteRunnerExecutor
    from preloop.services import flow_orchestrator

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    monkeypatch.setattr(
        flow_orchestrator.crud_flow_execution,
        "admit_runtime_start",
        lambda *a, **k: True,
    )
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
    context = {
        "agent_type": "codex",
        "agent_config": {},
        "account_id": str(uuid4()),
        "flow_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "trigger_event_data": {},
    }
    await orchestrator._start_agent_session(context)
    assert context["checkpoint_env"] == {}
    assert context["evidence_env"]["PRELOOP_EVIDENCE_PUT_TOKEN"]


def test_kubernetes_wrapper_legacy_still_emits_base64(tmp_path: Path) -> None:
    import shutil

    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    workspace = tmp_path / "workspace"
    (workspace / "evidence").mkdir(parents=True)
    (workspace / "result.json").write_text('{"status":"success"}')
    (workspace / "evidence" / "findings.json").write_text('[{"id":"X"}]')
    script = K8S_ARTIFACT_WRAPPER_SCRIPT.replace("/workspace", str(workspace)).replace(
        "/tmp/preloop-evidence.tar.gz", str(tmp_path / "ev.tar.gz")
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", "PRELOOP_INNER_SCRIPT": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "PRELOOP_ARTIFACT_B64 " in proc.stdout
    assert "PRELOOP_ARTIFACT_BEGIN evidence present" in proc.stdout


def test_kubernetes_direct_upload_omits_evidence_bytes(tmp_path: Path) -> None:
    import shutil

    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    workspace = tmp_path / "workspace"
    (workspace / "evidence").mkdir(parents=True)
    secret = '{"finding":"should-not-appear-in-logs"}'
    (workspace / "evidence" / "findings.json").write_text(secret)
    (workspace / "result.json").write_text('{"verdict":"fail"}')
    client = tmp_path / "client.py"
    client.write_text(
        "import sys\nfrom pathlib import Path\n"
        "Path('/tmp/preloop-evidence-reference.json').write_text('{}')\n"
        "print('PRELOOP_EVIDENCE committed 00000000-0000-0000-0000-000000000001')\n"
        "sys.exit(0)\n"
    )
    script = (
        K8S_ARTIFACT_WRAPPER_SCRIPT.replace("/workspace", str(workspace))
        .replace("/tmp/preloop-checkpoint-client.py", str(client))
        .replace("/tmp/preloop-evidence.tar.gz", str(tmp_path / "ev.tar.gz"))
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PRELOOP_INNER_SCRIPT": "true",
            "PRELOOP_EVIDENCE_PUT_TOKEN": "scoped-token",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "PRELOOP_ARTIFACT_B64 " not in proc.stdout
    assert secret not in proc.stdout
    assert "scoped-token" not in proc.stdout
    assert "PRELOOP_ARTIFACT_BEGIN evidence uploaded" in proc.stdout
    assert "PRELOOP_ARTIFACT_BEGIN result uploaded" in proc.stdout


@pytest.mark.asyncio
async def test_kubernetes_direct_path_does_not_decode_log_payload() -> None:
    executor = ContainerAgentExecutor(
        "codex", {}, image="test-image:latest", use_kubernetes=True
    )
    executor._direct_evidence = True
    archive = archive_with("evidence/findings.json", b"secret-finding")
    encoded = __import__("base64").b64encode(archive).decode()
    executor._get_kubernetes_terminal_logs = AsyncMock(
        return_value=[
            "PRELOOP_ARTIFACT_BEGIN evidence present 12",
            f"PRELOOP_ARTIFACT_B64 {encoded}",
            "PRELOOP_ARTIFACT_END evidence",
        ]
    )
    assert await executor.get_evidence_archive("job-123") is None


@pytest.mark.asyncio
async def test_orchestrator_records_failed_receipt_on_transport_error() -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator._evidence_archive = None
    orchestrator._evidence_receipt = None
    orchestrator.execution_log = Mock(id=uuid4(), trigger_event_details={})
    orchestrator.flow = None
    orchestrator.db = Mock()
    orchestrator.execution_logger = Mock()
    executor = Mock()
    executor.evidence_transport_error = "evidence_upload_failed"
    executor.get_evidence_archive = AsyncMock(return_value=None)
    await orchestrator._capture_evidence_archive(executor, "job")
    assert orchestrator._evidence_archive is None
    assert orchestrator._evidence_receipt is not None
    assert orchestrator._evidence_receipt["status"] == "failed"


@pytest.mark.asyncio
async def test_transport_failure_sets_executor_error() -> None:
    executor = ContainerAgentExecutor(
        "codex", {}, image="test-image:latest", use_kubernetes=True
    )
    executor._direct_evidence = True
    executor._get_kubernetes_terminal_logs = AsyncMock(
        return_value=[
            "PRELOOP_ARTIFACT_BEGIN evidence error",
            "PRELOOP_ARTIFACT_END evidence",
        ]
    )
    assert await executor.get_evidence_archive("job-123") is None
    assert executor.evidence_transport_error == "evidence_upload_failed"


def test_pack_evidence_rejects_sparse_oversize_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.agents import checkpoint_client as cc

    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    fd = os.open(evidence / "sparse.bin", os.O_CREAT | os.O_WRONLY, 0o600)
    os.ftruncate(fd, 2 * 1024 * 1024 * 1024)
    os.close(fd)

    def boom(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("must not read an oversized evidence member")

    monkeypatch.setattr(cc, "_read_bounded", boom)
    with pytest.raises(ValueError, match="evidence_expansion_limit"):
        pack_evidence(workspace, max_bytes=64 * 1024, max_expanded_bytes=1024)


def test_pack_evidence_rejects_result_cap_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.agents import checkpoint_client as cc

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = workspace / "result.json"
    fd = os.open(result, os.O_CREAT | os.O_WRONLY, 0o600)
    os.ftruncate(fd, 256 * 1024 + 1)
    os.close(fd)

    def boom(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("must not read an oversized result.json")

    monkeypatch.setattr(cc, "_read_bounded", boom)
    with pytest.raises(ValueError, match="evidence_result_oversized"):
        pack_evidence(workspace, max_bytes=64 * 1024, max_expanded_bytes=1024 * 1024)


def test_pack_evidence_rejects_symlinked_evidence_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text('{"secret":true}')
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "evidence").symlink_to(outside)
    with pytest.raises(ValueError, match="evidence_unsafe_member"):
        pack_evidence(workspace, max_bytes=64 * 1024, max_expanded_bytes=64 * 1024)


def test_pack_evidence_enforces_member_limit_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.agents import checkpoint_client as cc

    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    for name in ("a.json", "b.json", "c.json"):
        (evidence / name).write_text("{}")
    monkeypatch.setattr(cc, "MAX_EVIDENCE_MEMBERS", 2)

    def boom(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("must not pack after the member cap")

    monkeypatch.setattr(cc, "_read_bounded", boom)
    with pytest.raises(ValueError, match="evidence_invalid_members"):
        pack_evidence(workspace, max_bytes=64 * 1024, max_expanded_bytes=64 * 1024)


def test_read_bounded_rejects_growth_after_lstat(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    path.write_bytes(b"abc")
    before = path.lstat()
    path.write_bytes(b"abcdef")
    with pytest.raises(ValueError, match="evidence_busy"):
        _read_bounded(path, expected=before, limit=1024)


def test_read_bounded_rejects_symlink_replacement(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    secret = tmp_path / "secret.json"
    secret.write_bytes(b"secret-not-for-archive")
    path.write_bytes(b"abc")
    before = path.lstat()
    path.unlink()
    path.symlink_to(secret)
    with pytest.raises(ValueError, match="evidence_unsafe_member|evidence_busy"):
        _read_bounded(path, expected=before, limit=1024)


def test_public_status_does_not_query_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = Mock()
    execution.id = uuid4()
    execution.evidence_archive = None
    execution.evidence_receipt = {
        "status": "available",
        "transport": "direct",
        "sha256": "abc",
        "kind": "evidence",
    }

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("status polls must not query flow_artifact")

    monkeypatch.setattr("preloop.services.flow_artifacts.crud.latest", boom)
    status = public_evidence_status(execution)
    assert status["status"] == "available"
    assert status["integrity_verified"] is False
    assert status["digest"] == "abc"


def test_inspect_failed_receipt_is_not_resurrected_by_live_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = Mock()
    execution.id = uuid4()
    execution.flow_id = uuid4()
    execution.trigger_event_details = {}
    execution.evidence_archive = None
    execution.evidence_receipt = {
        "status": "failed",
        "transport": "direct",
        "error": "evidence_upload_failed",
    }
    execution.status = "FAILED"
    monkeypatch.setattr(
        "preloop.services.flow_artifacts.crud.latest",
        lambda *args, **kwargs: Mock(id=uuid4(), ciphertext=b"x"),
    )
    receipt = inspect_evidence(Mock(), account_id=uuid4(), execution=execution)
    assert receipt["status"] == "failed"


def test_inspect_live_refresh_before_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = Mock()
    execution.id = uuid4()
    execution.flow_id = uuid4()
    execution.trigger_event_details = {}
    execution.evidence_archive = None
    execution.status = "RUNNING"
    execution.evidence_receipt = {
        "status": "failed",
        "transport": "direct",
        "error": "evidence_upload_failed",
    }
    live = Mock(
        id=uuid4(),
        ciphertext=b"x",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        availability="available",
        manifest={"sha256": "abc"},
        manifest_sha256="def",
        kind="evidence",
        execution_id=execution.id,
    )
    monkeypatch.setattr(
        "preloop.services.flow_artifacts.crud.latest",
        lambda *args, **kwargs: live,
    )
    receipt = inspect_evidence(Mock(), account_id=uuid4(), execution=execution)
    assert receipt["status"] == "available"
    assert receipt["artifact_id"] == str(live.id)


def test_sanitize_strips_reserved_publication_keys() -> None:
    cleaned = sanitize_captured_result(
        {
            "verdict": "fail",
            "trusted_publication": {"url": "https://example.com/forged"},
            "_private_publication": {"phase": "complete"},
        }
    )
    assert cleaned == {"verdict": "fail"}


def test_sanitize_strips_forged_evidence_upload() -> None:
    cleaned = sanitize_captured_result(
        {"verdict": "fail", "evidence_upload": "uploaded"}
    )
    assert cleaned == {"verdict": "fail"}


@pytest.mark.asyncio
async def test_getterless_executor_strips_forged_keys_from_tar_result(
    tmp_path: Path,
) -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    workspace = tmp_path / "workspace"
    (workspace / "evidence").mkdir(parents=True)
    (workspace / "evidence" / "findings.json").write_text("[]")
    (workspace / "result.json").write_text(
        json.dumps(
            {
                "schema": "preloop.cra.vulnscan/v1",
                "verdict": "fail",
                "trusted_publication": {"url": "https://example.com/forged"},
                "_private_publication": {"phase": "complete"},
            }
        )
    )
    archive = pack_evidence(
        workspace, max_bytes=64 * 1024, max_expanded_bytes=64 * 1024
    )
    extracted = extract_result_json(archive)
    assert extracted is not None
    assert "trusted_publication" not in extracted
    assert "_private_publication" not in extracted

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator._evidence_archive = None
    orchestrator._evidence_receipt = None
    orchestrator._workspace_snapshot = None
    orchestrator.execution_log = Mock(id=uuid4(), trigger_event_details={})
    orchestrator.flow = None
    orchestrator.db = Mock()
    orchestrator.execution_logger = Mock()
    executor = SimpleNamespace(get_evidence_archive=AsyncMock(return_value=archive))
    result = await orchestrator._capture_result_artifact(executor, "job")
    assert result["verdict"] == "fail"
    assert "trusted_publication" not in result
    assert "_private_publication" not in result


@pytest.mark.asyncio
async def test_later_capture_replaces_early_trap_archive() -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    later = archive_with("evidence/after-postprocess.json", b'{"ok":true}')
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator._evidence_archive = b"stale-trap-bytes"
    orchestrator._evidence_receipt = {"status": "available", "transport": "legacy"}
    orchestrator._workspace_snapshot = None
    orchestrator.execution_log = Mock(id=uuid4(), trigger_event_details={})
    orchestrator.flow = None
    orchestrator.db = Mock()
    orchestrator.execution_logger = Mock()
    executor = Mock()
    executor.evidence_transport_error = None
    executor.get_evidence_archive = AsyncMock(return_value=later)
    await orchestrator._capture_evidence_archive(executor, "job")
    assert orchestrator._evidence_archive == later


@pytest.mark.asyncio
async def test_final_upload_failure_overrides_stale_trap_archive() -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator._evidence_archive = b"stale-trap-bytes"
    orchestrator._evidence_receipt = {"status": "available", "transport": "direct"}
    orchestrator.execution_log = Mock(id=uuid4(), trigger_event_details={})
    orchestrator.flow = None
    orchestrator.db = Mock()
    orchestrator.execution_logger = Mock()
    executor = Mock()
    executor.evidence_transport_error = "evidence_upload_failed"
    executor.get_evidence_archive = AsyncMock(return_value=None)
    await orchestrator._capture_evidence_archive(executor, "job")
    assert orchestrator._evidence_receipt["status"] == "failed"


def test_kubernetes_direct_upload_failure_is_honest(tmp_path: Path) -> None:
    import shutil

    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    workspace = tmp_path / "workspace"
    (workspace / "evidence").mkdir(parents=True)
    secret = '{"finding":"must-not-appear"}'
    (workspace / "evidence" / "findings.json").write_text(secret)
    (workspace / "result.json").write_text('{"verdict":"fail"}')
    client = tmp_path / "client.py"
    client.write_text("import sys\nsys.exit(1)\n")
    script = (
        K8S_ARTIFACT_WRAPPER_SCRIPT.replace("/workspace", str(workspace))
        .replace("/tmp/preloop-checkpoint-client.py", str(client))
        .replace("/tmp/preloop-evidence.tar.gz", str(tmp_path / "ev.tar.gz"))
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PRELOOP_INNER_SCRIPT": "true",
            "PRELOOP_EVIDENCE_PUT_TOKEN": "scoped-token",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "PRELOOP_ARTIFACT_B64 " not in proc.stdout
    assert secret not in proc.stdout
    assert "PRELOOP_ARTIFACT_BEGIN evidence error" in proc.stdout
    assert "PRELOOP_ARTIFACT_BEGIN result error" in proc.stdout
    assert "PRELOOP_ARTIFACT_BEGIN result uploaded" not in proc.stdout
    assert "PRELOOP_ARTIFACT_BEGIN evidence uploaded" not in proc.stdout


@pytest.mark.asyncio
async def test_direct_path_ignores_injected_cleartext_on_upload_failure() -> None:
    executor = ContainerAgentExecutor(
        "codex", {}, image="test-image:latest", use_kubernetes=True
    )
    executor._direct_evidence = True
    archive = archive_with("evidence/findings.json", b"secret-finding")
    encoded = __import__("base64").b64encode(archive).decode()
    executor._get_kubernetes_terminal_logs = AsyncMock(
        return_value=[
            "PRELOOP_ARTIFACT_BEGIN evidence error",
            f"PRELOOP_ARTIFACT_B64 {encoded}",
            "PRELOOP_ARTIFACT_END evidence",
            "PRELOOP_ARTIFACT_BEGIN result error",
            "PRELOOP_ARTIFACT_END result",
        ]
    )
    assert await executor.get_evidence_archive("job-123") is None
    assert executor.evidence_transport_error == "evidence_upload_failed"
    artifact = await executor.get_result_artifact("job-123")
    assert artifact is not None
    assert artifact["error"] == "result_artifact_fetch_failed"


def test_stale_marker_does_not_skip_repeat_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.agents import checkpoint_client as cc

    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "one.json").write_text('{"n":1}')
    marker = tmp_path / "preloop-evidence-reference.json"
    marker.write_text(
        json.dumps(
            {
                "artifact_id": "00000000-0000-0000-0000-000000000099",
                "sha256": "0" * 64,
            }
        )
    )
    puts: list[bytes] = []

    def fake_request(
        method: str,
        token: str,
        data: bytes | None = None,
        *,
        url: str | None = None,
    ) -> bytes:
        assert method == "PUT"
        assert data
        puts.append(data)
        return json.dumps(
            {
                "artifact_id": str(uuid4()),
                "execution_id": str(uuid4()),
                "manifest_sha256": "a" * 64,
            }
        ).encode()

    monkeypatch.setattr(cc, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(cc, "EVIDENCE_REFERENCE_PATH", marker)
    monkeypatch.setattr(cc, "request", fake_request)
    monkeypatch.setenv("PRELOOP_EVIDENCE_MAX_BYTES", "65536")
    monkeypatch.setenv("PRELOOP_EVIDENCE_EXPANDED_MAX_BYTES", "65536")
    monkeypatch.setenv("PRELOOP_EVIDENCE_PUT_TOKEN", "scoped-token")
    monkeypatch.setenv("PRELOOP_EVIDENCE_URL", "https://example.com/artifacts")
    monkeypatch.setattr(sys, "argv", ["checkpoint_client.py", "evidence"])
    cc.main()
    (evidence / "two.json").write_text('{"n":2}')
    cc.main()
    assert len(puts) == 2
    assert puts[0] != puts[1]
    assert json.loads(marker.read_text())["artifact_id"] != (
        "00000000-0000-0000-0000-000000000099"
    )
