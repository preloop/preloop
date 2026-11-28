"""Collect one compact CI health report and update the flow's ledger.

This module is provider-neutral and does no database access: the MCP tool
resolves repositories to adapters, loads the ledger and the flow's
published pull requests, calls :func:`collect`, and stores the new ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from preloop.utils.secret_scrubbing import scrub_secrets

from . import ledger as ledger_mod
from . import signatures as sig
from .aggregate import aggregate_runs, base_job_name, flaky_jobs, matches_any
from .models import FAILURE, CiJob, CiRun, RepositoryScan
from .providers import CiProvider, GitLabCi, parse_time
from .transport import BudgetExhaustedError, ProviderError

#: Run ids remembered per repository, so a run is counted exactly once.
COUNTED_RUNS_KEPT = 1500
MAX_FAILURES_LISTED = 20


@dataclass
class Target:
    """One repository and the adapter that reads it."""

    provider: CiProvider
    repository: str

    @property
    def key(self) -> str:
        """``provider:path``, the ledger's repository key."""
        return f"{self.provider.name}:{self.repository}"


@dataclass
class Options:
    """Collector knobs (validated by the tool schema)."""

    window_hours: int = 24
    branches: Sequence[str] = ()
    critical_path_jobs: Sequence[str] = ()
    protected_branches: Sequence[str] = ()
    max_runs: int = 300
    max_failed_logs: int = 20
    report_only: str = "auto"
    report_only_days: int = 14
    focus_urls: set[str] = field(default_factory=set)


def precheck_focus(precheck: Any) -> set[str]:
    """URLs the pre-check stage matched, to read their logs first.

    Args:
        precheck: ``trigger_event.payload.precheck`` of the run, if any.

    Returns:
        Item URLs from every matched check.
    """
    urls: set[str] = set()
    checks = precheck.get("checks") if isinstance(precheck, Mapping) else None
    if not isinstance(checks, Mapping):
        return urls
    for result in checks.values():
        if not isinstance(result, Mapping) or not result.get("matched"):
            continue
        for item in result.get("items") or []:
            if isinstance(item, Mapping) and isinstance(item.get("url"), str):
                urls.add(item["url"])
    return urls


def _report_only(ledger: Mapping[str, Any], options: Options, now: datetime) -> dict:
    if options.report_only in ("on", "off"):
        return {
            "report_only": options.report_only == "on",
            "reason": f"report_only is {options.report_only}",
        }
    first = parse_time(ledger.get("first_run_at")) or now
    until = first + timedelta(days=options.report_only_days)
    if now < until:
        return {
            "report_only": True,
            "reason": "first "
            f"{options.report_only_days} days of this flow (until {until.date()})",
        }
    return {"report_only": False, "reason": "report-only period is over"}


def _counted(ledger: dict[str, Any], key: str) -> list[str]:
    counted = ledger.setdefault("counted_runs", {})
    if not isinstance(counted, dict):
        ledger["counted_runs"] = counted = {}
    return list(counted.get(key) or [])


def _failed_jobs(run: CiRun) -> list[CiJob]:
    return [j for j in run.jobs if j.conclusion == FAILURE]


def _priority(run: CiRun, scan: RepositoryScan, options: Options) -> tuple:
    protected = set(options.protected_branches) or (
        {scan.default_branch} if scan.default_branch else set()
    )
    return (
        run.url not in options.focus_urls,
        run.branch not in protected,
        -run.created_at.timestamp(),
    )


async def _job_findings(
    target: Target, run: CiRun, job: CiJob, logs_left: list[int]
) -> tuple[sig.LogFindings, bool]:
    """Findings for one failed job; falls back to a job signature."""
    found = sig.LogFindings()
    read = False
    if logs_left[0] > 0:
        try:
            found = await target.provider.findings(target.repository, run, job)
            read = True
        except (BudgetExhaustedError, ProviderError):
            pass
        logs_left[0] -= 1
    if not found.signatures:
        found = sig.merge(
            [
                found,
                sig.LogFindings(
                    signatures=[
                        sig.job_signature(
                            run.workflow, base_job_name(job.name), job.failed_step
                        )
                    ]
                ),
            ]
        )
    return found, read


