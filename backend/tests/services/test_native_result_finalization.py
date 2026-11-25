"""A finished native review is not lost to a missing field or to the clock.

Orchestrator-level: a fake agent executor stands in for the container, the
monitor and the finalize gates (result schema, review integrity with its
inspection floor) are the real ones. No database or model provider is used.
"""

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.agents.codex import NATIVE_RESULT_REPAIR_CONTINUATIONS
from preloop.services.flow_orchestrator import (
    ExecutionDeadline,
    FlowExecutionOrchestrator,
    TimeoutBudget,
    _utcnow,
)
from tests.agents.test_native_result_repair import (
    CONDITIONAL_SCHEMA,
    SCHEMA,
    _blocks,
    _context,
    _markers,
    _run,
)
from tests.agents.test_native_result_repair import review as make_review

HEAD = "a" * 40
BASE = "b" * 40


def orchestrator() -> FlowExecutionOrchestrator:
    """A restricted-CI read-only native review with isolated persistence."""
    orch = FlowExecutionOrchestrator(MagicMock(), "flow", {}, MagicMock())
    orch.flow = SimpleNamespace(
        result_schema=SCHEMA,
        validate_result=True,
        git_clone_config={"publication_mode": "read_only"},
        agent_config={"native_result": True},
        agent_type="codex",
        prompt_template="Review",
        timeout_seconds=300,
    )
    orch.execution_log = SimpleNamespace(
        id="execution",
        ci_review_binding={"head_sha": HEAD, "base_sha": BASE},
        startup_receipt=None,
    )
    orch._native_result_effective = True
    orch._agent_exec_started = True
    orch._persist_cra_result_boundary = lambda value, **kwargs: value
    orch._store_captured_result = MagicMock()
    orch._capture_evidence_archive = AsyncMock()
    orch._capture_workspace_snapshot = AsyncMock()
    orch._record_completion = MagicMock()
    orch.execution_logger = MagicMock()
    return orch


def executor(artifact: Any, status: AgentStatus) -> AsyncMock:
    """A fake container that reports ``status`` and holds ``artifact``."""
    fake = AsyncMock()
    fake.get_status = AsyncMock(return_value=status)
    fake.get_result = AsyncMock(
        return_value=AgentExecutionResult(
            status=AgentStatus.SUCCEEDED,
            session_reference="native-session",
            output_summary="agent finished",
            exit_code=0,
        )
    )
    fake.get_result_artifact = AsyncMock(
        return_value={
            "_preloop_native_result_candidates_v1": {"native": artifact, "file": None}
        }
    )

    async def no_logs(session_reference: str) -> Any:
        if False:  # pragma: no cover - makes this an async generator
            yield

    fake.stream_logs = no_logs
    return fake


async def finalize(
    orch: FlowExecutionOrchestrator, fake: AsyncMock, *, inspected: bool = True
) -> tuple[str, dict[str, Any]]:
    """Monitor the fake agent, then apply the finalize gates in order."""
    with (
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution.get_stop_request",
            return_value=None,
        ),
        patch(
            "preloop.models.crud.execution_tool_evidence.inspection_summary",
            return_value={"diff_commands": int(inspected)},
        ),
        patch.object(orch, "_listen_for_commands", AsyncMock()),
        patch.object(orch, "_stream_logs_to_nats", AsyncMock()),
        patch.object(orch, "_publish_update", AsyncMock()),
        patch.object(orch, "_park_if_requested", AsyncMock(return_value=None)),
        patch.object(orch, "_check_no_progress", AsyncMock(return_value=False)),
        patch.object(
            orch, "_sync_runtime_tool_activity_metrics", AsyncMock(return_value=None)
        ),
        patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
    ):
        result = await orch._monitor_agent_execution("native-session", fake)
        status, result = orch._apply_result_schema_gate(result["status"], result)
        status, result, _ = orch._apply_review_integrity_gate(
            status, result, result.get("result")
        )
    return status, result


# --- defect 1: invalid then valid answer -----------------------------------


