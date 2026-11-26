"""Plan and run balanced backend test shards before pytest imports the suite.

``plan`` packs test files into shards greedily, heaviest first, so groups are
not contiguous. A file heavier than a third of the per-shard target is split
into node-level slices: each shard that owns a slice runs the file and
deselects the recorded nodes of the other slices. Stale duration entries can
only deselect tests that no longer exist; tests without history run, and are
weighed at the median test duration. ``run`` executes one planned shard with
pytest-xdist work stealing, each worker on its own database clone.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import math
import os
import re
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
        if value is None:
            continue
        if isinstance(node, ast.Assign):
            handled_targets.add(id(node.targets[0]))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
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
    for walked in ast.walk(tree):
        if isinstance(walked, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if walked.name == "pytest_ignore_collect":
                raise ValueError(
                    f"Dynamic pytest ignore hook is unsupported: {conftest}"
                )
        if (
            isinstance(walked, ast.Name)
            and isinstance(walked.ctx, ast.Store)
            and walked.id in {"collect_ignore", "collect_ignore_glob"}
            and id(walked) not in handled_targets
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


# JUnit outliers: cold Go builds, fresh interpreters and bulk database fixtures.
# Keep their module-scoped fixtures together in one pytest process.
SERIAL_FILES = frozenset(
    {
        "backend/tests/api/test_ci_tool_evidence.py",
        "backend/tests/api/test_gateway_import_footprint.py",
        "backend/tests/api/test_multiuser_edition.py",
        "backend/tests/models/crud/test_ai_model_detail_bounds.py",
        "backend/tests/test_flow_orchestrator.py",
    }
)

SPLIT_FRACTION = 1 / 3
DEFAULT_MAX_RATIO = 1.15
_PARAMS = re.compile(r"\[.*\]$")


def _test_functions(path: Path) -> set[str]:
    """Return ``Class::test`` / ``test`` names defined in one file, statically."""
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, ValueError):
        return set()
    names: set[str] = set()

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test"):
                    names.add(f"{prefix}{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(tree.body, "")
    return names


def _function_of(node: str) -> str:
    """Strip the file and parametrize id from a node id."""
    return _PARAMS.sub("", node.split("::", 1)[1]) if "::" in node else ""


def file_weights(
    files: list[str], durations: dict[str, float], root: Path
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Estimate each file's serial seconds and collect its known node timings.

    Args:
        files: Test files that exist now.
        durations: Node ids (``path::test``) or whole files mapped to seconds.
        root: Repository root, used to count test functions in each file.

    Returns:
        File weights, and the recorded node durations of each file.

    Raises:
        ValueError: A duration is negative or not finite.
    """
    nodes: dict[str, dict[str, float]] = {}
    whole: dict[str, float] = {}
    for node, duration in durations.items():
        if not math.isfinite(duration) or duration < 0:
            raise ValueError(f"invalid test duration for {node}")
        path = node.split("::", 1)[0]
        if "::" in node:
            nodes.setdefault(path, {})[node] = duration
        else:
            whole[path] = whole.get(path, 0.0) + duration
    node_values = [v for per in nodes.values() for v in per.values()]
    median_test = max(statistics.median(node_values), 0.001) if node_values else 0.0
    known_files = [w for path, w in whole.items() if w > 0]
    median_file = max(statistics.median(known_files), 1.0) if known_files else 1.0
    weights: dict[str, float] = {}
    for path in files:
        recorded = nodes.get(path, {})
        if path in whole:
            weights[path] = max(whole[path], 0.001)
            continue
        if not median_test:
            weights[path] = median_file
            continue
        seen = {_function_of(node) for node in recorded}
        new = _test_functions(root / path) - seen
        if not recorded and not new:
            new = {"<unknown>"}
        weights[path] = max(sum(recorded.values()) + median_test * len(new), 0.001)
    return weights, {path: nodes.get(path, {}) for path in files}


def _lpt(items: list[tuple[str, float]], bins: int) -> list[list[str]]:
    """Assign weighted items to bins, heaviest first, onto the lightest bin."""
    loads = [0.0] * bins
    groups: list[list[str]] = [[] for _ in range(bins)]
    for name, weight in sorted(items, key=lambda item: (-item[1], item[0])):
        index = min(range(bins), key=lambda i: (loads[i], i))
        loads[index] += weight
        groups[index].append(name)
    return groups


def plan_shards(
    files: list[str], durations: dict[str, float], shards: int, root: Path
) -> dict:
    """Pack files (and slices of heavy files) into balanced shards.

    Args:
        files: Test files that exist now.
        durations: Recorded node or file durations in seconds.
        shards: Number of shards.
        root: Repository root.

    Returns:
        A JSON-serialisable plan with ``shards``: one entry per shard holding
        ``files``, ``deselect`` and predicted ``seconds``.

    Raises:
        ValueError: ``shards`` is not between one and the number of files.
    """
    if shards < 1 or shards > len(files):
        raise ValueError("shards must be between one and the number of test files")
    weights, nodes = file_weights(files, durations, root)
    target = sum(weights.values()) / shards
    limit = target * SPLIT_FRACTION
    items: list[tuple[str, float]] = []
    slices: dict[str, list[str]] = {}
    for path in files:
        weight = weights[path]
        recorded = nodes[path]
        count = min(shards, len(recorded), math.ceil(weight / limit))
        if weight <= limit or count < 2:
            items.append((path, weight))
            continue
        groups = _lpt(list(recorded.items()), count)
        extra = (weight - sum(recorded.values())) / count
        for index, group in enumerate(groups):
            key = f"{path}#{index}"
            slices[key] = group
            items.append((key, sum(recorded[n] for n in group) + extra))
    packed = _lpt(items, shards)
    loads = dict(items)
    plan: list[dict] = []
    for group in packed:
        owned = set(group)
        selected = sorted({name.split("#", 1)[0] for name in group})
        own_nodes = [n for key in group if key in slices for n in slices[key]]
        # pytest's --deselect matches node-id prefixes: deselecting
        # ``test_get`` would also drop an owned ``test_get_user``. Such a
        # node runs here as well; a duplicate is safe, a lost test is not.
        deselect = sorted(
            node
            for key, members in slices.items()
            if key not in owned and key.split("#", 1)[0] in selected
            for node in members
            if not any(own.startswith(node) for own in own_nodes)
        )
        plan.append(
            {
                "files": selected,
                "deselect": deselect,
                "seconds": round(sum(loads[name] for name in group), 3),
            }
        )
    return {"shards": plan, "split_files": sorted({k.split("#")[0] for k in slices})}


