"""Tests for the Delivery Report evaluation set and its scorer (#2029)."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pytest

from preloop.services import delivery_report as dr
from preloop.services import delivery_report_eval as ev
from preloop.services.delivery_report_checks import PARENT_CHECK_SCHEMA

EVAL = Path(__file__).parent.parent / "fixtures" / "delivery_report" / "eval"
WINDOW = {"from": "2026-10-17T00:00:00+00:00", "to": "2026-10-24T00:00:00+00:00"}


@pytest.fixture(scope="module")
def evaluation() -> Dict[str, List[Dict[str, Any]]]:
    return ev.load_set(EVAL)


def _baseline() -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads(
        (EVAL / "baseline_spike_assessors.json").read_text()
    )
    return data


def _perfect_run(evaluation: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """A run that states every known criterion right and proposes the reopens."""
    return {
        "status": "success",
        "window": WINDOW,
        "closed_issues": [
            {"ref": i["ref"], "purpose": {"criteria": copy.deepcopy(i["criteria"])}}
            for i in evaluation["closed_issues"]
        ],
        "reopen_requests": [
            {"ref": i["ref"], "revision": "a" * 40, "evidence": ["latest comment"]}
            for i in evaluation["closed_issues"]
            if i["reopen_expected"]
        ],
        "shipped": [
            {
                "ref": s["ref"],
                "metadata_only": s["metadata_only_expected"],
                "purpose": {"criteria": []},
            }
            for s in evaluation["shipped"]
        ],
    }


class TestEvaluationSet:
    def test_at_least_twenty_closed_issues_from_the_week(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        refs = [i["ref"] for i in evaluation["closed_issues"]]
        assert len(refs) >= 20
        assert len(set(refs)) == len(refs)

    def test_every_verdict_category_is_sampled(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        verdicts = Counter(ev.expected_verdict(i) for i in evaluation["closed_issues"])
        # Every issue closed that week stated criteria, so no_criteria is
        # sampled from the merged PRs that link no issue.
        assert verdicts == {"covered": 15, "partial": 7, "unverified": 18}
        assert len(evaluation["shipped"]) >= 5

    def test_known_verdicts_need_cited_evidence(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        for issue in evaluation["closed_issues"]:
            for criterion in issue["criteria"]:
                if criterion["met"] is not None:
                    assert criterion["evidence"], (issue["ref"], criterion["text"])

    def test_corrected_cases_carry_the_double_checked_answer(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        by_ref = {i["ref"]: i for i in evaluation["closed_issues"]}
        assert {r for r, i in by_ref.items() if i["reopen_expected"]} == {
            "preloop/preloop#1693",
            "preloop/preloop#1694",
        }
        assert ev.expected_verdict(by_ref["preloop/preloop#1693"]) == "partial"
        assert ev.expected_verdict(by_ref["preloop/preloop#1957"]) == "unverified"
        assert ev.expected_verdict(by_ref["preloop/preloop#1340"]) == "covered"

    def test_shipped_changes_without_an_issue_are_no_criteria_from_code(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        prs = [
            {
                "number": int(s["ref"].rsplit("#", 1)[1]),
                "title": s["title"],
                "author": s["author"],
                "description": "",
                "url": f"https://github.com/{s['ref'].replace('#', '/pull/')}",
                "merged_at": "2026-10-20T00:00:00+00:00",
                "changes": {"changed_files": s["files"]},
            }
            for s in evaluation["shipped"]
        ]
        report = dr.assemble_report(window=WINDOW, pull_requests=prs, issues=[])
        got = {c["ref"]: c for c in report["shipped"]}
        for s in evaluation["shipped"]:
            assert got[s["ref"]]["purpose"]["verdict"] == dr.PURPOSE_NO_CRITERIA
            assert got[s["ref"]]["metadata_only"] == s["metadata_only_expected"]
        kinds = Counter(s["metadata_only_expected"] for s in evaluation["shipped"])
        assert kinds["dependabot"] and kinds["docs_only"] and kinds[None]


class TestScore:
    def test_a_run_that_matches_the_set_scores_full_marks(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        out = ev.score(_perfect_run(evaluation), evaluation)
        assert out["verdicts"]["accuracy"] == 1.0
        assert out["mismatches"] == []
        assert out["criteria"]["met_correct"] == out["criteria"]["known"]
        assert out["reopens"]["missed"] == [] and out["reopens"]["false"] == []
        assert out["shipped"]["no_criteria"] == out["shipped"]["total"]
        assert out["shipped"]["metadata_only_correct"] == out["shipped"]["total"]

    def test_the_spike_baseline_shows_the_three_known_errors(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        out = ev.score(_baseline(), evaluation)
        assert out["verdicts"]["correct"] == 37
        assert out["mismatches"] == [
            "preloop/preloop#1693: expected partial, got unverified",
            "preloop/preloop#1694: expected partial, got unverified",
            "preloop/preloop#1957: expected unverified, got partial",
        ]
        assert out["reopens"]["missed"] == [
            "preloop/preloop#1693",
            "preloop/preloop#1694",
        ]
        assert out["per_verdict"]["covered"] == {"expected": 15, "correct": 15}
        assert out["criteria"] == {"known": 185, "stated": 185, "met_correct": 181}

    def test_a_parent_check_record_fixes_the_baseline_miss(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        run = _baseline()
        issue = next(
            i for i in run["closed_issues"] if i["ref"] == "preloop/preloop#1957"
        )
        issue["closing_sha"] = "m" * 40
        issue["purpose"]["criteria"][2]["parent_check"] = {
            "schema": PARENT_CHECK_SCHEMA,
            "merge_sha": "m" * 40,
            "parent_sha": "p" * 40,
            "terms": ["duplicates-view", "assignments-view"],
            "paths": [
                {"path": p, "exists": True, "referenced": False, "hits": []}
                for p in ("router.test.ts", "lazy-routes.test.ts")
            ],
            "error": None,
        }
        out = ev.score(run, evaluation)
        assert (
            "preloop/preloop#1957: expected unverified, got partial"
            not in (out["mismatches"])
        )

    def test_a_missing_issue_is_counted_as_missing(
        self, evaluation: Dict[str, List[Dict[str, Any]]]
    ) -> None:
        run = _perfect_run(evaluation)
        run["closed_issues"] = run["closed_issues"][1:]
        out = ev.score(run, evaluation)
        first = evaluation["closed_issues"][0]
        expected = ev.expected_verdict(first)
        assert out["verdicts"]["confusion"][expected]["missing"] == 1
        assert out["mismatches"] == [
            f"{first['ref']}: expected {expected}, got missing"
        ]

    def test_criteria_are_matched_by_content_not_position(self) -> None:
        gold = [
            {"text": "Chain status under 300 ms", "met": True},
            {"text": "grouped p95 under 1.5 s", "met": False},
        ]
        run = [
            {"text": "The grouped p95 stays under 1.5 s", "met": False},
            {"text": "Unrelated", "met": True},
        ]
        matched = ev._match_criteria(gold, run)
        assert matched == [None, run[0]]

    def test_cli_exits_non_zero_below_the_bar(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "result.json"
        path.write_text(json.dumps(_baseline()))
        assert ev.main([str(path), "--set", str(EVAL), "--min-accuracy", "0.9"]) == 0
        assert ev.main([str(path), "--set", str(EVAL), "--min-accuracy", "0.95"]) == 1
        assert '"accuracy": 0.925' in capsys.readouterr().out


def test_missing_shipped_row_does_not_count_as_metadata_agreement(
    evaluation: Dict[str, List[Dict[str, Any]]],
) -> None:
    run = _perfect_run(evaluation)
    missing = next(s for s in run["shipped"] if s["metadata_only"] is None)
    run["shipped"].remove(missing)
    result = ev.score(run, evaluation)
    assert result["shipped"]["metadata_only_correct"] == len(evaluation["shipped"]) - 1
