"""Collect CI health for a scheduled review flow into one compact JSON file.

The daily CI and QA review flow runs this before the agent starts reasoning,
so the agent reads aggregates and failure signatures instead of paging the
CI API itself. Two subcommands:

``collect``
    Pages GitHub Actions runs (and, optionally, GitLab CI pipelines) created
    since the previous run's window end, one request at a time, reads the
    jobs of every completed run, fetches the logs of failed jobs (bounded),
    and extracts failure signatures from pytest, go test, vitest/jest and
    JUnit XML output. It aggregates per workflow and per job (queue wait,
    duration, success, reruns, runner minutes, shard imbalance), updates
    the flake ledger carried in the previous result, and writes one JSON
    document. It stops early, marking the data partial, when the rate limit
    runs low or the API asks it to back off.

``finalize``
    Merges the collector's persisted state (ledger, trend, value history)
    and this run's history entry into the agent's ``result.json``, so the
    next run receives it as ``previous/result.json``.

Only the standard library is used, so the flow can run it before installing
the repository's dependencies. Every request is a GET.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATE_KEY = "ci_health_state"
# The previous result reaches the next run inline and is capped at 256 KiB,
# so everything persisted here is bounded well below that.
MAX_LEDGER_ENTRIES = 100
MAX_TREND_ENTRIES = 84
MAX_HISTORY_ENTRIES = 60
MAX_RECENT = 3
MAX_SEEN_RUNS = 600
# Re-read this much before the previous window end, so a run that was still
# in progress at the last collection is picked up once it completes.
OVERLAP = timedelta(hours=1)
RESOLVED_RETENTION_DAYS = 30
# Serialized ceiling for the persisted state, leaving the rest of the 256 KiB
# previous-result cap to the agent's own report fields.
STATE_BUDGET_BYTES = 160 * 1024
MAX_NOTE_CHARS = 300
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_SIGNATURES_PER_JOB = 20
TOP_N = 15

Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


class StopFetchingError(Exception):
    """The API asked us to stop (rate limit, back-off, or auth failure)."""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects without forwarding headers to the new host.

    GitHub serves job logs through a redirect to a signed blob URL, which
    rejects a request that also carries an Authorization header.
    """

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Build the redirected request with no inherited headers.

        Args:
            req: The original request.
            fp: The response body.
            code: The redirect status.
            msg: The status message.
            headers: The response headers.
            newurl: The redirect target.

        Returns:
            A bare GET request for the target.
        """
        del req, fp, code, msg, headers
        return urllib.request.Request(newurl, method="GET")


def urllib_transport(
    url: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    """Issue one GET request with the standard library.

    Args:
        url: The absolute URL.
        headers: Request headers.

    Returns:
        Status code, lower-cased response headers, and up to MAX_LOG_BYTES
        of the body.

    Raises:
        StopFetchingError: The request failed below HTTP (DNS, TLS, timeout).
    """
    request = urllib.request.Request(url, method="GET")
    for name, value in headers.items():
        request.add_unredirected_header(name, value)
    opener = urllib.request.build_opener(_NoAuthRedirect())
    try:
        with opener.open(request, timeout=60) as response:
            body = response.read(MAX_LOG_BYTES)
            return (
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                body,
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            {k.lower(): v for k, v in (error.headers or {}).items()},
            error.read(65536) if error.fp else b"",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise StopFetchingError(f"network error: {error}") from error


class Client:
    """Sequential, rate-limit aware, cached GET client.

    Args:
        transport: Function performing one GET request.
        headers: Headers sent with every request (auth included).
        min_remaining: Stop once the remaining quota falls below this.
        cache_dir: Directory for cached immutable responses, or None.
        max_calls: Hard ceiling on network calls for one collection.
    """

    def __init__(
        self,
        transport: Transport,
        headers: dict[str, str],
        min_remaining: int = 200,
        cache_dir: Path | None = None,
        max_calls: int = 3000,
    ) -> None:
        self.transport = transport
        self.headers = headers
        self.min_remaining = min_remaining
        self.cache_dir = cache_dir
        self.max_calls = max_calls
        self.calls = 0
        self.cache_hits = 0
        self.remaining: int | None = None

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self.cache_dir / f"{digest}.bin"

    def get(self, url: str, cache: bool = False) -> bytes | None:
        """GET a URL, honouring the rate limit.

        Args:
            url: Absolute URL.
            cache: Whether the response is immutable and may be cached.

        Returns:
            The body, or None for a 404/410 (expired logs, deleted runs).

        Raises:
            StopFetchingError: Quota is low, the API asked for back-off, the
                token was refused, or the call ceiling was reached.
        """
        path = self._cache_path(url) if cache else None
        if path is not None and path.exists():
            self.cache_hits += 1
            return path.read_bytes()
        if self.remaining is not None and self.remaining < self.min_remaining:
            raise StopFetchingError(f"rate limit remaining {self.remaining}")
        if self.calls >= self.max_calls:
            raise StopFetchingError(f"call ceiling {self.max_calls} reached")
        self.calls += 1
        status, headers, body = self.transport(url, self.headers)
        remaining = headers.get("x-ratelimit-remaining") or headers.get(
            "ratelimit-remaining"
        )
        if remaining is not None and remaining.isdigit():
            self.remaining = int(remaining)
        if status in (404, 410):
            return None
        if status == 429 or "retry-after" in headers:
            raise StopFetchingError(f"back-off requested (HTTP {status})")
        if status in (401, 403):
            if self.remaining == 0:
                raise StopFetchingError("rate limit exhausted")
            raise StopFetchingError(f"HTTP {status}: token refused or lacks access")
        if status >= 400:
            raise StopFetchingError(f"HTTP {status} from the CI API")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return body

    def get_json(self, url: str, cache: bool = False) -> Any:
        """GET and decode JSON.

        Args:
            url: Absolute URL.
            cache: Whether the response is immutable and may be cached.

        Returns:
            The decoded document, or None for a missing resource.
        """
        body = self.get(url, cache=cache)
        return None if body is None else json.loads(body)


# --------------------------------------------------------------------------
# Normalized records
# --------------------------------------------------------------------------


@dataclass
class Job:
    """One CI job attempt, provider-neutral."""

    provider: str
    repo: str
    workflow: str
    name: str
    run_id: str
    run_url: str
    attempt: int
    sha: str
    branch: str
    event: str
    conclusion: str
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    os: str = "linux"
    job_id: str = ""
    signatures: list[dict[str, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Workflow/job key used for aggregation."""
        return f"{self.repo}:{self.workflow}/{self.name}"

    @property
    def queue_seconds(self) -> float | None:
        """Seconds between creation and start."""
        if self.created_at and self.started_at:
            return max(0.0, (self.started_at - self.created_at).total_seconds())
        return None

    @property
    def duration_seconds(self) -> float | None:
        """Seconds between start and completion."""
        if self.started_at and self.completed_at:
            return max(0.0, (self.completed_at - self.started_at).total_seconds())
        return None


