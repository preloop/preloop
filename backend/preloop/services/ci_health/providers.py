"""GitHub Actions and GitLab CI adapters behind one interface.

An adapter turns a provider's runs/pipelines and jobs into
:class:`~.models.CiRun` / :class:`~.models.CiJob` and reads failed-job logs.
Completed runs never change, so their jobs and log findings are cached in
process and a later call (the flow's next tick) only fetches new runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol, Sequence
from urllib.parse import quote

from . import signatures as sig
from .models import (
    CANCELLED,
    FAILURE,
    OTHER,
    RUNNING,
    SKIPPED,
    SUCCESS,
    CiJob,
    CiRun,
    RepositoryScan,
)
from .transport import BudgetExhaustedError, HttpTransport, LruCache, ProviderError

PAGE_SIZE = 100
#: Job pages read per run (1000 jobs); a run beyond it is read in part.
MAX_JOB_PAGES = 10
JOB_CACHE: LruCache[tuple[str, str, str, str, int], list[CiJob]] = LruCache(5000)
FINDINGS_CACHE: LruCache[tuple[str, str, str, str], sig.LogFindings] = LruCache(2000)


def parse_time(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp from a provider.

    Args:
        value: String such as ``2026-01-01T10:00:00Z`` or None.

    Returns:
        An aware datetime, or None.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CiProvider(Protocol):
    """What the collector needs from a CI provider."""

    name: str
    transport: HttpTransport

    async def scan(
        self,
        repository: str,
        since: datetime,
        *,
        max_runs: int,
        branches: Sequence[str],
    ) -> RepositoryScan:
        """List runs created since ``since`` with their jobs."""
        ...

    async def findings(
        self, repository: str, run: CiRun, job: CiJob
    ) -> sig.LogFindings:
        """Failure signatures and lines for one failed job."""
        ...

    async def pull_request_state(self, repository: str, number: str) -> dict[str, Any]:
        """State of one pull or merge request."""
        ...


def _keep_branch(branch: str, branches: Sequence[str]) -> bool:
    return not branches or branch in branches


class GitHubActions:
    """GitHub Actions through the REST API."""

    name = "github"

    def __init__(self, transport: HttpTransport) -> None:
        """Bind the adapter.

        Args:
            transport: Transport rooted at the GitHub API.
        """
        self.transport = transport

    @staticmethod
    def conclusion(status: Any, conclusion: Any) -> str:
        """Normalize a run or job outcome."""
        if status != "completed":
            return RUNNING
        return {
            "success": SUCCESS,
            "failure": FAILURE,
            "timed_out": FAILURE,
            "startup_failure": FAILURE,
            "cancelled": CANCELLED,
            "skipped": SKIPPED,
            "neutral": SKIPPED,
        }.get(conclusion, OTHER)

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        response = await self.transport.get_json(path, params)
        if response.status >= 400:
            raise ProviderError(response.status, f"GET {path} -> {response.status}")
        return response.body

    async def scan(
        self,
        repository: str,
        since: datetime,
        *,
        max_runs: int,
        branches: Sequence[str],
    ) -> RepositoryScan:
        """List workflow runs created since ``since``, newest first, with jobs."""
        scan = RepositoryScan(repository, self.name, None, [])
        repo = quote(repository, safe="/")
        try:
            meta = await self._get(f"/repos/{repo}")
            scan.default_branch = (meta or {}).get("default_branch")
            created = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            page = 1
            raw_runs: list[dict[str, Any]] = []
            while len(raw_runs) < max_runs:
                body = await self._get(
                    f"/repos/{repo}/actions/runs",
                    {"created": f">={created}", "per_page": PAGE_SIZE, "page": page},
                )
                batch = (body or {}).get("workflow_runs") or []
                raw_runs.extend(batch)
                if len(batch) < PAGE_SIZE:
                    break
                page += 1
            else:
                scan.partial, scan.partial_reason = True, "max_runs"
            for raw in raw_runs[:max_runs]:
                run = CiRun(
                    id=str(raw.get("id")),
                    workflow=str(raw.get("name") or raw.get("path") or "workflow"),
                    branch=str(raw.get("head_branch") or ""),
                    event=str(raw.get("event") or ""),
                    head_sha=str(raw.get("head_sha") or ""),
                    conclusion=self.conclusion(
                        raw.get("status"), raw.get("conclusion")
                    ),
                    created_at=parse_time(raw.get("created_at")) or since,
                    updated_at=parse_time(raw.get("updated_at")) or since,
                    attempt=int(raw.get("run_attempt") or 1),
                    url=str(raw.get("html_url") or ""),
                )
                if _keep_branch(run.branch, branches):
                    scan.runs.append(run)
            for run in scan.runs:
                await self._jobs(repo, run)
        except BudgetExhaustedError as exc:
            scan.partial, scan.partial_reason = True, exc.reason
        scan.requests = self.transport.budget.used
        scan.rate_limit_remaining = self.transport.budget.remaining
        return scan

    async def _jobs(self, repo: str, run: CiRun) -> None:
        key = (self.name, self.transport.base_url, repo, run.id, run.attempt)
        cached = JOB_CACHE.get(key) if run.completed else None
        if cached is not None:
            run.jobs, run.jobs_complete = cached, True
            return
        raw_list: list[dict[str, Any]] = []
        for page in range(1, MAX_JOB_PAGES + 1):
            body = await self._get(
                f"/repos/{repo}/actions/runs/{run.id}/jobs",
                {"filter": "all", "per_page": PAGE_SIZE, "page": page},
            )
            batch = (body or {}).get("jobs") or []
            raw_list.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        jobs = []
        for raw in raw_list:
            failed_step = next(
                (
                    s.get("name")
                    for s in raw.get("steps") or []
                    if s.get("conclusion") in ("failure", "timed_out")
                ),
                None,
            )
            labels = raw.get("labels") or []
            jobs.append(
                CiJob(
                    id=str(raw.get("id")),
                    name=str(raw.get("name") or "job"),
                    conclusion=self.conclusion(
                        raw.get("status"), raw.get("conclusion")
                    ),
                    created_at=parse_time(raw.get("created_at")),
                    started_at=parse_time(raw.get("started_at")),
                    completed_at=parse_time(raw.get("completed_at")),
                    attempt=int(raw.get("run_attempt") or 1),
                    url=str(raw.get("html_url") or ""),
                    failed_step=failed_step,
                    runner_label=str(labels[0]) if labels else None,
                )
            )
        run.jobs, run.jobs_complete = jobs, True
        if run.completed:
            JOB_CACHE.put(key, jobs)

    async def pull_request_state(self, repository: str, number: str) -> dict[str, Any]:
        """Current state of one pull request.

        Args:
            repository: ``owner/name``.
            number: Pull request number.

        Returns:
            ``{"state": open|closed|merged, "merged_at": ISO or None}``.
        """
        body = await self._get(f"/repos/{quote(repository, safe='/')}/pulls/{number}")
        merged_at = (body or {}).get("merged_at")
        state = "merged" if merged_at else str((body or {}).get("state") or "open")
        return {"state": state, "merged_at": merged_at}

    async def findings(
        self, repository: str, run: CiRun, job: CiJob
    ) -> sig.LogFindings:
        """Parse the failed job's log tail."""
        repo = quote(repository, safe="/")
        key = (self.name, self.transport.base_url, repo, job.id)
        cached = FINDINGS_CACHE.get(key)
        if cached is not None:
            return cached
        response = await self.transport.get_text_tail(
            f"/repos/{repo}/actions/jobs/{job.id}/logs"
        )
        found = (
            sig.parse_log(response.body or "") if response.body else sig.LogFindings()
        )
        FINDINGS_CACHE.put(key, found)
        return found


