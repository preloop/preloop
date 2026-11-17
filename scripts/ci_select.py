"""Compare CI impact decisions without removing existing PR coverage.

The legacy job selection remains authoritative. Security paths, stacks and full
events expand it; candidate test/leaf selections are receipts only until a
separately reviewed promotion after two weeks of comparison.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

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


def impacted_tests(paths: list[str], inventory: list[str]) -> tuple[list[str], str]:
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
        relative = path.removeprefix("backend/preloop/")
        if relative.startswith("api/endpoints/"):
            stem = Path(relative).stem
            mapped = [p for p in inventory if Path(p).stem.startswith(f"test_{stem}")]
        else:
            area = relative.split("/", 1)[0]
            if area not in {"sync", "agents", "services", "utils", "tools", "api"}:
                return inventory, "shared or unmapped module"
            mapped = [p for p in inventory if p.startswith(f"backend/tests/{area}/")]
        if not mapped:
            return inventory, "no matching tests"
        selected.update(mapped)
        selected.update(p for p in inventory if "/integration/" in p)
    return sorted(selected), "structural candidate"


def select(
    paths: list[str],
    policy: dict[str, Any],
    inventory: list[str],
    *,
    event: str = "pull_request",
    labels: tuple[str, ...] = (),
    base_ref: str = "main",
    attempt: int = 1,
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
    tests, impact_reason = impacted_tests(paths, inventory)
    if full or (live["backend"] and not tests):
        tests, impact_reason = inventory, reason or "unknown backend impact"
    fast = sorted(set(SMOKE) | set(tests)) if live["backend"] else []
    # Unknown impact retains the full heavy tier; fast feedback still runs the
    # smoke contract plus known changed tests, without duplicating all shards.
    if tests == inventory:
        fast = sorted(set(SMOKE) | {p for p in paths if p in inventory})
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