def parse_time(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from either provider.

    Args:
        value: The timestamp or None.

    Returns:
        An aware datetime, or None.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    """Format a datetime as UTC ISO-8601 with a Z suffix.

    Args:
        value: The datetime.

    Returns:
        The formatted timestamp.
    """
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runner_os(labels: Iterable[str]) -> str:
    """Classify a job's runner from its labels.

    Args:
        labels: Runner labels or tags.

    Returns:
        ``linux``, ``windows`` or ``macos``.
    """
    text = " ".join(labels).lower()
    if "windows" in text:
        return "windows"
    if "macos" in text or "mac-" in text:
        return "macos"
    return "linux"


# --------------------------------------------------------------------------
# Failure signatures
# --------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_GH_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ")
_PYTEST = re.compile(r"^(FAILED|ERROR) (\S+::\S+|\S+\.py)(?: - (.*))?$")
_GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+)")
_GO_PKG = re.compile(r"^FAIL\s+(\S+)\s+[\d.]+s$")
_GO_PANIC = re.compile(r"^panic: (.*)")
_VITEST = re.compile(r"^\s*FAIL\s+(\S+\.(?:[cm]?[jt]sx?))\s+>\s+(.+)$")
_JEST = re.compile(r"^\s*● (.+ › .+)$")
_GH_ERROR = re.compile(r"^##\[error\](.*)")
_ERROR_CLASS = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Failure|Timeout))")
_VOLATILE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0x?"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "N"),
    (re.compile(r"/tmp/\S+|/home/\S+|/Users/\S+|[A-Z]:\\\S+"), "<path>"),
    (re.compile(r"'[^']{40,}'|\"[^\"]{40,}\""), "<str>"),
]


def clean_line(line: str) -> str:
    """Strip ANSI colour codes and the Actions timestamp prefix.

    Args:
        line: A raw log line.

    Returns:
        The cleaned line.
    """
    return _GH_TIMESTAMP.sub("", _ANSI.sub("", line)).rstrip()


def normalize_error(message: str) -> str:
    """Reduce an error message to a stable class or shape.

    Args:
        message: The raw message.

    Returns:
        The error class when one leads the message, else the message with
        numbers, hashes, paths and long literals masked, at most 120 chars.
    """
    message = message.strip()
    match = _ERROR_CLASS.match(message)
    if match:
        return match.group(1)
    for pattern, replacement in _VOLATILE:
        message = pattern.sub(replacement, message)
    return message[:120]


def signature_id(kind: str, test: str, error: str) -> str:
    """Return the stable id of a failure signature.

    Args:
        kind: The parser that produced it.
        test: The test identifier.
        error: The normalized error.

    Returns:
        A 12 character hex id.
    """
    return hashlib.sha1(f"{kind}|{test}|{error}".encode()).hexdigest()[:12]


def _signature(kind: str, test: str, error: str, line: str) -> dict[str, str]:
    error = normalize_error(error)
    return {
        "id": signature_id(kind, test, error),
        "kind": kind,
        "test": test,
        "error": error,
        "line": line[:200],
    }


def _with_job(sig: dict[str, str], job: Job) -> dict[str, str]:
    """Key a non-test fallback signature by its job.

    Without this, "exit code 1" from every job would share one signature.

    Args:
        sig: A signature from :func:`extract_signatures`.
        job: The job whose log produced it.

    Returns:
        The signature, re-keyed when it names no test.
    """
    if sig["kind"] != "step" or sig["test"]:
        return sig
    test = f"{job.workflow}/{job.name}"
    return {**sig, "test": test, "id": signature_id("step", test, sig["error"])}


def extract_signatures(log: str) -> list[dict[str, str]]:
    """Extract failure signatures from a job log.

    Recognizes the pytest short summary, go test, vitest and jest output.
    When no test failure is recognized, the first Actions error annotation
    (or the last non-empty line) stands in, so a failed job is never blind.

    Args:
        log: The job log text.

    Returns:
        Unique signatures in order of appearance, at most
        MAX_SIGNATURES_PER_JOB.
    """
    found: dict[str, dict[str, str]] = {}
    go_tests: list[tuple[str, str]] = []
    go_panic = ""
    fallback: dict[str, str] | None = None
    last_line = ""

    def add(sig: dict[str, str]) -> None:
        if sig["id"] not in found and len(found) < MAX_SIGNATURES_PER_JOB:
            found[sig["id"]] = sig

    for raw in log.splitlines():
        line = clean_line(raw)
        if not line.strip():
            continue
        last_line = line
        if match := _PYTEST.match(line):
            add(_signature("pytest", match.group(2), match.group(3) or "", line))
        elif match := _GO_FAIL.match(line):
            go_tests.append((match.group(1), line))
        elif match := _GO_PKG.match(line):
            pkg = match.group(1)
            for name, test_line in go_tests:
                add(_signature("go", f"{pkg}.{name}", go_panic, test_line))
            go_tests = []
        elif match := _GO_PANIC.match(line):
            go_panic = match.group(1)
        elif match := _VITEST.match(line):
            test = f"{match.group(1)} > {match.group(2).strip()}"
            add(_signature("vitest", test, "", line))
        elif match := _JEST.match(line):
            add(_signature("jest", match.group(1).strip(), "", line))
        elif fallback is None and (match := _GH_ERROR.match(line)):
            fallback = _signature("step", "", match.group(1), line)
    for name, test_line in go_tests:
        add(_signature("go", name, go_panic, test_line))
    if not found:
        if fallback is None and last_line:
            fallback = _signature("step", "", last_line, last_line)
        if fallback is not None:
            add(fallback)
    return list(found.values())


