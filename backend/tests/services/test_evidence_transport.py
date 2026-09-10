"""Direct evidence transport: packing, receipts, wrapper, hosted/private env."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
from uuid import uuid4

import pytest

from preloop.agents.checkpoint_client import _read_bounded, pack_evidence
from preloop.agents.container import ContainerAgentExecutor, K8S_ARTIFACT_WRAPPER_SCRIPT
from preloop.config import settings
from preloop.cra.evidence_pack import (
    PACK_MANIFEST_NAME,
    PACK_MANIFEST_SCHEMA,
    EvidencePackError,
    read_pack_manifest,
    verify_pack_manifest,
)
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
            "product_provenance": {"mapping_status": "verified"},
            "dossier_manifest": {"schema": "forged"},
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
    # Capture now stamps manifest.json into a pack that arrived without
    # one, so the stored bytes are the described bytes.
    stored = orchestrator._evidence_archive
    assert stored != b"stale-trap-bytes"
    manifest = verify_pack_manifest(stored)
    assert [member["name"] for member in manifest["members"]] == [
        "evidence/after-postprocess.json"
    ]
    assert extract_result_json(stored) == extract_result_json(later)


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


@pytest.mark.asyncio
async def test_non_string_transport_error_does_not_drop_getter_archive() -> None:
    """Only a real error string is a transport failure; mock auto-attrs are not."""
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    archive = b"\x1f\x8b" + b"fake-evidence-tar-gz"
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator._evidence_archive = None
    orchestrator._evidence_receipt = None
    orchestrator._workspace_snapshot = None
    orchestrator.execution_log = Mock(
        id=uuid4(), status="RUNNING", trigger_event_details={}
    )
    orchestrator.flow = None
    orchestrator.db = Mock()
    orchestrator.execution_logger = Mock()
    executor = AsyncMock()
    executor.get_evidence_archive = AsyncMock(return_value=archive)
    await orchestrator._capture_evidence_archive(executor, "job")
    assert orchestrator._evidence_archive == archive
    assert orchestrator._evidence_receipt["status"] == "available"


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


class _FakeHttpResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._body
        return self._body[:n]

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_checkpoint_restore_read_limit_ignores_smaller_evidence_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.agents import checkpoint_client as cc

    evidence_cap = 32 * 1024 * 1024
    checkpoint_cap = 512 * 1024 * 1024
    body = os.urandom(evidence_cap + 1)
    monkeypatch.setenv("PRELOOP_EVIDENCE_MAX_BYTES", str(evidence_cap))
    monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", str(checkpoint_cap))
    monkeypatch.setenv("PRELOOP_CHECKPOINT_URL", "https://example.com/checkpoint")
    monkeypatch.setenv("PRELOOP_EVIDENCE_URL", "https://example.com/evidence")

    def fake_urlopen(
        req: urllib.request.Request, timeout: int = 0
    ) -> _FakeHttpResponse:
        return _FakeHttpResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    restored = cc.request("GET", "checkpoint-token")
    assert restored == body
    with pytest.raises(ValueError, match="checkpoint_response_oversized"):
        cc.request(
            "GET",
            "evidence-token",
            url=os.environ["PRELOOP_EVIDENCE_URL"],
        )


def test_pack_evidence_stays_on_evidence_cap_when_checkpoint_cap_is_larger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_cap = 32 * 1024 * 1024
    monkeypatch.setenv("PRELOOP_EVIDENCE_MAX_BYTES", str(evidence_cap))
    monkeypatch.setenv("PRELOOP_CHECKPOINT_MAX_BYTES", str(512 * 1024 * 1024))
    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "huge.bin").write_bytes(os.urandom(evidence_cap + 1))
    with pytest.raises(ValueError, match="evidence_oversized|evidence_expansion_limit"):
        pack_evidence(
            workspace,
            max_bytes=int(os.environ["PRELOOP_EVIDENCE_MAX_BYTES"]),
            max_expanded_bytes=2 * 1024**3,
        )


def test_malformed_receipt_expiry_stays_available() -> None:
    execution = SimpleNamespace(
        evidence_receipt={
            "status": "available",
            "kind": "evidence",
            "expires_at": "not-a-timestamp",
        },
        evidence_archive=None,
    )
    status = public_evidence_status(execution)
    assert status["status"] == "available"


@pytest.mark.asyncio
async def test_hosted_docker_direct_capture_binds_existing_artifact_without_reupload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.agents.base import AgentStatus
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    stored_id = uuid4()
    stored = SimpleNamespace(
        id=stored_id,
        ciphertext=b"encrypted",
        manifest={"sha256": "a" * 64, "size_bytes": 12},
        expires_at=None,
        created_at=None,
    )
    puts: list[object] = []

    def boom_put(*args: object, **kwargs: object) -> None:
        puts.append(kwargs)
        raise AssertionError("direct docker capture must not store a second copy")

    monkeypatch.setattr("preloop.services.flow_artifacts.put_artifact", boom_put)
    monkeypatch.setattr(
        "preloop.models.crud.flow_artifact.latest",
        lambda *args, **kwargs: stored,
    )

    executor = ContainerAgentExecutor(
        "codex", {}, image="test-image:latest", use_kubernetes=False
    )
    mock_container = AsyncMock()
    type(mock_container).id = PropertyMock(return_value="container-123")
    mock_container.log = AsyncMock(
        return_value=[
            b"PRELOOP_EVIDENCE committed 00000000-0000-0000-0000-000000000001"
        ]
    )
    mock_container.show = AsyncMock(
        return_value={
            "State": {
                "Running": False,
                "Status": "exited",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
            "Name": "/agent",
            "Id": "container-123",
        }
    )
    mock_container.get_archive = AsyncMock(
        side_effect=AssertionError("must not re-fetch leftover files")
    )
    mock_container.start = AsyncMock()
    docker = AsyncMock()
    docker.images.inspect = AsyncMock()
    docker.containers.create = AsyncMock(return_value=mock_container)
    docker.containers.get = AsyncMock(return_value=mock_container)

    with patch("preloop.agents.container.aiodocker.Docker", return_value=docker):
        session = await executor.start(
            {
                "flow_id": str(uuid4()),
                "execution_id": str(uuid4()),
                "prompt": "do work",
                "agent_config": {},
                "evidence_env": {"PRELOOP_EVIDENCE_PUT_TOKEN": "scoped-token"},
            }
        )
        result = await executor.get_result(session)
        assert result.status == AgentStatus.SUCCEEDED
        captured = await executor.get_evidence_archive(session)
        assert captured is None
        assert executor.evidence_transport_error is None

        orchestrator = object.__new__(FlowExecutionOrchestrator)
        orchestrator._evidence_archive = None
        orchestrator._evidence_receipt = None
        orchestrator._evidence_artifact_id = None
        orchestrator.execution_log = SimpleNamespace(
            id=uuid4(),
            status="RUNNING",
            trigger_event_details={},
            evidence_receipt=None,
        )
        orchestrator.flow = SimpleNamespace(id=uuid4(), account_id=uuid4())
        orchestrator.db = Mock()
        orchestrator.execution_logger = Mock()
        await orchestrator._capture_evidence_archive(executor, session)

    assert puts == []
    assert orchestrator._evidence_receipt is not None
    assert orchestrator._evidence_receipt["status"] == "available"
    assert orchestrator._evidence_receipt["artifact_id"] == str(stored_id)
    mock_container.get_archive.assert_not_called()


@pytest.mark.asyncio
async def test_hosted_docker_failed_direct_upload_does_not_store_leftovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    monkeypatch.setattr(
        "preloop.services.flow_artifacts.put_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed upload must not re-store leftover files")
        ),
    )
    monkeypatch.setattr(
        "preloop.models.crud.flow_artifact.latest",
        lambda *args, **kwargs: SimpleNamespace(
            id=uuid4(), ciphertext=b"stale", manifest={"sha256": "b" * 64}
        ),
    )

    executor = ContainerAgentExecutor(
        "codex", {}, image="test-image:latest", use_kubernetes=False
    )
    executor._direct_evidence = True
    mock_container = AsyncMock()
    mock_container.log = AsyncMock(return_value=[b"PRELOOP_EVIDENCE failed OSError"])
    mock_container.get_archive = AsyncMock(
        side_effect=AssertionError("must not re-fetch leftover files")
    )
    docker = AsyncMock()
    docker.containers.get = AsyncMock(return_value=mock_container)

    with patch("preloop.agents.container.aiodocker.Docker", return_value=docker):
        captured = await executor.get_evidence_archive("container-123")
        assert captured is None
        assert executor.evidence_transport_error == "evidence_upload_failed"

        orchestrator = object.__new__(FlowExecutionOrchestrator)
        orchestrator._evidence_archive = None
        orchestrator._evidence_receipt = None
        orchestrator._evidence_artifact_id = None
        orchestrator.execution_log = SimpleNamespace(
            id=uuid4(),
            status="RUNNING",
            trigger_event_details={},
            evidence_receipt=None,
        )
        orchestrator.flow = SimpleNamespace(id=uuid4(), account_id=uuid4())
        orchestrator.db = Mock()
        orchestrator.execution_logger = Mock()
        await orchestrator._capture_evidence_archive(executor, "container-123")

    assert orchestrator._evidence_receipt is not None
    assert orchestrator._evidence_receipt["status"] == "failed"
    mock_container.get_archive.assert_not_called()


class TestEvidencePackManifest:
    """Packs describe themselves: members, inputs, declared source."""

    def test_container_pack_carries_a_verifiable_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PRELOOP_EVIDENCE_MANIFEST", raising=False)
        body = _evidence_archive(tmp_path)
        manifest = verify_pack_manifest(body)
        assert manifest["schema"] == PACK_MANIFEST_SCHEMA
        assert sorted(member["name"] for member in manifest["members"]) == [
            "evidence/findings.json",
            "result.json",
        ]
        assert manifest["inputs"] == []
        assert manifest["source"] == {}

    def test_container_pack_embeds_the_control_plane_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        execution_id = str(uuid4())
        monkeypatch.setenv(
            "PRELOOP_EVIDENCE_MANIFEST",
            json.dumps(
                {
                    "execution_id": execution_id,
                    "inputs": [{"path": "sbom.json", "sha256": "c" * 64}],
                    "source": {"status": "declared", "repositories": []},
                }
            ),
        )
        manifest = verify_pack_manifest(_evidence_archive(tmp_path))
        assert manifest["execution_id"] == execution_id
        assert manifest["inputs"] == [{"path": "sbom.json", "sha256": "c" * 64}]
        assert manifest["source"]["status"] == "declared"

    def test_container_pack_manifest_matches_its_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib

        monkeypatch.delenv("PRELOOP_EVIDENCE_MANIFEST", raising=False)
        payload = b'{"id":"CVE-2026-0001"}'
        manifest = read_pack_manifest(_evidence_archive(tmp_path, payload))
        findings = next(
            member
            for member in manifest["members"]
            if member["name"] == "evidence/findings.json"
        )
        assert findings["sha256"] == hashlib.sha256(payload).hexdigest()
        assert findings["size_bytes"] == len(payload)

    def test_transport_env_delivers_seed_digests_not_seed_contents(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64
        import hashlib

        monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
        content = b'{"bomFormat":"CycloneDX"}'
        execution_id = str(uuid4())
        env = evidence_transport_env(
            {
                "account_id": str(uuid4()),
                "flow_id": str(uuid4()),
                "execution_id": execution_id,
                "trigger_event_data": {
                    "payload": {
                        "workspace_files": [
                            {
                                "path": "sbom.json",
                                "content_base64": base64.b64encode(content).decode(),
                            }
                        ]
                    }
                },
            }
        )
        context = json.loads(env["PRELOOP_EVIDENCE_MANIFEST"])
        assert context["execution_id"] == execution_id
        assert context["inputs"] == [
            {
                "path": "sbom.json",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
        assert (
            base64.b64encode(content).decode() not in env["PRELOOP_EVIDENCE_MANIFEST"]
        )

    def test_a_pack_missing_its_manifest_member_is_not_silently_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dropping a listed member is a verification failure, not a shorter pack."""
        import io
        import tarfile

        monkeypatch.delenv("PRELOOP_EVIDENCE_MANIFEST", raising=False)
        body = _evidence_archive(tmp_path)
        buf = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as src,
            tarfile.open(fileobj=buf, mode="w:gz") as out,
        ):
            for member in src.getmembers():
                if member.name == "evidence/findings.json":
                    continue
                out.addfile(member, src.extractfile(member))
        with pytest.raises(EvidencePackError, match="not in the archive"):
            verify_pack_manifest(buf.getvalue())

    def test_manifest_member_name_is_at_the_archive_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io
        import tarfile

        monkeypatch.delenv("PRELOOP_EVIDENCE_MANIFEST", raising=False)
        with tarfile.open(
            fileobj=io.BytesIO(_evidence_archive(tmp_path)), mode="r:gz"
        ) as tar:
            assert PACK_MANIFEST_NAME in tar.getnames()


