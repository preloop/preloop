"""Choose CI jobs and the backend tests a change affects.

Job selection: the legacy path filters stay authoritative, and security
paths, stacks and full events expand them to every job.

Backend test selection follows the static import graph in
``ci_import_graph``: a diff selects the test files that load a changed module,
the changed test files and the smoke set. A change to a shared root (models,
CRUD, migrations, config, any ``conftest.py``, packaging and lock files, CI
files) selects the whole inventory, as do version tags, ``ci:full``, reruns,
stacked PRs, merge-queue, scheduled and manual runs. Pull requests diff
against their base; pushes to ``main`` diff against the last ``main`` commit
whose CI run passed (falling back to the push's ``before``), so commits whose
runs were cancelled by a newer push are still covered.
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import re
from typing import Any, Callable
import urllib.request

import ci_import_graph
from run_backend_shard import SERIAL_FILES, discover_files

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
# Paths whose impact the import graph cannot bound: everything loads them, or
# they change how every test runs. Any match selects the whole inventory.
SHARED_ROOTS = (
    "backend/preloop/models/**",
    "backend/preloop/config.py",
    "**/conftest.py",
    "pyproject.toml",
    "**/pyproject.toml",
    "requirements/**",
    "**/requirements*.txt",
    "**/requirements*.in",
    "**/*.lock",
    ".github/**",
    "scripts/ci_*.py",
    "scripts/run_backend_shard.py",
    "scripts/init_db.py",
)
# How many deferred (function-local) imports a dependency chain may cross.
# Following them without a bound joins almost every module into one import
# cycle; the replay recorded in the PR that introduced this showed one hop
# catches every historical failure.
LAZY_HOPS = 1
# Names too common to identify the file that mentions them.
GENERIC_NAMES = {
    "__init__.py",
    "Dockerfile",
    "Makefile",
    "README.md",
    "index.html",
    "index.js",
    "index.ts",
    "main.go",
    "package.json",
    "requirements.txt",
}
GRAPH_CACHE = ROOT / ".ci-import-graph.json"
SHARDS = 6
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


def _mentions(path: str) -> list[str]:
    """Return the strings whose presence in a source file names ``path``."""
    parts = Path(path).parts
    needles = [path]
    if len(parts) > 2:
        needles.append("/".join(parts[-2:]))
    if parts[-1] not in GENERIC_NAMES:
        needles.append(parts[-1])
    return needles


@functools.lru_cache(maxsize=1)
def _graph() -> ci_import_graph.Graph:
    """Build the repository import graph once per process."""
    return ci_import_graph.build(ROOT, GRAPH_CACHE)


_APP_TESTS: list[tuple[ci_import_graph.Graph, Callable[[str], str], set[str]]] = []


def _app_fixture_tests(
    graph: ci_import_graph.Graph, read: Callable[[str], str]
) -> set[str]:
    """Find app-fixture tests once per graph and reader (the parse is slow)."""
    for cached_graph, cached_read, cached in _APP_TESTS:
        if cached_graph is graph and cached_read is read:
            return cached
    tests: set[str] = ci_import_graph.app_fixture_tests(
        {path: read(path) for path in graph if path.startswith("backend/tests/")}
    )
    _APP_TESTS[:] = [(graph, read, tests)]
    return tests


def impacted_tests(
    paths: list[str],
    inventory: list[str],
    read: Callable[[str], str] = _read_source,
    graph: ci_import_graph.Graph | None = None,
) -> tuple[list[str], str]:
    """Select the test files a diff can affect.

    Args:
        paths: Changed repository paths, including deleted ones.
        inventory: Every collectable test file.
        read: Returns a file's source text.
        graph: Backend import graph; built from the checkout when omitted.

    Returns:
        The sorted selection (without the smoke set) and the reason recorded
        in the receipt. A shared root returns the whole inventory.
    """
    for path in paths:
        if any(matches(path, pattern) for pattern in SHARED_ROOTS):
            return inventory, f"shared root: {path}"
    graph = _graph() if graph is None else graph
    modules: set[str] = set()
    files: list[str] = []
    for path in paths:
        top = path.split("/")[:2]
        if path.endswith(".py") and top[:1] == ["backend"] and len(top) == 2:
            if top[1] in ci_import_graph.PACKAGES:
                modules.add(ci_import_graph.module_name(path))
                continue
        files.append(path)
    # A data file, template or doc reaches tests through whichever module
    # (or test) spells its name.
    for path in files:
        needles = _mentions(path)
        for source in graph:
            if any(needle in read(source) for needle in needles):
                modules.add(ci_import_graph.module_name(source))
    loaders = ci_import_graph.importers(graph, sorted(modules), LAZY_HOPS)
    # Endpoint tests often reach a router only over HTTP, through a conftest
    # app or client fixture, so an API change also selects those tests.
    if any(m == "preloop.api" or m.startswith("preloop.api.") for m in modules):
        loaders |= _app_fixture_tests(graph, read)
    known = set(inventory)
    selected = sorted((loaders | set(paths)) & known)
    return selected, (
        f"import graph: {len(modules)} changed or mentioning modules select "
        f"{len(selected)} test files"
    )


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


def shard_count(
    tests: list[str], inventory: list[str], durations: dict[str, float]
) -> int:
    """Size the Backend Tests matrix so a shard runs no longer than in full.

    Args:
        tests: Selected test files.
        inventory: Every collectable test file.
        durations: Recorded per-node durations in seconds.

    Returns:
        ``SHARDS`` for the whole inventory; otherwise enough shards to keep
        each near a full-suite shard's estimated length, at least one and at
        most one per file.
    """
    if set(tests) >= set(inventory):
        return SHARDS
    per_shard = estimated_seconds(inventory, durations) / SHARDS
    needed = math.ceil(estimated_seconds(tests, durations) / max(per_shard, 1.0))
    return max(1, min(SHARDS, len(tests), needed))


def select(
    paths: list[str],
    policy: dict[str, Any],
    inventory: list[str],
    *,
    event: str = "pull_request",
    ref: str = "",
    labels: tuple[str, ...] = (),
    base_ref: str = "main",
    attempt: int = 1,
    durations: dict[str, float] | None = None,
    read: Callable[[str], str] = _read_source,
    graph: ci_import_graph.Graph | None = None,
) -> dict[str, Any]:
    """Return job and backend test decisions with their reasons.

    Args:
        paths: Changed repository paths (PR diff, or the ``main`` push diff).
        policy: Parsed ``.github/ci-policy.json``.
        inventory: Every collectable test file.
        event: ``GITHUB_EVENT_NAME``.
        ref: ``GITHUB_REF``; a push to ``refs/heads/main`` is selective.
        labels: Pull request label names.
        base_ref: Pull request base branch.
        attempt: ``GITHUB_RUN_ATTEMPT``.
        durations: Recorded per-node durations in seconds.
        read: Returns a file's source text.
        graph: Backend import graph; built from the checkout when omitted.

    Returns:
        The receipt: job outputs (``live``), the backend test files, their
        reason, count and shard plan, and the fast-lane selection.
    """
    durations = durations or {}
    main_push = event == "push" and ref == "refs/heads/main"
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

    # Backend tests: only the jobs' full reasons that mean "validate
    # everything" select the whole inventory. A security path still runs
    # every job, but its tests come from the import graph like any module.
    tests_full = ""
    if main_push:
        tests_full = "full main rerun" if attempt > 1 else ""
        if not tests_full and not paths:
            tests_full = "empty or unavailable main diff"
    elif event != "pull_request" or reason in {
        "full PR rerun",
        "ci:full",
        "stacked PR: validate its real base",
        "empty or unavailable diff",
    }:
        tests_full = reason
    if tests_full:
        tests, impact_reason = inventory, tests_full
    else:
        tests, impact_reason = impacted_tests(paths, inventory, read, graph)
    backend_full = set(tests) >= set(inventory)
    backend = sorted(set(tests) | (set(SMOKE) & set(inventory)))
    fast: list[str] = []
    fast_reason = "backend not selected"
    if live["backend"]:
        fast, fast_reason = fast_selection(
            paths, inventory, tests, impact_reason, durations, read
        )
    regular = [path for path in backend if path not in SERIAL_FILES]
    regular_inventory = [path for path in inventory if path not in SERIAL_FILES]
    shards = shard_count(regular, regular_inventory, durations) if regular else 0
    jobs = shards + bool(SERIAL_FILES.intersection(backend))
    return {
        "version": policy["version"],
        "mode": "impact",
        "full": full,
        "reason": reason or "legacy job paths remain authoritative",
        "paths": paths,
        "legacy": legacy,
        "live": live,
        "candidate_leaves": candidate,
        "candidate_tests": backend,
        "impact_reason": impact_reason,
        "backend_full": backend_full,
        "backend_count": len(backend),
        "backend_estimated_seconds": round(estimated_seconds(backend, durations), 1),
        "backend_shards": shards,
        "backend_jobs": jobs,
        "fast_tests": fast,
        "fast_reason": fast_reason,
        "fast_estimated_seconds": round(estimated_seconds(fast, durations), 1),
    }


def changed_paths(base: str, head: str) -> list[str]:
    """Read the complete PR diff; renames include both old and new paths."""
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"],
        check=True,
        capture_output=True,
    )
    return sorted(set(result.stdout.decode().strip("\0").split("\0")) - {""})


def _is_commit(sha: str) -> bool:
    """Whether ``sha`` names a commit present in the checkout."""
    if not sha or set(sha) == {"0"}:
        return False
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
        ).returncode
        == 0
    )


def last_green_main(head: str) -> str:
    """Return the newest ``main`` commit before ``head`` whose CI run passed.

    Superseded ``main`` runs are cancelled, so diffing only against the
    push's ``before`` would never test the cancelled commits' changes on
    ``main``. Any API or ancestry problem returns ``""`` and the caller falls
    back to ``before``.

    Args:
        head: The commit under test.

    Returns:
        A commit SHA that is an ancestor of ``head``, or ``""``.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return ""
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/ci.yml/runs"
        "?branch=main&event=push&status=success&per_page=30"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            runs = json.load(response).get("workflow_runs", [])
    except (OSError, ValueError):
        return ""
    for run in runs:
        sha = str(run.get("head_sha") or "")
        if sha == head or not _is_commit(sha):
            continue
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, head], capture_output=True
        )
        if ancestor.returncode == 0:
            return sha
    return ""


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
    ref = os.environ.get("GITHUB_REF", "")
    group = event.get("merge_group", {})
    head = (
        pr.get("head", {}).get("sha")
        or group.get("head_sha")
        or os.environ.get("CI_HEAD_SHA")
        or os.environ["GITHUB_SHA"]
    )
    main_push = name == "push" and ref == "refs/heads/main"
    base = (
        pr.get("base", {}).get("sha")
        or group.get("base_sha")
        or os.environ.get("CI_BASE_SHA")
        or (last_green_main(head) if main_push else "")
        or event.get("before")
        or subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip()
    )
    paths: list[str] = []
    if name == "pull_request":
        paths = changed_paths(base, head)
    elif main_push and _is_commit(base):
        paths = changed_paths(base, head)
    durations = json.loads((ROOT / ".github/pytest-split-durations.json").read_text())
    receipt = select(
        paths,
        json.loads((ROOT / ".github/ci-policy.json").read_text()),
        discover_files(ROOT),
        event=name,
        ref=ref,
        labels=tuple(label["name"] for label in pr.get("labels", [])),
        base_ref=pr.get("base", {}).get("ref", "main"),
        attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
        durations=durations,
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
    shards = receipt["backend_shards"]
    jobs = receipt["backend_jobs"]
    outputs = {
        **{k: str(v).lower() for k, v in receipt["live"].items()},
        "full": str(receipt["full"]).lower(),
        "backend_full": str(receipt["backend_full"]).lower(),
        "backend_shards": str(shards),
        "backend_jobs": str(jobs),
        "backend_groups": json.dumps(list(range(1, jobs + 1))),
    }
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(
            f"### Backend test selection\n\n{receipt['backend_count']} of "
            f"{len(discover_files(ROOT))} test files in {jobs} job(s): "
            f"{receipt['impact_reason']}\n\n"
        )
        summary.write("### Selection receipt\n\n```json\n")
        summary.write(json.dumps(receipt, indent=2) + "\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