def junit_signatures(xml_text: str | bytes) -> list[dict[str, str]]:
    """Extract failure signatures from a JUnit XML report.

    Args:
        xml_text: The report.

    Returns:
        One signature per failed or errored test case.
    """
    root = ET.fromstring(xml_text)  # noqa: S314 - CI artifact, no entities
    found: dict[str, dict[str, str]] = {}
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue
        classname = case.get("classname", "")
        name = case.get("name", "")
        test = f"{classname}::{name}" if classname else name
        error = problem.get("type") or problem.get("message") or ""
        message = (problem.get("message") or "")[:200]
        sig = _signature("junit", test, error, f"{test} - {message}")
        found.setdefault(sig["id"], sig)
    return list(found.values())


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


def _conclusion(value: str | None) -> str:
    value = (value or "").lower()
    if value in ("success", "passed"):
        return "success"
    if value in ("failure", "failed", "timed_out", "startup_failure"):
        return "failure"
    if value in ("cancelled", "canceled"):
        return "cancelled"
    if value in ("skipped", "neutral", "manual"):
        return "skipped"
    return value or "unknown"


class GitHubSource:
    """GitHub Actions adapter.

    Args:
        client: The HTTP client carrying the token.
        repo: ``owner/name``.
        api: API base URL.
    """

    provider = "github"

    def __init__(
        self, client: Client, repo: str, api: str = "https://api.github.com"
    ) -> None:
        self.client = client
        self.repo = repo
        self.api = api.rstrip("/")

    def runs(self, since: datetime, until: datetime) -> Iterator[dict[str, Any]]:
        """Yield completed workflow runs created in the window.

        Args:
            since: Window start.
            until: Window end.

        Yields:
            Raw run objects.
        """
        created = urllib.parse.quote(f"{iso(since)}..{iso(until)}")
        page = 1
        while True:
            url = (
                f"{self.api}/repos/{self.repo}/actions/runs?status=completed"
                f"&created={created}&per_page=100&page={page}"
            )
            data = self.client.get_json(url) or {}
            items = data.get("workflow_runs") or []
            yield from items
            if len(items) < 100:
                return
            page += 1

    def jobs(self, run: dict[str, Any]) -> list[Job]:
        """Read every attempt's jobs for a run.

        Args:
            run: A raw run object.

        Returns:
            Normalized jobs, earlier attempts included.
        """
        url = (
            f"{self.api}/repos/{self.repo}/actions/runs/{run['id']}/jobs"
            "?filter=all&per_page=100"
        )
        data = self.client.get_json(url, cache=True) or {}
        jobs = []
        for raw in data.get("jobs") or []:
            jobs.append(
                Job(
                    provider=self.provider,
                    repo=self.repo,
                    workflow=run.get("name") or str(run.get("workflow_id")),
                    name=raw.get("name", ""),
                    run_id=str(run["id"]),
                    run_url=run.get("html_url", ""),
                    attempt=int(raw.get("run_attempt") or 1),
                    sha=run.get("head_sha", ""),
                    branch=run.get("head_branch") or "",
                    event=run.get("event", ""),
                    conclusion=_conclusion(raw.get("conclusion")),
                    created_at=parse_time(raw.get("created_at")),
                    started_at=parse_time(raw.get("started_at")),
                    completed_at=parse_time(raw.get("completed_at")),
                    os=runner_os(raw.get("labels") or []),
                    job_id=str(raw.get("id", "")),
                )
            )
        return jobs

    def log(self, job: Job) -> str | None:
        """Fetch a failed job's log.

        Args:
            job: The job.

        Returns:
            The log text, or None when it has expired.
        """
        url = f"{self.api}/repos/{self.repo}/actions/jobs/{job.job_id}/logs"
        body = self.client.get(url, cache=True)
        return None if body is None else body.decode("utf-8", "replace")

    def flow_pulls(self, prefix: str) -> list[dict[str, Any]]:
        """List the newest pull requests opened from the flow's branches.

        One request: the 100 most recently created pull requests, filtered
        by head branch prefix.

        Args:
            prefix: Head branch prefix of the flow's pull requests.

        Returns:
            Normalized pull requests (see :func:`_pull`).
        """
        url = (
            f"{self.api}/repos/{self.repo}/pulls?state=all&sort=created"
            "&direction=desc&per_page=100"
        )
        return [
            _pull(
                item.get("html_url"),
                item.get("created_at"),
                item.get("merged_at"),
                item.get("body"),
            )
            for item in self.client.get_json(url) or []
            if ((item.get("head") or {}).get("ref") or "").startswith(prefix)
        ]


