"""Compare CI impact decisions without removing existing PR coverage.

The legacy job selection remains authoritative. Security paths, stacks and full
events expand it; candidate test/leaf selections are receipts only until a
separately reviewed promotion after two weeks of comparison.
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import os
from pathlib import Path
import subprocess
import sys
import re
from typing import Any, Callable

from run_backend_shard import discover_files

ROOT = Path(__file__).resolve().parents[1]
SMOKE = (
    "backend/tests/endpoints/test_auth.py",
    "backend/tests/endpoints/test_auth_comprehensive.py",
    "backend/tests/endpoints/test_health.py",
    "backend/tests/models/crud/test_permission_crud.py",
    "backend/tests/models/crud/test_subscription_columns_drivers.py",
    "backend/tests/services/test_permission_prompt.py",
    "backend/tests/services/test_policy_evaluator.py",
)
MAPPED_AREAS = {"sync", "agents", "services", "utils", "tools", "api"}
# Backend Fast runs serially under a 15 minute job timeout. Recorded file
# durations track the real serial runtime closely (a 755 s estimate ran in
# 824 s), so 240 s keeps the lane near four minutes of tests. The sharded
# Backend Tests job still runs the whole inventory.
FAST_BUDGET_SECONDS = 240.0


def matches(path: str, pattern: str) -> bool:
    """Match repository globs, including zero directories for double-star."""
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if "**/" in pattern:
        return matches(path, pattern.replace("**/", "", 1))
    return False


def relevant(paths: list[str], patterns: list[str]) -> bool:
    """Apply include globs with exclusions to each changed path."""
    return any(
        any(matches(path, p) for p in patterns if not p.startswith("!"))
        and not any(matches(path, p[1:]) for p in patterns if p.startswith("!"))
        for path in paths
    )


def _module_name(path: str) -> str:
    """Return the dotted import name of a ``backend/`` source file."""
    parts = Path(path).relative_to("backend").with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@functools.lru_cache(maxsize=None)
def _read_source(path: str) -> str:
    """Read a test file for import matching; unreadable files match nothing."""
    try:
        return (ROOT / path).read_text(errors="replace")
    except OSError:
        return ""


_DOTTED = re.compile(r"\bpreloop(?:\.\w+)+")
_FROM_IMPORT = re.compile(r"from\s+(preloop(?:\.\w+)*)\s+import\s+(\([^)]*\)|[^\n]*)")


@functools.lru_cache(maxsize=8)
def _import_index(
    inventory: tuple[str, ...], read: Callable[[str], str]
) -> dict[str, frozenset[str]]:
    """Map each test file to every ``preloop`` module name it references.

    A dotted reference also covers its parents (``a.b.c`` names ``a.b``), and
    ``from a import b`` names ``a.b``, so a submodule imported by name counts.
    """
    index: dict[str, frozenset[str]] = {}
    for test in inventory:
        source = read(test)
        names: set[str] = set()
        for match in _DOTTED.finditer(source):
            parts = match.group(0).split(".")
            names.update(".".join(parts[:i]) for i in range(2, len(parts) + 1))
        for match in _FROM_IMPORT.finditer(source):
            for member in re.findall(r"\w+", match.group(2)):
                names.add(f"{match.group(1)}.{member}")
        index[test] = frozenset(names)
    return index


def module_tests(
    path: str,
    inventory: list[str],
    read: Callable[[str], str] = _read_source,
    *,
    importers: bool = True,
) -> list[str]:
    """Select test files that import a module or are named after it.

    Args:
        path: Changed ``backend/preloop/**/*.py`` source file.
        inventory: Every collectable test file.
        read: Returns a test file's source text.
        importers: Also select files that import the module, not just the
            ``test_<module>*`` files.

    Returns:
        Sorted test files naming ``test_<module>*`` or referencing the dotted
        module (``import``, ``from ... import`` or a ``patch`` target string).
    """
    dotted = _module_name(path)
    name = dotted.rpartition(".")[2]
    index = _import_index(tuple(inventory), read) if importers else {}
    selected = [
        test
        for test in inventory
        if Path(test).stem.startswith(f"test_{name}")
        or dotted in index.get(test, frozenset())
    ]
    return sorted(selected)


def narrow_tests(
    paths: list[str],
    inventory: list[str],
    read: Callable[[str], str] = _read_source,
    *,
    importers: bool = True,
) -> list[str]:
    """Return changed test files plus module-level matches for changed code."""
    selected = {p for p in paths if p in inventory}
    for path in paths:
        if path.startswith("backend/preloop/") and path.endswith(".py"):
            selected.update(module_tests(path, inventory, read, importers=importers))
    return sorted(selected)


def impacted_tests(
    paths: list[str],
    inventory: list[str],
    read: Callable[[str], str] = _read_source,
) -> tuple[list[str], str]:
    """Map whole files conservatively; unknown or shared impact selects all."""
    selected: set[str] = set()
    for path in paths:
        if not path.startswith("backend/"):
            continue
        if Path(path).name in {"conftest.py", "__init__.py"}:
            return inventory, "shared fixture or package"
        if path.startswith("backend/tests/"):
            if path not in inventory:
                return inventory, "unknown or deleted test"
            selected.add(path)
            continue
        if not path.startswith("backend/preloop/"):
            return inventory, "unmapped backend path"
        area = path.removeprefix("backend/preloop/").split("/", 1)[0]
        if area not in MAPPED_AREAS or not path.endswith(".py"):
            return inventory, "shared or unmapped module"
        mapped = module_tests(path, inventory, read)
        if not mapped:
            return inventory, "no matching tests"
        selected.update(mapped)
    return sorted(selected), "module-level match"


def estimated_seconds(tests: list[str], durations: dict[str, float]) -> float:
    """Estimate serial runtime; files without history use the median file."""
    weights: dict[str, float] = {}
    for node, seconds in durations.items():
        file = node.split("::", 1)[0]
        weights[file] = weights.get(file, 0.0) + seconds
    known = sorted(w for w in weights.values() if w > 0)
    fallback = known[len(known) // 2] if known else 1.0
    return sum(weights.get(test, fallback) for test in tests)


def fast_selection(
    paths: list[str],
    inventory: list[str],
    tests: list[str],
    impact_reason: str,
    durations: dict[str, float],
    read: Callable[[str], str] = _read_source,
) -> tuple[list[str], str]:
    """Choose Backend Fast files within the serial time budget.

    Unknown impact (``tests == inventory``) keeps the full heavy tier in the
    shards, so fast feedback narrows to smoke, changed tests and module
    matches. Any selection over ``FAST_BUDGET_SECONDS`` narrows the same way;
    if module importers alone still exceed it, only ``test_<module>*`` files
    are kept.

    Args:
        paths: Changed repository paths.
        inventory: Every collectable test file.
        tests: The impact selection for the heavy tier.
        impact_reason: Why ``tests`` was chosen.
        durations: Recorded per-node durations in seconds.
        read: Returns a test file's source text.

    Returns:
        The sorted fast files and the reason recorded in the receipt.
    """
    fast = sorted(set(SMOKE) | set(tests))
    if tests == inventory:
        why = impact_reason
    else:
        cost = estimated_seconds(fast, durations)
        if cost <= FAST_BUDGET_SECONDS:
            return fast, impact_reason
        why = f"over fast budget ({cost:.0f}s > {FAST_BUDGET_SECONDS:.0f}s)"
    fast = sorted(set(SMOKE) | set(narrow_tests(paths, inventory, read)))
    if estimated_seconds(fast, durations) <= FAST_BUDGET_SECONDS:
        return fast, f"{why}: smoke, changed tests and module importers"
    fast = sorted(
        set(SMOKE) | set(narrow_tests(paths, inventory, read, importers=False))
    )
    return fast, f"{why}: smoke, changed tests and test_<module> files"


def select(
    paths: list[str],
    policy: dict[str, Any],
    inventory: list[str],
    *,
    event: str = "pull_request",
    labels: tuple[str, ...] = (),
    base_ref: str = "main",
    attempt: int = 1,
    durations: dict[str, float] | None = None,
    read: Callable[[str], str] = _read_source,
) -> dict[str, Any]:
    """Return live and shadow decisions with an explicit expansion reason."""
    legacy = {k: relevant(paths, v) for k, v in policy["legacy"].items()}
    reason = ""
    if event != "pull_request":
        reason = event
    elif attempt > 1:
        reason = "full PR rerun"
    elif "ci:full" in labels:
        reason = "ci:full"
    elif base_ref != "main":
        reason = "stacked PR: validate its real base"
    elif relevant(paths, policy["sensitive"]):
        reason = "security, dependency or CI path"
    elif not paths:
        reason = "empty or unavailable diff"
    full = bool(reason)
    live = {k: full or legacy["workflow"] or v for k, v in legacy.items()}
    candidate = {k: full or relevant(paths, v) for k, v in policy["leaves"].items()}
    # Parity is deliberately still every-push; Windows keeps its existing map.
    live.update(candidate)
    live["parity"] = True
    live["windows"] = full or relevant(paths, policy["leaves"]["windows"])
    tests, impact_reason = impacted_tests(paths, inventory, read)
    if full or (live["backend"] and not tests):
        tests, impact_reason = inventory, reason or "unknown backend impact"
    fast: list[str] = []
    fast_reason = "backend not selected"
    if live["backend"]:
        fast, fast_reason = fast_selection(
            paths, inventory, tests, impact_reason, durations or {}, read
        )
    return {
        "version": policy["version"],
        "mode": "comparison",
        "full": full,
        "reason": reason or "legacy job paths remain authoritative",
        "paths": paths,
        "legacy": legacy,
        "live": live,
        "candidate_leaves": candidate,
        "candidate_tests": tests,
        "impact_reason": impact_reason,
        "fast_tests": fast,
        "fast_reason": fast_reason,
        "fast_estimated_seconds": round(estimated_seconds(fast, durations or {}), 1),
    }


def changed_paths(base: str, head: str) -> list[str]:
    """Read the complete PR diff; renames include both old and new paths."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"],
        check=True,
        capture_output=True,
    )
    return sorted(set(result.stdout.decode().strip("\0").split("\0")) - {""})


