"""Native answers and legacy file results have deterministic, checked precedence."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiodocker.exceptions import DockerError

from preloop.agents.codex import CodexAgent
from preloop.services.flow_orchestrator import (
    FlowExecutionOrchestrator,
    _success_instruction_for_prompt,
)
from preloop.services.native_result import NATIVE_RESULT_BUNDLE_PATH

HEAD = "a" * 40
BASE = "b" * 40
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["review", "head_sha", "base_sha"],
    "properties": {
        key: {"type": "string"} for key in ("review", "head_sha", "base_sha")
    },
}


def document(review: str = "No issues found") -> dict[str, str]:
    """Return a minimal legitimate review."""
    return {"review": review, "head_sha": HEAD, "base_sha": BASE}


def orchestrator() -> FlowExecutionOrchestrator:
    """Build an admitted native review with isolated persistence boundaries."""
    orch = FlowExecutionOrchestrator(MagicMock(), "flow", {}, MagicMock())
    orch.flow = SimpleNamespace(
        result_schema=SCHEMA,
        validate_result=True,
        git_clone_config={"publication_mode": "read_only"},
        agent_config={"native_result": True},
        prompt_template="Review",
    )
    orch.execution_log = SimpleNamespace(
        id="execution",
        ci_review_binding={"head_sha": HEAD, "base_sha": BASE},
        startup_receipt=None,
    )
    orch._native_result_effective = True
    orch._persist_cra_result_boundary = lambda value, **kwargs: value
    orch._store_captured_result = MagicMock()
    orch._capture_evidence_archive = AsyncMock()
    orch._capture_workspace_snapshot = AsyncMock()
    return orch


def test_native_completion_requires_only_final_answer() -> None:
    instruction = _success_instruction_for_prompt("Review", native_result=True)
    assert "final answer" in instruction
    assert "ends the run" in instruction
    assert "/workspace/result.json" not in instruction
    assert "FLOW_EXECUTION_SUCCESS" not in instruction
    assert "FLOW_EXECUTION_SUCCESS" in _success_instruction_for_prompt("Review")


@pytest.mark.parametrize("native_first", [True, False])
def test_native_capture_preserves_both_write_orders(
    tmp_path: Path, native_first: bool
) -> None:
    from preloop.services.native_result import NATIVE_RESULT_PATH, NATIVE_CANDIDATES_KEY

    file_path = tmp_path / "result.json"
    native_path = tmp_path / "native.json"
    candidates = [
        (native_path, document("Native review completed")),
        (file_path, document("File review completed")),
    ]
    for path, value in candidates if native_first else reversed(candidates):
        path.write_text(json.dumps(value))
    script = CodexAgent._native_result_capture_block(
        {
            "agent_config": {"native_result": True},
            "git_clone_config": {"publication_mode": "read_only"},
        }
    )
    script = (
        script.replace(NATIVE_RESULT_PATH, str(native_path))
        .replace("/workspace/result.json", str(file_path))
        .replace("/workspace", str(tmp_path))
    )
    subprocess.run(["bash", "-c", script], check=True, capture_output=True)
    bundle_path = tmp_path / Path(NATIVE_RESULT_BUNDLE_PATH).name
    bundle = json.loads(bundle_path.read_text())[NATIVE_CANDIDATES_KEY]
    assert bundle["native"] == candidates[0][1]
    assert bundle["file"] == candidates[1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native,expected",
    [
        (document("Native review completed"), "Native review completed"),
        (None, "File review completed"),
        ({"review": "partial"}, "File review completed"),
        (document("..."), "File review completed"),
        ({**document(), "base_sha": HEAD}, "File review completed"),
    ],
)
async def test_capture_chooses_usable_native_then_verified_file(
    native: Any, expected: str
) -> None:
    orch = orchestrator()
    executor = AsyncMock()
    executor.get_result_artifact.return_value = {
        "_preloop_native_result_candidates_v1": {
            "native": native,
            "file": document("File review completed"),
        }
    }
    with patch(
        "preloop.models.crud.execution_tool_evidence.inspection_summary",
        return_value={"diff_commands": 1},
    ):
        captured = await orch._capture_result_artifact(executor, "session")
    assert captured["review"] == expected
    assert orch._result_schema_validation["valid"] is True


@pytest.mark.parametrize(
    "file_result,inspected",
    [
        (document("..."), True),
        ({**document(), "base_sha": HEAD}, True),
        (document(), False),
    ],
)
def test_file_safety_net_cannot_bypass_integrity(
    file_result: dict, inspected: bool
) -> None:
    orch = orchestrator()
    with patch(
        "preloop.models.crud.execution_tool_evidence.inspection_summary",
        return_value={"diff_commands": int(inspected)},
    ):
        assert not orch._usable_native_review(file_result)


@pytest.mark.asyncio
@pytest.mark.parametrize("usable", [True, False])
async def test_review_written_before_tool_loop_is_checked_and_recovered(
    usable: bool,
) -> None:
    from preloop.agents.base import AgentStatus

    orch = orchestrator()
    orch.execution_log.id = "test-execution-id"
    executor = AsyncMock()
    executor.get_status.return_value = AgentStatus.RUNNING
    executor.get_result_artifact.return_value = (
        document() if usable else document("...")
    )
    detection = {
        "pattern_length": 1,
        "repetitions": 20,
        "pattern": [{"server_name": "codex_cli", "tool_name": "Bash"}],
    }
    with (
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution.get_stop_request",
            return_value=None,
        ),
        patch(
            "preloop.models.crud.execution_tool_evidence.inspection_summary",
            return_value={"diff_commands": 1},
        ),
        patch.object(orch, "_listen_for_commands", AsyncMock()),
        patch.object(orch, "_stream_logs_to_nats", AsyncMock()),
        patch.object(orch, "_publish_update", AsyncMock()),
        patch.object(orch, "_check_no_progress", AsyncMock(return_value=False)),
        patch.object(
            orch,
            "_sync_runtime_tool_activity_metrics",
            AsyncMock(return_value=detection),
        ),
    ):
        result = await orch._monitor_agent_execution("native-session", executor)
    assert result["status"] == ("SUCCEEDED" if usable else "FAILED")
    assert result["failure_category"] == (None if usable else "agent_tool_loop")
    if not usable:
        assert "20" in result["error_message"]
    assert result["result"] == executor.get_result_artifact.return_value
    executor.stop.assert_awaited_once_with("native-session")


def test_explicit_native_failure_cannot_be_replaced_by_successful_file() -> None:
    orch = orchestrator()
    orch.flow.result_schema = {
        **SCHEMA,
        "properties": {**SCHEMA["properties"], "status": {"type": "string"}},
    }
    failed = {**document("Review could not complete"), "status": "failure"}
    with patch(
        "preloop.models.crud.execution_tool_evidence.inspection_summary",
        return_value={"diff_commands": 1},
    ):
        assert (
            orch._select_native_result_candidate(
                {
                    "_preloop_native_result_candidates_v1": {
                        "native": failed,
                        "file": document(),
                    }
                }
            )
            == failed
        )


def test_candidate_transport_preserves_each_existing_artifact_allowance(
    tmp_path: Path,
) -> None:
    from preloop.services.native_result import NATIVE_RESULT_PATH, NATIVE_CANDIDATES_KEY
    from preloop.services.flow_artifacts import extract_result_json
    from preloop.agents.checkpoint_client import pack_evidence

    result = tmp_path / "result.json"
    native = tmp_path / "native.json"
    native.write_text(json.dumps(document("Native " + "x" * 200000)))
    result.write_text(json.dumps(document("File " + "x" * 200000)))
    script = CodexAgent._native_result_capture_block(
        {
            "agent_config": {"native_result": True},
            "git_clone_config": {"publication_mode": "read_only"},
        }
    )
    script = (
        script.replace(NATIVE_RESULT_PATH, str(native))
        .replace("/workspace/result.json", str(result))
        .replace("/workspace", str(tmp_path))
    )
    subprocess.run(["bash", "-c", script], check=True, capture_output=True)
    raw = (tmp_path / Path(NATIVE_RESULT_BUNDLE_PATH).name).read_bytes()
    assert len(raw) > 256 * 1024
    executor = CodexAgent({})
    captured = executor._interpret_result_artifact_bytes(raw, "session")
    assert set(captured[NATIVE_CANDIDATES_KEY]) == {"native", "file"}
    archive = pack_evidence(
        tmp_path, max_bytes=1024 * 1024, max_expanded_bytes=1024 * 1024
    )
    assert extract_result_json(archive) == captured
    assert (
        executor._interpret_result_artifact_bytes(
            json.dumps(document("x" * 300000)).encode(), "session"
        )["error"]
        == "result_artifact_too_large"
    )


@pytest.mark.parametrize("oversized", ["native", "file"])
def test_native_capture_rejects_oversized_candidate_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oversized: str
) -> None:
    """Neither native answers nor fallback files may bypass the document cap."""
    from preloop.services.native_result import (
        NATIVE_CANDIDATES_KEY,
        NATIVE_RESULT_CAPTURE,
        RESULT_DOCUMENT_MAX_BYTES,
    )

    native = tmp_path / ".preloop-native-result.json"
    result = tmp_path / "result.json"
    native.write_text(json.dumps(document("...")))
    result.write_text(json.dumps(document()))
    path = native if oversized == "native" else result
    path.write_text(json.dumps(document("x" * RESULT_DOCUMENT_MAX_BYTES)))
    fdopen = os.fdopen

    def guarded_fdopen(fd: int, mode: str, *args: Any, **kwargs: Any) -> Any:
        """Fail on any attempt to read a known oversized input."""
        source = fdopen(fd, mode, *args, **kwargs)
        if "r" not in mode or os.fstat(fd).st_size <= RESULT_DOCUMENT_MAX_BYTES:
            return source
        guarded = MagicMock(wraps=source)
        guarded.__enter__.return_value = guarded
        guarded.__exit__.side_effect = source.__exit__
        guarded.read.side_effect = AssertionError(
            "must not read an oversized candidate"
        )
        return guarded

    monkeypatch.setattr(os, "fdopen", guarded_fdopen)
    exec(NATIVE_RESULT_CAPTURE.replace("/workspace", str(tmp_path)), {})
    bundle = json.loads((tmp_path / Path(NATIVE_RESULT_BUNDLE_PATH).name).read_text())
    assert bundle[NATIVE_CANDIDATES_KEY][oversized] == {
        "error": "result_artifact_too_large"
    }
    orch = orchestrator()
    with patch(
        "preloop.models.crud.execution_tool_evidence.inspection_summary",
        return_value={"diff_commands": 1},
    ):
        selected = orch._select_native_result_candidate(bundle)
        assert orch._usable_native_review(selected) is (oversized == "native")


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle_available", [True, False])
async def test_docker_native_capture_selects_bounded_transport(
    bundle_available: bool,
) -> None:
    """Docker prefers the bundle and retains the file cap when it is absent."""
    from preloop.services.native_result import (
        NATIVE_CANDIDATES_KEY,
        RESULT_DOCUMENT_MAX_BYTES,
    )
    from backend.tests.agents.test_agent_container import _result_artifact_tar

    bundle = {
        NATIVE_CANDIDATES_KEY: {
            "native": document("Native " + "x" * 200000),
            "file": document("File " + "x" * 200000),
        }
    }
    raw = json.dumps(bundle if bundle_available else document("x" * 300000)).encode()
    archive = _result_artifact_tar(raw)
    container = AsyncMock()
    container.get_archive.side_effect = (
        [archive]
        if bundle_available
        else [DockerError(404, {"message": "missing bundle"}), archive]
    )
    docker = AsyncMock()
    docker.containers.get.return_value = container
    executor = CodexAgent({})
    executor.use_kubernetes = False
    executor._get_docker_client = AsyncMock(return_value=docker)
    with patch.object(archive, "extractfile", wraps=archive.extractfile) as extract:
        captured = await executor.get_result_artifact("session")
        if bundle_available:
            assert captured == bundle
            container.get_archive.assert_awaited_once_with(NATIVE_RESULT_BUNDLE_PATH)
        else:
            assert captured["error"] == "result_artifact_too_large"
            assert captured["limit_bytes"] == RESULT_DOCUMENT_MAX_BYTES
            extract.assert_not_called()
            assert [call.args[0] for call in container.get_archive.await_args_list] == [
                NATIVE_RESULT_BUNDLE_PATH,
                "/workspace/result.json",
            ]


@pytest.mark.parametrize("link", ["temporary", "native", "file", "bundle"])
def test_capture_never_follows_agent_planted_links(tmp_path: Path, link: str) -> None:
    from preloop.services.native_result import NATIVE_RESULT_PATH

    native, result = tmp_path / "native.json", tmp_path / "result.json"
    bundle_path = tmp_path / Path(NATIVE_RESULT_BUNDLE_PATH).name
    secret = tmp_path / "protected.json"
    protected = json.dumps(document("Private unrelated contents"))
    secret.write_text(protected)
    native.write_text(json.dumps(document("Native review completed")))
    result.write_text(json.dumps(document("File review completed")))
    target = {
        "temporary": result.with_suffix(".capture.tmp"),
        "native": native,
        "file": result,
        "bundle": bundle_path,
    }[link]
    target.unlink(missing_ok=True)
    target.symlink_to(secret)
    script = CodexAgent._native_result_capture_block(
        {
            "agent_config": {"native_result": True},
            "git_clone_config": {"publication_mode": "read_only"},
        }
    )
    script = (
        script.replace(NATIVE_RESULT_PATH, str(native))
        .replace("/workspace/result.json", str(result))
        .replace("/workspace", str(tmp_path))
    )
    subprocess.run(["bash", "-c", script], check=True, capture_output=True)
    assert secret.read_text() == protected
    if link == "native":
        assert not bundle_path.exists()
    else:
        assert "Private unrelated contents" not in bundle_path.read_text()


def test_capture_runs_only_after_workspace_reclamation() -> None:
    with patch.object(CodexAgent, "_prepare_init_commands", return_value=""):
        script = CodexAgent({})._build_codex_script(
            {
                "agent_config": {"native_result": True},
                "git_clone_config": {"publication_mode": "read_only"},
                "result_schema": SCHEMA,
                "prompt": "Review",
            }
        )
    assert script.rindex("Take the workspace back from the agent user") < script.index(
        "native_path = Path("
    )
