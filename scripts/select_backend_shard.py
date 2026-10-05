#!/usr/bin/env python3
"""Print the backend test files for one CI shard.

The suite is split by recorded file duration, in collection order, so each
shard gets a similar amount of work and a file stays on one runner.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def shard_paths(
    records: list[tuple[str, float]],
    splits: int,
    extra: list[tuple[str, float]] | None = None,
) -> list[list[str]]:
    """Return ``splits`` contiguous groups of about equal recorded duration.

    Args:
        records: Files in collection order, with their recorded seconds.
        splits: Number of shards.
        extra: Files absent from the recording. Each is placed on the
            lightest shard so a new test file still runs.

    Returns:
        One path list per shard, index 0 for shard 1.

    Raises:
        ValueError: ``splits`` is less than 1, or a shard would be empty
            before extras are placed.
    """
    if splits < 1:
        raise ValueError("splits must be positive")
    total = sum(seconds for _, seconds in records)
    target = total / splits if records else 0.0
    groups: list[list[str]] = [[] for _ in range(splits)]
    durations = [0.0] * splits
    index = 0
    for path, seconds in records:
        if target > 0 and durations[index] >= target and index < splits - 1:
            index += 1
        groups[index].append(path)
        durations[index] += seconds
    for path, seconds in extra or []:
        lightest = min(range(splits), key=lambda shard: (durations[shard], shard))
        groups[lightest].append(path)
        durations[lightest] += seconds
    return groups


def load_records(path: Path) -> list[tuple[str, float]]:
    """Load the checked-in ``[{path, seconds}, ...]`` recording."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    records: list[tuple[str, float]] = []
    for item in payload:
        records.append((str(item["path"]), float(item["seconds"])))
    return records


def _new_test_files(root: Path, known: set[str]) -> list[str]:
    """Test modules under backend/tests that the recording does not name."""
    base = root / "backend" / "tests"
    found = sorted(
        path.relative_to(root).as_posix()
        for path in base.rglob("test_*.py")
        if path.is_file()
    )
    return [path for path in found if path not in known]


def paths_for_group(
    root: Path, durations_path: Path, splits: int, group: int
) -> list[str]:
    """Resolve one 1-based shard, dropping recordings whose files are gone."""
    if group < 1 or group > splits:
        raise ValueError(f"group must be from 1 to {splits}")
    records = [
        (path, seconds)
        for path, seconds in load_records(durations_path)
        if (root / path).is_file()
    ]
    known = {path for path, _ in records}
    average = sum(seconds for _, seconds in records) / len(records) if records else 1.0
    extra = [(path, average) for path in _new_test_files(root, known)]
    return shard_paths(records, splits, extra)[group - 1]


def main() -> None:
    """Print the selected shard's paths, one per line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--splits", type=int, default=8)
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument(
        "--durations",
        type=Path,
        default=Path(__file__).with_name("backend_test_durations.json"),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    for path in paths_for_group(args.root, args.durations, args.splits, args.group):
        print(path)


if __name__ == "__main__":
    main()
