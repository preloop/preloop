#!/usr/bin/env python3
"""Reuse recent successful same-PR tests only for identical effective inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

WORKFLOW = ".github/workflows/ci.yml"
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SUITES = {
    "backend": {
        **{f"Backend Tests ({i}/8)": ("Run tests",) for i in range(1, 9)},
        "Backend Coverage": ("Combine coverage and enforce floor",),
    },
    "frontend": {"Frontend Tests": ("Check formatting", "Typecheck", "Run tests")},
    "plugins": {
        "Runtime Plugin Tests": (
            "Run runtime-plugin pytest",
            "Run OpenClaw plugin tests",
            "Run Claude plugin tests",
        )
    },
    "cli": {"CLI Tests (Windows)": ("Build", "Vet", "Test")},
}


def git(*args: str) -> bytes:
    """Run bounded, argument-separated Git commands without exposing credentials."""
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, timeout=20
    ).stdout


def fingerprint_tree(tree: bytes) -> str:
    """Hash all tracked modes, object IDs and paths except CHANGELOG text bytes.

    Keep CHANGELOG's path/mode so addition, deletion, symlink or executable-mode
    changes still invalidate evidence. No other Markdown/documentation is ignored.
    """
    records = []
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, _oid = metadata.split(b" ", 2)
        if path == b"CHANGELOG.md" and mode == b"100644" and kind == b"blob":
            record = b"100644 blob CHANGELOG-CONTENT-ONLY\tCHANGELOG.md"
        records.append(record)
    return hashlib.sha256(b"preloop-ci-inputs-v1\0" + b"\0".join(records)).hexdigest()


def fingerprint(commit: str) -> str:
    """Fingerprint an immutable Git object, including workflow and helper code."""
    if not SHA.fullmatch(commit):
        raise ValueError("Invalid commit SHA")
    return fingerprint_tree(git("ls-tree", "-r", "-z", commit))


def successful(job: dict[str, Any], required_steps: tuple[str, ...]) -> bool:
    """Require an actual completed job and all named steps, never a skipped test."""
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        return False
    for name in required_steps:
        steps = [step for step in job.get("steps", []) if step.get("name") == name]
        if len(steps) != 1 or steps[0].get("conclusion") != "success":
            return False
        if steps[0].get("status") != "completed":
            return False
    return True


def proven_suites(jobs: list[dict[str, Any]], digest: str) -> set[str]:
    """Accept exact, unique fingerprinted job names with real successful steps."""
    proven = set()
    for suite, required_jobs in SUITES.items():
        valid = True
        for name, steps in required_jobs.items():
            expected = f"{name} [inputs {digest}]"
            matches = [job for job in jobs if job.get("name") == expected]
            if len(matches) != 1 or not successful(matches[0], steps):
                valid = False
                break
        if valid:
            proven.add(suite)
    return proven


def tested_commit(jobs: list[dict[str, Any]]) -> str | None:
    """Read GitHub-evaluated SHA metadata, backed by successful checkout checking."""
    matches = []
    for job in jobs:
        match = re.fullmatch(
            r"Detect changed paths \[merge ([0-9a-f]{40})\]", job.get("name", "")
        )
        if match and successful(job, (f"Verify checkout {match[1]}",)):
            matches.append(match[1])
    return matches[0] if len(matches) == 1 else None


class GitHub:
    """Read Actions metadata with a read-only token; no artifacts or cache trust."""

    def __init__(self, repo: str, token: str) -> None:
        if not REPOSITORY.fullmatch(repo):
            raise ValueError("Invalid repository")
        self.repo = repo
        self.token = token

    def get(self, path: str) -> dict[str, Any]:
        """Fetch bounded JSON from the fixed GitHub API host."""
        request = Request(
            f"https://api.github.com/repos/{self.repo}/{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        return self.read_json(request)

    @staticmethod
    def read_json(request: Request) -> dict[str, Any]:
        """Reject non-object API replies so unavailable evidence means fresh CI."""
        with urlopen(request, timeout=10) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("Expected API object")
        return data

    def trusted_workflow(self, run: dict[str, Any]) -> bool:
        """Verify GitHub's immutable executed workflow source before job names.

        WorkflowRun.file is the executed workflow, unlike mutable PR head/base
        metadata. A different historical workflow cannot spoof the SHA marker.
        """
        query = "query($id: ID!) { node(id: $id) { ... on WorkflowRun { databaseId file { path repositoryFileUrl } } } }"
        request = Request(
            "https://api.github.com/graphql",
            data=json.dumps(
                {"query": query, "variables": {"id": run["node_id"]}}
            ).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        result = self.read_json(request)
        if result.get("errors"):
            return False
        node = result.get("data", {}).get("node", {})
        source = node.get("file", {})
        if node.get("databaseId") != run["id"] or source.get("path") != WORKFLOW:
            return False
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(self.repo)}/blob/([0-9a-f]{{40}})/{re.escape(WORKFLOW)}",
            source.get("repositoryFileUrl", ""),
        )
        if not match:
            return False
        commit = match[1]
        git(
            "fetch",
            "--no-tags",
            "--depth=1",
            f"https://github.com/{self.repo}.git",
            commit,
        )
        return git("show", f"{commit}:{WORKFLOW}") == git("show", f"HEAD:{WORKFLOW}")

    def matching_fingerprint(self, commit: str, digest: str) -> bool:
        """Fetch the actual historic merge commit and independently hash its tree.

        GitHub force-updates refs/pull/N/merge, so an older merge SHA may no
        longer be reachable. Treat fetch/object failures as no-match so the
        caller can try another run instead of aborting all reuse.
        """
        if not SHA.fullmatch(commit):
            return False
        try:
            git(
                "fetch",
                "--no-tags",
                "--depth=1",
                f"https://github.com/{self.repo}.git",
                commit,
            )
            if git("cat-file", "-t", commit).strip() != b"commit":
                return False
            return fingerprint(commit) == digest
        except subprocess.SubprocessError:
            return False


def recent_run(
    run: dict[str, Any], repo: str, pr: int, current_run: int, now: datetime
) -> bool:
    """Reject other PRs/repos, stale runs, canceled runs and ambiguous metadata."""
    created = datetime.fromisoformat(
        str(run.get("created_at", "")).replace("Z", "+00:00")
    )
    return (
        type(run.get("id")) is int
        and run["id"] != current_run
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] >= 1
        and run.get("event") == "pull_request"
        and run.get("path") == WORKFLOW
        and run.get("status") == "completed"
        and run.get("conclusion") in {"success", "failure"}
        and run.get("repository", {}).get("full_name") == repo
        and run.get("head_repository", {}).get("full_name") == repo
        and [item.get("number") for item in run.get("pull_requests", [])] == [pr]
        and now - timedelta(hours=24) <= created <= now
    )


def records(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Validate lists at the API boundary, including null/partial replies."""
    rows = data.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid API records")
    return rows