class GitLabCi:
    """GitLab CI pipelines through the REST API (v4)."""

    name = "gitlab"

    def __init__(self, transport: HttpTransport) -> None:
        """Bind the adapter.

        Args:
            transport: Transport rooted at ``<gitlab>/api/v4``.
        """
        self.transport = transport

    @staticmethod
    def conclusion(status: Any, allow_failure: bool = False) -> str:
        """Normalize a pipeline or job status."""
        if status == "failed":
            return OTHER if allow_failure else FAILURE
        return {
            "success": SUCCESS,
            "canceled": CANCELLED,
            "canceling": CANCELLED,
            "skipped": SKIPPED,
            "manual": SKIPPED,
        }.get(status, RUNNING)

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        response = await self.transport.get_json(path, params)
        if response.status >= 400:
            raise ProviderError(response.status, f"GET {path} -> {response.status}")
        return response.body

    async def scan(
        self,
        repository: str,
        since: datetime,
        *,
        max_runs: int,
        branches: Sequence[str],
    ) -> RepositoryScan:
        """List pipelines updated since ``since``, newest first, with jobs."""
        scan = RepositoryScan(repository, self.name, None, [])
        project = quote(repository, safe="")
        try:
            meta = await self._get(f"/projects/{project}")
            scan.default_branch = (meta or {}).get("default_branch")
            after = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            page = 1
            raw_runs: list[dict[str, Any]] = []
            while len(raw_runs) < max_runs:
                batch = await self._get(
                    f"/projects/{project}/pipelines",
                    {
                        "updated_after": after,
                        "per_page": PAGE_SIZE,
                        "page": page,
                        "order_by": "id",
                        "sort": "desc",
                    },
                )
                batch = batch or []
                raw_runs.extend(batch)
                if len(batch) < PAGE_SIZE:
                    break
                page += 1
            else:
                scan.partial, scan.partial_reason = True, "max_runs"
            for raw in raw_runs[:max_runs]:
                created = parse_time(raw.get("created_at")) or since
                if created < since:
                    continue
                run = CiRun(
                    id=str(raw.get("id")),
                    workflow=str(raw.get("name") or raw.get("source") or "pipeline"),
                    branch=str(raw.get("ref") or ""),
                    event=str(raw.get("source") or ""),
                    head_sha=str(raw.get("sha") or ""),
                    conclusion=self.conclusion(raw.get("status")),
                    created_at=created,
                    updated_at=parse_time(raw.get("updated_at")) or created,
                    url=str(raw.get("web_url") or ""),
                )
                if _keep_branch(run.branch, branches):
                    scan.runs.append(run)
            for run in scan.runs:
                await self._jobs(project, run)
        except BudgetExhaustedError as exc:
            scan.partial, scan.partial_reason = True, exc.reason
        scan.requests = self.transport.budget.used
        scan.rate_limit_remaining = self.transport.budget.remaining
        return scan

    async def _jobs(self, project: str, run: CiRun) -> None:
        key = (self.name, self.transport.base_url, project, run.id, 0)
        cached = JOB_CACHE.get(key) if run.completed else None
        if cached is not None:
            run.jobs, run.jobs_complete = cached, True
            run.attempt = max((j.attempt for j in cached), default=1)
            return
        raw_jobs: list[dict[str, Any]] = []
        for page in range(1, MAX_JOB_PAGES + 1):
            batch = await self._get(
                f"/projects/{project}/pipelines/{run.id}/jobs",
                {"include_retried": "true", "per_page": PAGE_SIZE, "page": page},
            )
            raw_jobs.extend(batch or [])
            if len(batch or []) < PAGE_SIZE:
                break
        attempts: dict[str, int] = {}
        jobs = []
        for raw in sorted(raw_jobs or [], key=lambda j: int(j.get("id") or 0)):
            name = str(raw.get("name") or "job")
            attempts[name] = attempts.get(name, 0) + 1
            jobs.append(
                CiJob(
                    id=str(raw.get("id")),
                    name=name,
                    conclusion=self.conclusion(
                        raw.get("status"), bool(raw.get("allow_failure"))
                    ),
                    created_at=parse_time(raw.get("created_at")),
                    started_at=parse_time(raw.get("started_at")),
                    completed_at=parse_time(raw.get("finished_at")),
                    attempt=attempts[name],
                    url=str(raw.get("web_url") or ""),
                    failed_step=raw.get("failure_reason"),
                    runner_label=(raw.get("tag_list") or [None])[0],
                )
            )
        run.jobs, run.jobs_complete = jobs, True
        run.attempt = max(attempts.values(), default=1)
        if run.completed:
            JOB_CACHE.put(key, jobs)

    async def pull_request_state(self, repository: str, number: str) -> dict[str, Any]:
        """Current state of one merge request.

        Args:
            repository: Project path.
            number: Merge request iid.

        Returns:
            ``{"state": open|closed|merged, "merged_at": ISO or None}``.
        """
        body = await self._get(
            f"/projects/{quote(repository, safe='')}/merge_requests/{number}"
        )
        raw = str((body or {}).get("state") or "opened")
        state = {"opened": "open", "locked": "open"}.get(raw, raw)
        return {"state": state, "merged_at": (body or {}).get("merged_at")}

    async def findings(
        self, repository: str, run: CiRun, job: CiJob
    ) -> sig.LogFindings:
        """Parse the failed job's trace tail."""
        project = quote(repository, safe="")
        key = (self.name, self.transport.base_url, project, job.id)
        cached = FINDINGS_CACHE.get(key)
        if cached is not None:
            return cached
        response = await self.transport.get_text_tail(
            f"/projects/{project}/jobs/{job.id}/trace"
        )
        found = (
            sig.parse_log(response.body or "") if response.body else sig.LogFindings()
        )
        FINDINGS_CACHE.put(key, found)
        return found

    async def test_report(self, repository: str, run: CiRun) -> sig.LogFindings:
        """Failed cases of the pipeline's JUnit-derived test report.

        Args:
            repository: Project path.
            run: The pipeline.

        Returns:
            Findings with ``junit`` signatures (empty when no report exists).
        """
        project = quote(repository, safe="")
        key = (self.name, self.transport.base_url, project, f"report:{run.id}")
        cached = FINDINGS_CACHE.get(key)
        if cached is not None:
            return cached
        response = await self.transport.get_json(
            f"/projects/{project}/pipelines/{run.id}/test_report"
        )
        found = sig.LogFindings()
        for suite in (response.body or {}).get("test_suites") or []:
            for case in suite.get("test_cases") or []:
                if case.get("status") not in ("failed", "error"):
                    continue
                classname = case.get("classname") or ""
                name = case.get("name") or ""
                test = f"{classname}::{name}" if classname else name
                found.add(sig.Signature("junit", test, str(case.get("status"))))
        FINDINGS_CACHE.put(key, found)
        return found