class GitLabSource:
    """GitLab CI adapter (read-only).

    Args:
        client: The HTTP client carrying the token.
        project: ``group/name`` path or numeric id.
        api: API base URL ending in ``/api/v4``.
    """

    provider = "gitlab"

    def __init__(
        self, client: Client, project: str, api: str = "https://gitlab.com/api/v4"
    ) -> None:
        self.client = client
        self.repo = project
        self.api = api.rstrip("/")
        self.encoded = urllib.parse.quote(project, safe="")

    def runs(self, since: datetime, until: datetime) -> Iterator[dict[str, Any]]:
        """Yield finished pipelines updated in the window.

        Args:
            since: Window start.
            until: Window end.

        Yields:
            Raw pipeline objects.
        """
        page = 1
        after = urllib.parse.quote(iso(since))
        before = urllib.parse.quote(iso(until))
        while True:
            url = (
                f"{self.api}/projects/{self.encoded}/pipelines?updated_after={after}"
                f"&updated_before={before}&per_page=100&page={page}"
            )
            items = self.client.get_json(url) or []
            for item in items:
                if item.get("status") in ("success", "failed", "canceled"):
                    yield item
            if len(items) < 100:
                return
            page += 1

    def jobs(self, run: dict[str, Any]) -> list[Job]:
        """Read a pipeline's jobs, retried attempts included.

        Args:
            run: A raw pipeline object.

        Returns:
            Normalized jobs. Retries of one job get increasing attempts.
        """
        url = (
            f"{self.api}/projects/{self.encoded}/pipelines/{run['id']}/jobs"
            "?include_retried=true&per_page=100"
        )
        raw_jobs = self.client.get_json(url, cache=True) or []
        raw_jobs = sorted(raw_jobs, key=lambda item: item.get("id", 0))
        attempts: dict[str, int] = {}
        jobs = []
        for raw in raw_jobs:
            name = raw.get("name", "")
            attempts[name] = attempts.get(name, 0) + 1
            jobs.append(
                Job(
                    provider=self.provider,
                    repo=self.repo,
                    workflow=raw.get("stage") or "pipeline",
                    name=name,
                    run_id=str(run["id"]),
                    run_url=run.get("web_url", ""),
                    attempt=attempts[name],
                    sha=run.get("sha", ""),
                    branch=run.get("ref", ""),
                    event=run.get("source", ""),
                    conclusion=_conclusion(raw.get("status")),
                    created_at=parse_time(raw.get("created_at")),
                    started_at=parse_time(raw.get("started_at")),
                    completed_at=parse_time(raw.get("finished_at")),
                    os=runner_os(raw.get("tag_list") or []),
                    job_id=str(raw.get("id", "")),
                )
            )
        return jobs

    def log(self, job: Job) -> str | None:
        """Fetch a failed job's trace.

        Args:
            job: The job.

        Returns:
            The trace text, or None when it has expired.
        """
        url = f"{self.api}/projects/{self.encoded}/jobs/{job.job_id}/trace"
        body = self.client.get(url, cache=True)
        return None if body is None else body.decode("utf-8", "replace")

    def flow_pulls(self, prefix: str) -> list[dict[str, Any]]:
        """List the newest merge requests opened from the flow's branches.

        Args:
            prefix: Source branch prefix of the flow's merge requests.

        Returns:
            Normalized merge requests (see :func:`_pull`).
        """
        url = (
            f"{self.api}/projects/{self.encoded}/merge_requests?state=all"
            "&order_by=created_at&sort=desc&per_page=100"
        )
        return [
            _pull(
                item.get("web_url"),
                item.get("created_at"),
                item.get("merged_at"),
                item.get("description"),
            )
            for item in self.client.get_json(url) or []
            if (item.get("source_branch") or "").startswith(prefix)
        ]


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

_SHARD = re.compile(
    r"\s*(?:\(\s*\d+\s*/\s*\d+\s*\)|\(\s*\d+\s*\)|\bshard[\s_-]*\d+(?:\s*/\s*\d+)?"
    r"|\[\s*\d+\s*/\s*\d+\s*\]|\s\d+/\d+$)",
    re.IGNORECASE,
)


def shard_group(name: str) -> str | None:
    """Return a job's shard group name, or None when it is not a shard.

    Args:
        name: The job name, for example ``backend (2/4)``.

    Returns:
        The name with the shard index removed.
    """
    stripped = _SHARD.sub("", name).strip(" -_,")
    return stripped if stripped != name.strip() else None


