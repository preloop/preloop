"""Corrections from the first measured Delivery Report run (#2029).

Fixture-only: no model is called. The measured week is the evaluation set
in ``tests/fixtures/delivery_report/eval``.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from preloop.services import delivery_report as dr
from preloop.services import delivery_report_eval as dre

EVAL = Path(__file__).parent.parent / "fixtures" / "delivery_report" / "eval"


def _measured_week_result() -> Dict[str, Any]:
    """The hand-checked week as a run result, with wrong counts in its prose."""
    evaluation = dre.load_set(EVAL)
    return {
        "status": "success",
        "window": {"from": "2026-10-17T00:00:00Z", "to": "2026-10-24T00:00:00Z"},
        "shipped": [
            {
                "ref": item["ref"],
                "title": item["title"],
                "author": item["author"],
                "files": [f["filename"] for f in item["files"]],
                "files_changed": len(item["files"]),
                "issues": [],
                "purpose": {"verdict": "no_criteria", "criteria": []},
            }
            for item in evaluation["shipped"]
        ],
        "closed_issues": [
            {
                "ref": issue["ref"],
                "title": issue["title"],
                "closed_by": issue["closed_by"],
                "purpose": {"verdict": "covered", "criteria": issue["criteria"]},
            }
            for issue in evaluation["closed_issues"]
        ],
        # Prose the model must never be counted from.
        "summary": {"closed": 99, "closed_verdicts": {"covered": 40}},
        "report_markdown": "All 40 closed issues are covered.",
    }


class TestMeasuredWeekCounts:
    def test_counts_come_from_the_structured_output(self) -> None:
        evaluation = dre.load_set(EVAL)
        expected = Counter(dre.expected_verdict(i) for i in evaluation["closed_issues"])
        report = dr.finalize_delivery_report(_measured_week_result())
        summary = report["summary"]
        assert summary["closed"] == len(evaluation["closed_issues"]) == 40
        assert summary["shipped"] == 8
        assert summary["closed_verdicts"] == {
            v: expected.get(v, 0) for v in dr.PURPOSE_VERDICTS
        }
        criteria: List[Dict[str, Any]] = [
            c for i in evaluation["closed_issues"] for c in i["criteria"]
        ]
        assert summary["evidence_availability"] == {
            "with_evidence": sum(1 for c in criteria if c.get("evidence")),
            "total": len(criteria),
        }
        assert summary["linkage"] == {"linked": 0, "total": 8}

    def test_prose_states_the_same_counts_as_the_json(self) -> None:
        report = dr.finalize_delivery_report(_measured_week_result())
        summary = report["summary"]
        text = report["report_markdown"]
        assert "All 40 closed issues are covered." not in text
        verdicts = summary["closed_verdicts"]
        assert (
            f"{verdicts['covered']} covered, {verdicts['partial']} partial, "
            f"{verdicts['unverified']} unverified, {verdicts['no_criteria']} with "
            "no criteria" in text
        )
        crit = summary["criteria_availability"]
        assert (
            f"Criteria available: {crit['with_criteria']} of {crit['total']} "
            "closed issues" in text
        )
        ev = summary["evidence_availability"]
        assert (
            f"Evidence available: {ev['with_evidence']} of {ev['total']} "
            "of their criteria" in text
        )
        assert "Linkage: 0 of 8 shipped" in text
        assert json.dumps(summary).count("score") == 0

    def test_metadata_only_is_derived_when_the_run_omits_it(self) -> None:
        evaluation = dre.load_set(EVAL)
        report = dr.finalize_delivery_report(_measured_week_result())
        got = {c["ref"]: c["metadata_only"] for c in report["shipped"]}
        want = {i["ref"]: i["metadata_only_expected"] for i in evaluation["shipped"]}
        assert got == want
        for change in report["shipped"]:
            if change["metadata_only"]:
                assert change["purpose"]["source"] == dr.SOURCE_METADATA


class TestMetadataOnlyDerivation:
    def test_incomplete_file_list_is_not_docs_only(self) -> None:
        change = {"author": "a", "files": ["README.md"], "files_changed": 3}
        assert dr.shipped_metadata_only(change) is None

    def test_docs_only_and_dependabot(self) -> None:
        assert dr.shipped_metadata_only({"files": ["docs/a.md"]}) == "docs_only"
        assert (
            dr.shipped_metadata_only({"author": "dependabot[bot]", "files": ["x.py"]})
            == "dependabot"
        )
        assert dr.shipped_metadata_only({"files": []}) is None

    def test_the_runs_label_is_kept(self) -> None:
        report = dr.finalize_delivery_report(
            {
                "shipped": [
                    {"ref": "a/b#1", "files": ["x.py"], "metadata_only": "docs_only"}
                ]
            }
        )
        assert report["shipped"][0]["metadata_only"] == "docs_only"


def _closed(**overrides: Any) -> Dict[str, Any]:
    issue: Dict[str, Any] = {
        "ref": "acme/app#7",
        "title": "Export is fast",
        "url": "https://github.com/acme/app/issues/7",
        "closing_sha": "m3rge",
        "closing_head_sha": "h3ad",
        "updated_at": "2026-10-20T00:00:00Z",
        "purpose": {
            "criteria": [
                {
                    "text": "Export finishes under 2 s",
                    "met": False,
                    "evidence": ["comment 2026-10-21: measured 4.1 s"],
                }
            ]
        },
    }
    issue.update(overrides)
    return issue


_FOLLOW_UP = {
    "ref": "acme/app#7",
    "title": "Speed up export",
    "reason": "Last comment measures 4.1 s",
    "evidence": ["https://github.com/acme/app/issues/7#issuecomment-1"],
}


class TestReopenNotFollowUp:
    def test_a_follow_up_on_a_closed_miss_becomes_a_reopen(self) -> None:
        report = dr.finalize_delivery_report(
            {"closed_issues": [_closed()], "follow_ups": [_FOLLOW_UP]}
        )
        assert report["follow_ups"] == []
        [request] = report["reopen_requests"]
        assert request["ref"] == "acme/app#7"
        assert request["revision"] == "m3rge"
        assert request["applied"] is False
        assert "Export finishes under 2 s" in request["reason"]
        assert _FOLLOW_UP["evidence"][0] in request["evidence"]
        assert "comment 2026-10-21: measured 4.1 s" in request["evidence"]
        assert report["summary"]["reopen_requests"] == 1
        assert report["summary"]["follow_ups_proposed"] == 0

    def test_an_evidence_gap_stays_a_follow_up(self) -> None:
        issue = _closed(
            purpose={"criteria": [{"text": "x", "met": None, "evidence": []}]}
        )
        report = dr.finalize_delivery_report(
            {"closed_issues": [issue], "follow_ups": [_FOLLOW_UP]}
        )
        assert len(report["follow_ups"]) == 1
        assert report["reopen_requests"] == []

    def test_no_closing_revision_keeps_the_follow_up(self) -> None:
        report = dr.finalize_delivery_report(
            {"closed_issues": [_closed(closing_sha=None)], "follow_ups": [_FOLLOW_UP]}
        )
        assert len(report["follow_ups"]) == 1
        assert report["reopen_requests"] == []

    def test_an_existing_reopen_is_not_duplicated(self) -> None:
        existing = {
            "ref": "acme/app#7",
            "revision": "m3rge",
            "evidence": ["comment"],
        }
        report = dr.finalize_delivery_report(
            {
                "closed_issues": [_closed()],
                "follow_ups": [_FOLLOW_UP],
                "reopen_requests": [existing],
            }
        )
        assert report["follow_ups"] == []
        assert len(report["reopen_requests"]) == 1

    def test_follow_ups_on_other_items_are_kept(self) -> None:
        other = dict(_FOLLOW_UP, ref="acme/app#99")
        report = dr.finalize_delivery_report(
            {"closed_issues": [_closed()], "follow_ups": [other]}
        )
        assert report["follow_ups"] == [other]
        assert report["reopen_requests"] == []