def test_legacy_status_verifies_the_bytes_it_already_has() -> None:
    """A pack whose integrity verifies three ways must not report unverified.

    Round 2's evidence-status said integrity_verified: false, manifest_sha256:
    null on both runs, while the download of the same pack returned
    x-preloop-evidence-integrity: verified and a matching sha256 (P8).
    """
    archive = b"legacy evidence pack"
    digest = hashlib.sha256(archive).hexdigest()
    execution = SimpleNamespace(
        id=uuid4(),
        evidence_receipt={
            "status": "available",
            "kind": "evidence",
            "transport": "legacy",
        },
        evidence_archive=archive,
    )

    status = public_evidence_status(execution)

    assert status["status"] == "available"
    assert status["integrity_verified"] is True
    assert status["integrity"] == "verified"
    assert status["sha256"] == digest
    assert status["digest"] == digest


def test_legacy_status_without_a_receipt_still_verifies() -> None:
    archive = b"legacy evidence pack"
    execution = SimpleNamespace(
        id=uuid4(), evidence_receipt=None, evidence_archive=archive
    )

    status = public_evidence_status(execution)

    assert status["integrity"] == "verified"
    assert status["sha256"] == hashlib.sha256(archive).hexdigest()


def test_legacy_status_reports_a_real_mismatch_as_failed() -> None:
    """Verified and unchecked are not the only two outcomes."""
    execution = SimpleNamespace(
        id=uuid4(),
        evidence_receipt={
            "status": "available",
            "kind": "evidence",
            "transport": "legacy",
            "sha256": "0" * 64,
        },
        evidence_archive=b"legacy evidence pack",
    )

    status = public_evidence_status(execution)

    assert status["status"] == "failed"
    assert status["integrity"] == "failed"
    assert status["integrity_verified"] is False
    assert status["error"] == "artifact_digest_mismatch"


def test_direct_status_says_not_checked_rather_than_unverified() -> None:
    """No bytes were read, so no claim is made about them either way."""
    execution = SimpleNamespace(
        id=uuid4(),
        evidence_receipt={
            "status": "available",
            "kind": "evidence",
            "transport": "direct",
            "sha256": "a" * 64,
        },
        evidence_archive=None,
    )

    status = public_evidence_status(execution)

    assert status["integrity_verified"] is False
    assert status["integrity"] == "not_checked"
    assert "verified on download" in status["integrity_note"]
    assert status["sha256"] == "a" * 64