def percentile(values: list[float], q: float) -> float | None:
    """Return the q-th percentile (nearest rank).

    Args:
        values: The samples.
        q: Percentile in [0, 100].

    Returns:
        The percentile rounded to one decimal, or None for no samples.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(q / 100 * (len(ordered) - 1))))
    return round(ordered[rank], 1)


def _stats(jobs: list[Job]) -> dict[str, Any]:
    finished = [j for j in jobs if j.conclusion in ("success", "failure")]
    queue = [q for j in jobs if (q := j.queue_seconds) is not None]
    duration = [d for j in jobs if (d := j.duration_seconds) is not None]
    successes = sum(1 for j in finished if j.conclusion == "success")
    return {
        "jobs": len(jobs),
        "failures": sum(1 for j in jobs if j.conclusion == "failure"),
        "success_rate": round(successes / len(finished), 3) if finished else None,
        "reruns": sum(1 for j in jobs if j.attempt > 1),
        "queue_p50_s": percentile(queue, 50),
        "queue_p95_s": percentile(queue, 95),
        "duration_p50_s": percentile(duration, 50),
        "duration_p95_s": percentile(duration, 95),
        "runner_minutes": round(sum(duration) / 60, 1),
    }


def flaky_keys(jobs: list[Job]) -> set[tuple[str, str]]:
    """Find job keys that failed and passed on the same commit.

    Args:
        jobs: All jobs in the window.

    Returns:
        ``(job key, sha)`` pairs with both a failure and a success.
    """
    outcomes: dict[tuple[str, str], set[str]] = {}
    for job in jobs:
        outcomes.setdefault((job.key, job.sha), set()).add(job.conclusion)
    return {k for k, v in outcomes.items() if {"success", "failure"} <= v}


def aggregate(jobs: list[Job], runs_read: int) -> dict[str, Any]:
    """Aggregate jobs into totals, per-workflow, per-job and shard figures.

    Args:
        jobs: Normalized jobs.
        runs_read: Number of runs or pipelines read.

    Returns:
        The aggregate section of the output.
    """
    by_workflow: dict[str, list[Job]] = {}
    by_job: dict[str, list[Job]] = {}
    for job in jobs:
        by_workflow.setdefault(f"{job.repo}:{job.workflow}", []).append(job)
        by_job.setdefault(job.key, []).append(job)
    minutes_by_os: dict[str, float] = {}
    for job in jobs:
        minutes_by_os[job.os] = minutes_by_os.get(job.os, 0.0) + (
            (job.duration_seconds or 0.0) / 60
        )
    totals = _stats(jobs)
    totals["runs"] = runs_read
    totals["runner_minutes_by_os"] = {
        k: round(v, 1) for k, v in sorted(minutes_by_os.items())
    }
    workflows = sorted(
        ({"workflow": k, **_stats(v)} for k, v in by_workflow.items()),
        key=lambda item: -item["runner_minutes"],
    )
    job_rows = sorted(
        ({"job": k, **_stats(v)} for k, v in by_job.items()),
        key=lambda item: (-item["failures"], -item["runner_minutes"]),
    )
    return {
        "totals": totals,
        "workflows": workflows[:TOP_N],
        "jobs": job_rows[:TOP_N],
        "slowest_jobs": sorted(
            job_rows, key=lambda item: -(item["duration_p95_s"] or 0)
        )[:5],
        "shards": shard_imbalance(jobs),
    }


def shard_imbalance(jobs: list[Job]) -> list[dict[str, Any]]:
    """Measure shard imbalance as slowest shard over mean shard per run.

    Args:
        jobs: Normalized jobs.

    Returns:
        Per shard group: shard count, mean and worst ratio, and the shard
        that was slowest most often.
    """
    groups: dict[tuple[str, str, str], list[Job]] = {}
    for job in jobs:
        group = shard_group(job.name)
        if group and job.conclusion == "success" and job.duration_seconds:
            prefix = f"{job.repo}:{job.workflow}/{group}"
            groups.setdefault((prefix, job.run_id, str(job.attempt)), []).append(job)
    ratios: dict[str, list[float]] = {}
    slowest: dict[str, dict[str, int]] = {}
    shard_counts: dict[str, int] = {}
    for (prefix, _, _), members in groups.items():
        if len(members) < 2:
            continue
        durations = [m.duration_seconds or 0.0 for m in members]
        mean = statistics.mean(durations)
        if mean <= 0:
            continue
        ratios.setdefault(prefix, []).append(max(durations) / mean)
        worst = max(members, key=lambda m: m.duration_seconds or 0.0).name
        slowest.setdefault(prefix, {})
        slowest[prefix][worst] = slowest[prefix].get(worst, 0) + 1
        shard_counts[prefix] = max(shard_counts.get(prefix, 0), len(members))
    rows = [
        {
            "group": prefix,
            "shards": shard_counts[prefix],
            "runs": len(values),
            "imbalance_mean": round(statistics.mean(values), 2),
            "imbalance_max": round(max(values), 2),
            "slowest_shard": max(slowest[prefix].items(), key=lambda kv: kv[1])[0],
        }
        for prefix, values in ratios.items()
    ]
    return sorted(rows, key=lambda row: -row["imbalance_mean"])


def failure_summary(jobs: list[Job]) -> list[dict[str, Any]]:
    """Group this window's failures by signature.

    Args:
        jobs: Normalized jobs with signatures attached to failed ones.

    Returns:
        Signatures with counts, jobs, commits, run links and whether the
        same job also passed on the same commit.
    """
    flaky = flaky_keys(jobs)
    rows: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if job.conclusion != "failure":
            continue
        for sig in job.signatures:
            row = rows.setdefault(
                sig["id"],
                {
                    **sig,
                    "count": 0,
                    "jobs": [],
                    "shas": [],
                    "runs": [],
                    "passed_on_same_sha": False,
                },
            )
            row["count"] += 1
            for name, value in (
                ("jobs", job.key),
                ("shas", job.sha[:12]),
                ("runs", job.run_url),
            ):
                if value and value not in row[name] and len(row[name]) < MAX_RECENT:
                    row[name].append(value)
            if (job.key, job.sha) in flaky:
                row["passed_on_same_sha"] = True
    return sorted(rows.values(), key=lambda row: -row["count"])


# --------------------------------------------------------------------------
# Ledger, trend and value
# --------------------------------------------------------------------------


def _merge_recent(old: list[str], new: list[str]) -> list[str]:
    merged = [v for v in old if v not in new] + new
    return merged[-MAX_RECENT:]


def update_ledger(
    ledger: dict[str, dict[str, Any]],
    failures: list[dict[str, Any]],
    jobs: list[Job],
    now: datetime,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Fold this window's failures into the cross-run flake ledger.

    Agent-owned fields (``status`` other than the automatic ones, ``issue``,
    ``pr``, ``note``) are preserved. A resolved entry that fails again is
    marked ``regressed``.

    Args:
        ledger: Entries by signature id, from the previous result.
        failures: This window's failure summary.
        jobs: This window's jobs, used to count passes since last seen.
        now: The collection time.

    Returns:
        The updated, bounded ledger and the ids that are new or regressed.
    """
    ledger = {k: dict(v) for k, v in ledger.items()}
    changed: list[str] = []
    stamp = iso(now)
    for row in failures:
        entry = ledger.get(row["id"])
        if entry is None:
            entry = {
                "id": row["id"],
                "kind": row["kind"],
                "test": row["test"],
                "error": row["error"],
                "line": row["line"],
                "first_seen": stamp,
                "failures": 0,
                "windows_seen": 0,
                "flaky_evidence": 0,
                "jobs": [],
                "shas": [],
                "runs": [],
                "status": "new",
                "issue": None,
                "pr": None,
                "note": None,
            }
            changed.append(row["id"])
        elif entry.get("status") == "resolved":
            entry["status"] = "regressed"
            changed.append(row["id"])
        entry["last_seen"] = stamp
        entry["failures"] = int(entry.get("failures", 0)) + row["count"]
        entry["windows_seen"] = int(entry.get("windows_seen", 0)) + 1
        if row["passed_on_same_sha"]:
            entry["flaky_evidence"] = int(entry.get("flaky_evidence", 0)) + 1
        for name in ("jobs", "shas", "runs"):
            entry[name] = _merge_recent(entry.get(name) or [], row[name])
        entry["passes_since_seen"] = 0
        ledger[row["id"]] = entry
    seen = {row["id"] for row in failures}
    passes: dict[str, int] = {}
    for job in jobs:
        if job.conclusion == "success":
            passes[job.key] = passes.get(job.key, 0) + 1
    for sig_id, entry in ledger.items():
        if sig_id in seen:
            continue
        if entry.get("status") == "new":
            entry["status"] = "open"
        entry["passes_since_seen"] = int(entry.get("passes_since_seen", 0)) + sum(
            passes.get(key, 0) for key in entry.get("jobs") or []
        )
    return bound_ledger(ledger, now), changed


