"""Refresh node-level test durations from a complete successful JUnit shard set.

Main-branch CI runs this after every backend shard passed and caches the output;
pull requests plan their shards from the newest cached file and fall back to the
committed ``.github/pytest-split-durations.json``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET


def read_durations(reports: list[Path], root: Path) -> dict[str, float]:
    """Convert pytest JUnit classnames to node IDs without importing tests.

    A test without history runs in every shard that owns a slice of a split
    file, so a node can appear more than once; its longest time is kept.

    Args:
        reports: JUnit XML files, one per shard.
        root: Repository root, used to find the source file of a classname.

    Returns:
        Node ids mapped to seconds.

    Raises:
        ValueError: A test failed, or a classname maps to no source file.
    """
    durations: dict[str, float] = {}
    for report in reports:
        tree = ET.parse(report)
        for case in tree.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                raise ValueError("Only successful shard reports can refresh weights")
            parts = case.attrib["classname"].split(".")
            for end in range(len(parts), 0, -1):
                path = "/".join(parts[:end]) + ".py"
                if (root / path).is_file():
                    node = "::".join([path, *parts[end:], case.attrib["name"]])
                    break
            else:
                raise ValueError(f"JUnit classname has no test source: {parts}")
            seconds = float(case.attrib["time"])
            durations[node] = max(durations.get(node, 0.0), seconds)
    return durations


def shard_seconds(reports: list[Path]) -> list[float]:
    """Return each report's pytest wall time (the largest ``testsuite`` time)."""
    totals = []
    for report in reports:
        root = ET.parse(report).getroot()
        suites = [root, *root.iter("testsuite")]
        totals.append(max(float(suite.attrib.get("time", 0)) for suite in suites))
    return totals


def summarise(reports: list[Path]) -> str:
    """Describe the actual per-shard pytest seconds and their spread."""
    seconds = shard_seconds(reports)
    lines = ["Actual pytest seconds per shard:"]
    for report, value in zip(reports, seconds, strict=False):
        lines.append(f"- {report.parent.name}: {value:.1f}s")
    mean = statistics.fmean(seconds)
    lines.append(
        f"- max {max(seconds):.1f}s / mean {mean:.1f}s = {max(seconds) / mean:.3f}"
    )
    return "\n".join(lines)


def _sort_key(report: Path) -> int:
    return int(report.parent.name.rsplit("-", 1)[1])


def main() -> None:
    """Require every shard report, then summarise or write node durations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    reports = sorted(
        args.reports.glob("backend-test-results-*/test-results.xml"), key=_sort_key
    )
    expected = {f"backend-test-results-{i}" for i in range(1, args.shards + 1)}
    if {p.parent.name for p in reports} != expected:
        raise ValueError(f"Require exactly one report from each of {args.shards}")
    if args.summary:
        text = summarise(reports)
        print(text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
    if args.out is None:
        return
    durations = read_durations(reports, root)
    args.out.write_text(
        json.dumps({k: round(v, 4) for k, v in durations.items()}, sort_keys=True)
        + "\n"
    )
    print(f"wrote {len(durations)} node durations to {args.out}")


if __name__ == "__main__":
    main()