def main() -> int:
    """Write job outputs and the auditable selector receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-fast", action="store_true")
    args = parser.parse_args()
    receipt_path = ROOT / "ci-selection.json"
    if args.run_fast:
        tests = json.loads(receipt_path.read_text())["fast_tests"]
        if not tests:
            return 0
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *tests,
                "-q",
                "-m",
                "not integration",
                "--junitxml=fast-test-results.xml",
            ],
            check=False,
            env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
        ).returncode
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event.get("pull_request", {})
    name = os.environ["GITHUB_EVENT_NAME"]
    group = event.get("merge_group", {})
    base = (
        pr.get("base", {}).get("sha")
        or group.get("base_sha")
        or os.environ.get("CI_BASE_SHA")
        or event.get("before")
        or subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip()
    )
    head = (
        pr.get("head", {}).get("sha")
        or group.get("head_sha")
        or os.environ.get("CI_HEAD_SHA")
        or os.environ["GITHUB_SHA"]
    )
    paths = changed_paths(base, head) if name == "pull_request" else []
    receipt = select(
        paths,
        json.loads((ROOT / ".github/ci-policy.json").read_text()),
        discover_files(ROOT),
        event=name,
        labels=tuple(label["name"] for label in pr.get("labels", [])),
        base_ref=pr.get("base", {}).get("ref", "main"),
        attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
        durations=json.loads(
            (ROOT / ".github/pytest-split-durations.json").read_text()
        ),
    )
    receipt.update(
        head_sha=head,
        base_sha=base or os.environ.get("CI_BASE_SHA", ""),
        tested_sha=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        pr_created_at=pr.get("created_at"),
        pr_ready=not pr.get("draft", False) if pr else None,
        actor_kind="bot" if event.get("sender", {}).get("type") == "Bot" else "unknown",
        trigger=name,
        run_id=os.environ["GITHUB_RUN_ID"],
        attempt=os.environ["GITHUB_RUN_ATTEMPT"],
    )
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in {**receipt["live"], "full": receipt["full"]}.items():
            output.write(f"{key}={str(value).lower()}\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write("### Selection comparison\n\n```json\n")
        summary.write(json.dumps(receipt, indent=2) + "\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