@pytest.mark.asyncio
async def test_repaired_answer_finalizes_as_a_review(tmp_path: Path) -> None:
    calls, answer, _ = _run(
        tmp_path, _blocks(_context()), [make_review(findings=...), make_review()]
    )
    assert calls == 2
    document = json.loads(answer.read_text())
    # The fixture repository's SHAs differ from the bound ones; rebind.
    document.update(head_sha=HEAD, base_sha=BASE)
    orch = orchestrator()
    status, result = await finalize(orch, executor(document, AgentStatus.SUCCEEDED))
    assert status == "SUCCEEDED", result.get("error_message")
    assert orch._result_schema_validation["valid"] is True
    assert result["result"]["findings"]


@pytest.mark.asyncio
async def test_answer_still_invalid_after_repairs_fails_as_before(
    tmp_path: Path,
) -> None:
    calls, answer, _ = _run(tmp_path, _blocks(_context()), [make_review(findings=...)])
    assert calls == 1 + NATIVE_RESULT_REPAIR_CONTINUATIONS
    document = json.loads(answer.read_text())
    document.update(head_sha=HEAD, base_sha=BASE)
    orch = orchestrator()
    status, result = await finalize(orch, executor(document, AgentStatus.SUCCEEDED))
    assert status == "FAILED"
    assert result["failure_category"] == "result_schema_invalid"
    assert "$.findings" in result["error_message"]
    assert orch._result_schema_validation["errors"][0]["path"] == "$.findings"


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [True, False], ids=["repaired", "exhausted"])
async def test_conditional_review_finalizes_after_repair(
    tmp_path: Path, corrected: bool
) -> None:
    """Only a model-written correction can pass the final schema gate."""
    answers = [make_review(success=True)]
    if corrected:
        answers.append(make_review(success=False))
    calls, answer, log = _run(
        tmp_path,
        _blocks(_context(result_schema=CONDITIONAL_SCHEMA)),
        answers,
        result_schema=CONDITIONAL_SCHEMA,
    )
    document = json.loads(answer.read_text())
    document.update(head_sha=HEAD, base_sha=BASE)
    orch = orchestrator()
    orch.flow.result_schema = CONDITIONAL_SCHEMA
    status, result = await finalize(orch, executor(document, AgentStatus.SUCCEEDED))
    assert calls == (2 if corrected else 3)
    assert all(marker["paths"] == ["$.success"] for marker in _markers(log))
    assert result["result"]["success"] is (not corrected)
    if corrected:
        assert status == "SUCCEEDED", result.get("error_message")
        assert orch._result_schema_validation["valid"] is True
    else:
        assert status == "FAILED"
        assert result["failure_category"] == "result_schema_invalid"
        assert "$.success: False was expected" in result["error_message"]
        assert orch._result_schema_validation["errors"] == [
            {"path": "$.success", "message": "False was expected"}
        ]


# --- defect 2: valid answer, then the time limit ---------------------------


def expired(orch: FlowExecutionOrchestrator) -> None:
    """Put the execution past its 300 s budget before the first poll."""
    budget = TimeoutBudget(seconds=300, source="flow")
    orch._execution_timeout_budget = lambda: budget  # type: ignore[method-assign]
    orch._deadline = ExecutionDeadline(
        anchor=_utcnow() - timedelta(seconds=305), budget_seconds=300
    )


@pytest.mark.asyncio
async def test_ready_review_at_the_time_limit_finalizes_as_a_review() -> None:
    orch = orchestrator()
    expired(orch)
    fake = executor(make_review(head_sha=HEAD, base_sha=BASE), AgentStatus.RUNNING)
    status, result = await finalize(orch, fake)
    assert status == "SUCCEEDED", result.get("error_message")
    assert result["error_message"] is None
    assert result["result"]["verdict"] == "changes_requested"
    orch._record_completion.assert_any_call(time_limit_after_result=True)
    milestones = [c.args[0] for c in orch.execution_logger.log_milestone.mock_calls]
    assert "agent_execution_timeout" in milestones
    assert "native_result_ready_at_time_limit" in milestones
    fake.stop.assert_awaited()


