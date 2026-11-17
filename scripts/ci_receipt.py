"""Validate the full aggregate and retain timing/attribution evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import subprocess
from typing import Any

from ci_control import GitHub

JOB_PATHS = {
    "helm-lint": "helm",
    "requirements-resolve": "requirements",
    "test-backend": "backend",
    "test-backend-coverage": "backend",
    "test-frontend": "frontend",
    "test-runtime-plugins": "plugins",
    "test-egress-proxy": "egress",
    "test-browser-profile": "browser",
    "test-cli-windows": "cli",
    "test-cli-linux": "cli",
    **{
        f"leaf-{key.replace('_', '-')}": key
        for key in (
            "parity",
            "windows",
            "docs",
            "fuzz",
            "browser_fixture",
            "totp",
            "release_validation",
            "upgrade",
            "claude",
        )
    },
}
SCANNERS = {"full-codeql", "full-secret-scan", "full-zizmor"}
ALWAYS = {"changes", "pick-runner", "fast", "lint", "cli-vuln-scan"}


def validate(needs: dict[str, Any], event: str, *, fork: bool = False) -> None:
    """Reject missing results and unexplained skips as well as explicit reds."""
    expected_jobs = set(JOB_PATHS) | SCANNERS | ALWAYS
    if set(needs) != expected_jobs:
        raise ValueError("Aggregate job inventory differs from the coverage contract")
    outputs = needs["changes"]["outputs"]
    for job, value in needs.items():
        expected = "success"
        if job in JOB_PATHS:
            selected = outputs[JOB_PATHS[job]]
            if selected not in {"true", "false"}:
                raise ValueError(f"Invalid selection output for {job}")
            expected = "success" if selected == "true" else "skipped"
        if job in SCANNERS and event in {"pull_request", "push"}:
            expected = "skipped"  # independent every-PR and main/tag scanner workflows
        if job == "leaf-claude" and fork and value["result"] in {"success", "skipped"}:
            continue  # the live-SDK job needs secrets, so it skips for forks
        if value["result"] != expected:
            raise ValueError(f"{job}: expected {expected}, got {value['result']}")


def seconds(start: str, end: str) -> float:
    """Measure timestamp differences without conflating wait and execution."""
    return (
        datetime.fromisoformat(end.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()


def timings(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Record steps and shard balance separately for each runner class."""
    rows = []
    shards: dict[str, list[float]] = {}
    for job in jobs:
        kind = (
            "self-hosted" if "self-hosted" in job.get("labels", []) else "github-hosted"
        )
        duration = (
            seconds(job["started_at"], job["completed_at"])
            if job.get("started_at") and job.get("completed_at")
            else None
        )
        row = {
            k: job.get(k)
            for k in (
                "id",
                "name",
                "conclusion",
                "created_at",
                "started_at",
                "completed_at",
                "runner_id",
            )
        }
        row.update(runner_class=kind, execution_seconds=duration)
        row["queue_seconds"] = (
            seconds(job["created_at"], job["started_at"])
            if job.get("created_at") and job.get("started_at")
            else None
        )
        row["steps"] = [
            {
                k: step.get(k)
                for k in ("name", "conclusion", "started_at", "completed_at")
            }
            for step in job.get("steps", [])
        ]
        rows.append(row)
        if (
            job["name"].startswith("Backend Tests (")
            and job["conclusion"] == "success"
            and duration
        ):
            shards.setdefault(kind, []).append(duration)
    return {
        "jobs": rows,
        "shard_max_over_median": {
            kind: max(values) / statistics.median(values)
            for kind, values in shards.items()
        },
    }


def fetch_jobs(api: GitHub, run_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """Read this run's jobs for the timing receipt, without gating on it.

    The pass/fail decision is made by ``validate`` from ``needs``; timings are
    evidence only, so an API failure is recorded and warned about instead of
    failing the aggregate check.

    Args:
        api: Repository API client.
        run_id: Workflow run id.

    Returns:
        The job rows (empty on failure) and the error text, if any.
    """
    try:
        return api.pages(f"actions/runs/{run_id}/jobs", "jobs"), None
    except (
        RuntimeError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        print(f"::warning::timing receipt incomplete: {exc}")
        return [], str(exc)


def main() -> None:
    """Fail closed and upload an immutable candidate/timing receipt."""
    needs = json.loads(os.environ["CI_NEEDS"])
    validate(
        needs,
        os.environ["GITHUB_EVENT_NAME"],
        fork=os.environ.get("CI_FORK") == "true",
    )
    api = GitHub(os.environ["GITHUB_REPOSITORY"])
    selection = json.loads(Path("ci-selection.json").read_text())
    jobs, timings_error = fetch_jobs(api, os.environ["GITHUB_RUN_ID"])
    receipt = {
        **selection,
        "timings_error": timings_error,
        **timings(jobs),
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "fast_green_at": next(
            (j["completed_at"] for j in jobs if j["name"] == "CI Fast"), None
        ),
        "results": {k: v["result"] for k, v in needs.items()},
        "full_validation": selection["full"],
        "runner_availability": needs["pick-runner"].get("outputs", {}),
    }
    Path("ci-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