def find_reuse(
    api: GitHub, pr: int, current_run: int, digest: str, now: datetime, branch: str
) -> dict[str, str]:
    """Find independent actual suite passes within a small read/time budget."""
    started = time.monotonic()
    evidence: dict[str, str] = {}
    query = urlencode({"event": "pull_request", "per_page": 20, "branch": branch})
    runs = records(api.get(f"actions/workflows/ci.yml/runs?{query}"), "workflow_runs")
    for run in runs[:20]:
        if time.monotonic() - started >= 90:
            break
        if not recent_run(run, api.repo, pr, current_run, now):
            continue
        run_id, attempt = run["id"], run["run_attempt"]
        response = api.get(
            f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
        )
        jobs = records(response, "jobs")
        # Never infer success from a partial page, a different run, or duplicate
        # matrix names. Existing CI is comfortably below this bound.
        if response.get("total_count") != len(jobs) or len(jobs) > 100:
            continue
        if any(job.get("run_id") != run_id for job in jobs):
            continue
        commit = tested_commit(jobs)
        suites = proven_suites(jobs, digest) - evidence.keys()
        if not commit or not suites or not api.trusted_workflow(run):
            continue
        if not api.matching_fingerprint(commit, digest):
            continue
        url = f"https://github.com/{api.repo}/actions/runs/{run_id}/attempts/{attempt}"
        evidence.update(dict.fromkeys(suites, url))
        if len(evidence) == len(SUITES):
            break
    return evidence


def plan(event: dict[str, Any], env: dict[str, str], digest: str) -> dict[str, str]:
    """Force fresh main/tag/manual/rerun/fork jobs; API errors also mean fresh."""
    repo = env["GITHUB_REPOSITORY"]
    pr = event.get("pull_request", {})
    if (
        env.get("GITHUB_EVENT_NAME") != "pull_request"
        or env.get("GITHUB_RUN_ATTEMPT", "1") != "1"
        or pr.get("head", {}).get("repo", {}).get("full_name") != repo
        or pr.get("base", {}).get("repo", {}).get("full_name") != repo
        or type(event.get("number")) is not int
        or any(label.get("name") == "ci-force-fresh" for label in pr.get("labels", []))
    ):
        return {}
    try:
        return find_reuse(
            GitHub(repo, env["GH_TOKEN"]),
            event["number"],
            int(env["GITHUB_RUN_ID"]),
            digest,
            datetime.now(timezone.utc),
            pr["head"]["ref"],
        )
    except (
        URLError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ):
        # Do not echo API responses, auth headers or subprocess stderr to logs.
        print("CI reuse evidence unavailable or invalid; running fresh tests.")
        return {}


def main() -> None:
    """Publish safe scheduling outputs and transparent historical-run links."""
    commit = os.environ["GITHUB_SHA"]
    if git("rev-parse", "HEAD").decode().strip() != commit:
        raise ValueError("Checkout differs from the event's immutable commit")
    digest = fingerprint(commit)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    evidence = plan(event, dict(os.environ), digest)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"fingerprint={digest}\n")
        for suite in SUITES:
            output.write(f"reuse_{suite}={'true' if suite in evidence else 'false'}\n")
            output.write(f"evidence_{suite}={evidence.get(suite, '')}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(
            f"### CI test evidence\n\nEffective input fingerprint: `{digest}`.\n\n"
        )
        for suite in SUITES:
            if suite in evidence:
                summary.write(
                    f"- {suite}: reuse [actual successful tests]({evidence[suite]}).\n"
                )
            else:
                summary.write(
                    f"- {suite}: no reuse; run when required by changed paths.\n"
                )
        summary.write(
            "\nOnly regular root CHANGELOG.md content is excluded; all other tracked inputs must match.\n"
        )


if __name__ == "__main__":
    main()
