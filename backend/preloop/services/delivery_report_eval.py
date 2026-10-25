"""Score a Delivery Report run against a set of known verdicts (#2029).

The evaluation set is the first measured week of this repository's own
report: closed issues whose criteria were checked by hand, each with the
right ``met`` value per criterion, the verdict that follows from them and
whether a reopen is the right proposal; plus merged PRs without a linked
issue, which must come out ``no_criteria`` and, for dependency bumps and
docs-only changes, metadata-only. See
``backend/tests/fixtures/delivery_report/eval/``.

A prompt change is measured by running the report on the same inputs and
scoring its ``result.json``::

    python -m preloop.services.delivery_report_eval result.json

The result goes through :func:`~preloop.services.delivery_report.
finalize_delivery_report` first, so the score is of what the platform
would publish, not of the raw model output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from preloop.services import delivery_report as dr
from preloop.services.delivery_report_checks import criterion_words

#: The evaluation set in this repository.
DEFAULT_SET = (
    Path(__file__)
    .resolve()
    .parents[2]
    .joinpath("tests", "fixtures", "delivery_report", "eval")
)


def load_set(path: Path = DEFAULT_SET) -> Dict[str, List[Dict[str, Any]]]:
    """The closed issues and shipped changes of an evaluation set."""
    return {
        "closed_issues": json.loads((path / "closed_issues.json").read_text()),
        "shipped": json.loads((path / "shipped_without_issue.json").read_text()),
    }


def expected_verdict(issue: Mapping[str, Any]) -> str:
    """The verdict the platform's honesty rules give the known criteria."""
    return str(dr.normalize_purpose({"criteria": issue.get("criteria")})["verdict"])


def _match_criteria(
    gold: Sequence[Mapping[str, Any]], run: Sequence[Mapping[str, Any]]
) -> List[Optional[Mapping[str, Any]]]:
    """For each known criterion, the run's criterion that states it, if any.

    Greedy by shared content words, best pair first, each run criterion
    used once. Fewer than two shared words (one for a one-word criterion)
    is no match.
    """
    pairs = []
    for g_index, g in enumerate(gold):
        g_words = criterion_words(str(g.get("text") or ""))
        for r_index, r in enumerate(run):
            shared = len(g_words & criterion_words(str(r.get("text") or "")))
            if shared >= min(2, len(g_words) or 1):
                pairs.append((shared, g_index, r_index))
    matched: List[Optional[Mapping[str, Any]]] = [None] * len(gold)
    used: set[int] = set()
    for _, g_index, r_index in sorted(pairs, key=lambda p: -p[0]):
        if matched[g_index] is None and r_index not in used:
            matched[g_index] = run[r_index]
            used.add(r_index)
    return matched


def score(
    result: Any, evaluation: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None
) -> Dict[str, Any]:
    """Score one run's result against the evaluation set.

    Returns:
        ``verdicts``: correct, total, accuracy and a confusion table
        (expected verdict to the run's verdict, ``missing`` when the run did
        not report the issue); ``per_verdict``: recall per expected verdict;
        ``criteria``: known criteria the run stated, and how many of those
        carry the right ``met``; ``reopens``: expected, proposed, correct,
        false and missed refs; ``shipped``: no-criteria and metadata-only
        agreement on PRs without an issue; ``mismatches``: one line per
        wrong verdict.
    """
    evaluation = evaluation or load_set()
    report = dr.finalize_delivery_report(result)
    by_ref = {str(i.get("ref")): i for i in report["closed_issues"]}
    confusion: Dict[str, Dict[str, int]] = {}
    correct = 0
    stated = right_met = known = 0
    mismatches: List[str] = []
    for issue in evaluation["closed_issues"]:
        expected = expected_verdict(issue)
        got_issue = by_ref.get(str(issue["ref"]))
        purpose = (got_issue or {}).get("purpose") or {}
        got = str(purpose.get("verdict")) if got_issue else "missing"
        confusion.setdefault(expected, {}).setdefault(got, 0)
        confusion[expected][got] += 1
        if got == expected:
            correct += 1
        else:
            mismatches.append(f"{issue['ref']}: expected {expected}, got {got}")
        gold_criteria = issue.get("criteria") or []
        known += len(gold_criteria)
        for g, r in zip(
            gold_criteria,
            _match_criteria(gold_criteria, purpose.get("criteria") or []),
            strict=False,
        ):
            if r is None:
                continue
            stated += 1
            if r.get("met") is g.get("met"):
                right_met += 1
    total = len(evaluation["closed_issues"])
    per_verdict = {
        verdict: {
            "expected": sum(row.values()),
            "correct": row.get(verdict, 0),
        }
        for verdict, row in sorted(confusion.items())
    }

    expected_reopens = {
        str(i["ref"]) for i in evaluation["closed_issues"] if i.get("reopen_expected")
    }
    proposed = {str(r["ref"]) for r in report["reopen_requests"]}

    shipped_by_ref = {str(c.get("ref")): c for c in report["shipped"]}
    shipped_rows = evaluation.get("shipped") or []
    no_criteria = metadata = 0
    for change in shipped_rows:
        got_change = shipped_by_ref.get(str(change["ref"])) or {}
        if (got_change.get("purpose") or {}).get("verdict") == dr.PURPOSE_NO_CRITERIA:
            no_criteria += 1
        if got_change and got_change.get("metadata_only") == change.get(
            "metadata_only_expected"
        ):
            metadata += 1

    return {
        "verdicts": {
            "correct": correct,
            "total": total,
            "accuracy": round(correct / total, 3) if total else None,
            "confusion": confusion,
        },
        "per_verdict": per_verdict,
        "criteria": {"known": known, "stated": stated, "met_correct": right_met},
        "reopens": {
            "expected": sorted(expected_reopens),
            "proposed": sorted(proposed),
            "correct": sorted(expected_reopens & proposed),
            "false": sorted(proposed - expected_reopens),
            "missed": sorted(expected_reopens - proposed),
        },
        "shipped": {
            "total": len(shipped_rows),
            "no_criteria": no_criteria,
            "metadata_only_correct": metadata,
        },
        "mismatches": mismatches,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the score of a ``result.json``; exit 1 below ``--min-accuracy``."""
    parser = argparse.ArgumentParser(prog="delivery_report_eval")
    parser.add_argument("result", help="the run's result.json")
    parser.add_argument("--set", default=str(DEFAULT_SET), help="evaluation set dir")
    parser.add_argument("--min-accuracy", type=float, default=None)
    args = parser.parse_args(argv)
    result = json.loads(Path(args.result).read_text())
    out = score(result, load_set(Path(args.set)))
    json.dump(out, sys.stdout, indent=1)
    sys.stdout.write("\n")
    accuracy = out["verdicts"]["accuracy"] or 0.0
    if args.min_accuracy is not None and accuracy < args.min_accuracy:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
