"""Hosted read-only review startup receipt and restricted result projection."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.agents.codex import CodexAgent
from preloop.api.endpoints.flows import _ci_schema_validation
from preloop.services.ci_execution import ci_result_withheld_paths, public_ci_result
from preloop.services.flow_orchestrator import (
    AGENT_EXEC_START_MARKER,
    FlowExecutionOrchestrator,
)
from preloop.services.read_only_review import (
    HOST_READ_ONLY_RESULT_KEY,
    public_startup_receipt,
    read_only_startup_line,
)
from preloop.utils.workspace_snapshot import SETUP_FAILED_MARKER

SHA = "a" * 40
URL = "https://github.com/example/project.git"


def _review_context() -> dict[str, Any]:
    return {
        "read_only_review": True,
        "git_clone_config": {
            "enabled": True,
            "publication_mode": "read_only",
            "repositories": [
                {
                    "project_id": str(uuid4()),
                    "tracker_id": str(uuid4()),
                    "repository_url": URL,
                    "branch": "main",
                    "commit": SHA,
                }
            ],
            "source_branch": SHA,
            "setup_commands": ["make deps"],
        },
        "git_credentials_map": {
            URL: {"token": "read-only-lease", "permission": "read"}
        },
    }


def _executor() -> CodexAgent:
    executor = CodexAgent.__new__(CodexAgent)
    executor.logger = MagicMock()
    executor.environment_profile = None
    executor.use_kubernetes = False
    return executor


def test_startup_line_is_last_link_and_chain_fails_closed() -> None:
    commands = _executor()._prepare_init_commands(_review_context())
    line = read_only_startup_line(SHA)
    assert commands.endswith(f"&& echo '{line}' || exit 1")
    # Printed only after the verified pinned checkout and the setup block.
    assert commands.index(f'"$(git rev-parse HEAD)" != {SHA}') < commands.index(line)
    assert commands.index("make deps") < commands.index(line)


def test_startup_line_requires_the_admitted_commit() -> None:
    context = _review_context()
    context["git_clone_config"]["repositories"][0].pop("commit")
    with pytest.raises(ValueError):
        _executor()._prepare_init_commands(context)
    with pytest.raises(ValueError):
        read_only_startup_line("A" * 40)


def _orchestrator() -> tuple[FlowExecutionOrchestrator, MagicMock]:
    orch = FlowExecutionOrchestrator(MagicMock(), uuid4(), {}, AsyncMock())
    orch.execution_log = SimpleNamespace(id=uuid4())  # type: ignore[assignment]
    orch.execution_logger = MagicMock()
    orch._read_only_admitted_commit = SHA
    return orch, MagicMock()


async def _feed(orch: FlowExecutionOrchestrator, lines: list[str]) -> None:
    for index, line in enumerate(lines):
        await orch._process_agent_log_line(
            line,
            session_reference="session",
            previous_line="",
            log_count=index,
            publish_raw=False,
        )


@pytest.mark.asyncio
async def test_receipt_recorded_when_startup_precedes_agent_start() -> None:
    orch, _ = _orchestrator()
    with patch(
        "preloop.services.flow_orchestrator.crud_flow_execution.record_startup_receipt"
    ) as record:
        await _feed(orch, [read_only_startup_line(SHA), AGENT_EXEC_START_MARKER])
    receipt = record.call_args.kwargs["receipt"]
    assert receipt["commit"] == SHA
    assert receipt["executor"] == "hosted"
    assert receipt["checkout_verified"] is True
    assert receipt["setup_completed"] is True
    assert receipt["agent_started_at"]
    assert receipt["lease_revoked"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lines",
    [
        # Agent output after the start cannot author the startup line.
        [AGENT_EXEC_START_MARKER, read_only_startup_line(SHA)],
        # Another commit is not the admitted one.
        [read_only_startup_line("b" * 40), AGENT_EXEC_START_MARKER],
        # A reported setup failure is never a completed startup.
        [
            f"{SETUP_FAILED_MARKER} exit=2",
            read_only_startup_line(SHA),
            AGENT_EXEC_START_MARKER,
        ],
        [AGENT_EXEC_START_MARKER],
    ],
)
async def test_no_receipt_without_controller_startup(lines: list[str]) -> None:
    orch, _ = _orchestrator()
    with patch(
        "preloop.services.flow_orchestrator.crud_flow_execution.record_startup_receipt"
    ) as record:
        await _feed(orch, lines)
    record.assert_not_called()


@pytest.mark.asyncio
async def test_later_setup_failure_withdraws_receipt() -> None:
    orch, _ = _orchestrator()
    with patch(
        "preloop.services.flow_orchestrator.crud_flow_execution.record_startup_receipt"
    ) as record:
        await _feed(
            orch,
            [
                read_only_startup_line(SHA),
                AGENT_EXEC_START_MARKER,
                f"{SETUP_FAILED_MARKER} exit=1",
            ],
        )
    final = record.call_args.kwargs["receipt"]
    assert final["checkout_verified"] is False
    assert final["setup_completed"] is False


@pytest.mark.asyncio
async def test_non_review_execution_records_nothing() -> None:
    orch, _ = _orchestrator()
    orch._read_only_admitted_commit = None
    with patch(
        "preloop.services.flow_orchestrator.crud_flow_execution.record_startup_receipt"
    ) as record:
        await _feed(orch, [read_only_startup_line(SHA), AGENT_EXEC_START_MARKER])
    record.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_revocation_is_added_to_receipt() -> None:
    orch, _ = _orchestrator()
    orch._read_only_review_leases = [MagicMock(), MagicMock()]
    with (
        patch.object(orch, "_execution_reached_terminal_state", return_value=True),
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution."
            "record_startup_receipt"
        ) as record,
        patch(
            "preloop.services.publication_credentials.revoke_repository_lease",
            AsyncMock(),
        ),
    ):
        await _feed(orch, [read_only_startup_line(SHA), AGENT_EXEC_START_MARKER])
        await orch._revoke_read_only_review_leases()
    assert record.call_args.kwargs["receipt"]["lease_revoked"] is True


def test_hosted_capture_drops_agent_authored_runner_receipt() -> None:
    orch, _ = _orchestrator()
    forged = {
        "review": "ok",
        HOST_READ_ONLY_RESULT_KEY: {"commit": SHA, "sandbox": "read-only"},
    }
    with (
        patch.object(
            orch, "_persist_cra_result_boundary", side_effect=lambda doc, **_: doc
        ),
        patch.object(orch, "_check_result_schema"),
    ):
        assert orch._persist_merged_artifact(forged) == {"review": "ok"}


def test_projection_of_hosted_and_private_receipts() -> None:
    hosted = SimpleNamespace(
        startup_receipt={
            "commit": SHA,
            "checkout_verified": True,
            "setup_completed": True,
            "agent_started_at": "2030-01-01T00:00:00+00:00",
            "lease_revoked": True,
            "unexpected": "dropped",
        },
        result=None,
        ci_review_binding={"head_sha": SHA},
    )
    projected = public_startup_receipt(hosted)
    assert projected is not None
    assert projected["executor"] == "hosted"
    assert "unexpected" not in projected
    private = SimpleNamespace(
        startup_receipt=None,
        result={
            HOST_READ_ONLY_RESULT_KEY: {
                "commit": SHA,
                "sandbox": "read-only",
                "lease_revoked": True,
            }
        },
        ci_review_binding={"head_sha": SHA},
    )
    assert public_startup_receipt(private) == {
        "version": 1,
        "executor": "private_runner",
        "commit": SHA,
        "checkout_verified": True,
        "setup_completed": True,
        "agent_started_at": None,
        "lease_revoked": True,
        "sandbox": "read-only",
    }
    none = SimpleNamespace(startup_receipt=None, result={}, ci_review_binding={})
    assert public_startup_receipt(none) is None


def test_withheld_paths_name_every_member_the_projection_drops() -> None:
    document = {
        "verdict": "approve",
        "findings": [{"file": "a.py", "api_key": "x"}, {"logs": ["y"]}],
        "_internal": 1,
        "a/b": {"token": "z"},
    }
    assert public_ci_result(document) == {
        "verdict": "approve",
        "findings": [{"file": "a.py"}, {}],
        "a/b": {},
    }
    assert ci_result_withheld_paths(document) == [
        "/findings/0/api_key",
        "/findings/1/logs",
        "/_internal",
        "/a~1b/token",
    ]
    assert ci_result_withheld_paths({"verdict": "approve"}) == []


def test_ci_schema_validation_returns_errors_and_guards_messages() -> None:
    execution = SimpleNamespace(
        result_schema_validation={
            "valid": False,
            "errors": [{"path": "$.score", "message": "'x' is not an integer"}],
            "error_count": 1,
        }
    )
    assert _ci_schema_validation(execution, messages=True) == {
        "valid": False,
        "errors": [{"path": "$.score", "message": "'x' is not an integer"}],
        "error_count": 1,
    }
    assert _ci_schema_validation(execution, messages=False) == {
        "valid": False,
        "errors": [{"path": "$.score"}],
        "error_count": 1,
    }
    assert (
        _ci_schema_validation(
            SimpleNamespace(result_schema_validation=None), messages=True
        )
        is None
    )


def test_trigger_route_documents_the_ci_contract() -> None:
    from fastapi import FastAPI

    from preloop.api.endpoints import flows

    app = FastAPI()
    app.include_router(flows.router)
    operation = app.openapi()["paths"]["/flows/{flow_id}/trigger"]["post"]
    assert operation["summary"] == "Trigger a restricted CI review or a test execution"
    for phrase in ("pr_number", "head_sha", "rejected with 422", "``test_mode`` false"):
        assert phrase in operation["description"]
