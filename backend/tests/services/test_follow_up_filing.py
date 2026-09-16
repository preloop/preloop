"""Filing the approved portfolio follow ups as tracker issues (issue #687).

The claim being tested is the product's third output: a portfolio run ends
with an inventory, an assessment, and the follow ups a human approved sitting
in the tracker as issues. What makes it safe is where the filing happens: the
flow that read forty untrusted projects holds no ``create_issue``, so the
issues are created by the control plane after the agent has exited.

Everything here runs against a fake tracker client. No database, no network,
no agent: the unit under test is the decision of what to file, what never to
file twice, and what to say when nothing was filed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.services.flow_artifacts import (
    RESERVED_RESULT_FIELDS,
    sanitize_captured_result,
)
from preloop.services.follow_up_filing import (
    FOLLOW_UP_FILING_REASONS,
    FOLLOW_UP_FILING_RESULT_KEY,
    FOLLOW_UP_ROW_OUTCOMES,
    OUTCOME_FAILED,
    OUTCOME_FILED,
    OUTCOME_NOTHING_FILED,
    OUTCOME_PARTIAL,
    ROW_ALREADY_FILED,
    ROW_FAILED,
    ROW_FILED,
    ROW_NOT_FILED,
    FilingContext,
    FollowUpFilingPlan,
    apply_filing_to_result,
    approved_follow_ups,
    blocked_outcome,
    build_issue_request,
    collect_filed_follow_ups,
    file_follow_ups,
    filing_blocked_reason,
    resolve_follow_up_filing,
)

TRACKER_SECRET = "glpat-not-a-real-token-687"


class FakeTracker:
    """A tracker that records what it was asked to create.

    ``refuse`` names the follow up titles it fails on, which is how the
    "one bad row does not abort the rest" case is built.
    """

    tracker_type = "github"

    def __init__(self, refuse: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, object]] = []
        self.refuse = set(refuse)
        self._next = 0

    async def create_issue(self, project_key, issue_data):
        self.calls.append((project_key, issue_data))
        if any(name in issue_data.title for name in self.refuse):
            raise RuntimeError(f"tracker refused the request (auth {TRACKER_SECRET})")
        self._next += 1
        return SimpleNamespace(
            id=str(1000 + self._next),
            key=f"WIDGETS-{self._next}",
            url=f"https://tracker.example.com/widgets/issues/{self._next}",
        )


def _follow_up(index: int, **overrides) -> dict:
    project = overrides.pop("project", f"services/project-{index}")
    row = {
        "id": f"portfolio:{project}:finding-{index}",
        "project": project,
        "title": f"Document the run command of project {index}",
        "severity": "high",
        "evidence": f"{project}/README.md:{10 + index}",
        "rank": index,
        "status": "approved",
        "note": f"note {index} typed by the human",
        "approved_by": "user-2f0b1d4c",
        "approved_at": "2026-09-16T11:12:00Z",
        "child_execution_id": None,
        "filed": False,
        "filed_issue": None,
    }
    row.update(overrides)
    return row


def _result(rows=None, *, gate="answered", **overrides) -> dict:
    rows = [_follow_up(index) for index in (1, 2, 3)] if rows is None else rows
    result = {
        "schema": "preloop.review.portfolio/v1",
        "flow": "portfolio-review",
        "status": "success",
        "git": {
            "remote": "https://github.com/example/widgets.git",
            "commit": "a" * 40,
        },
        "artifacts": {"report": "evidence/portfolio-report.md"},
        "questions": [
            {"phase": "selection", "asked": True, "status": "answered"},
            {
                "phase": "follow_ups",
                "asked": True,
                "status": gate,
                "expires_at": "2026-09-19T11:00:00Z",
            },
        ],
        "follow_ups": rows,
        "rollup": {
            "follow_ups_total": len(rows),
            "follow_ups_approved": sum(
                1 for row in rows if row.get("status") == "approved"
            ),
            "issues_filed": 0,
        },
    }
    result.update(overrides)
    return result


def _plan(**overrides) -> FollowUpFilingPlan:
    values = {"enabled": True, "max_issues": 25}
    values.update(overrides)
    return FollowUpFilingPlan(**values)


def _context() -> FilingContext:
    return FilingContext(
        execution_id="d6f1c0f1-0000-4000-8000-000000000001",
        flow_name="Portfolio Review",
        repository="https://github.com/example/widgets.git",
        commit="a" * 40,
        report_path="evidence/portfolio-report.md",
    )


async def _file(result, *, client=None, plan=None, already_filed=None):
    """File the approved rows of ``result`` and write the outcome back."""
    client = client or FakeTracker()
    plan = plan or _plan()
    rows = approved_follow_ups(result)
    blocked = filing_blocked_reason(result, rows)
    if blocked is not None:
        outcome = blocked_outcome(blocked)
    else:
        outcome = await file_follow_ups(
            client=client,
            project_key="widgets",
            rows=rows,
            context=_context(),
            plan=plan,
            already_filed=already_filed,
            tracker=client.tracker_type,
        )
    apply_filing_to_result(result, outcome)
    return client, outcome


class TestConfiguration:
    """A flow files only when it was configured to."""

    def test_no_block_means_no_filing(self):
        assert resolve_follow_up_filing(None) is None
        assert resolve_follow_up_filing({"enabled": True}) is None

    def test_a_disabled_block_is_the_same_as_no_block(self):
        assert (
            resolve_follow_up_filing({"follow_up_filing": {"enabled": False}}) is None
        )

    def test_an_enabled_block_carries_its_labels_and_ceiling(self):
        plan = resolve_follow_up_filing(
            {
                "follow_up_filing": {
                    "enabled": True,
                    "labels": ["preloop", "preloop", " follow-up "],
                    "max_issues": 3,
                    "project_id": "0f2f5b24-0000-4000-8000-000000000002",
                }
            }
        )
        assert plan is not None
        assert plan.labels == ("preloop", "follow-up")
        assert plan.max_issues == 3
        assert plan.project_id == "0f2f5b24-0000-4000-8000-000000000002"

    def test_an_absurd_ceiling_is_clamped_not_obeyed(self):
        plan = resolve_follow_up_filing(
            {"follow_up_filing": {"enabled": True, "max_issues": 10_000}}
        )
        assert plan is not None and plan.max_issues == 100

    def test_the_schema_refuses_a_misspelled_key(self):
        from pydantic import ValidationError

        from preloop.models.schemas.flow import GitCloneConfig

        with pytest.raises(ValidationError):
            GitCloneConfig.model_validate(
                {"enabled": True, "follow_up_filing": {"enabled": True, "lables": []}}
            )

    def test_the_schema_refuses_an_empty_label(self):
        from pydantic import ValidationError

        from preloop.models.schemas.flow import GitCloneConfig

        with pytest.raises(ValidationError):
            GitCloneConfig.model_validate(
                {
                    "enabled": True,
                    "follow_up_filing": {"enabled": True, "labels": [" "]},
                }
            )


class TestApprovedRowsAreFiled:
    """Three approved rows, three issues, each carrying its own facts."""

    @pytest.mark.asyncio
    async def test_three_approved_rows_produce_exactly_three_issues(self):
        result = _result()
        client, outcome = await _file(result)
        assert len(client.calls) == 3
        assert outcome.filed == 3
        assert outcome.outcome == OUTCOME_FILED
        assert [call[0] for call in client.calls] == ["widgets"] * 3

    @pytest.mark.asyncio
    async def test_each_issue_carries_priority_note_project_execution_and_evidence(
        self,
    ):
        result = _result(
            [
                _follow_up(
                    1,
                    severity="medium",
                    child_execution_id="c0ffee00-0000-4000-8000-00000000000a",
                )
            ]
        )
        client, _ = await _file(result)
        ((_, issue),) = client.calls
        assert issue.priority == "medium"
        assert "note 1 typed by the human" in issue.description
        assert "services/project-1" in issue.description
        assert "services/project-1/README.md:11" in issue.description
        assert "c0ffee00-0000-4000-8000-00000000000a" in issue.description
        assert "d6f1c0f1-0000-4000-8000-000000000001" in issue.description
        assert "portfolio:services/project-1:finding-1" in issue.description
        assert issue.title.startswith("services/project-1: ")
        assert "priority:medium" in (issue.labels or [])

    @pytest.mark.asyncio
    async def test_an_inline_run_says_it_had_no_child_execution(self):
        result = _result([_follow_up(1)])
        client, _ = await _file(result)
        ((_, issue),) = client.calls
        assert "Child execution: none (the review ran inline)" in issue.description

    @pytest.mark.asyncio
    async def test_the_rows_are_filed_in_rank_order(self):
        """The ranking is the review's opinion of what matters; a tracker
        that shows creation order should show that opinion."""
        rows = [_follow_up(3), _follow_up(1), _follow_up(2)]
        client, _ = await _file(_result(rows))
        filed = [call[1].title for call in client.calls]
        assert filed == [
            "services/project-1: Document the run command of project 1",
            "services/project-2: Document the run command of project 2",
            "services/project-3: Document the run command of project 3",
        ]

    def test_the_issue_body_is_one_unit_of_work_for_the_implementer(self):
        """Preset 011 reads the title, the description and the url only, so
        everything an implementer needs is in the description itself."""
        row = approved_follow_ups(_result([_follow_up(1)]))[0]
        request = build_issue_request(row, _context(), _plan())
        for heading in (
            "## What to do",
            "## Where",
            "## Why it is here",
            "## Done when",
        ):
            assert heading in request.description
        assert request.title

    @pytest.mark.asyncio
    async def test_the_ceiling_stops_a_runaway_portfolio(self):
        result = _result([_follow_up(index) for index in range(1, 6)])
        client, outcome = await _file(result, plan=_plan(max_issues=2))
        assert len(client.calls) == 2
        assert outcome.filed == 2
        assert outcome.not_filed == 3
        assert {row.reason for row in outcome.rows if row.outcome == ROW_NOT_FILED} == {
            "limit_reached"
        }


class TestNothingIsFiledWithoutApproval:
    """A row the human did not approve, and a gate that closed."""

    @pytest.mark.asyncio
    async def test_an_unapproved_row_produces_no_issue(self):
        result = _result(
            [
                _follow_up(1),
                _follow_up(
                    2,
                    status="unapproved",
                    note=None,
                    approved_by=None,
                    approved_at=None,
                ),
            ]
        )
        client, outcome = await _file(result)
        assert len(client.calls) == 1
        assert outcome.filed == 1
        assert result["follow_ups"][1]["filed"] is False
        assert result["follow_ups"][1]["filed_issue"] is None

    @pytest.mark.asyncio
    async def test_an_expired_gate_files_nothing_and_says_so(self):
        rows = [
            _follow_up(
                index,
                status="unapproved",
                note=None,
                approved_by=None,
                approved_at=None,
            )
            for index in (1, 2, 3)
        ]
        result = _result(rows, gate="expired")
        client, outcome = await _file(result)
        assert client.calls == []
        assert outcome.outcome == OUTCOME_NOTHING_FILED
        receipt = result[FOLLOW_UP_FILING_RESULT_KEY]
        assert receipt["reason"] == "gate_expired"
        assert receipt["filed"] == 0
        assert result["rollup"]["issues_filed"] == 0

    @pytest.mark.asyncio
    async def test_an_answered_gate_that_approved_nothing_is_its_own_reason(self):
        rows = [
            _follow_up(
                1, status="unapproved", note=None, approved_by=None, approved_at=None
            )
        ]
        result = _result(rows)
        _, outcome = await _file(result)
        assert result[FOLLOW_UP_FILING_RESULT_KEY]["reason"] == "nothing_approved"
        assert outcome.outcome == OUTCOME_NOTHING_FILED

    @pytest.mark.asyncio
    async def test_a_run_with_no_candidates_files_nothing(self):
        result = _result([])
        _, outcome = await _file(result)
        assert result[FOLLOW_UP_FILING_RESULT_KEY]["reason"] == "no_follow_ups"

    def test_another_flows_result_is_not_a_portfolio(self):
        assert filing_blocked_reason({"schema": "preloop.review.docscurrency/v1"}, [])
        assert (
            filing_blocked_reason({"schema": "preloop.review.docscurrency/v1"}, [])
            == "not_a_portfolio_result"
        )
        assert filing_blocked_reason("not a result", []) == "not_a_portfolio_result"

    def test_a_row_with_no_id_or_title_is_not_filed(self):
        rows = [
            _follow_up(1, id=""),
            _follow_up(2, title="   "),
            _follow_up(3, project=""),
        ]
        assert approved_follow_ups(_result(rows)) == []


class TestTheIdentifierIsWrittenBack:
    """The report and the next run both read the filing off the row."""

    @pytest.mark.asyncio
    async def test_each_filed_issue_lands_on_its_follow_up_row(self):
        result = _result()
        _, outcome = await _file(result)
        for index, row in enumerate(result["follow_ups"], start=1):
            assert row["filed"] is True
            assert row["filing_status"] == ROW_FILED
            assert row["filed_issue"]["key"] == f"WIDGETS-{index}"
            assert row["filed_issue"]["url"].endswith(f"/issues/{index}")
        assert result["rollup"]["issues_filed"] == 3

    @pytest.mark.asyncio
    async def test_the_receipt_names_the_tracker_and_the_project(self):
        result = _result()
        await _file(result)
        receipt = result[FOLLOW_UP_FILING_RESULT_KEY]
        assert receipt["tracker"] == "github"
        assert receipt["project"] == "widgets"
        assert receipt["considered"] == 3
        assert {row["outcome"] for row in receipt["rows"]} == {ROW_FILED}

    @pytest.mark.asyncio
    async def test_every_receipt_value_comes_from_the_closed_vocabulary(self):
        result = _result([_follow_up(1), _follow_up(2)])
        await _file(result, client=FakeTracker(refuse=("project 2",)))
        receipt = result[FOLLOW_UP_FILING_RESULT_KEY]
        assert receipt["reason"] in FOLLOW_UP_FILING_REASONS
        for row in receipt["rows"]:
            assert row["outcome"] in FOLLOW_UP_ROW_OUTCOMES
            assert row["reason"] in FOLLOW_UP_FILING_REASONS

    def test_the_receipt_key_cannot_be_authored_by_the_agent(self):
        assert FOLLOW_UP_FILING_RESULT_KEY in RESERVED_RESULT_FIELDS
        cleaned = sanitize_captured_result(
            {"summary": "done", FOLLOW_UP_FILING_RESULT_KEY: {"outcome": "filed"}}
        )
        assert cleaned == {"summary": "done"}

    @pytest.mark.asyncio
    async def test_a_row_that_claims_an_issue_nobody_filed_is_corrected(self):
        """An agent with no write tool cannot have filed anything, so a
        result that says otherwise is rewritten, not believed."""
        rows = [
            _follow_up(
                1,
                status="unapproved",
                note=None,
                approved_by=None,
                approved_at=None,
                filed=True,
                filed_issue={"key": "WIDGETS-999"},
            )
        ]
        result = _result(rows, gate="expired")
        await _file(result)
        assert result["follow_ups"][0]["filed"] is False
        assert result["follow_ups"][0]["filed_issue"] is None
        assert result["rollup"]["issues_filed"] == 0


class TestIdempotency:
    """The stable follow up id is what stops a second issue."""

    @pytest.mark.asyncio
    async def test_a_second_run_files_nothing_new_for_the_same_follow_up(self):
        first = _result()
        first_client, _ = await _file(first)
        assert len(first_client.calls) == 3

        ledger = collect_filed_follow_ups([first])
        assert set(ledger) == {row["id"] for row in first["follow_ups"]}

        second = _result()
        second_client, outcome = await _file(second, already_filed=ledger)
        assert second_client.calls == []
        assert outcome.filed == 0
        assert outcome.already_filed == 3
        assert second["rollup"]["issues_filed"] == 3
        assert second["follow_ups"][0]["filed_issue"]["key"] == "WIDGETS-1"
        assert second["follow_ups"][0]["filing_status"] == ROW_ALREADY_FILED

    @pytest.mark.asyncio
    async def test_a_new_follow_up_in_a_re_run_is_still_filed(self):
        first = _result([_follow_up(1)])
        await _file(first)
        ledger = collect_filed_follow_ups([first])

        second = _result([_follow_up(1), _follow_up(2)])
        client, outcome = await _file(second, already_filed=ledger)
        assert len(client.calls) == 1
        assert outcome.filed == 1
        assert outcome.already_filed == 1

    @pytest.mark.asyncio
    async def test_the_same_id_twice_in_one_result_files_once(self):
        rows = [_follow_up(1), _follow_up(1, rank=2)]
        result = _result(rows)
        client, outcome = await _file(result)
        assert len(client.calls) == 1
        assert outcome.not_filed == 1
        assert [row.reason for row in outcome.rows if row.outcome == ROW_NOT_FILED] == [
            "duplicate_follow_up"
        ]

    def test_the_ledger_reads_rows_as_well_as_receipts(self):
        """An older result that kept the row but not the receipt is still a
        record of a filing, and still stops a duplicate."""
        legacy = {
            "follow_ups": [
                {
                    "id": "portfolio:services/api:readme",
                    "filed": True,
                    "filed_issue": {"key": "WIDGETS-7", "url": "https://x/7"},
                }
            ]
        }
        assert collect_filed_follow_ups([legacy]) == {
            "portfolio:services/api:readme": {"key": "WIDGETS-7", "url": "https://x/7"}
        }

    def test_an_unfiled_row_is_not_in_the_ledger(self):
        result = _result()
        assert collect_filed_follow_ups([result]) == {}


class TestOneTrackerErrorDoesNotAbortTheRest:
    """A refused row is reported; the remaining rows are still filed."""

    @pytest.mark.asyncio
    async def test_the_remaining_rows_are_filed_and_the_failure_is_reported(self):
        result = _result()
        client, outcome = await _file(result, client=FakeTracker(refuse=("project 2",)))
        assert len(client.calls) == 3
        assert outcome.filed == 2
        assert outcome.failed == 1
        assert outcome.outcome == OUTCOME_PARTIAL

        failed_row = result["follow_ups"][1]
        assert failed_row["filed"] is False
        assert failed_row["filing_status"] == ROW_FAILED
        assert failed_row["filing_reason"] == "tracker_error"
        assert result["follow_ups"][0]["filed"] is True
        assert result["follow_ups"][2]["filed"] is True
        assert result["rollup"]["issues_filed"] == 2

    @pytest.mark.asyncio
    async def test_the_tracker_message_never_reaches_the_result(self):
        result = _result([_follow_up(1)])
        await _file(result, client=FakeTracker(refuse=("project 1",)))
        import json

        assert TRACKER_SECRET not in json.dumps(result)
        assert result[FOLLOW_UP_FILING_RESULT_KEY]["outcome"] == OUTCOME_FAILED
        assert result[FOLLOW_UP_FILING_RESULT_KEY]["reason"] == "tracker_error"

    @pytest.mark.asyncio
    async def test_an_issue_with_no_identifier_is_a_failure_not_a_filing(self):
        class Amnesiac(FakeTracker):
            async def create_issue(self, project_key, issue_data):
                self.calls.append((project_key, issue_data))
                return SimpleNamespace(id=None, key=None, url=None)

        result = _result([_follow_up(1)])
        _, outcome = await _file(result, client=Amnesiac())
        assert outcome.failed == 1
        assert result["follow_ups"][0]["filed"] is False


class TestOrchestratorWiring:
    """Where the platform does the filing, and how it degrades."""

    def _orchestrator(self, monkeypatch, *, git_clone_config, ledger=None):
        from preloop.services import flow_orchestrator as module

        orchestrator = module.FlowExecutionOrchestrator.__new__(
            module.FlowExecutionOrchestrator
        )
        orchestrator.db = object()
        orchestrator.flow = SimpleNamespace(
            id="5b1b0f6c-0000-4000-8000-000000000003",
            name="Portfolio Review",
            account_id="5b1b0f6c-0000-4000-8000-000000000004",
            git_clone_config=git_clone_config,
            trigger_project_ids=None,
        )
        orchestrator.execution_log = SimpleNamespace(
            id="d6f1c0f1-0000-4000-8000-000000000001"
        )
        orchestrator.execution_logger = SimpleNamespace(log_milestone=MagicMock())
        orchestrator.trigger_event_data = {}
        orchestrator._emit_execution_warning = AsyncMock()
        monkeypatch.setattr(
            module, "load_filed_follow_ups", lambda *a, **k: dict(ledger or {})
        )
        return orchestrator

    @pytest.mark.asyncio
    async def test_a_flow_without_the_block_files_nothing_and_records_nothing(
        self, monkeypatch
    ):
        orchestrator = self._orchestrator(
            monkeypatch, git_clone_config={"enabled": True}
        )
        orchestrator._follow_up_filing_target = AsyncMock(
            side_effect=AssertionError("must not resolve a tracker")
        )
        result = _result()
        await orchestrator._file_approved_follow_ups(result)
        assert FOLLOW_UP_FILING_RESULT_KEY not in result

    @pytest.mark.asyncio
    async def test_the_configured_flow_files_and_records_a_milestone(self, monkeypatch):
        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={"enabled": True, "follow_up_filing": {"enabled": True}},
        )
        client = FakeTracker()
        orchestrator._follow_up_filing_target = AsyncMock(
            return_value=(client, "widgets", "github")
        )
        result = _result()
        await orchestrator._file_approved_follow_ups(result)
        assert len(client.calls) == 3
        assert result[FOLLOW_UP_FILING_RESULT_KEY]["filed"] == 3
        orchestrator.execution_logger.log_milestone.assert_called_once()
        assert (
            orchestrator.execution_logger.log_milestone.call_args[0][0]
            == FOLLOW_UP_FILING_RESULT_KEY
        )

    @pytest.mark.asyncio
    async def test_a_tracker_it_cannot_reach_degrades_with_a_warning(self, monkeypatch):
        from preloop.services.follow_up_filing import FollowUpFilingError

        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={"enabled": True, "follow_up_filing": {"enabled": True}},
        )
        orchestrator._follow_up_filing_target = AsyncMock(
            side_effect=FollowUpFilingError("project_missing", "no project")
        )
        result = _result()
        await orchestrator._file_approved_follow_ups(result)
        assert result[FOLLOW_UP_FILING_RESULT_KEY]["reason"] == "project_missing"
        assert result["rollup"]["issues_filed"] == 0
        orchestrator._emit_execution_warning.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_filing_never_raises_into_the_execution(self, monkeypatch):
        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={"enabled": True, "follow_up_filing": {"enabled": True}},
        )
        orchestrator._follow_up_filing_target = AsyncMock(
            side_effect=RuntimeError("the control plane blinked")
        )
        await orchestrator._file_approved_follow_ups(_result())

    @pytest.mark.asyncio
    async def test_an_earlier_run_of_the_same_flow_is_the_ledger(self, monkeypatch):
        first = _result()
        await _file(first)
        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={"enabled": True, "follow_up_filing": {"enabled": True}},
            ledger=collect_filed_follow_ups([first]),
        )
        client = FakeTracker()
        orchestrator._follow_up_filing_target = AsyncMock(
            return_value=(client, "widgets", "github")
        )
        second = _result()
        await orchestrator._file_approved_follow_ups(second)
        assert client.calls == []
        assert second[FOLLOW_UP_FILING_RESULT_KEY]["already_filed"] == 3

    def test_the_project_comes_from_flow_configuration(self, monkeypatch):
        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={
                "enabled": True,
                "repositories": [
                    {"tracker_id": "t-1", "project_id": "p-1"},
                    {"tracker_id": "t-1", "project_id": "p-1"},
                ],
                "follow_up_filing": {"enabled": True},
            },
        )
        plan = resolve_follow_up_filing(orchestrator.flow.git_clone_config)
        assert orchestrator._follow_up_filing_project_id(plan) == "p-1"

    def test_the_block_can_name_the_project_itself(self, monkeypatch):
        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={
                "enabled": True,
                "repositories": [{"tracker_id": "t-1", "project_id": "p-1"}],
                "follow_up_filing": {"enabled": True, "project_id": "p-2"},
            },
        )
        plan = resolve_follow_up_filing(orchestrator.flow.git_clone_config)
        assert orchestrator._follow_up_filing_project_id(plan) == "p-2"

    def test_two_projects_are_an_ambiguity_not_a_guess(self, monkeypatch):
        from preloop.services.follow_up_filing import FollowUpFilingError

        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={
                "enabled": True,
                "repositories": [
                    {"tracker_id": "t-1", "project_id": "p-1"},
                    {"tracker_id": "t-2", "project_id": "p-2"},
                ],
                "follow_up_filing": {"enabled": True},
            },
        )
        plan = resolve_follow_up_filing(orchestrator.flow.git_clone_config)
        with pytest.raises(FollowUpFilingError) as error:
            orchestrator._follow_up_filing_project_id(plan)
        assert error.value.reason == "project_ambiguous"

    def test_no_project_anywhere_is_reported_not_invented(self, monkeypatch):
        from preloop.services.follow_up_filing import FollowUpFilingError

        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={"enabled": True, "follow_up_filing": {"enabled": True}},
        )
        plan = resolve_follow_up_filing(orchestrator.flow.git_clone_config)
        with pytest.raises(FollowUpFilingError) as error:
            orchestrator._follow_up_filing_project_id(plan)
        assert error.value.reason == "project_missing"

    def test_the_triggering_project_is_the_last_fallback(self, monkeypatch):
        orchestrator = self._orchestrator(
            monkeypatch,
            git_clone_config={"enabled": True, "follow_up_filing": {"enabled": True}},
        )
        orchestrator.trigger_event_data = {"project_id": "p-9"}
        plan = resolve_follow_up_filing(orchestrator.flow.git_clone_config)
        assert orchestrator._follow_up_filing_project_id(plan) == "p-9"
