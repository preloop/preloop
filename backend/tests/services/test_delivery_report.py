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
        assert policy.tracker_writes.create_follow_up == "allow"
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

    def test_read_tools_only(self) -> None:
        tools = {t["name"] for t in _preset()["allowed_mcp_tools"]}
        assert tools == {
            "search_issues",
            "get_issue",
            "get_pull_request",
            "search_sessions",
        }
        assert not tools & {"create_issue", "update_issue", "add_comment"}

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
        ui = {f["flag"] for f in by_ref["acme/app#104"]["risk_flags"]}
        assert dr.RISK_UI_WITHOUT_SCREENSHOT in ui
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
        assert report["spend"]["cost_usd"] == 0.5
        assert report["spend"]["limit_usd"] == 5.0
        statuses: List[str] = [q["status"] for q in report["delta"]["open_questions"]]
        assert statuses == ["still_open", "still_open"]