@pytest.mark.parametrize(
    "document,inspected",
    [
        (make_review(head_sha=HEAD, base_sha=BASE, findings=...), True),
        (make_review(head_sha=HEAD, base_sha=HEAD), True),
        (make_review(head_sha=HEAD, base_sha=BASE, review="..."), True),
        (make_review(head_sha=HEAD, base_sha=BASE), False),
        (None, True),
    ],
    ids=["schema", "binding", "degenerate", "not-inspected", "no-result"],
)
@pytest.mark.asyncio
async def test_time_limit_keeps_timeout_when_any_check_fails(
    document: Any, inspected: bool
) -> None:
    orch = orchestrator()
    expired(orch)
    status, result = await finalize(
        orch, executor(document, AgentStatus.RUNNING), inspected=inspected
    )
    assert status == "FAILED"
    assert "timed out" in result["error_message"].lower() or "timeout" in (
        result["error_message"].lower()
    )
    milestones = [c.args[0] for c in orch.execution_logger.log_milestone.mock_calls]
    assert "native_result_ready_at_time_limit" not in milestones


@pytest.mark.asyncio
async def test_time_limit_without_native_results_is_unchanged() -> None:
    orch = orchestrator()
    orch._native_result_effective = False
    orch.flow.agent_config = {}
    expired(orch)
    status, result = await finalize(
        orch, executor(make_review(head_sha=HEAD, base_sha=BASE), AgentStatus.RUNNING)
    )
    assert status == "FAILED"
    assert result["error_message"]


# --- a review that ends on a progress note ---------------------------------

#: Valid JSON, none of the schema's required properties, no ``status``.
PROGRESS_NOTE: dict[str, Any] = {"": {"Content": "Fetching the diff before review."}}


def codex(orch: FlowExecutionOrchestrator) -> FlowExecutionOrchestrator:
    """Make ``orch`` a Codex run that called tools, so the hint applies."""
    orch.agent_type = "codex"
    orch.tool_calls_count = 3
    return orch


def bundle(native: Any, **extra: Any) -> dict[str, Any]:
    """The capture transport for a native answer."""
    return {
        "_preloop_native_result_candidates_v1": {
            "native": native,
            "file": None,
            **extra,
        }
    }


@pytest.mark.asyncio
async def test_combined_continuation_review_finalizes_as_a_review(
    tmp_path: Path,
) -> None:
    calls, answer, _ = _run(
        tmp_path,
        _blocks(_context()),
        [make_review(), make_review(), PROGRESS_NOTE, make_review()],
        inspect_from=4,
    )
    assert calls == 4
    document = json.loads(answer.read_text())
    document.update(head_sha=HEAD, base_sha=BASE)
    orch = orchestrator()
    status, result = await finalize(orch, executor(document, AgentStatus.SUCCEEDED))
    assert status == "SUCCEEDED", result.get("error_message")


@pytest.mark.asyncio
async def test_progress_note_is_schema_invalid_not_unconfirmed(
    tmp_path: Path,
) -> None:
    calls, answer, _ = _run(
        tmp_path, _blocks(_context()), [PROGRESS_NOTE], inspect_from=4
    )
    assert calls == 6
    orch = codex(orchestrator())
    status, result = await finalize(
        orch, executor(json.loads(answer.read_text()), AgentStatus.SUCCEEDED)
    )
    assert status == "FAILED"
    assert result["failure_category"] == "result_schema_invalid"
    message = result["error_message"]
    assert message.startswith("Result document does not match the flow result schema")
    assert "did not confirm" not in message
    assert "$.findings" in message and "$.verdict" in message
    paths = {error["path"] for error in orch._result_schema_validation["errors"]}
    assert {"$.findings", "$.verdict", "$.review"} <= paths
    assert result["result"] == PROGRESS_NOTE


