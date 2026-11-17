"""Refresh whole-file weights from a complete successful JUnit shard set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET

from run_backend_shard import discover_files, partition_files


def read_durations(reports: list[Path], root: Path) -> dict[str, float]:
    """Convert pytest JUnit classnames to node IDs without importing tests."""
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
            if node in durations:
                raise ValueError(f"Duplicate test node across shards: {node}")
            durations[node] = float(case.attrib["time"])
    return durations


def main() -> None:
    """Require all shards and print the reproducible measured-weight replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    reports = sorted(args.reports.glob("backend-test-results-*/test-results.xml"))
    if {p.parent.name for p in reports} != {
        f"backend-test-results-{i}" for i in range(1, 19)
    }:
        raise ValueError("Require exactly one report from each of the 18 shards")
    durations = read_durations(reports, root)
    files = discover_files(root)
    groups = partition_files(files, durations, 18)
    costs = {
        path: sum(v for k, v in durations.items() if k.split("::", 1)[0] == path)
        for path in files
    }
    weights = [sum(costs[p] for p in group) for group in groups]
    receipt = {
        "source_run": args.run_id,
        "source_url": f"https://github.com/preloop/preloop/actions/runs/{args.run_id}",
        "test_cases": len(durations),
        "files": len(files),
        "group_seconds": weights,
        "max_over_median": max(weights) / statistics.median(weights),
        "scope": "JUnit execution replay, not predicted wall time; measure each runner class after merge",
    }
    file_weights: dict[str, float] = {}
    for node, duration in durations.items():
        path = node.split("::", 1)[0]
        file_weights[path] = file_weights.get(path, 0.0) + duration
    (root / ".github/pytest-split-durations.json").write_text(
        json.dumps(
            {p: round(v, 6) for p, v in file_weights.items()}, indent=2, sort_keys=True
        )
        + "\n"
    )
    (root / ".github/ci-duration-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