async def collect(
    targets: Sequence[Target],
    *,
    ledger_value: Any,
    now: datetime,
    options: Options,
    ledger_updates: Iterable[Mapping[str, Any]] = (),
    published_pull_requests: Iterable[Mapping[str, Any]] = (),
    execution_id: Optional[str] = None,
    unavailable: Sequence[Mapping[str, str]] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Scan every target, aggregate, update the ledger and build the report.

    Args:
        targets: Repositories with their adapters.
        ledger_value: Stored ledger (None on the first run).
        now: Current time.
        options: Knobs.
        ledger_updates: Agent changes to entry status, links and notes.
        published_pull_requests: Pull requests earlier runs published.
        execution_id: The calling execution, recorded in the history.
        unavailable: Repositories that could not be resolved, with reasons.

    Returns:
        ``(report, ledger)``.
    """
    ledger = ledger_mod.load(ledger_value, now)
    refused = ledger_mod.apply_updates(ledger, ledger_updates)
    ledger_mod.record_pull_requests(ledger, published_pull_requests)
    since = now - timedelta(hours=options.window_hours)
    repositories: list[dict[str, Any]] = []
    changed = False
    partial = False
    logs_left = [options.max_failed_logs]
    point_repos: dict[str, Any] = {}

    for target in targets:
        try:
            scan = await target.provider.scan(
                target.repository,
                since,
                max_runs=options.max_runs,
                branches=options.branches,
            )
        except ProviderError as exc:
            repositories.append(
                {
                    "repository": target.repository,
                    "provider": target.provider.name,
                    "error": f"provider answered {exc.status or 'no response'}",
                }
            )
            partial = True
            continue
        partial = partial or scan.partial
        counted = _counted(ledger, target.key)
        counted_set = set(counted)
        new_runs = [
            r
            for r in scan.runs
            if r.completed and r.jobs_complete and r.id not in counted_set
        ]
        changed = changed or bool(new_runs)
        stats = aggregate_runs(scan.runs, critical_path_jobs=options.critical_path_jobs)
        failures: list[dict[str, Any]] = []
        logs_read = 0
        for run in sorted(new_runs, key=lambda r: _priority(r, scan, options)):
            failed = _failed_jobs(run)
            report_findings: Optional[sig.LogFindings] = None
            if failed and isinstance(target.provider, GitLabCi) and logs_left[0] > 0:
                try:
                    report_findings = await target.provider.test_report(
                        target.repository, run
                    )
                except (BudgetExhaustedError, ProviderError):
                    report_findings = None
            for job in failed:
                found, read = await _job_findings(target, run, job, logs_left)
                logs_read += int(read)
                if report_findings and report_findings.signatures:
                    found = sig.merge([report_findings, found])
                job_name = base_job_name(job.name)
                ids = []
                for signature in found.signatures:
                    # The id stays computed from the canonical text; what is
                    # stored and returned is scrubbed.
                    shown = signature.as_dict()
                    shown["test"] = scrub_secrets(shown["test"]) or ""
                    shown["error"] = scrub_secrets(shown["error"]) or ""
                    entry = ledger_mod.record_failure(
                        ledger,
                        signature=shown,
                        repository=target.key,
                        job=job_name,
                        head_sha=run.head_sha,
                        run_url=run.url,
                        seen_at=run.created_at,
                    )
                    ids.append(signature.id)
                    del entry  # recorded in place
                if len(failures) < MAX_FAILURES_LISTED:
                    failures.append(
                        {
                            "workflow": run.workflow,
                            "job": job.name,
                            "critical": bool(options.critical_path_jobs)
                            and matches_any(job.name, options.critical_path_jobs),
                            "branch": run.branch,
                            "head_sha": run.head_sha[:12],
                            "attempt": job.attempt,
                            "url": job.url or run.url,
                            "failed_step": job.failed_step,
                            "signatures": ids,
                            "lines": [
                                scrub_secrets(line) or "" for line in found.lines
                            ],
                        }
                    )
            counted.append(run.id)
        ledger["counted_runs"][target.key] = counted[-COUNTED_RUNS_KEPT:]
        ledger_mod.mark_flaky(ledger, flaky_jobs(scan.runs))
        default_branch_runs = [
            r
            for r in scan.runs
            if scan.default_branch and r.branch == scan.default_branch
        ]
        latest_default = max(
            (r for r in default_branch_runs if r.completed),
            key=lambda r: r.created_at,
            default=None,
        )
        repositories.append(
            {
                "repository": target.repository,
                "provider": target.provider.name,
                "default_branch": scan.default_branch,
                "default_branch_state": latest_default.conclusion
                if latest_default
                else None,
                "partial": scan.partial,
                "partial_reason": scan.partial_reason,
                "new_runs": len(new_runs),
                "failed_logs_read": logs_read,
                **stats,
                "failures": failures,
            }
        )
        totals = stats["totals"]
        gate = stats["critical_path"]["slowest_gate"]
        point_repos[target.key] = {
            "runs": totals["runs"],
            "success_rate": totals["success_rate"],
            "rerun_rate": totals["rerun_rate"],
            "queue_p95_s": totals["queue_p95_s"],
            "run_wall_p95_s": totals["run_wall_p95_s"],
            "runner_minutes": totals["runner_minutes"],
            "slowest_gate": gate["job"] if gate else None,
            "slowest_gate_p95_s": gate["gate_p95_s"] if gate else None,
        }

    await refresh_pull_requests(ledger, targets)
    ledger_mod.age(ledger, now)
    mode = _report_only(ledger, options, now)
    ledger["last_run_at"] = now.isoformat()
    summary = ledger_mod.summary(ledger)
    ledger_mod.append_history(
        ledger,
        {
            "at": now.isoformat(),
            "execution_id": execution_id,
            "changed": changed,
            "repositories": point_repos,
            "open_signatures": summary["counts"]["open"]
            + summary["counts"]["regressed"],
            "flaky_signatures": summary["flaky"],
            "top_flakes": [e["id"] for e in summary["top"] if e.get("flaky")][:5],
        },
    )
    report = {
        "generated_at": now.isoformat(),
        "window": {
            "hours": options.window_hours,
            "start": since.isoformat(),
            "end": now.isoformat(),
        },
        "changed_since_last_run": changed,
        "partial": partial,
        "mode": mode,
        "repositories": repositories,
        "unavailable": list(unavailable),
        "ledger": {
            "key": ledger_mod.LEDGER_KEY,
            **summary,
            "refused_updates": refused,
        },
        "previous": ledger["history"][-2] if len(ledger["history"]) > 1 else None,
    }
    return report, ledger


def pull_request_repository(url: str) -> Optional[tuple[str, str, str]]:
    """Split a pull or merge request URL.

    Args:
        url: ``https://github.com/o/r/pull/7`` or
            ``https://gitlab.example.com/g/p/-/merge_requests/7``.

    Returns:
        ``(host, repository path, number)`` or None.
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 4 and parts[-2] == "pull":
        return parsed.netloc, "/".join(parts[:-2]), parts[-1]
    if len(parts) >= 5 and parts[-3] == "-" and parts[-2] == "merge_requests":
        return parsed.netloc, "/".join(parts[:-3]), parts[-1]
    return None


#: Open pull requests whose state is refreshed per call.
MAX_PR_REFRESH = 10


async def refresh_pull_requests(
    ledger: dict[str, Any], targets: Sequence[Target]
) -> None:
    """Update the state of open pull requests the flow published.

    Only pull requests in one of the scanned repositories are refreshed, with
    that repository's adapter, so no extra credential is ever used.

    Args:
        ledger: Ledger to update in place.
        targets: Scanned repositories.
    """
    refreshed = 0
    for pr in ledger["pull_requests"]:
        if pr.get("state") != "open" or refreshed >= MAX_PR_REFRESH:
            continue
        parsed = pull_request_repository(str(pr.get("url") or ""))
        target = (
            next(
                (
                    t
                    for t in targets
                    if t.repository.lower() == parsed[1].lower()
                    and _same_host(t, parsed[0])
                ),
                None,
            )
            if parsed
            else None
        )
        if parsed is None or target is None:
            continue
        refreshed += 1
        try:
            state = await target.provider.pull_request_state(
                target.repository, parsed[2]
            )
        except (BudgetExhaustedError, ProviderError):
            continue
        pr.update(state)


def _same_host(target: Target, host: str) -> bool:
    """Whether a pull request URL host belongs to the target's provider.

    GitHub's API lives on ``api.<host>`` (``api.github.com``); GitLab's on the
    instance host itself.
    """
    api_host = urlparse(target.provider.transport.base_url).netloc.lower()
    host = host.lower()
    if target.provider.name == "github":
        return api_host in (host, f"api.{host}")
    return api_host == host
