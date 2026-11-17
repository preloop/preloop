"""Trusted CI control operations; never import or execute candidate code."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import threading
import zipfile


DOWNLOAD_TIMEOUT = 45.0


class GitHub:
    """Use bounded authenticated requests without exposing credentials."""

    def __init__(self, repository: str) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository = repository

    def request(
        self, path: str, data: dict[str, Any] | None = None, *, method: str = "GET"
    ) -> Any:
        """Read or mutate a repository resource using structured input."""
        args = ["gh", "api", f"repos/{self.repository}/{path}", "--method", method]
        if data is not None:
            args += ["--input", "-"]
        result = subprocess.run(
            args,
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"GitHub {method} failed; no raw response logged")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def pages(self, path: str, key: str = "") -> list[dict[str, Any]]:
        """Read every page, failing closed at the explicit bound."""
        rows: list[dict[str, Any]] = []
        join = "&" if "?" in path else "?"
        for page in range(1, 101):
            response = self.request(f"{path}{join}per_page=100&page={page}")
            batch = response[key] if key else response
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise RuntimeError("Pagination bound exceeded")


def approved_head(pr: dict[str, Any], reviews: list[dict[str, Any]]) -> bool:
    """Require a current-head approval and no outstanding change request."""
    latest: dict[str, dict[str, Any]] = {}
    for review in sorted(reviews, key=lambda r: r["id"]):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["login"]] = review
    return not any(r["state"] == "CHANGES_REQUESTED" for r in latest.values()) and any(
        r["state"] == "APPROVED"
        and r["commit_id"] == pr["head"]["sha"]
        and r["user"]["login"] != pr["user"]["login"]
        for r in latest.values()
    )


def read_selection(api: GitHub, run_id: int) -> dict[str, Any] | None:
    """Read bounded JSON evidence; never extract or execute artifact content."""
    artifacts = api.pages(f"actions/runs/{run_id}/artifacts", "artifacts")
    matches = [a for a in artifacts if a["name"] == "ci-selection" and not a["expired"]]
    if len(matches) != 1:
        return None
    limit = 4 * 1024 * 1024
    # Stream with a hard bound: the artifact comes from the candidate's own
    # job, so never buffer more than the limit before rejecting it.
    with subprocess.Popen(
        [
            "gh",
            "api",
            f"repos/{api.repository}/actions/artifacts/{matches[0]['id']}/zip",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as proc:
        assert proc.stdout is not None
        # The wall-clock bound covers the read itself: a stalled download is
        # killed mid-read, which ends the read with a short or empty payload.
        timer = threading.Timer(DOWNLOAD_TIMEOUT, proc.kill)
        timer.start()
        try:
            payload = proc.stdout.read(limit + 1)
        finally:
            timer.cancel()
        if len(payload) > limit:
            proc.kill()
            return None
        try:
            code = proc.wait(timeout=DOWNLOAD_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            return None
    if code:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entry = archive.getinfo("ci-selection.json")
            if entry.file_size > 2 * 1024 * 1024:
                return None
            with archive.open(entry) as handle:
                raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return None
        selection = json.loads(raw)
    except (zipfile.BadZipFile, KeyError, ValueError):
        # Malformed evidence fails closed: no rerun is requested.
        return None
    return selection if isinstance(selection, dict) else None


def approve_full(api: GitHub, number: int) -> str:
    """Request full tests in the original PR context, preserving its trust scope."""
    pr = api.request(f"pulls/{number}")
    if pr["state"] != "open" or not pr.get("merge_commit_sha"):
        return "PR is closed or its merge candidate is unavailable"
    if not approved_head(pr, api.pages(f"pulls/{number}/reviews")):
        return "no approval on the current head"
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    runs = api.pages(
        f"actions/workflows/ci.yml/runs?event=pull_request&head_sha={head}",
        "workflow_runs",
    )
    eligible = [
        r
        for r in runs
        if r["head_sha"] == head
        and (
            not r["pull_requests"]
            or any(p["number"] == number for p in r["pull_requests"])
        )
    ]
    if not eligible:
        return "no CI run for this head"
    run = max(eligible, key=lambda r: r["id"])
    jobs = api.pages(f"actions/runs/{run['id']}/jobs", "jobs")
    if not any(j["name"] == "CI Fast" and j["conclusion"] == "success" for j in jobs):
        return "fast tier is not green"
    selection = read_selection(api, run["id"])
    if (
        not selection
        or selection.get("head_sha") != head
        or selection.get("base_sha") != base
    ):
        return "no receipt for the current head/base; a fresh PR event or merge queue is required"
    tested = selection.get("tested_sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", tested):
        return "invalid tested commit"
    commit = api.request(f"git/commits/{tested}")
    if [p["sha"] for p in commit["parents"]] != [base, head]:
        return "tested candidate does not contain the current head and real base"
    if selection.get("full") is True or run["run_attempt"] > 1:
        return "this head/base already runs the full tier"
    if run["status"] != "completed":
        return "full rerun follows completion of the current PR run"
    fresh = api.request(f"pulls/{number}")
    if (
        fresh["state"] != "open"
        or fresh["head"]["sha"] != head
        or fresh["base"]["sha"] != base
        or not approved_head(fresh, api.pages(f"pulls/{number}/reviews"))
    ):
        return "candidate changed before full rerun"
    latest = api.request(f"actions/runs/{run['id']}")
    if latest["run_attempt"] != 1 or latest["status"] != "completed":
        return "a rerun is already pending"
    # Reruns retain the PR ref, read-only fork token and PR-scoped caches.
    # Never dispatch default-branch code that checks out an arbitrary PR SHA.
    api.request(f"actions/runs/{run['id']}/rerun", {}, method="POST")
    return "requested full rerun of the approved PR merge candidate"


def failing_tests(log: str) -> list[str]:
    """Extract test identifiers, never copy arbitrary job output into issues."""
    found: set[str] = set()
    for line in log.splitlines():
        match = re.search(r"(?:FAILED\s+|--- FAIL:\s+)([A-Za-z0-9_./:\[\]-]+)", line)
        if not match:
            match = re.search(r"([A-Za-z0-9_./-]+\.spec\.[jt]sx?:\d+(?::\d+)?)", line)
        if match:
            found.add(match[1][:250])
    return sorted(found)[:100]


def nightly_issue(
    job: dict[str, Any],
    run: dict[str, Any],
    baseline: str,
    tests: list[str],
    existing: dict[str, Any] | None,
) -> dict[str, str]:
    """Build a stable per-job issue while retaining the first failure range."""
    title = f"Nightly CI: {job['name']}"
    marker = f"<!-- nightly-job:{job['name']} -->"
    first = re.search(
        r"<!-- first-range:([^>]+) -->", (existing or {}).get("body") or ""
    )
    commit_range = first[1] if first else f"{baseline or 'unknown'}...{run['head_sha']}"
    failures = "\n".join(f"- `{name}`" for name in tests)
    if not failures:
        failures = "No test identifier was emitted; failing steps:\n" + "\n".join(
            f"- {step['name']}"
            for step in job.get("steps", [])
            if step.get("conclusion") in {"failure", "timed_out", "cancelled"}
        )
    body = (
        f"{marker}\n<!-- first-range:{commit_range} -->\n"
        f"First failing commit range: `{commit_range}`\n\n"
        f"Failing tests:\n{failures}\n\n"
        f"Latest failing run: {run['html_url']}\n"
        f"Job: {job['html_url']}\n"
    )
    return {"title": title, "body": body}


def follow_up_nightly(api: GitHub, run_id: int) -> str:
    """Open/update exactly one issue per failed job from a scheduled main run."""
    run = api.request(f"actions/runs/{run_id}")
    if (
        run["path"] != ".github/workflows/ci.yml"
        or run["event"] != "schedule"
        or run["head_branch"] != "main"
        or run["status"] != "completed"
        or run["head_repository"]["full_name"] != api.repository
    ):
        return "not a completed trusted nightly"
    jobs = api.pages(f"actions/runs/{run_id}/jobs", "jobs")
    failed = [
        j for j in jobs if j["conclusion"] in {"failure", "timed_out", "cancelled"}
    ]
    if not failed:
        return "nightly has no failing jobs"
    issues = [i for i in api.pages("issues?state=all") if "pull_request" not in i]
    previous = api.pages(
        "actions/workflows/ci.yml/runs?branch=main&status=success", "workflow_runs"
    )
    prior = [
        r
        for r in previous
        if r["created_at"] < run["created_at"] and r["event"] in {"push", "schedule"}
    ]
    prior = sorted(prior, key=lambda r: r["created_at"], reverse=True)[:30]
    previous_jobs: dict[int, list[dict[str, Any]]] = {}
    label = os.environ.get("CI_NIGHTLY_ISSUE_LABEL", "").strip()
    # The founder owns flow activation. No hard-coded flow-triggering labels;
    # an explicit repository variable is the sole opt-in for automatic routing.
    for job in failed:
        marker = f"<!-- nightly-job:{job['name']} -->"
        matching = [i for i in issues if marker in (i.get("body") or "")]
        if len(matching) > 1:
            raise RuntimeError("Duplicate nightly issue markers require reconciliation")
        existing = matching[0] if matching else None
        baseline = ""
        if not existing:
            # A green overall run is not proof this particular job existed or
            # ran. Search recent successful runs for that exact job's success.
            for previous_run in prior:
                previous_id = previous_run["id"]
                if previous_id not in previous_jobs:
                    previous_jobs[previous_id] = api.pages(
                        f"actions/runs/{previous_id}/jobs", "jobs"
                    )
                if any(
                    j["name"] == job["name"] and j["conclusion"] == "success"
                    for j in previous_jobs[previous_id]
                ):
                    baseline = previous_run["head_sha"]
                    break
        result = subprocess.run(
            ["gh", "api", f"repos/{api.repository}/actions/jobs/{job['id']}/logs"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=45,
            check=False,
        )
        tests = failing_tests(result.stdout) if result.returncode == 0 else []
        data: dict[str, Any] = nightly_issue(job, run, baseline, tests, existing)
        if existing:
            data["state"] = "open"
            api.request(f"issues/{existing['number']}", data, method="PATCH")
            number = existing["number"]
        else:
            created = api.request("issues", data, method="POST")
            number = created["number"]
            issues.append(created)
        if label:
            api.request(f"issues/{number}/labels", {"labels": [label]}, method="POST")
    return f"followed up {len(failed)} failing jobs"


def main() -> None:
    """Run only from a trusted default-branch control workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["approve", "nightly"])
    args = parser.parse_args()
    api = GitHub(os.environ["GITHUB_REPOSITORY"])
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if args.operation == "nightly":
        print(follow_up_nightly(api, int(event["workflow_run"]["id"])))
        return
    numbers = (
        {event["pull_request"]["number"]}
        if "pull_request" in event
        else {
            pr["number"]
            for pr in event.get("workflow_run", {}).get("pull_requests", [])
        }
    )
    # Some fork workflow_run payloads omit PR associations. Resolve the commit
    # instead; approve_full still rechecks every live head before a mutation.
    head = event.get("pull_request", {}).get("head", {}).get("sha") or event.get(
        "workflow_run", {}
    ).get("head_sha")
    if head:
        numbers.update(
            pr["number"]
            for pr in api.pages(f"commits/{head}/pulls")
            if pr["state"] == "open" and pr["head"]["sha"] == head
        )
    for number in sorted(numbers):
        print(f"PR #{number}: {approve_full(api, number)}")
    print(f"checked_at={datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
