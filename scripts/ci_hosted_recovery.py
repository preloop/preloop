#!/usr/bin/env python3
"""Recover CI from unavailable optional runners once, without running PR code."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

POLL_SECONDS = 30
QUEUE_GRACE_SECONDS = 180
WATCH_SECONDS = 30 * 60
CANCEL_SECONDS = 120
BACKEND_JOB = re.compile(r"Backend Tests \(\d+/18\)\Z")
GENERAL_LABELS = {"self-hosted", "Linux", "X64", "preloop-ci-general"}


class ApiError(RuntimeError):
    """An API operation failed; never print captured output or credentials."""


class GitHub:
    """Bounded GitHub CLI calls; the inventory token is read-only and optional."""

    def __init__(self, repository: str) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository = repository
        self.prefix = f"repos/{repository}"

    def request(
        self, path: str, *, method: str = "GET", inventory: bool = False
    ) -> Any:
        env = dict(os.environ)
        if inventory:
            token = env.get("RUNNER_READ_TOKEN")
            if not token:
                raise ApiError("Runner inventory credential unavailable")
            env["GH_TOKEN"] = token
        try:
            result = subprocess.run(
                ["gh", "api", "--method", method, f"{self.prefix}/{path}"],
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError("GitHub request unavailable") from exc
        if result.returncode:
            raise ApiError(f"GitHub {method} request failed")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise ApiError("Invalid GitHub response") from exc

    def pages(self, path: str, key: str, *, inventory: bool = False) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, 21):
            response = self.request(
                f"{path}?per_page=100&page={page}", inventory=inventory
            )
            batch = response[key]
            if not isinstance(batch, list):
                raise ApiError("Invalid GitHub page")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise ApiError("GitHub pagination limit reached")


def current_trusted_run(api: GitHub, run: dict) -> bool:
    """Require the current head of a trusted PR or a main push, first attempt."""
    if (
        run.get("name") != "CI"
        or run.get("path") != ".github/workflows/ci.yml"
        or run.get("run_attempt") != 1
        or (run.get("head_repository") or {}).get("full_name") != api.repository
        or (run.get("actor") or {}).get("login") == "dependabot[bot]"
    ):
        return False
    if run.get("event") == "pull_request":
        for reference in run.get("pull_requests") or []:
            pr = api.request(f"pulls/{int(reference['number'])}")
            if (
                pr.get("state") == "open"
                and pr["head"]["sha"] == run["head_sha"]
                and pr["head"]["repo"]["full_name"] == api.repository
            ):
                return True
        return False
    if run.get("event") == "push" and run.get("head_branch") == "main":
        ref = api.request("git/ref/heads/main")
        return ref["object"]["sha"] == run["head_sha"]
    return False


def self_hosted_backend(job: dict) -> bool:
    """Select only the CI matrix's eligible general-pool backend jobs."""
    labels = set(job.get("labels") or [])
    return bool(
        BACKEND_JOB.fullmatch(job.get("name", ""))
        and GENERAL_LABELS <= labels
        and "gce-tests-only" not in labels
    )


def needs_recovery(jobs: list[dict], runners: list[dict] | None, now: datetime) -> bool:
    """Detect long unassigned queues or an interrupted assigned runner.

    Offline inventory is evidence of infrastructure loss, not proof a failed
    test was caused by it. A single hosted retry still has to pass every check.
    Unknown inventory never justifies retrying a completed test failure.
    """
    online = (
        {runner["id"] for runner in runners if runner.get("status") == "online"}
        if runners is not None
        else None
    )
    for job in jobs:
        if not self_hosted_backend(job):
            continue
        if job.get("status") == "queued" and not job.get("runner_id"):
            created = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
            if (now - created).total_seconds() >= QUEUE_GRACE_SECONDS:
                return True
        if (
            online is not None
            and job.get("runner_id")
            and job["runner_id"] not in online
            and (
                job.get("status") == "in_progress"
                or job.get("conclusion") in {"failure", "timed_out"}
            )
        ):
            return True
    return False


def recover(api: GitHub, run_id: int) -> str:
    """Watch one run, cancel only a current active run, then retry once hosted."""
    path = f"actions/runs/{run_id}"
    deadline = time.monotonic() + WATCH_SECONDS
    while time.monotonic() < deadline:
        run = api.request(path)
        if not current_trusted_run(api, run):
            return "not a current trusted first attempt"
        if run.get("conclusion") in {"cancelled", "success", "skipped", "neutral"}:
            return "run already finished or was canceled"
        jobs = api.pages(f"{path}/attempts/1/jobs", "jobs")
        runners = None
        if any(
            self_hosted_backend(job)
            and job.get("runner_id")
            and (
                job.get("status") == "in_progress"
                or job.get("conclusion") in {"failure", "timed_out"}
            )
            for job in jobs
        ):
            try:
                runners = api.pages("actions/runners", "runners", inventory=True)
            except ApiError:
                pass
        if needs_recovery(jobs, runners, datetime.now(timezone.utc)):
            # Recheck immediately before every mutation: a new PR push or a
            # user cancellation must not be undone by an old watchdog.
            fresh = api.request(path)
            if not current_trusted_run(api, fresh):
                return "run superseded before recovery"
            if fresh.get("status") == "completed":
                if fresh.get("conclusion") not in {"failure", "timed_out"}:
                    return "run finished before recovery"
            else:
                api.request(f"{path}/cancel", method="POST")
                cancel_deadline = time.monotonic() + CANCEL_SECONDS
                while time.monotonic() < cancel_deadline:
                    fresh = api.request(path)
                    if not current_trusted_run(api, fresh):
                        return "run superseded during cancellation"
                    if fresh.get("status") == "completed":
                        break
                    time.sleep(5)
                else:
                    return "cancellation did not finish; no retry"
                if fresh.get("conclusion") not in {"cancelled", "failure", "timed_out"}:
                    return "run finished successfully during cancellation"
            fresh = api.request(path)
            if (
                not current_trusted_run(api, fresh)
                or fresh.get("status") != "completed"
            ):
                return "run changed before retry"
            if fresh.get("conclusion") not in {"cancelled", "failure", "timed_out"}:
                return "run no longer needs retry"
            api.request(f"{path}/rerun-failed-jobs", method="POST")
            return "requested one hosted retry of unfinished jobs"
        if run.get("status") == "completed":
            return "completed without evidence of unavailable runners"
        time.sleep(POLL_SECONDS)
    return "watch deadline reached; no mutation"


def main() -> None:
    """Entrypoint used only by the trusted default-branch watchdog workflow."""
    api = GitHub(os.environ["GITHUB_REPOSITORY"])
    run_id = int(os.environ["CI_RUN_ID"])
    try:
        outcome = recover(api, run_id)
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        # No raw API output, logs, tokens or PR-controlled text in logs.
        print(f"Recovery stopped safely ({type(exc).__name__}); no further action")
        raise SystemExit(1) from None
    print(outcome)


if __name__ == "__main__":
    main()