def bound_ledger(
    ledger: dict[str, dict[str, Any]], now: datetime
) -> dict[str, dict[str, Any]]:
    """Drop old resolved entries, then the stalest, to stay bounded.

    Args:
        ledger: Entries by signature id.
        now: The collection time.

    Returns:
        At most MAX_LEDGER_ENTRIES entries.
    """
    cutoff = now - timedelta(days=RESOLVED_RETENTION_DAYS)
    kept = {
        k: v
        for k, v in ledger.items()
        if not (
            v.get("status") == "resolved"
            and (parse_time(v.get("last_seen")) or now) < cutoff
        )
    }
    if len(kept) <= MAX_LEDGER_ENTRIES:
        return kept
    ordered = sorted(
        kept.items(),
        key=lambda kv: (
            kv[1].get("status") not in ("resolved",),
            kv[1].get("last_seen") or "",
        ),
        reverse=True,
    )
    return dict(ordered[:MAX_LEDGER_ENTRIES])


def fit_state(state: dict[str, Any]) -> dict[str, Any]:
    """Shrink the persisted state until it fits STATE_BUDGET_BYTES.

    Notes are truncated first, then the oldest trend points and the
    stalest ledger entries are dropped.

    Args:
        state: The state, modified in place.

    Returns:
        The same state.
    """
    for entry in state.get("ledger", {}).values():
        if isinstance(entry.get("note"), str):
            entry["note"] = entry["note"][:MAX_NOTE_CHARS]

    def size() -> int:
        return len(json.dumps(state, separators=(",", ":")))

    while size() > STATE_BUDGET_BYTES:
        ledger = state.get("ledger") or {}
        if len(state.get("trend") or []) > 12:
            state["trend"] = state["trend"][len(state["trend"]) // 4 :]
        elif ledger:
            stalest = min(ledger, key=lambda k: ledger[k].get("last_seen") or "")
            del ledger[stalest]
        else:
            break
    return state


_MINUTES_SAVED = re.compile(
    r"runner minutes saved per week[^0-9\n]*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE
)


def _pull(
    url: str | None, created: str | None, merged: str | None, body: str | None
) -> dict[str, Any]:
    """Normalize a pull or merge request for the value summary.

    The flow's prompt has every pull request body state
    ``Runner minutes saved per week (estimate): N``; that claim is read
    back here so the summary needs no per-run bookkeeping.

    Args:
        url: The web URL.
        created: Creation time.
        merged: Merge time, if merged.
        body: The description.

    Returns:
        ``{"url", "created_at", "merged_at", "minutes_saved_per_week"}``.
    """
    match = _MINUTES_SAVED.search(body or "")
    return {
        "url": url,
        "created_at": created,
        "merged_at": merged,
        "minutes_saved_per_week": float(match.group(1)) if match else 0.0,
    }


def value_summary(
    history: list[dict[str, Any]],
    ledger: dict[str, dict[str, Any]],
    pulls: list[dict[str, Any]],
    now: datetime,
    days: int = 7,
) -> dict[str, Any]:
    """Summarize what the flow delivered over the last ``days`` days.

    Args:
        history: Per-run entries recorded by ``finalize``.
        ledger: The current flake ledger.
        pulls: The flow's pull requests (see :func:`_pull`).
        now: The collection time.
        days: The summary period.

    Returns:
        Runs, PRs opened and merged, failures fixed, the estimated runner
        minutes saved per week by merged changes, tokens and cost, and the
        cost per merged PR where the runs recorded their usage.
    """
    start = now - timedelta(days=days)
    recent = [h for h in history if (parse_time(h.get("at")) or start) >= start]

    def within(stamp: str | None) -> bool:
        parsed = parse_time(stamp)
        return parsed is not None and parsed >= start

    prs = [p for p in pulls if within(p.get("created_at"))]
    merged = [p for p in pulls if within(p.get("merged_at"))]
    costs = [h["cost_usd"] for h in recent if h.get("cost_usd") is not None]
    tokens = [h["tokens"] for h in recent if h.get("tokens") is not None]
    fixed = [
        e["id"]
        for e in ledger.values()
        if e.get("status") == "resolved"
        and (parse_time(e.get("resolved_at")) or start) >= start
    ]
    total_cost = round(sum(costs), 2) if costs else None
    return {
        "period_start": iso(start),
        "period_end": iso(now),
        "runs": len(recent),
        "early_stops": sum(1 for h in recent if h.get("early_stop")),
        "prs_opened": len(prs),
        "prs_merged": len(merged),
        "merged_pull_requests": [p["url"] for p in merged][:10],
        "failures_fixed": len(fixed),
        "runner_minutes_saved_per_week_est": round(
            sum(p["minutes_saved_per_week"] for p in merged),
            1,
        ),
        "tokens": sum(tokens) if tokens else None,
        "cost_usd": total_cost,
        "usage_recorded_runs": len(costs),
        "cost_per_merged_pr_usd": (
            round(total_cost / len(merged), 2) if merged and total_cost else None
        ),
        "tokens_per_merged_pr": (
            round(sum(tokens) / len(merged)) if merged and tokens else None
        ),
    }


# --------------------------------------------------------------------------
# Collect
# --------------------------------------------------------------------------


def load_previous_state(path: Path | None) -> dict[str, Any]:
    """Read the persisted state from the previous result.

    Args:
        path: ``previous/result.json``, which may be absent.

    Returns:
        The previous ``ci_health_state`` or an empty mapping.
    """
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    state = data.get(STATE_KEY) if isinstance(data, dict) else None
    return state if isinstance(state, dict) else {}


def collect(
    sources: list[Any],
    previous: dict[str, Any],
    now: datetime,
    window_hours: float,
    max_logs: int = 40,
    junit_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    """Run one collection over every source.

    The window starts at the previous run's window end when there is one
    (never further back than ``window_hours``), so a frequent schedule reads
    only what is new.

    Args:
        sources: Provider adapters.
        previous: The previous ``ci_health_state``.
        now: The window end.
        window_hours: The longest look-back.
        max_logs: Ceiling on failed-job log fetches.
        junit_paths: Local JUnit XML reports to add signatures from.

    Returns:
        The output document, including the new state under STATE_KEY.
    """
    earliest = now - timedelta(hours=window_hours)
    prev_end = parse_time(previous.get("window_end"))
    since = max(prev_end - OVERLAP, earliest) if prev_end else earliest
    seen_before = set(previous.get("seen_runs") or [])
    seen_now: list[str] = []
    jobs: list[Job] = []
    runs_read = 0
    logs_read = 0
    logs_skipped = 0
    runs_skipped = 0
    partial_reasons: list[str] = []
    source_rows = []
    for source in sources:
        row: dict[str, Any] = {
            "provider": source.provider,
            "repo": source.repo,
            "runs": 0,
            "partial": False,
        }
        try:
            for run in source.runs(since, now):
                # Run and pipeline ids are unique per provider instance.
                run_key = f"{source.provider[:2]}{run['id']}"
                if run_key in seen_before:
                    runs_skipped += 1
                    continue
                # GitLab filters on updated_at, GitHub on created_at; the
                # later of the two decides whether the overlap can repeat it.
                stamp = parse_time(run.get("updated_at") or run.get("created_at"))
                if stamp is None or stamp >= now - 2 * OVERLAP:
                    seen_now.append(run_key)
                run_jobs = source.jobs(run)
                runs_read += 1
                row["runs"] += 1
                for job in run_jobs:
                    if job.conclusion != "failure":
                        continue
                    if logs_read >= max_logs:
                        logs_skipped += 1
                        continue
                    text = source.log(job)
                    logs_read += 1
                    if text is not None:
                        job.signatures = [
                            _with_job(sig, job) for sig in extract_signatures(text)
                        ]
                jobs.extend(run_jobs)
        except StopFetchingError as stop:
            row["partial"] = True
            row["reason"] = str(stop)
            partial_reasons.append(f"{source.provider}:{source.repo}: {stop}")
        source_rows.append(row)
    if logs_skipped:
        partial_reasons.append(
            f"log fetches capped at {max_logs}; {logs_skipped} failed jobs "
            "have no signature"
        )
    junit: list[dict[str, str]] = []
    for path in junit_paths:
        try:
            junit.extend(junit_signatures(path.read_bytes()))
        except (OSError, ET.ParseError):
            continue
    if junit:
        jobs.append(
            Job(
                provider="local",
                repo="local",
                workflow="junit",
                name="reports",
                run_id="local",
                run_url="",
                attempt=1,
                sha="",
                branch="",
                event="local",
                conclusion="failure",
                created_at=None,
                started_at=None,
                completed_at=None,
                signatures=junit,
            )
        )
    aggregates = aggregate([j for j in jobs if j.provider != "local"], runs_read)
    failures = failure_summary(jobs)
    ledger, new_ids = update_ledger(previous.get("ledger") or {}, failures, jobs, now)
    trend_point = {
        "window_end": iso(now),
        "runs": runs_read,
        "success_rate": aggregates["totals"]["success_rate"],
        "queue_p95_s": aggregates["totals"]["queue_p95_s"],
        "runner_minutes": aggregates["totals"]["runner_minutes"],
        "failures": aggregates["totals"]["failures"],
        "slowest_job": (aggregates["slowest_jobs"] or [{}])[0].get("job"),
    }
    trend = (list(previous.get("trend") or []) + [trend_point])[-MAX_TREND_ENTRIES:]
    reasons = []
    if not previous:
        reasons.append("no previous state")
    if aggregates["totals"]["failures"]:
        reasons.append(f"{aggregates['totals']['failures']} failed jobs")
    if new_ids:
        reasons.append(f"{len(new_ids)} new or regressed signatures")
    week_ago = now - timedelta(days=7)
    last_summary = parse_time(previous.get("value_summary_at"))
    summary_due = last_summary is None or last_summary <= week_ago
    if summary_due:
        reasons.append("weekly value summary due")
    previous_open = {
        k
        for k, v in (previous.get("ledger") or {}).items()
        if v.get("status") in ("new", "open", "regressed")
    }
    # A ledger entry that went quiet is worth a look (it may be resolved).
    quiet = [
        k
        for k in previous_open
        if k in ledger
        and int(ledger[k].get("passes_since_seen", 0)) >= 10
        and k not in {f["id"] for f in failures}
    ]
    if quiet:
        reasons.append(f"{len(quiet)} open signatures quiet for 10+ passes")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso(now),
        "window": {
            "start": iso(since),
            "end": iso(now),
            "runs_read": runs_read,
            "jobs_read": len(jobs),
            "logs_read": logs_read,
            "runs_skipped_already_seen": runs_skipped,
            "partial": bool(partial_reasons),
            "partial_reasons": partial_reasons,
        },
        "sources": source_rows,
        **aggregates,
        "failures": failures[:TOP_N],
        "new_signatures": new_ids,
        "quiet_signatures": sorted(quiet),
        "trend": trend[-12:],
        "summary_due": summary_due,
        "early_stop": not reasons,
        "changed_reasons": reasons,
        STATE_KEY: fit_state(
            {
                "schema_version": SCHEMA_VERSION,
                # Advance even after a partial read: re-reading would count the
                # runs already folded into the ledger twice. The gap is recorded.
                "window_end": iso(now),
                # Only runs the next overlap can return again need remembering.
                "seen_runs": seen_now[-MAX_SEEN_RUNS:],
                "gaps": (
                    list(previous.get("gaps") or [])
                    + (
                        [{"start": iso(since), "end": iso(now)}]
                        if partial_reasons
                        else []
                    )
                )[-10:],
                "ledger": ledger,
                "trend": trend,
                "history": list(previous.get("history") or [])[-MAX_HISTORY_ENTRIES:],
                "value_summary_at": previous.get("value_summary_at"),
            }
        ),
    }


def add_value_summary(
    output: dict[str, Any],
    sources: list[Any],
    now: datetime,
    branch_prefix: str | None,
) -> None:
    """Attach the weekly value summary when it is due.

    The flow's pull requests are found by head branch prefix, one request
    per source; without a prefix, PR figures are reported as unknown.

    Args:
        output: The collector output, updated in place.
        sources: Provider adapters.
        now: The collection time.
        branch_prefix: Head branch prefix of the flow's pull requests.
    """
    if not output["summary_due"]:
        return
    state = output[STATE_KEY]
    pulls: list[dict[str, Any]] = []
    complete = bool(branch_prefix)
    for source in sources if branch_prefix else []:
        try:
            pulls.extend(source.flow_pulls(branch_prefix))
        except StopFetchingError:
            complete = False
    summary = value_summary(state["history"], state["ledger"], pulls, now)
    if not complete:
        summary["pull_requests_complete"] = False
    output["value_summary"] = summary
    state["value_summary_at"] = iso(now)


def finalize(
    collector_path: Path, result_path: Path, entry: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Merge the collector state and a history entry into result.json.

    The agent may have edited ledger entries (status, issue, pr, note,
    resolved_at) in its own result under STATE_KEY.ledger; those edits win
    over the collector's copy, field by field.

    Args:
        collector_path: The collector's output file.
        result_path: The agent's result.json, created if missing.
        entry: This run's history entry (change kind, tokens,
            cost_usd, early_stop).
        now: The finalize time.

    Returns:
        The merged result document, also written to result_path.
    """
    collected = json.loads(collector_path.read_text())
    state = dict(collected[STATE_KEY])
    result: dict[str, Any] = {}
    if result_path.exists():
        result = json.loads(result_path.read_text())
    edits = ((result.get(STATE_KEY) or {}).get("ledger")) or {}
    ledger = {k: dict(v) for k, v in state["ledger"].items()}
    for sig_id, edit in edits.items():
        if sig_id not in ledger or not isinstance(edit, dict):
            continue
        for name in ("status", "issue", "pr", "note", "resolved_at"):
            if name in edit:
                ledger[sig_id][name] = edit[name]
        if ledger[sig_id].get("status") == "resolved" and not ledger[sig_id].get(
            "resolved_at"
        ):
            ledger[sig_id]["resolved_at"] = iso(now)
    state["ledger"] = ledger
    history_entry = {"at": iso(now), **entry}
    state["history"] = (list(state.get("history") or []) + [history_entry])[
        -MAX_HISTORY_ENTRIES:
    ]
    result[STATE_KEY] = fit_state(state)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _token(env_name: str | None) -> str:
    return os.environ.get(env_name or "", "") if env_name else ""


def build_sources(args: argparse.Namespace, transport: Transport) -> list[Any]:
    """Create provider adapters from the command line.

    Args:
        args: Parsed arguments.
        transport: The GET transport.

    Returns:
        GitHub sources first, then GitLab sources.
    """
    cache = Path(args.cache_dir) if args.cache_dir else None
    sources: list[Any] = []
    if args.github_repo:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-health-collect",
        }
        token = _token(args.github_token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        client = Client(transport, headers, args.min_remaining, cache, args.max_calls)
        sources += [
            GitHubSource(client, repo, args.github_api) for repo in args.github_repo
        ]
    if args.gitlab_project:
        token = _token(args.gitlab_token_env)
        if not token:
            print("GitLab token not set; skipping GitLab sources", file=sys.stderr)
        else:
            headers = {"PRIVATE-TOKEN": token, "User-Agent": "ci-health-collect"}
            client = Client(
                transport, headers, args.min_remaining, cache, args.max_calls
            )
            sources += [
                GitLabSource(client, project, args.gitlab_api)
                for project in args.gitlab_project
            ]
    return sources


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments, or None for sys.argv.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("collect", help="collect CI health into one JSON file")
    run.add_argument("--github-repo", action="append", default=[])
    run.add_argument("--github-token-env", default="GH_TOKEN")
    run.add_argument("--github-api", default="https://api.github.com")
    run.add_argument("--gitlab-project", action="append", default=[])
    run.add_argument("--gitlab-token-env", default="GITLAB_TOKEN")
    run.add_argument("--gitlab-api", default="https://gitlab.com/api/v4")
    run.add_argument("--window-hours", type=float, default=24.0)
    run.add_argument("--previous", default="previous/result.json")
    run.add_argument("--cache-dir", default=".ci-health-cache")
    run.add_argument("--max-logs", type=int, default=40)
    run.add_argument("--max-calls", type=int, default=3000)
    run.add_argument("--min-remaining", type=int, default=200)
    run.add_argument("--junit", action="append", default=[])
    run.add_argument(
        "--flow-branch-prefix",
        help="head branch prefix of the flow's own pull requests, for the "
        "weekly value summary (for example preloop/daily-ci-and-qa-review-)",
    )
    run.add_argument("--out", default="evidence/ci-health.json")
    run.add_argument("--now", help="window end (ISO-8601), for tests")
    fin = sub.add_parser("finalize", help="merge collector state into result.json")
    fin.add_argument("--collector", default="evidence/ci-health.json")
    fin.add_argument("--result", default="result.json")
    fin.add_argument("--entry", default="{}", help="this run's history entry, JSON")
    fin.add_argument("--now", help="finalize time (ISO-8601), for tests")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, transport: Transport = urllib_transport) -> int:
    """Entry point.

    Args:
        argv: Arguments, or None for sys.argv.
        transport: The GET transport (replaced in tests).

    Returns:
        The process exit code: 0 on success, 2 when no source could be read.
    """
    args = parse_args(argv)
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise SystemExit("--now must be an ISO-8601 timestamp")
    if args.command == "finalize":
        entry = json.loads(args.entry)
        if not isinstance(entry, dict):
            raise SystemExit("--entry must be a JSON object")
        finalize(Path(args.collector), Path(args.result), entry, now)
        return 0
    sources = build_sources(args, transport)
    previous = load_previous_state(Path(args.previous) if args.previous else None)
    started = time.monotonic()
    output = collect(
        sources,
        previous,
        now,
        args.window_hours,
        args.max_logs,
        [Path(p) for p in args.junit],
    )
    add_value_summary(output, sources, now, args.flow_branch_prefix)
    clients = {id(s.client): s.client for s in sources}.values()
    output["window"]["api_calls"] = sum(c.calls for c in clients)
    output["window"]["cache_hits"] = sum(c.cache_hits for c in clients)
    output["window"]["seconds"] = round(time.monotonic() - started, 1)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, indent=1, sort_keys=True) + "\n")
    readable = any(not row["partial"] or row["runs"] for row in output["sources"])
    print(
        f"ci-health: {output['window']['runs_read']} runs, "
        f"{output['totals']['failures']} failed jobs, "
        f"early_stop={output['early_stop']}, partial={output['window']['partial']}"
    )
    return 0 if readable or not sources else 2


if __name__ == "__main__":
    sys.exit(main())
