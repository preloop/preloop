"""Select complete test files before pytest imports the suite.

Historical node durations balance contiguous file groups. New files are always
included, with a conservative estimated weight; stale duration entries cannot
select deleted tests. Pytest still performs normal collection within each file.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tomllib


def _conftest_ignores(directory: Path) -> tuple[list[str], list[str]]:
    """Read static pytest ignore declarations without importing conftest."""
    conftest = directory / "conftest.py"
    if not conftest.is_file():
        return [], []
    ignored: list[str] = []
    globs: list[str] = []
    tree = ast.parse(conftest.read_text())
    handled_targets: set[int] = set()
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target not in {"collect_ignore", "collect_ignore_glob"}:
            continue
        if isinstance(node, ast.Assign):
            handled_targets.add(id(node.targets[0]))
        else:
            handled_targets.add(id(node.target))
        try:
            entries = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise ValueError(
                f"Dynamic pytest ignore is unsupported: {conftest}"
            ) from exc
        if not isinstance(entries, (list, tuple)) or not all(
            isinstance(entry, str) for entry in entries
        ):
            raise ValueError(f"Pytest ignore must be a list of paths: {conftest}")
        destination = ignored if target == "collect_ignore" else globs
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.op, ast.Add):
                raise ValueError(f"Unsupported pytest ignore update: {conftest}")
            destination.extend(entries)
        else:
            destination[:] = entries
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "pytest_ignore_collect":
                raise ValueError(
                    f"Dynamic pytest ignore hook is unsupported: {conftest}"
                )
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in {"collect_ignore", "collect_ignore_glob"}
            and id(node) not in handled_targets
        ):
            raise ValueError(f"Dynamic pytest ignore is unsupported: {conftest}")
    return ignored, globs


def discover_files(root: Path) -> list[str]:
    """Discover configured test files without importing tests or fixtures."""
    config = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["pytest"][
        "ini_options"
    ]
    patterns = config["python_files"]
    if isinstance(patterns, str):
        patterns = patterns.split()
    excluded = config.get("norecursedirs", [])
    ignores: dict[Path, tuple[list[str], list[str]]] = {}

    def ignored(path: Path) -> bool:
        for directory in (path.parent, *path.parent.parents):
            if not directory.is_relative_to(root):
                break
            if directory not in ignores:
                ignores[directory] = _conftest_ignores(directory)
            rules, globs = ignores[directory]
            relative = path.relative_to(directory).as_posix()
            if any(
                relative == rule or relative.startswith(f"{rule}/") for rule in rules
            ):
                return True
            if any(fnmatch.fnmatch(relative, pattern) for pattern in globs):
                return True
        return False

    files: set[str] = set()
    for test_root in config["testpaths"]:
        for directory, dirs, names in os.walk(root / test_root):
            dirs[:] = sorted(
                name
                for name in dirs
                if not (Path(directory) / name / "pyvenv.cfg").is_file()
                and not any(
                    fnmatch.fnmatch(
                        (Path(directory) / name).relative_to(root).as_posix()
                        if "/" in rule
                        else name,
                        rule,
                    )
                    for rule in excluded
                )
                and not ignored(Path(directory) / name)
            )
            for name in names:
                path = Path(directory) / name
                if any(
                    fnmatch.fnmatch(name, pattern) for pattern in patterns
                ) and not ignored(path):
                    files.add(path.relative_to(root).as_posix())
    return sorted(files)


def partition_files(
    files: list[str], durations: dict[str, float], shards: int
) -> list[list[str]]:
    """Partition sorted whole files, preserving order and covering each once."""
    if shards < 1 or shards > len(files):
        raise ValueError("shards must be between one and the number of test files")
    weights: dict[str, float] = {}
    for node, duration in durations.items():
        if not math.isfinite(duration) or duration < 0:
            raise ValueError(f"invalid test duration for {node}")
        path = node.split("::", 1)[0]
        weights[path] = weights.get(path, 0.0) + duration
    known = [weights[path] for path in files if weights.get(path, 0) > 0]
    fallback = max(statistics.median(known), 1.0) if known else 1.0
    costs = [max(weights.get(path, fallback), 0.001) for path in files]
    remaining = sum(costs)
    groups: list[list[str]] = []
    start = 0
    for index in range(shards - 1):
        target = remaining / (shards - index)
        end = start + 1
        weight = costs[start]
        # Leave at least one file for every remaining group.
        limit = len(files) - (shards - index - 1)
        while end < limit and abs(weight + costs[end] - target) <= abs(weight - target):
            weight += costs[end]
            end += 1
        groups.append(files[start:end])
        start = end
        remaining -= weight
    groups.append(files[start:])
    return groups


def main() -> int:
    """Run only the selected files, retaining pytest's exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 1 <= args.group <= args.shards:
        parser.error("group must be between one and shards")
    root = Path(__file__).resolve().parents[1]
    durations = json.loads((root / ".github/pytest-split-durations.json").read_text())
    groups = partition_files(discover_files(root), durations, args.shards)
    selected = groups[args.group - 1]
    if args.plan:
        print(json.dumps(selected))
        return 0
    options = args.pytest_args
    if options[:1] == ["--"]:
        options = options[1:]
    print(
        f"Shard {args.group}/{args.shards}: collecting {len(selected)} files",
        flush=True,
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", *selected, *options], cwd=root
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