@pytest.mark.asyncio
async def test_uninspected_progress_note_fails_as_not_inspected(
    tmp_path: Path,
) -> None:
    calls, answer, _ = _run(
        tmp_path, _blocks(_context()), [PROGRESS_NOTE], inspect_from=99
    )
    assert calls == 4
    orch = codex(orchestrator())
    status, result = await finalize(
        orch,
        executor(json.loads(answer.read_text()), AgentStatus.SUCCEEDED),
        inspected=False,
    )
    # The repaired classification does not bypass the inspection evidence.
    assert status == "FAILED"
    assert result["failure_category"] == "review_not_inspected"


@pytest.mark.asyncio
async def test_uninspected_valid_answer_still_fails_as_not_inspected() -> None:
    orch = codex(orchestrator())
    fake = executor(make_review(head_sha=HEAD, base_sha=BASE), AgentStatus.SUCCEEDED)
    status, result = await finalize(orch, fake, inspected=False)
    assert status == "FAILED"
    assert result["failure_category"] == "review_not_inspected"


@pytest.mark.parametrize(
    "artifact,recorded",
    [
        (bundle(None, native_error="not_json"), "The final answer was not valid JSON"),
        (bundle(["a progress note"]), "The final answer was not a JSON object"),
    ],
    ids=["not-json", "not-object"],
)
@pytest.mark.asyncio
async def test_written_answer_that_is_not_json_is_schema_invalid(
    artifact: Any, recorded: str
) -> None:
    """An answer exists but is unusable: a schema failure, not a missing one."""
    orch = codex(orchestrator())
    fake = executor(None, AgentStatus.SUCCEEDED)
    fake.get_result_artifact = AsyncMock(return_value=artifact)
    nudge = AsyncMock(return_value={"outcome": "skipped"})
    with patch.object(orch, "_run_confirmation_nudge", nudge):
        status, result = await finalize(orch, fake)
    assert status == "FAILED"
    assert result["failure_category"] == "result_schema_invalid"
    assert result["error_message"] == (
        f"Result document does not match the flow result schema: $: {recorded}"
    )
    assert orch._result_schema_validation["errors"] == [
        {"path": "$", "message": recorded}
    ]
    nudge.assert_not_called()


@pytest.mark.asyncio
async def test_fenced_answer_is_unwrapped_and_succeeds(tmp_path: Path) -> None:
    from preloop.services.native_result import (
        NATIVE_RESULT_BUNDLE_PATH,
        native_result_capture_script,
    )

    fenced = (
        "```json\n" + json.dumps(make_review(head_sha=HEAD, base_sha=BASE)) + "\n```"
    )
    (tmp_path / ".preloop-native-result.json").write_text(fenced)
    exec(native_result_capture_script().replace("/workspace", str(tmp_path)), {})
    captured = json.loads((tmp_path / Path(NATIVE_RESULT_BUNDLE_PATH).name).read_text())
    orch = codex(orchestrator())
    fake = executor(None, AgentStatus.SUCCEEDED)
    fake.get_result_artifact = AsyncMock(return_value=captured)
    status, result = await finalize(orch, fake)
    assert status == "SUCCEEDED", result.get("error_message")
    assert orch._native_answer_unwrapped == "markdown_fence"
    orch.execution_logger.log_milestone.assert_any_call(
        "native_answer_unwrapped", {"wrapper": "markdown_fence"}
    )


@pytest.mark.parametrize(
    "artifact,observed,recorded",
    [(None, "no final answer was captured", "No final answer was captured")],
    ids=["none"],
)
@pytest.mark.asyncio
async def test_unconfirmed_run_says_what_was_observed(
    artifact: Any, observed: str, recorded: str
) -> None:
    orch = codex(orchestrator())
    fake = executor(None, AgentStatus.SUCCEEDED)
    fake.get_result_artifact = AsyncMock(return_value=artifact)
    with (
        patch.object(orch, "_refetch_exited_session_logs", AsyncMock(return_value=[])),
        patch.object(
            orch,
            "_run_confirmation_nudge",
            AsyncMock(return_value={"outcome": "skipped"}),
        ),
    ):
        status, result = await finalize(orch, fake)
    assert status == "FAILED"
    message = result["error_message"]
    assert "did not confirm success" in message
    assert observed in message
    assert "and did not" not in message
    assert "result.json against" not in message
    assert orch._result_schema_validation["errors"] == [
        {"path": "$", "message": recorded}
    ]


