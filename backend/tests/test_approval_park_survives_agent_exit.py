"""The park must survive both an async workflow and a fast agent exit.

Staging execution ``e42c6086-f637-4d18-be09-2395c4d488ca`` (preset 006,
approval ``6a7cd2dc-a9f8-4fa5-9870-b834f5bc1db2``) raised a CRA waiver at
16:47:41Z against a three day window and was marked FAILED at 16:52:05Z with
``parked_at``, ``park_request_id`` and ``park_expires_at`` all null. The human
approved at 16:54:25Z, 2 minutes 20 seconds too late, with nothing left to
resume. #510 had shipped; the park was simply never reached.

Two independent reasons, one per half of this file:

1. the account's default approval workflow has ``async_approval_enabled``,
   and that branch of ``require_approval`` returns a ``pending_approval``
   payload before the poll loop where the park handshake lives (the tool
   call took 528 ms, not the 90 seconds a park needs);
2. even once it parks, the park request is written by a different process
   and observed on a 5 second poll, so an agent that receives
   ``parked_for_human`` and exits can outrun the observation, and the
   terminal branch used to complete the run and fail it closed.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.services import approval_park

UTC = timezone.utc

# The result.json preset 006 actually wrote on the staging run: the #512
# incompletion envelope. Note there is no "status" key at all.
STAGING_INCOMPLETION_ENVELOPE = {
    "schema": "preloop.cra.releaseaudit/v1",
    "flow": "release-security-audit",
    "run_at": "2026-09-09T16:51:36Z",
    "regime_profile": "cra",
    "verdict": "error",
    "incomplete": {
        "reason": (
            "The audit could not be completed because a required human "
            "decision on approval request "
            "6a7cd2dc-a9f8-4fa5-9870-b834f5bc1db2 did not arrive before "
            "the execution limit."
        ),
        "stage": "vuln_scan",
    },
    "disclaimer": "Machine-generated evidence for conformity assessment support.",
}


@pytest.mark.asyncio
class TestAsyncWorkflowParks:
    """An async approval workflow must park, not hand out polling advice."""

    @staticmethod
    def _patch_park(monkeypatch, *, recorded=True):
        monkeypatch.setattr("preloop.models.db.session.get_session_factory", MagicMock)
        monkeypatch.setattr(
            "preloop.api.loop_safety.run_db_off_loop",
            AsyncMock(side_effect=lambda fn: fn()),
        )
        write = MagicMock(return_value=recorded)
        monkeypatch.setattr(approval_park, "request_park", write)
        return write

    async def test_a_three_day_window_parks_and_tells_the_agent_to_stop(
        self, monkeypatch
    ):
        """The staging case: 259200 seconds, an execution, so park."""
        from preloop.services import approval_helper

        write = self._patch_park(monkeypatch)
        expires = datetime.now(UTC) + timedelta(days=3)
        payload = await approval_helper._park_and_build_payload(
            execution_id="e42c6086",
            approval_request_id="6a7cd2dc",
            expires_at=expires,
            window_seconds=259200,
            tool_name="ask_user",
            arguments={"question": "Please review and submit any waivers."},
            base_url="https://staging.example.test",
        )
        assert payload is not None
        parsed = json.loads(payload)
        assert parsed["status"] == "parked_for_human"
        assert parsed["request_id"] == "6a7cd2dc"
        assert parsed["tool_name"] == "ask_user"
        assert "do not poll" in parsed["message"]
        assert parsed["question"] == "Please review and submit any waivers."
        assert write.call_args.kwargs["expires_at"] == expires

    async def test_a_short_window_keeps_the_polling_payload(self, monkeypatch):
        """Under the park threshold nothing changes: no park, no restart."""
        from preloop.services import approval_helper

        write = self._patch_park(monkeypatch)
        assert (
            await approval_helper._park_and_build_payload(
                execution_id="exec-1",
                approval_request_id="req-1",
                expires_at=None,
                window_seconds=30,
                tool_name="ask_user",
                arguments={},
                base_url="https://example.test",
            )
            is None
        )
        write.assert_not_called()

    async def test_a_call_with_no_execution_behind_it_never_parks(self, monkeypatch):
        """A plain MCP session has no run to suspend."""
        from preloop.services import approval_helper

        write = self._patch_park(monkeypatch)
        assert (
            await approval_helper._park_and_build_payload(
                execution_id=None,
                approval_request_id="req-1",
                expires_at=None,
                window_seconds=259200,
                tool_name="ask_user",
                arguments={},
                base_url="https://example.test",
            )
            is None
        )
        write.assert_not_called()

    async def test_a_run_that_already_finished_is_not_parked(self, monkeypatch):
        """request_park claims zero rows, so the caller keeps its old path."""
        from preloop.services import approval_helper

        self._patch_park(monkeypatch, recorded=False)
        assert (
            await approval_helper._park_and_build_payload(
                execution_id="exec-1",
                approval_request_id="req-1",
                expires_at=None,
                window_seconds=259200,
                tool_name="ask_user",
                arguments={},
                base_url="https://example.test",
            )
            is None
        )

    async def test_the_async_branch_reaches_the_park_helper(self, monkeypatch):
        """Regression guard for the defect itself.

        The async branch returned ~70 lines above the poll loop, so the park
        was unreachable. Pin that the async payload is only built when the
        park helper declines.
        """
        import inspect

        from preloop.services import approval_helper

        source = inspect.getsource(approval_helper.require_approval)
        async_at = source.index("if workflow_async_enabled:")
        park_at = source.index("_park_and_build_payload", async_at)
        pending_at = source.index('"status": "pending_approval"', async_at)
        assert park_at < pending_at, (
            "the park attempt must precede the pending_approval payload, "
            "or an async workflow can never park"
        )


@pytest.mark.asyncio
class TestParkSurvivesAFastAgentExit:
    """The agent stops when told to; the monitor must not call that an end."""

    def _orchestrator(self, monkeypatch, *, park_request):
        from preloop.services import flow_orchestrator as module

        orchestrator = object.__new__(module.FlowExecutionOrchestrator)
        orchestrator.db = MagicMock()
        orchestrator.execution_log = SimpleNamespace(id=uuid.uuid4())
        orchestrator.execution_logger = MagicMock()
        orchestrator.execution_logger.get_actions_taken.return_value = []
        orchestrator.execution_logger.get_mcp_usage_logs.return_value = []
        orchestrator._publish_update = AsyncMock()
        orchestrator._capture_result_artifact = AsyncMock(
            return_value=dict(STAGING_INCOMPLETION_ENVELOPE)
        )
        orchestrator._chain_consumed_seconds = MagicMock(return_value=0)
        monkeypatch.setattr(
            module.crud_flow_execution,
            "get_park_request",
            MagicMock(return_value=park_request),
        )
        return orchestrator

    async def test_a_pending_park_wins_over_the_container_exit(self, monkeypatch):
        request_id = uuid.uuid4()
        expires = datetime.now(UTC) + timedelta(days=3)
        orchestrator = self._orchestrator(
            monkeypatch,
            park_request={
                "request_id": request_id,
                "requested_at": datetime.now(UTC),
                "expires_at": expires,
                "parked_at": None,
            },
        )
        executor = SimpleNamespace(stop=AsyncMock())
        result = await orchestrator._park_if_requested(executor, "session-1", 295)

        assert result is not None
        assert result["status"] == "WAITING_FOR_HUMAN"
        assert result["error_message"] is None
        assert result["park"]["approval_request_id"] == str(request_id)
        assert result["park"]["compute_seconds"] == 295
        executor.stop.assert_awaited_once_with("session-1")

    async def test_an_already_confirmed_park_is_not_parked_twice(self, monkeypatch):
        orchestrator = self._orchestrator(
            monkeypatch,
            park_request={
                "request_id": uuid.uuid4(),
                "requested_at": datetime.now(UTC),
                "expires_at": None,
                "parked_at": datetime.now(UTC),
            },
        )
        assert (
            await orchestrator._park_if_requested(
                SimpleNamespace(stop=AsyncMock()), "session-1", 10
            )
            is None
        )

    async def test_no_park_request_leaves_the_run_alone(self, monkeypatch):
        orchestrator = self._orchestrator(monkeypatch, park_request=None)
        assert (
            await orchestrator._park_if_requested(
                SimpleNamespace(stop=AsyncMock()), "session-1", 10
            )
            is None
        )

    async def test_a_runtime_that_will_not_stop_still_parks(self, monkeypatch):
        """The container has already exited on the terminal-branch call."""
        orchestrator = self._orchestrator(
            monkeypatch,
            park_request={
                "request_id": uuid.uuid4(),
                "requested_at": datetime.now(UTC),
                "expires_at": None,
                "parked_at": None,
            },
        )
        executor = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("gone")))
        result = await orchestrator._park_if_requested(executor, "session-1", 5)
        assert result["status"] == "WAITING_FOR_HUMAN"

    async def test_the_terminal_branch_asks_about_the_park(self):
        """The race is only closed if the exit path re-checks."""
        import inspect

        from preloop.services import flow_orchestrator as module

        source = inspect.getsource(
            module.FlowExecutionOrchestrator._monitor_agent_execution
        )
        terminal_at = source.index("if status in (")
        assert "_park_if_requested" in source[terminal_at:], (
            "the terminal branch must re-check the park request, or an agent "
            "that exits between two polls is completed instead of parked"
        )
        # Two call sites: the loop head and the terminal branch.
        assert source.count("_park_if_requested") == 2


class TestFailClosedDoesNotTouchAParkedRun:
    """A run that has not finished has no release to deny."""

    def _orchestrator(self):
        from preloop.cra.persist import CraPersistDecision
        from preloop.cra.validate import CraValidationResult
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = object.__new__(FlowExecutionOrchestrator)
        orchestrator._cra_persist_decision = CraPersistDecision(
            artifact=dict(STAGING_INCOMPLETION_ENVELOPE),
            validation=CraValidationResult(
                ok=False,
                failures=["run did not complete: the waiver was unanswered"],
                schema_id="preloop.cra.releaseaudit/v1",
                incomplete=True,
            ),
        )
        return orchestrator

    def test_a_parked_run_is_never_failed_closed(self):
        orchestrator = self._orchestrator()
        assert orchestrator._cra_persist_decision.invalid is True
        status, error = orchestrator._apply_cra_fail_closed("WAITING_FOR_HUMAN", None)
        assert status == "WAITING_FOR_HUMAN"
        assert error is None

    def test_a_finished_run_still_fails_closed(self):
        """The guard is about park, not about weakening fail-closed."""
        orchestrator = self._orchestrator()
        status, error = orchestrator._apply_cra_fail_closed("SUCCEEDED", None)
        assert status == "FAILED"
        assert error


class TestFailureMessageNamesTheFieldThatDecided:
    """`status=None` sent operators looking for a key that does not exist."""

    def test_the_incompletion_envelope_is_reported_on_its_verdict(self):
        from preloop.services.flow_orchestrator import _artifact_failure_signal

        field, value = _artifact_failure_signal(STAGING_INCOMPLETION_ENVELOPE)
        assert (field, value) == ("verdict", "error")
        assert "status" not in STAGING_INCOMPLETION_ENVELOPE

    def test_an_explicit_status_still_wins(self):
        from preloop.services.flow_orchestrator import _artifact_failure_signal

        assert _artifact_failure_signal({"status": "failure", "verdict": "error"}) == (
            "status",
            "failure",
        )

    def test_no_artifact_reports_status_none(self):
        from preloop.services.flow_orchestrator import _artifact_failure_signal

        assert _artifact_failure_signal(None) == ("status", None)

    def test_the_envelope_is_still_classified_as_a_failure(self):
        from preloop.services.flow_orchestrator import _result_artifact_confirmation

        assert _result_artifact_confirmation(STAGING_INCOMPLETION_ENVELOPE) == "failure"


@pytest.mark.asyncio
class TestTheWholeHandshakeOnAFastExit:
    """Monitor result to persisted park to the resume the decision starts."""

    async def test_a_fast_exit_parks_and_notifies_nobody(self, monkeypatch):
        from preloop.services import flow_orchestrator as module

        request_id = uuid.uuid4()
        monkeypatch.setattr(
            module.crud_flow_execution,
            "get_park_request",
            MagicMock(
                return_value={
                    "request_id": request_id,
                    "requested_at": datetime.now(UTC),
                    "expires_at": datetime.now(UTC) + timedelta(days=3),
                    "parked_at": None,
                }
            ),
        )
        confirm = MagicMock()
        monkeypatch.setattr(module.crud_flow_execution, "confirm_park", confirm)

        orchestrator = object.__new__(module.FlowExecutionOrchestrator)
        orchestrator.db = MagicMock()
        orchestrator.execution_log = SimpleNamespace(id=uuid.uuid4())
        orchestrator.flow = SimpleNamespace(id=uuid.uuid4(), account_id=uuid.uuid4())
        orchestrator.execution_logger = MagicMock()
        orchestrator.execution_logger.get_actions_taken.return_value = []
        orchestrator.execution_logger.get_mcp_usage_logs.return_value = []
        orchestrator._publish_update = AsyncMock()
        orchestrator._capture_result_artifact = AsyncMock(
            return_value=dict(STAGING_INCOMPLETION_ENVELOPE)
        )
        orchestrator._chain_consumed_seconds = MagicMock(return_value=0)
        orchestrator.tool_calls_count = 4
        orchestrator.total_tokens = 6568865
        orchestrator.estimated_cost = 0.0
        orchestrator._update_execution_log = AsyncMock()
        orchestrator._sync_runtime_session = MagicMock()
        orchestrator._notify_terminal = AsyncMock()
        orchestrator._start_queued_followup = AsyncMock()

        # The agent exited between two polls, so the terminal branch is where
        # the park is observed.
        agent_result = await orchestrator._park_if_requested(
            SimpleNamespace(stop=AsyncMock()), "session-1", 558
        )
        assert agent_result["status"] == "WAITING_FOR_HUMAN"

        # Fail-closed must not touch it on the way out.
        from preloop.cra.persist import CraPersistDecision
        from preloop.cra.validate import CraValidationResult

        orchestrator._cra_persist_decision = CraPersistDecision(
            artifact=dict(STAGING_INCOMPLETION_ENVELOPE),
            validation=CraValidationResult(ok=False, failures=["incomplete"]),
        )
        status, error = orchestrator._apply_cra_fail_closed(
            agent_result["status"], agent_result["error_message"]
        )
        assert (status, error) == ("WAITING_FOR_HUMAN", None)

        await orchestrator._finalize_park(
            agent_result=agent_result,
            output_summary=None,
            merged_result=agent_result["result"],
        )

        kwargs = orchestrator._update_execution_log.await_args.kwargs
        assert kwargs["status"] == "WAITING_FOR_HUMAN"
        assert "end_time" not in kwargs
        assert confirm.call_args.kwargs["compute_seconds"] == 558
        # A parked run is alive: nobody is told it ended.
        orchestrator._notify_terminal.assert_not_awaited()
        orchestrator._start_queued_followup.assert_not_awaited()

    async def test_the_decision_then_resumes_the_parked_run(self, monkeypatch):
        """The half that was dead on staging: an answer with a run to land on."""
        parked = SimpleNamespace(
            id=uuid.uuid4(),
            flow_id=uuid.uuid4(),
            trigger_event_details={"payload": {"source": "cra"}},
            cli_session={"session_id": "codex-01a0870d"},
            park_request_id=uuid.uuid4(),
            parked_compute_seconds=558,
            start_time=datetime.now(UTC),
            parked_at=datetime.now(UTC),
        )
        request = SimpleNamespace(
            id=parked.park_request_id,
            status="approved",
            tool_name="ask_user",
            tool_args={"question": "Please review and submit any waivers."},
            answer_text="waived GO-2026-5932",
            selected_option=None,
            approver_comment=None,
            responses=[{"user_id": "founder"}],
            resolved_at=datetime.now(UTC),
            structured_answer={
                "waived": [{"id": "GO-2026-5932", "reason": "VEX not_affected"}]
            },
        )
        started = []

        monkeypatch.setattr(approval_park, "_load_request", lambda db, rid: request)
        monkeypatch.setattr(
            "preloop.models.db.session.get_session_factory",
            lambda: MagicMock(return_value=MagicMock(__enter__=lambda s: s)),
        )
        monkeypatch.setattr(
            "preloop.models.crud.crud_flow_execution.list_parked_for_request",
            MagicMock(return_value=[parked]),
        )
        monkeypatch.setattr(
            "preloop.models.crud.crud_flow_execution.claim_parked_for_resume",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            "preloop.models.crud.crud_flow.get",
            MagicMock(return_value=SimpleNamespace(id=parked.flow_id)),
        )

        async def _start(db, flow, row, details):
            started.append(details)
            return uuid.uuid4()

        monkeypatch.setattr(approval_park, "_start_resume_execution", _start)

        ids = await approval_park.resume_parked_executions(parked.park_request_id)
        assert len(ids) == 1
        details = started[0]
        assert details["_resume"]["execution_id"] == str(parked.id)
        assert details["_resume"]["cli_session"]["session_id"] == "codex-01a0870d"
        answer = details["payload"]["answers"][str(parked.park_request_id)]
        assert answer["status"] == "approved"
        assert answer["structured_answer"]["waived"][0]["id"] == "GO-2026-5932"
        # The human's wait is not charged to the resumed run's budget.
        assert details["_answers"]["consumed_seconds"] == 558
