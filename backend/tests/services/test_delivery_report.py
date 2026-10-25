"""Tests for the Delivery Report preset and its report pass (#2019)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
import yaml

from preloop.flow_presets import DEFAULT_PRESETS_DIR, PRESET_SLUGS
from preloop.models.schemas.flow import FlowCreate, parse_schedule_config
from preloop.services import delivery_report as dr

FIXTURES = Path(__file__).parent.parent / "fixtures" / "delivery_report"
PRESET_FILE = DEFAULT_PRESETS_DIR / "022-delivery-report.yaml"
WINDOW = {"from": "2026-10-12T07:00:00+00:00", "to": "2026-10-19T07:00:00+00:00"}


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _preset() -> Dict[str, Any]:
    return yaml.safe_load(PRESET_FILE.read_text())


def _window_report(**kwargs: Any) -> Dict[str, Any]:
    return dr.assemble_report(
        window=WINDOW,
        pull_requests=_load("pull_requests.json"),
        issues=_load("issues.json"),
        assessments=_load("assessments.json"),
        **kwargs,
    )


class TestPresetRegistration:
    def test_registered_in_catalog(self) -> None:
        assert PRESET_SLUGS["delivery-report"] == "Delivery Report"

    def test_preset_is_a_valid_flow(self) -> None:
        data = _preset()
        data.pop("slug")
        data.pop("supports_persistent")
        flow = FlowCreate(**data)
        assert flow.trigger_event_source == "schedule"

    def test_weekly_schedule_reads_previous_report(self) -> None:
        schedule = parse_schedule_config(_preset()["schedule_config"])
        dumped = schedule.model_dump()
        assert dumped["type"] == "weekly"
        assert dumped["payload"] == {"previous_result_execution_id": "last"}

    def test_policy_defaults_render_the_preset_schedule(self) -> None:
        policy = dr.DeliveryReportPolicy()
        assert policy.schedule_config() == _preset()["schedule_config"]
        assert policy.execution_limits() == _preset()["agent_config"]["limits"]
        assert policy.tracker_writes.create_follow_up == "deny"
        assert policy.tracker_writes.reopen == "approval"
        assert policy.tracker_writes.close == "deny"

    def test_daily_and_manual_cadence(self) -> None:
        daily = dr.DeliveryReportPolicy(cadence="daily").schedule_config()
        assert daily is not None
        assert parse_schedule_config(daily).model_dump()["type"] == "daily"
        assert dr.DeliveryReportPolicy(cadence="manual").schedule_config() is None

    def test_close_can_never_be_allowed(self) -> None:
        with pytest.raises(ValueError):
            dr.TrackerWritePolicy(close="allow")  # type: ignore[arg-type]

    def test_write_tools_are_listed_and_close_is_forbidden_in_the_prompt(
        self,
    ) -> None:
        # The MCP endpoints gate the write tools on the report policy
        # (tests/services/test_delivery_report_actions.py).
        tools = {t["name"] for t in _preset()["allowed_mcp_tools"]}
        assert tools == {
            "search_issues",
            "get_issue",
            "get_pull_request",
            "search_sessions",
            "create_issue",
            "update_issue",
            "add_comment",
        }
        prompt = " ".join(_preset()["prompt_template"].split())
        assert "Never close an issue" in prompt
        assert 'Only when tracker_writes.create_follow_up is "allow"' in prompt

    def test_marked_for_the_report_pass(self) -> None:
        flow = SimpleNamespace(agent_config=_preset()["agent_config"])
        assert dr.is_delivery_report_flow(flow)
        assert not dr.is_delivery_report_flow(SimpleNamespace(agent_config={}))

    def test_prompt_states_the_honesty_rules(self) -> None:
        prompt = " ".join(_preset()["prompt_template"].split())
        assert 'never "covered"' in prompt
        assert "Never call a change fine without having looked" in prompt
        assert "file and line" in prompt


class TestFixtureWindow:
    """Four merged PRs, two closed issues (one unmet), one aged PR."""

    def test_every_section_is_present(self) -> None:
        report = _window_report()
        for section in dr.SECTIONS:
            assert section in report
        assert report["schema"] == dr.REPORT_SCHEMA

    def test_shipped_lists_the_four_merged_prs(self) -> None:
        report = _window_report()
        refs = [c["ref"] for c in report["shipped"]]
        assert refs == ["acme/app#101", "acme/app#102", "acme/app#103", "acme/app#104"]
        assert report["summary"]["shipped"] == 4

    def test_per_change_assessment(self) -> None:
        by_ref = {c["ref"]: c for c in _window_report()["shipped"]}
        assert by_ref["acme/app#101"]["purpose"]["verdict"] == dr.PURPOSE_COVERED
        assert by_ref["acme/app#102"]["purpose"]["verdict"] == dr.PURPOSE_PARTIAL
        # No linked issue, no criteria.
        assert by_ref["acme/app#104"]["purpose"]["verdict"] == dr.PURPOSE_NO_CRITERIA

    def test_risk_flags_cite_the_files(self) -> None:
        by_ref = {c["ref"]: c for c in _window_report()["shipped"]}
        flags = {f["flag"]: f for f in by_ref["acme/app#103"]["risk_flags"]}
        assert dr.RISK_MIGRATION_WITHOUT_DOWNGRADE in flags
        assert dr.RISK_HOT_PATH_WITHOUT_TESTS in flags
        assert any(
            "versions/0042_add_column.py" in e
            for e in flags[dr.RISK_MIGRATION_WITHOUT_DOWNGRADE]["evidence"]
        )
        # #104 is an 11-line logic change in one frontend module: under the
        # threshold and no view or style file, so no screenshot is owed.
        ui = {f["flag"] for f in by_ref["acme/app#104"]["risk_flags"]}
        assert dr.RISK_UI_WITHOUT_SCREENSHOT not in ui
        assert by_ref["acme/app#101"]["risk_flags"] == []

    def test_closed_issues_and_the_unmet_criterion(self) -> None:
        report = _window_report()
        closed = {i["ref"]: i for i in report["closed_issues"]}
        assert set(closed) == {"acme/app#11", "acme/app#12"}
        assert closed["acme/app#11"]["purpose"]["verdict"] == dr.PURPOSE_COVERED
        assert closed["acme/app#12"]["purpose"]["verdict"] == dr.PURPOSE_PARTIAL
        needs = [i for i in report["needs_you"] if i["kind"] == "closed_unverified"]
        assert [i["ref"] for i in needs] == ["acme/app#12"]
        assert needs[0]["unmet_criteria"] == ["Exports respect the date filter"]
        assert report["summary"]["closed_unverified"] == 1

    def test_aged_pr_is_open_past_window(self) -> None:
        report = _window_report()
        aged = report["open_past_window"]
        assert [(i["kind"], i["ref"]) for i in aged] == [
            ("pull_request", "acme/app#90")
        ]
        assert aged[0]["age_days"] == 18
        assert any(i["kind"] == "aged" for i in report["needs_you"])

    def test_markdown_has_every_heading(self) -> None:
        markdown = _window_report()["report_markdown"]
        for heading in (
            "## Summary",
            "## Needs you",
            "## Shipped",
            "## Closed issues",
            "## Open past the window",
            "## Since the previous report",
            "## What this run spent",
        ):
            assert heading in markdown
        assert "Exports respect the date filter" in markdown
        assert "backend/app/export.py:40" in markdown


class TestHonestyRules:
    def test_criteria_less_issue_is_no_criteria_not_covered(self) -> None:
        purpose = dr.normalize_purpose({"verdict": "covered", "criteria": []})
        assert purpose["verdict"] == dr.PURPOSE_NO_CRITERIA
        assert purpose["reason"] == "no criteria stated"
        assert purpose["claimed_verdict"] == "covered"

    def test_criteria_less_closed_issue_in_a_window(self) -> None:
        issue = {
            "key": "acme/app#30",
            "title": "Tidy logging",
            "url": "https://example.com/acme/app/issues/30",
            "status": "closed",
            "description": "Logs are noisy.",
            "updated_at": "2026-10-15T10:00:00+00:00",
        }
        report = dr.assemble_report(window=WINDOW, pull_requests=[], issues=[issue])
        verdict = report["closed_issues"][0]["purpose"]["verdict"]
        assert verdict == dr.PURPOSE_NO_CRITERIA
        assert report["needs_you"] == []

    def test_covered_without_evidence_is_unverified(self) -> None:
        purpose = dr.normalize_purpose(
            {"verdict": "covered", "criteria": [{"text": "A", "met": True}]}
        )
        assert purpose["verdict"] == dr.PURPOSE_UNVERIFIED
        assert purpose["criteria"][0]["met"] is None

    def test_partial_evidence_is_partial(self) -> None:
        purpose = dr.normalize_purpose(
            {
                "verdict": "covered",
                "criteria": [
                    {"text": "A", "met": True, "evidence": ["a.py:1"]},
                    {"text": "B", "met": True},
                ],
            }
        )
        assert purpose["verdict"] == dr.PURPOSE_PARTIAL

    def test_agent_may_be_more_cautious(self) -> None:
        purpose = dr.normalize_purpose(
            {
                "verdict": "unverified",
                "criteria": [{"text": "A", "met": True, "evidence": ["a.py:1"]}],
            }
        )
        assert purpose["verdict"] == dr.PURPOSE_UNVERIFIED

    def test_risk_flag_without_evidence_is_dropped(self) -> None:
        assert dr.normalize_risk_flags(
            [{"flag": "hot_path_without_tests"}, {"flag": "x", "evidence": ["y"]}]
        ) == [{"flag": "x", "evidence": ["y"]}]

    def test_criteria_from_triage_section_first(self) -> None:
        body = (
            "Intro\n\n## Acceptance\n- body criterion\n\n"
            "<!-- preloop-issue-triage:start -->\n## Triage\n\n"
            "### Acceptance criteria\n- triaged criterion\n"
            "<!-- preloop-issue-triage:end -->"
        )
        assert dr.extract_criteria(body, source="triage_assessment") == [
            "triaged criterion"
        ]
        assert dr.extract_criteria(body, source="description") == ["body criterion"]

    def test_inline_acceptance_paragraph(self) -> None:
        body = "**Why.** x\n\n**Acceptance.** Preset registered and schedulable.\n"
        assert dr.extract_criteria(body, source="triage_assessment") == [
            "Preset registered and schedulable."
        ]


class TestSpendAndDelta:
    def test_spend_is_recorded_and_platform_owned(self) -> None:
        report = dr.finalize_delivery_report(
            {
                "status": "success",
                "spend": {"cost_usd": 0.0, "skipped_for_budget": ["risk_detail"]},
            },
            cost_usd=1.23456,
            total_tokens=4200,
            limit_usd=5.0,
        )
        assert report["spend"] == {
            "cost_usd": 1.2346,
            "total_tokens": 4200,
            "limit_usd": 5.0,
            "skipped_for_budget": ["risk_detail"],
            "source": "gateway",
        }
        assert "$1.23 of a $5.00 ceiling" in report["report_markdown"]

    def test_unpriced_spend_is_unknown_not_zero(self) -> None:
        report = dr.finalize_delivery_report({"status": "success"})
        assert report["spend"]["cost_usd"] is None
        assert "unknown" in report["report_markdown"]

    def test_previous_open_questions_appear_in_the_delta(self) -> None:
        report = _window_report(previous=_load("previous_report.json"))
        questions = report["delta"]["open_questions"]
        assert questions == [
            {
                "question": "Does the export change cover the date filter?",
                "ref": "acme/app#102",
                "status": "answered",
            },
            {
                "question": "Who owns the stalled search PR?",
                "ref": "acme/app#90",
                "status": "still_open",
            },
        ]
        assert report["delta"]["previous_report"]["shipped"] == 3
        assert "Who owns the stalled search PR?" in report["report_markdown"]

    def test_no_previous_report(self) -> None:
        report = _window_report()
        assert report["delta"] == {"previous_report": None, "open_questions": []}

    def test_malformed_result_becomes_error_report(self) -> None:
        report = dr.finalize_delivery_report("not json")
        assert report["status"] == "error"
        assert "spend" in report

    def test_finalize_for_execution_reads_previous_and_usage(self) -> None:
        from preloop.services.flow_execution_limits import ExecutionUsage

        previous = SimpleNamespace(result=_load("previous_report.json"))
        flow = SimpleNamespace(
            id="flow-1",
            account_id="acct-1",
            agent_config={"report_kind": "delivery_report", "limits": {"max_usd": 5}},
        )
        execution = SimpleNamespace(id="exec-2")
        with (
            patch(
                "preloop.models.crud.crud_flow_execution.latest_with_result",
                return_value=previous,
            ) as latest,
            patch(
                "preloop.services.flow_execution_limits.get_execution_usage",
                return_value=ExecutionUsage(total_tokens=10, cost_usd=0.5, turns=2),
            ),
        ):
            report = dr.finalize_for_execution(
                object(), flow=flow, execution=execution, result={"status": "success"}
            )
        assert latest.call_args.kwargs["exclude_execution_id"] == "exec-2"
        assert latest.call_args.kwargs["account_id"] == "acct-1"
        assert latest.call_args.kwargs["succeeded_only"] is True
        assert report["spend"]["cost_usd"] == 0.5
        assert report["spend"]["limit_usd"] == 5.0
        statuses: List[str] = [q["status"] for q in report["delta"]["open_questions"]]
        assert statuses == ["still_open", "still_open"]


class TestReviewFollowUps:
    @pytest.mark.parametrize(
        "purpose",
        ["covered", 3, ["covered"], None, {"verdict": ["covered"], "criteria": "x"}],
    )
    def test_malformed_purpose_still_gets_the_honesty_pass(self, purpose: Any) -> None:
        report = dr.finalize_delivery_report(
            {
                "status": "success",
                "shipped": [{"ref": "acme/app#1", "purpose": purpose}],
            }
        )
        assert report["shipped"][0]["purpose"]["verdict"] == dr.PURPOSE_NO_CRITERIA
        assert "spend" in report and "report_markdown" in report

    def test_this_periods_questions_are_rendered(self) -> None:
        report = dr.finalize_delivery_report(
            {
                "status": "success",
                "open_questions": [
                    {"question": "Is the cache warmed?", "ref": "acme/app#7"}
                ],
            }
        )
        assert "Is the cache warmed? (acme/app#7)" in report["report_markdown"]

    def test_still_open_questions_are_persisted_for_the_next_run(self) -> None:
        previous = _load("previous_report.json")
        report = dr.finalize_delivery_report(
            {"status": "error", "reason": "budget"}, previous=previous
        )
        assert report["open_questions"] == [
            {
                "question": "Does the export change cover the date filter?",
                "ref": "acme/app#102",
            },
            {"question": "Who owns the stalled search PR?", "ref": "acme/app#90"},
        ]
        # The failed run's result, used as the next baseline, keeps them.
        following = dr.finalize_delivery_report({"status": "success"}, previous=report)
        assert [q["status"] for q in following["delta"]["open_questions"]] == [
            "still_open",
            "still_open",
        ]

    def test_a_carried_question_is_not_duplicated(self) -> None:
        previous = _load("previous_report.json")
        report = dr.finalize_delivery_report(
            {
                "status": "success",
                "open_questions": [
                    {
                        "question": "Who owns the stalled search PR?",
                        "ref": "acme/app#90",
                    }
                ],
            },
            previous=previous,
        )
        texts = [q["question"] for q in report["open_questions"]]
        assert texts.count("Who owns the stalled search PR?") == 1

    def test_gitlab_file_shape_is_read(self) -> None:
        pr = {
            "description": "",
            "changes": {
                "changed_files": [
                    {
                        "new_path": "db/migrations/0001_init.py",
                        "diff": "+def upgrade():\n+    pass\n",
                    }
                ]
            },
        }
        flags = {f["flag"] for f in dr.detect_risk_flags(pr)}
        assert dr.RISK_MIGRATION_WITHOUT_DOWNGRADE in flags

    def test_missing_patch_is_not_a_missing_downgrade(self) -> None:
        pr = {"changes": {"changed_files": [{"path": "db/migrations/0001_init.py"}]}}
        flags = {f["flag"] for f in dr.detect_risk_flags(pr)}
        assert dr.RISK_MIGRATION_WITHOUT_DOWNGRADE not in flags


class TestOrchestratorHook:
    def _orchestrator(self, agent_config: Dict[str, Any]) -> Any:
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = object.__new__(FlowExecutionOrchestrator)
        orchestrator.db = object()
        orchestrator.execution_log = SimpleNamespace(id="exec-1")
        orchestrator.flow = SimpleNamespace(
            id="flow-1", account_id="acct-1", agent_config=agent_config
        )
        return orchestrator

    def test_marked_flow_result_is_finalized(self) -> None:
        orchestrator = self._orchestrator({"report_kind": "delivery_report"})
        with (
            patch(
                "preloop.models.crud.crud_flow_execution.latest_with_result",
                return_value=None,
            ),
            patch(
                "preloop.services.flow_execution_limits.get_execution_usage",
                side_effect=RuntimeError("no db"),
            ),
        ):
            result = orchestrator._finalize_delivery_report({"status": "success"})
        assert result["schema"] == dr.REPORT_SCHEMA
        for key in ("report_markdown", "delta", "spend", "summary"):
            assert key in result
        assert result["spend"]["cost_usd"] is None

    def test_other_flows_are_untouched(self) -> None:
        orchestrator = self._orchestrator({})
        original = {"status": "success"}
        assert orchestrator._finalize_delivery_report(original) is original

    def test_a_failing_pass_keeps_the_agent_result(self) -> None:
        orchestrator = self._orchestrator({"report_kind": "delivery_report"})
        original = {"status": "success"}
        with patch(
            "preloop.services.flow_orchestrator.finalize_delivery_report_for_execution",
            side_effect=RuntimeError("boom"),
        ):
            assert orchestrator._finalize_delivery_report(original) is original


# ---------------------------------------------------------------------------
# Alignment with audits, briefs and the measured run (#2029)
# ---------------------------------------------------------------------------


def _executions() -> List[SimpleNamespace]:
    from datetime import datetime

    rows = []
    for raw in _load("merge_audit_executions.json"):
        rows.append(
            SimpleNamespace(
                id=raw["id"],
                start_time=datetime.fromisoformat(raw["start_time"]),
                end_time=None,
                trigger_event_details=raw["trigger_event_details"],
                result=raw["result"],
            )
        )
    return rows


def _audits() -> List[Dict[str, Any]]:
    return dr.merge_audits_document(_executions())["audits"]


def _pr(files: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    return {"description": "", "changes": {"changed_files": files}, **extra}


class TestMergeAuditReuse:
    def test_only_audits_at_the_closing_revision_are_kept(self) -> None:
        audits = _audits()
        # The mismatched checkout and the deployment audit are dropped, and
        # the newest audit per issue wins over the older one.
        assert len(audits) == 1
        audit = audits[0]
        assert audit["issue_ref"] == "acme/app#12"
        assert audit["merge_sha"] == "2" * 40
        assert [c["met"] for c in audit["purpose"]["criteria"]] == [True, True]
        assert audit["purpose"]["criteria"][0]["text"] == "CSV export includes headers"

    def test_only_supplied_runtime_evidence_counts(self) -> None:
        execution = _executions()[0]
        lifecycle = execution.trigger_event_details["payload"]["lifecycle"]
        lifecycle["runtime_evidence"] = {"artifacts": [{"id": "artifact:55"}]}
        first, second = execution.result["criteria"]
        execution.result = {
            **execution.result,
            "criteria": [
                {**first, "code": [], "tests": [], "runtime": ["artifact:55"]},
                # The controller dropped this invented id and the verdict.
                {**second, "code": [], "tests": [], "runtime": ["preview:404"]},
            ],
        }
        record = dr.merge_audit_record(execution)
        assert record is not None
        headers, date_filter = record["purpose"]["criteria"]
        assert headers["met"] is True
        assert headers["evidence"] == ["export output inspected", "artifact:55"]
        assert date_filter["met"] is None
        assert "preview:404" not in date_filter["evidence"]

    def test_audited_closed_issue_takes_the_audit(self) -> None:
        report = _window_report(merge_audits=_audits())
        closed = {i["ref"]: i for i in report["closed_issues"]}
        purpose = closed["acme/app#12"]["purpose"]
        # The run's own pass said the date filter was not met; the audit at
        # the closing revision decides instead.
        assert purpose["verdict"] == dr.PURPOSE_COVERED
        assert purpose["source"] == dr.SOURCE_AUDIT
        assert purpose["revision"] == "2" * 40
        shipped = {c["ref"]: c for c in report["shipped"]}
        assert shipped["acme/app#102"]["purpose"]["source"] == dr.SOURCE_AUDIT
        assert closed["acme/app#11"]["purpose"]["source"] != dr.SOURCE_AUDIT
        assert report["summary"]["assessment_sources"][dr.SOURCE_AUDIT] == 1

    def test_audit_at_another_revision_is_not_used(self) -> None:
        report = dr.finalize_delivery_report(
            {
                "closed_issues": [
                    {
                        "ref": "acme/app#12",
                        "closing_sha": "9" * 40,
                        "purpose": {"criteria": ["CSV export includes headers"]},
                    }
                ]
            },
            merge_audits=_audits(),
        )
        purpose = report["closed_issues"][0]["purpose"]
        assert purpose["source"] == dr.SOURCE_NONE
        assert purpose["verdict"] == dr.PURPOSE_UNVERIFIED

    def test_audits_are_seeded_beside_the_previous_report(self) -> None:
        import base64

        document = dr.merge_audits_document(_executions())
        trigger = {"payload": {"previous_result_execution_id": "last"}}
        seeded = dr.with_merge_audits_seed(trigger, document)
        assert seeded is not None and "workspace_files" not in trigger["payload"]
        (seed,) = seeded["payload"]["workspace_files"]
        assert seed["path"] == "previous/merge_audits.json"
        decoded = json.loads(base64.b64decode(seed["content_base64"]))
        assert decoded["schema"] == dr.MERGE_AUDITS_SCHEMA
        assert decoded["audits"][0]["issue_ref"] == "acme/app#12"

    def test_a_declared_seed_at_the_path_wins(self) -> None:
        trigger = {
            "payload": {
                "workspace_files": [
                    {"path": "previous/merge_audits.json", "content_base64": "e30="}
                ]
            }
        }
        assert dr.with_merge_audits_seed(trigger, {"audits": []}) is trigger

    def test_oversized_document_is_truncated(self) -> None:
        many = _executions()[:1] * 1
        document = dr.merge_audits_document(many, max_bytes=100)
        assert document["truncated"] is True
        assert document["audits"] == []

    def test_finalize_for_execution_applies_the_audits(self) -> None:
        flow = SimpleNamespace(id="flow-1", account_id="acct-1", agent_config={})
        result = {
            "window": WINDOW,
            "closed_issues": [
                {
                    "ref": "acme/app#12",
                    "url": "https://example.com/acme/app/issues/12",
                    "purpose": {"criteria": []},
                }
            ],
        }
        with (
            patch(
                "preloop.models.crud.crud_flow_execution.latest_with_result",
                return_value=None,
            ),
            patch(
                "preloop.models.crud.crud_flow_execution.completed_merge_audits",
                return_value=_executions(),
            ) as audits,
            patch(
                "preloop.services.flow_execution_limits.get_execution_usage",
                side_effect=RuntimeError("no db"),
            ),
        ):
            report = dr.finalize_for_execution(
                object(), flow=flow, execution=SimpleNamespace(id="e"), result=result
            )
        assert audits.call_args.kwargs["account_id"] == "acct-1"
        assert audits.call_args.kwargs["since"].isoformat() == "2026-10-12T07:00:00"
        assert report["closed_issues"][0]["purpose"]["source"] == dr.SOURCE_AUDIT

    def test_orchestrator_seeds_audits_for_report_flows(self) -> None:
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = object.__new__(FlowExecutionOrchestrator)
        orchestrator.db = object()
        orchestrator.flow = SimpleNamespace(
            id="flow-1", account_id="acct-1", agent_config={}
        )
        with patch(
            "preloop.models.crud.crud_flow_execution.completed_merge_audits",
            return_value=_executions(),
        ):
            data = orchestrator._delivery_report_inputs({"payload": {"window": WINDOW}})
        assert data is not None
        paths = [s["path"] for s in data["payload"]["workspace_files"]]
        assert paths == [
            "tools/delivery_report_checks.py",
            "previous/merge_audits.json",
        ]

    def test_a_failed_audit_read_still_seeds_the_checks(self) -> None:
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = object.__new__(FlowExecutionOrchestrator)
        orchestrator.db = object()
        orchestrator.flow = SimpleNamespace(id="f", account_id="a", agent_config={})
        trigger = {"payload": {}}
        with patch(
            "preloop.models.crud.crud_flow_execution.completed_merge_audits",
            side_effect=RuntimeError("db down"),
        ):
            data = orchestrator._delivery_report_inputs(trigger)
        assert trigger == {"payload": {}}
        assert data is not None
        paths = [s["path"] for s in data["payload"]["workspace_files"]]
        assert paths == ["tools/delivery_report_checks.py"]


class TestOrchestratorPolicy:
    def _orchestrator(self) -> Any:
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = object.__new__(FlowExecutionOrchestrator)
        orchestrator.db = object()
        orchestrator.execution_log = SimpleNamespace(id="exec-1")
        orchestrator.trigger_event_data = {"payload": {"window": WINDOW}}
        orchestrator.flow = SimpleNamespace(
            id="flow-1",
            account_id="acct-1",
            agent_config={"report_kind": "delivery_report"},
        )
        return orchestrator

    def test_an_assessed_run_receives_the_policy(self) -> None:
        orchestrator = self._orchestrator()
        run = dr.PolicyRun(decision="onboarding", policy=dr.DeliveryReportPolicy())
        with (
            patch("preloop.services.delivery_report.plan_policy_run", return_value=run),
            patch(
                "preloop.models.crud.crud_flow_execution.completed_merge_audits",
                return_value=[],
            ),
        ):
            data = orchestrator._delivery_report_inputs({"payload": {"window": WINDOW}})
        assert data is not None
        block = data["payload"]["report_policy"]
        assert block["decision"] == "onboarding"
        assert block["policy"]["tracker_writes"]["close"] == "deny"

    def test_a_run_that_may_not_call_a_model_builds_the_report_itself(self) -> None:
        orchestrator = self._orchestrator()
        run = dr.PolicyRun(
            decision="skipped_no_credit", policy=dr.DeliveryReportPolicy()
        )
        built = dr.assemble_report(
            window=WINDOW, pull_requests=[], issues=[], assessment=run.decision
        )
        with patch(
            "preloop.services.delivery_report.deterministic_report", return_value=built
        ) as build:
            result = orchestrator._run_deterministic_delivery_report(run)
        assert build.call_args.kwargs["window"]["to"] == WINDOW["to"]
        assert result["status"] == "SUCCEEDED"
        assert result["result"]["summary"]["assessments_skipped_reason"] == "no_credit"

    def test_an_unreadable_policy_keeps_the_preset_defaults(self) -> None:
        orchestrator = self._orchestrator()
        with patch(
            "preloop.services.delivery_report.plan_policy_run",
            side_effect=RuntimeError("db down"),
        ):
            assert orchestrator._plan_delivery_report_run() is None

    def test_a_disabled_report_is_not_finalized(self) -> None:
        orchestrator = self._orchestrator()
        orchestrator._delivery_report_run = dr.PolicyRun(
            decision="disabled", policy=dr.DeliveryReportPolicy(enabled=False)
        )
        result = {"status": "disabled", "reason": "off"}
        assert orchestrator._finalize_delivery_report(result) is result


class TestBriefsAndSummaries:
    def test_brief_sections_are_parsed(self) -> None:
        brief = dr.parse_change_brief((FIXTURES / "pr_body_brief.md").read_text())
        assert brief is not None
        assert brief["what_changed"][0].startswith("backend/app/export.py")
        assert brief["why"] == ["#12 asks for CSV export"]
        assert len(brief["not_verified"]) == 2
        assert brief["rollback"] == ["revert is clean"]
        assert brief["decisions"][0].startswith("Stream rows")

    def test_missing_sections_and_none_bullets_are_empty(self) -> None:
        body = (
            "<!-- PRELOOP_BRIEF -->\n### What changed\n- a.py: x\n"
            "### Not verified\n- none\n<!-- /PRELOOP_BRIEF -->"
        )
        brief = dr.parse_change_brief(body)
        assert brief is not None
        assert brief["not_verified"] == []
        assert brief["assumptions"] == []
        assert brief["what_changed"] == ["a.py: x"]

    def test_unterminated_brief_still_parses(self) -> None:
        brief = dr.parse_change_brief(
            "<!-- PRELOOP_BRIEF -->\n### Not verified\n- p95 after deploy\n"
        )
        assert brief is not None and brief["not_verified"] == ["p95 after deploy"]

    def test_no_brief_is_none(self) -> None:
        assert dr.parse_change_brief("Fixes #1") is None
        assert dr.parse_change_brief(None) is None
        assert dr.parse_review_summary("Fixes #1") is None

    def test_review_summary_lines(self) -> None:
        summary = dr.parse_review_summary((FIXTURES / "pr_body_summary.md").read_text())
        assert summary == {
            "risk_level": "low",
            "not_verified": ["Rendering in the light theme"],
            "must_know": ["frontend/src/report-card.ts now formats dates in UTC"],
        }

    def test_summary_without_the_new_lines(self) -> None:
        summary = dr.parse_review_summary(
            "<!-- PRELOOP_SUMMARY -->\n> **Medium**\n> x\n>\n> **Overview**\n"
            "> y\n<!-- /PRELOOP_SUMMARY -->"
        )
        assert summary == {"risk_level": "medium", "not_verified": [], "must_know": []}

    def test_not_verified_bullets_become_evidence_gaps(self) -> None:
        prs = _load("pull_requests.json")
        for pr in prs:
            if pr["number"] == 102:
                pr["description"] = (FIXTURES / "pr_body_brief.md").read_text()
        report = dr.assemble_report(
            window=WINDOW, pull_requests=prs, issues=_load("issues.json")
        )
        change = {c["ref"]: c for c in report["shipped"]}["acme/app#102"]
        assert len(change["evidence_gaps"]) == 2
        assert report["summary"]["evidence_gaps"] == 2
        assert "Evidence gap (stated in the PR)" in report["report_markdown"]

    def test_described_pr_skips_the_model_risk_pass(self) -> None:
        body = (FIXTURES / "pr_body_summary.md").read_text()
        quiet = _pr(
            [{"filename": "backend/app/x.py"}, {"filename": "backend/tests/test_x.py"}],
            description=body,
        )
        assert dr.detect_risk_flags(quiet) == []
        assert dr.needs_model_risk_pass(quiet) is False
        undescribed = dict(quiet, description="")
        assert dr.needs_model_risk_pass(undescribed) is True

    def test_a_deterministic_flag_brings_the_pass_back(self) -> None:
        body = (FIXTURES / "pr_body_summary.md").read_text()
        flagged = _pr([{"filename": "backend/app/auth/session.py"}], description=body)
        assert dr.needs_model_risk_pass(flagged) is True


class TestDeterministicRiskRules:
    @pytest.mark.parametrize(
        ("path", "label"),
        [
            ("backend/preloop/api/auth/jwt.py", "auth_session"),
            ("backend/app/session_store.py", "auth_session"),
            ("backend/alembic/versions/0042_add.py", "migration"),
            ("backend/app/billing/stripe.py", "billing_payments"),
            ("openapi.yaml", "public_api"),
            ("backend/preloop/api/endpoints/flows.py", "public_api"),
            (".github/workflows/release.yml", "ci_release"),
            ("backend/app/secrets.py", "secrets"),
        ],
    )
    def test_path_classes(self, path: str, label: str) -> None:
        assert dr.path_class(path) == label
        flags = {f["flag"]: f for f in dr.detect_risk_flags(_pr([{"filename": path}]))}
        assert f"{label}: {path}" in flags[dr.RISK_SENSITIVE_PATH]["evidence"]

    @pytest.mark.parametrize(
        "path", ["backend/app/author.py", "frontend/src/keyboard.ts", "README.md"]
    )
    def test_ordinary_paths_have_no_class(self, path: str) -> None:
        assert dr.path_class(path) is None

    def test_logic_only_frontend_touch_needs_no_screenshot(self) -> None:
        pr = _pr(
            [
                {"filename": "backend/app/auth.py", "additions": 80},
                {"filename": "frontend/src/api/client.ts", "additions": 6},
            ]
        )
        flags = {f["flag"] for f in dr.detect_risk_flags(pr)}
        assert dr.RISK_UI_WITHOUT_SCREENSHOT not in flags

    def test_style_or_view_change_needs_a_screenshot(self) -> None:
        for name in ("frontend/src/styles/theme.css", "frontend/src/views/home.ts"):
            pr = _pr([{"filename": name, "additions": 2}])
            flags = {f["flag"] for f in dr.detect_risk_flags(pr)}
            assert dr.RISK_UI_WITHOUT_SCREENSHOT in flags, name

    def test_large_frontend_change_needs_a_screenshot(self) -> None:
        pr = _pr([{"filename": "frontend/src/components/card.ts", "additions": 41}])
        flags = {f["flag"]: f for f in dr.detect_risk_flags(pr)}
        assert "threshold 40" in flags[dr.RISK_UI_WITHOUT_SCREENSHOT]["evidence"][0]
        with_image = dict(pr, description="![after](https://example.com/a.png)")
        assert dr.RISK_UI_WITHOUT_SCREENSHOT not in {
            f["flag"] for f in dr.detect_risk_flags(with_image)
        }

    def test_merge_revision_raises_no_downgrade_flag(self) -> None:
        patch_text = (
            '+revision = "abc"\n+down_revision = ("def", "ghi")\n'
            "+def upgrade():\n+    pass\n+\n+def downgrade():\n+    pass\n"
        )
        pr = _pr(
            [{"filename": "backend/alembic/versions/abc_merge.py", "patch": patch_text}]
        )
        assert dr.RISK_MIGRATION_WITHOUT_DOWNGRADE not in {
            f["flag"] for f in dr.detect_risk_flags(pr)
        }
        typed = patch_text.replace(
            "down_revision =", "down_revision: Union[str, Sequence[str], None] ="
        )
        assert dr.is_merge_revision(typed)
        single = patch_text.replace('("def", "ghi")', '"def"')
        assert dr.RISK_MIGRATION_WITHOUT_DOWNGRADE in {
            f["flag"]
            for f in dr.detect_risk_flags(
                _pr([{"filename": "backend/alembic/versions/x.py", "patch": single}])
            )
        }

    def test_short_file_list_is_flagged(self) -> None:
        files = [{"filename": f"src/f{i}.py"} for i in range(100)]
        pr = {
            "description": "",
            "changes": {"files_changed": 144, "changed_files": files},
        }
        flags = {f["flag"]: f for f in dr.detect_risk_flags(pr)}
        assert (
            "144 files; 100 were listed"
            in (flags[dr.RISK_FILE_LIST_INCOMPLETE]["evidence"][0])
        )
        complete = {
            "description": "",
            "changes": {"files_changed": 100, "changed_files": files},
        }
        assert dr.RISK_FILE_LIST_INCOMPLETE not in {
            f["flag"] for f in dr.detect_risk_flags(complete)
        }


class TestDenominators:
    def test_summary_carries_separate_numbers(self) -> None:
        summary = _window_report()["summary"]
        assert summary["linkage"] == {"linked": 3, "total": 4}
        assert summary["criteria_availability"] == {"with_criteria": 2, "total": 2}
        assert summary["evidence_availability"] == {"with_evidence": 2, "total": 3}
        assert summary["closed_verdicts"] == {
            "covered": 1,
            "partial": 1,
            "unverified": 0,
            "no_criteria": 0,
        }
        assert "score" not in json.dumps(summary)

    def test_markdown_counts_match_the_json(self) -> None:
        report = _window_report()
        summary = report["summary"]
        text = report["report_markdown"]
        assert f"Linkage: {summary['linkage']['linked']} of 4 shipped" in text
        assert "Criteria available: 2 of 2 closed issues" in text
        assert "Evidence available: 2 of 3 of their criteria" in text
        verdicts = summary["closed_verdicts"]
        assert (
            f"{verdicts['covered']} covered, {verdicts['partial']} partial, "
            f"{verdicts['unverified']} unverified, {verdicts['no_criteria']} with "
            "no criteria" in text
        )

    def test_unverified_is_described_as_an_evidence_gap(self) -> None:
        report = dr.finalize_delivery_report(
            {"closed_issues": [{"ref": "acme/app#1", "purpose": {"criteria": ["x"]}}]}
        )
        text = report["report_markdown"]
        assert "unverified (evidence gap)" in text
        assert "Closed with an evidence gap" in text
        assert "It is not a failure" in text
        assert "failed" not in text.lower()

    def test_assessments_skipped_for_budget_is_a_flag(self) -> None:
        report = dr.finalize_delivery_report(
            {"spend": {"skipped_for_budget": ["closed-issue assessments"]}}
        )
        assert report["summary"]["assessments_skipped_for_budget"] is True
        assert "Assessments were skipped for budget" in report["report_markdown"]
        assert (
            dr.finalize_delivery_report({})["summary"]["assessments_skipped_for_budget"]
            is False
        )


class TestPolicyDefaults:
    def test_tracker_writes_start_off(self) -> None:
        writes = dr.DeliveryReportPolicy().tracker_writes
        assert (writes.create_follow_up, writes.reopen, writes.close) == (
            "deny",
            "approval",
            "deny",
        )

    def test_budgets_and_modes(self) -> None:
        default = dr.DeliveryReportPolicy()
        assert (default.per_run_usd, default.monthly_usd) == (5.0, 25.0)
        # Deterministic is the default; recurring assessment is opted into.
        assert default.report_mode == "deterministic"
        assert default.assessments_allowed(0.0) is False
        policy = dr.DeliveryReportPolicy(report_mode="assessed")
        assert policy.assessments_allowed(24.99) is True
        assert policy.assessments_allowed(25.0) is False
        assert policy.assessments_allowed(None) is True
        deterministic = dr.DeliveryReportPolicy(report_mode="deterministic")
        assert deterministic.assessments_allowed(0.0) is False

    def test_preset_states_the_promise(self) -> None:
        promise = (
            "See what landed, what the available evidence supports against "
            "stated criteria, and which gaps deserve investigation."
        )
        assert _preset()["description"].startswith(promise)


class TestMeasuredRunRules:
    def test_only_comments_after_the_last_criteria_edit(self) -> None:
        issue = {
            "description": "## Acceptance\n- p95 under 1.5 s\n",
            "comments": [
                {"created_at": "2026-10-10T09:00:00Z", "body": "measured 4 s"},
                {
                    "created_at": "2026-10-12T09:00:00Z",
                    "body": "Revised.\n\n## Acceptance\n- p95 under 1.5 s grouped\n",
                },
                {"created_at": "2026-10-19T08:39:00Z", "body": "grouped p95 3.13 s"},
            ],
        }
        comments = dr.comments_after_last_criteria_edit(issue)
        assert [c["body"] for c in comments] == ["grouped p95 3.13 s"]

    def test_no_criteria_needs_no_comments(self) -> None:
        issue = {"description": "Make it faster", "comments": [{"body": "x"}]}
        assert dr.comments_after_last_criteria_edit(issue) == []

    def test_reopen_request_needs_a_revision_and_evidence(self) -> None:
        report = dr.finalize_delivery_report(
            {
                "reopen_requests": [
                    {
                        "ref": "acme/app#7",
                        "revision": "a" * 40,
                        "reason": "last comment measures p95 3.13 s against 1.5 s",
                        "evidence": ["https://example.com/acme/app/issues/7#c3"],
                    },
                    {"ref": "acme/app#8", "evidence": ["x"]},
                ]
            }
        )
        assert [r["ref"] for r in report["reopen_requests"]] == ["acme/app#7"]
        assert report["needs_you"][0]["kind"] == "reopen_request"
        assert "Proposed reopen (assessed at " in report["report_markdown"]

    def test_prompt_states_the_measured_rules(self) -> None:
        prompt = _preset()["prompt_template"]
        for phrase in (
            "previous/merge_audits.json",
            "<!-- PRELOOP_BRIEF -->",
            "the comments after the last comment that",
            "reopen_requests",
            "tools/delivery_report_checks.py parent-check",
            "tools/delivery_report_checks.py scope",
            "updated_at and closing PR head SHA",
            "metadata_only",
            "down_revision is a tuple",
            "never count in prose",
        ):
            assert phrase in prompt, phrase

    def test_unchanged_closed_issue_keeps_the_previous_assessment(self) -> None:
        cached_purpose = {
            "verdict": "covered",
            "source": "report",
            "criteria": [{"text": "x", "met": True, "evidence": ["a.py:1"]}],
        }
        previous = {
            "schema": dr.REPORT_SCHEMA,
            "closed_issues": [
                {
                    "ref": "acme/app#5",
                    "cache_key": dr.assessment_cache_key(
                        "2026-10-01T00:00:00Z", "c" * 40
                    ),
                    "purpose": cached_purpose,
                }
            ],
        }
        current = {
            "closed_issues": [
                {
                    "ref": "acme/app#5",
                    "updated_at": "2026-10-01T00:00:00+00:00",
                    "closing_head_sha": "c" * 40,
                    "purpose": {"criteria": ["x"]},
                }
            ]
        }
        report = dr.finalize_delivery_report(current, previous=previous)
        purpose = report["closed_issues"][0]["purpose"]
        assert (purpose["verdict"], purpose["source"]) == ("covered", "cache")
        edited = json.loads(json.dumps(current))
        edited["closed_issues"][0]["updated_at"] = "2026-10-20T00:00:00Z"
        purpose = dr.finalize_delivery_report(edited, previous=previous)[
            "closed_issues"
        ][0]["purpose"]
        assert purpose["source"] == "none"

    def test_cache_key_needs_both_parts(self) -> None:
        assert dr.assessment_cache_key(None, "c" * 40) is None
        assert dr.assessment_cache_key("2026-10-01T00:00:00Z", "") is None

    def test_dependabot_and_docs_prs_are_metadata_only(self) -> None:
        bump = _pr([{"filename": "package-lock.json"}], author="dependabot[bot]")
        docs = _pr([{"filename": "docs/guide.md"}, {"filename": "README.md"}])
        code = _pr([{"filename": "backend/app/x.py"}])
        assert dr.metadata_only_reason(bump) == "dependabot"
        assert dr.metadata_only_reason(docs) == "docs_only"
        assert dr.metadata_only_reason(code) is None
        assert dr.needs_model_risk_pass(bump) is False

    def test_metadata_only_pr_ignores_an_assessment(self) -> None:
        prs = _load("pull_requests.json")
        prs.append(
            {
                "number": 105,
                "title": "Bump lodash",
                "author": "dependabot[bot]",
                "description": "",
                "state": "closed",
                "url": "https://example.com/acme/app/pull/105",
                "merged_at": "2026-10-16T12:00:00+00:00",
                "changes": {"changed_files": [{"filename": "package-lock.json"}]},
            }
        )
        assessments = dict(_load("assessments.json"))
        assessments["acme/app#105"] = {"purpose": {"verdict": "covered"}}
        report = dr.assemble_report(
            window=WINDOW,
            pull_requests=prs,
            issues=_load("issues.json"),
            assessments=assessments,
        )
        change = {c["ref"]: c for c in report["shipped"]}["acme/app#105"]
        assert change["metadata_only"] == "dependabot"
        assert change["purpose"]["source"] == dr.SOURCE_METADATA
        assert change["purpose"]["verdict"] == dr.PURPOSE_NO_CRITERIA
        assert "dependabot, reported from metadata" in report["report_markdown"]


class TestReviewFindings:
    def test_shipped_change_ignores_an_audit_at_another_revision(self) -> None:
        report = dr.finalize_delivery_report(
            {
                "shipped": [
                    {
                        "ref": "acme/app#102",
                        "project": "acme/app",
                        "issues": ["#12"],
                        "merge_sha": "9" * 40,
                        "purpose": {"criteria": ["CSV export includes headers"]},
                    }
                ]
            },
            merge_audits=_audits(),
        )
        assert report["shipped"][0]["purpose"]["source"] == dr.SOURCE_NONE

    def test_a_copied_cache_key_is_recomputed(self) -> None:
        previous = {
            "schema": dr.REPORT_SCHEMA,
            "closed_issues": [
                {
                    "ref": "acme/app#5",
                    "cache_key": "old",
                    "purpose": {"verdict": "covered", "source": "report"},
                }
            ],
        }
        current = {
            "closed_issues": [
                {
                    "ref": "acme/app#5",
                    "cache_key": "old",
                    "updated_at": "2026-10-20T00:00:00Z",
                    "closing_head_sha": "c" * 40,
                    "purpose": {"criteria": ["x"]},
                }
            ]
        }
        issue = dr.finalize_delivery_report(current, previous=previous)[
            "closed_issues"
        ][0]
        assert issue["cache_key"] != "old"
        assert issue["purpose"]["source"] == dr.SOURCE_NONE

    @pytest.mark.parametrize("payload", [["x"], "text", 3])
    def test_a_non_mapping_payload_is_left_alone(self, payload: Any) -> None:
        trigger = {"payload": payload}
        assert dr.with_merge_audits_seed(trigger, {"audits": []}) is trigger

    def test_closed_issue_carries_the_latest_comment(self) -> None:
        issues = _load("issues.json")
        issues[0]["comments"] = [
            {"author": "jane-doe", "created_at": "2026-10-13T10:00:00Z", "body": "ok"},
            {
                "author": "john-doe",
                "created_at": "2026-10-13T11:00:00Z",
                "body": "measured: title search misses prefixes",
            },
        ]
        report = dr.assemble_report(
            window=WINDOW, pull_requests=_load("pull_requests.json"), issues=issues
        )
        closed = {i["ref"]: i for i in report["closed_issues"]}
        latest = closed["acme/app#11"]["latest_comment"]
        assert latest["body"] == "measured: title search misses prefixes"
        assert closed["acme/app#12"]["latest_comment"] is None


class TestPolicyResolution:
    def test_defaults_resolve_as_stated(self) -> None:
        policy = dr.resolve_policy(None, None)
        assert policy.enabled is True
        assert (policy.cadence, policy.weekday) == ("weekly", "mon")
        assert policy.report_mode == "deterministic"
        assert policy.onboarding_assessed_run is True
        assert policy.recipients == "account_owner"
        assert policy.tracker_writes.create_follow_up == "deny"
        assert policy.tracker_writes.close == "deny"

    def test_project_overrides_layer_on_the_account(self) -> None:
        account = {"pr_ready_days": 3, "tracker_writes": {"create_follow_up": "allow"}}
        project = {"pr_ready_days": 10, "per_run_usd": 99.0}
        policy = dr.resolve_policy(account, project)
        assert policy.pr_ready_days == 10
        # Budgets are the account's; a stored project budget is ignored.
        assert policy.per_run_usd == 5.0
        assert policy.tracker_writes.create_follow_up == "allow"
        assert policy.tracker_writes.reopen == "approval"

    def test_a_stored_layer_that_no_longer_validates_is_skipped(self) -> None:
        policy = dr.resolve_policy({"pr_ready_days": 0, "unknown": 1})
        assert policy.pr_ready_days == 7

    def test_close_allowed_is_rejected(self) -> None:
        with pytest.raises(dr.PolicyOverrideError) as err:
            dr.validate_account_overrides({"tracker_writes": {"close": "allow"}})
        assert err.value.code == "close_not_allowed"

    def test_recurring_assessment_needs_both_budgets(self) -> None:
        with pytest.raises(dr.PolicyOverrideError) as err:
            dr.validate_account_overrides({"report_mode": "assessed"})
        assert err.value.code == "budget_required"
        assert (
            dr.validate_account_overrides(
                {"report_mode": "assessed", "per_run_usd": 2.0, "monthly_usd": 10.0}
            )["report_mode"]
            == "assessed"
        )

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(dr.PolicyOverrideError):
            dr.validate_account_overrides({"bogus": True})

    def test_project_rejects_account_settings(self) -> None:
        with pytest.raises(dr.PolicyOverrideError) as err:
            dr.validate_project_overrides({"monthly_usd": 1.0})
        assert err.value.code == "account_setting"
        assert dr.validate_project_overrides({"enabled": False}) == {"enabled": False}


class TestAssessmentDecision:
    def test_first_run_is_the_onboarding_assessment(self) -> None:
        policy = dr.DeliveryReportPolicy()
        assert dr.assessment_decision(policy, has_previous_report=False) == "onboarding"
        assert (
            dr.assessment_decision(policy, has_previous_report=True) == "deterministic"
        )

    def test_recurrence_respects_the_monthly_budget(self) -> None:
        policy = dr.DeliveryReportPolicy(report_mode="assessed", monthly_usd=10.0)
        assert (
            dr.assessment_decision(
                policy, has_previous_report=True, spent_this_month_usd=9.0
            )
            == "assessed"
        )
        assert (
            dr.assessment_decision(
                policy, has_previous_report=True, spent_this_month_usd=10.0
            )
            == "skipped_budget"
        )

    def test_no_credit_skips_assessments(self) -> None:
        policy = dr.DeliveryReportPolicy(report_mode="assessed")
        assert (
            dr.assessment_decision(
                policy, has_previous_report=True, credit_available_usd=0.0
            )
            == "skipped_no_credit"
        )

    def test_turned_off(self) -> None:
        policy = dr.DeliveryReportPolicy(enabled=False)
        assert dr.assessment_decision(policy, has_previous_report=False) == "disabled"


class TestPolicyInTheReport:
    def test_no_credit_report_says_assessments_were_skipped(self) -> None:
        report = dr.assemble_report(
            window={"from": "2026-10-17T00:00:00Z", "to": "2026-10-24T00:00:00Z"},
            pull_requests=[
                {
                    "url": "https://github.com/acme/app/pull/7",
                    "merged_at": "2026-10-20T10:00:00Z",
                    "state": "merged",
                    "linked_issues": ["acme/app#3"],
                }
            ],
            issues=[],
            assessment="skipped_no_credit",
        )
        assert report["summary"]["assessments_skipped_reason"] == "no_credit"
        assert report["shipped"][0]["issues"] == ["acme/app#3"]
        assert "the account has no credit left" in report["report_markdown"]

    def test_agent_cannot_author_the_decision(self) -> None:
        report = dr.finalize_delivery_report(
            {"assessment": {"decision": "skipped_no_credit"}}
        )
        assert "assessment" not in report
        assert report["summary"]["assessments_skipped_reason"] is None

    def test_disabled_repository_is_left_out(self) -> None:
        report = dr.assemble_report(
            window={"from": "2026-10-17T00:00:00Z", "to": "2026-10-24T00:00:00Z"},
            pull_requests=[
                {
                    "url": "https://github.com/acme/app/pull/7",
                    "merged_at": "2026-10-20T10:00:00Z",
                },
                {
                    "url": "https://github.com/acme/docs/pull/8",
                    "merged_at": "2026-10-20T10:00:00Z",
                },
            ],
            issues=[],
            project_policies={"acme/docs": dr.resolve_policy(None, {"enabled": False})},
        )
        assert [c["ref"] for c in report["shipped"]] == ["acme/app#7"]


def test_reopen_requests_keep_whether_the_run_applied_them() -> None:
    base = {"ref": "example/app#7", "revision": "abc123", "evidence": ["a.py:1"]}
    kept = dr.normalize_reopen_requests(
        [{**base, "applied": True}, {**base, "ref": "example/app#8", "applied": "yes"}]
    )
    assert [r["applied"] for r in kept] == [True, False]