def test_hint_names_the_failing_paths_of_a_schema_invalid_answer() -> None:
    orch = codex(orchestrator())
    orch._check_result_schema(PROGRESS_NOTE)
    hint = orch._native_result_hint()
    assert "native_result was on" in hint
    assert "did not match the flow result schema" in hint
    assert "$.findings" in hint


@pytest.mark.parametrize(
    "error,provider",
    [
        ("Provider returned HTTP 401: invalid api key", True),
        (
            "unexpected status 400 Bad Request: unsupported parameter: response_format",
            True,
        ),
        ("Agent container was killed: out of memory", False),
    ],
)
@pytest.mark.asyncio
async def test_failed_model_request_is_not_reported_as_a_missing_file(
    error: str, provider: bool
) -> None:
    orch = codex(orchestrator())
    fake = executor(None, AgentStatus.FAILED)
    fake.get_result_artifact = AsyncMock(return_value=None)
    fake.get_result = AsyncMock(
        return_value=AgentExecutionResult(
            status=AgentStatus.FAILED,
            session_reference="native-session",
            output_summary="agent failed",
            error_message=error,
            exit_code=1,
        )
    )
    status, result = await finalize(orch, fake)
    assert status == "FAILED"
    assert result["error_message"] == error
    (recorded,) = orch._result_schema_validation["errors"]
    assert "result.json" not in recorded["message"]
    if provider:
        assert "model request failed" in recorded["message"]
        assert "provider error" in recorded["message"]
    else:
        assert recorded["message"] == "No final answer was captured"


@pytest.mark.asyncio
async def test_missing_file_wording_is_unchanged_without_native_results() -> None:
    orch = orchestrator()
    orch._native_result_effective = False
    orch.flow.agent_config = {}
    fake = executor(None, AgentStatus.FAILED)
    fake.get_result_artifact = AsyncMock(return_value=None)
    await finalize(orch, fake)
    assert orch._result_schema_validation["errors"] == [
        {"path": "$", "message": "No result.json was captured"}
    ]


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("Fetching the full diff before review.", {"native": None, "file": None}),
        ("  \n", None),
        (None, None),
    ],
    ids=["text", "blank", "absent"],
)
def test_capture_reports_an_answer_that_is_not_json(
    tmp_path: Path, answer: Any, expected: Any
) -> None:
    from preloop.services.native_result import (
        NATIVE_CANDIDATES_KEY,
        NATIVE_RESULT_BUNDLE_PATH,
        native_result_capture_script,
    )

    if answer is not None:
        (tmp_path / ".preloop-native-result.json").write_text(answer)
    exec(native_result_capture_script().replace("/workspace", str(tmp_path)), {})
    written = tmp_path / Path(NATIVE_RESULT_BUNDLE_PATH).name
    if expected is None:
        # Nothing to report: the capture stays as it was before.
        assert not written.exists()
        return
    candidates = json.loads(written.read_text())[NATIVE_CANDIDATES_KEY]
    # The reason travels, the text of the answer does not.
    assert candidates == {**expected, "native_error": "not_json"}


def test_an_earlier_capture_does_not_decide_the_wording_of_a_later_one() -> None:
    orch = codex(orchestrator())
    assert orch._select_native_result_candidate(bundle(["note"])) == ["note"]
    assert orch._native_answer_error == "not_object"
    assert orch._select_native_result_candidate(None) is None
    orch._check_result_schema(None)
    assert orch._result_schema_validation["errors"] == [
        {"path": "$", "message": "No final answer was captured"}
    ]