def spread(plan: dict) -> tuple[float, float]:
    """Return the predicted max and mean shard seconds of a plan."""
    seconds = [shard["seconds"] for shard in plan["shards"]]
    return max(seconds), statistics.fmean(seconds)


def _load_durations(path: Path) -> dict[str, float]:
    loaded = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must map test ids to seconds")
    return {str(k): float(v) for k, v in loaded.items()}


def command_plan(args: argparse.Namespace, root: Path) -> int:
    """Write a plan, print predicted seconds, and enforce the balance ratio."""
    files = discover_files(root)
    selection = getattr(args, "selection", None)
    if selection is not None:
        chosen = set(json.loads(selection.read_text())["candidate_tests"])
        files = [path for path in files if path in chosen]
        if not files:
            print("::error::the selection receipt names no collectable test file")
            return 2
    durations = _load_durations(args.durations)
    serial = sorted(SERIAL_FILES.intersection(files))
    regular = [path for path in files if path not in SERIAL_FILES]
    plan: dict = {"shards": [], "split_files": []}
    lines = [f"Predicted serial seconds per shard (durations: {args.durations}):"]
    ratio = 1.0
    if regular:
        plan = plan_shards(regular, durations, args.shards, root)
        peak, mean = spread(plan)
        ratio = peak / mean
        for index, shard in enumerate(plan["shards"], start=1):
            lines.append(
                f"- shard {index}: {shard['seconds']:.1f}s, {len(shard['files'])} files,"
                f" {len(shard['deselect'])} deselected nodes"
            )
        lines.append(
            f"- max {peak:.1f}s / mean {mean:.1f}s = {ratio:.3f}"
            f" (limit {args.max_ratio}); split files: {len(plan['split_files'])}"
        )
    if serial:
        weights, _ = file_weights(serial, durations, root)
        plan["shards"].append(
            {
                "files": serial,
                "deselect": [],
                "seconds": round(sum(weights.values()), 3),
                "serial": True,
            }
        )
        lines.append(
            f"- group {len(plan['shards'])} runs serially: {', '.join(serial)}"
        )
    plan["durations"] = str(args.durations)
    plan["predicted_max_over_mean"] = round(ratio, 4)
    args.out.write_text(json.dumps(plan, indent=1) + "\n")
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    if ratio > args.max_ratio:
        print(f"::error::predicted shard imbalance {ratio:.3f} > {args.max_ratio}")
        return 1
    return 0


def available_cpus() -> int:
    """Return the CPUs this process may run on (affinity, not the host total)."""
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        return max(len(affinity(0)), 1)
    return os.cpu_count() or 1


def command_run(args: argparse.Namespace, root: Path) -> int:
    """Run one planned shard, retaining pytest's exit status."""
    plan = json.loads(args.plan.read_text())
    if not 1 <= args.group <= len(plan["shards"]):
        raise ValueError("group must be between one and the planned shard count")
    shard = plan["shards"][args.group - 1]
    options = args.pytest_args
    if options[:1] == ["--"]:
        options = options[1:]
    env = dict(os.environ)
    if shard.get("serial"):
        workers = 1
    elif args.workers == "auto":
        workers = available_cpus()
    else:
        workers = int(args.workers)
    if workers > 1:
        from ci_postgres import clone_worker_databases

        clone_worker_databases(workers)
        env["PRELOOP_CI_WORKER_DATABASES"] = "1"
        options = ["-n", str(workers), "--dist", "worksteal", *options]
    print(
        f"Shard {args.group}/{len(plan['shards'])}: {len(shard['files'])} files,"
        f" {len(shard['deselect'])} deselected nodes, {workers} workers,"
        f" predicted {shard['seconds']:.0f}s serial",
        flush=True,
    )
    deselect = [arg for node in shard["deselect"] for arg in ("--deselect", node)]
    return subprocess.run(
        [sys.executable, "-m", "pytest", *shard["files"], *deselect, *options],
        cwd=root,
        env=env,
    ).returncode


def main() -> int:
    """Dispatch ``plan`` or ``run``."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    planner = sub.add_parser("plan", help="write a balanced shard plan")
    planner.add_argument("--shards", type=int, required=True)
    planner.add_argument("--durations", type=Path, required=True)
    planner.add_argument("--out", type=Path, required=True)
    planner.add_argument("--max-ratio", type=float, default=DEFAULT_MAX_RATIO)
    planner.add_argument(
        "--selection",
        type=Path,
        help="ci-selection.json receipt; plan only its candidate_tests",
    )
    runner = sub.add_parser("run", help="run one planned shard")
    runner.add_argument("--plan", type=Path, required=True)
    runner.add_argument("--group", type=int, required=True)
    runner.add_argument("--workers", default="1", help="worker count or 'auto'")
    runner.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.command == "plan":
        return command_plan(args, root)
    return command_run(args, root)


if __name__ == "__main__":
    raise SystemExit(main())
