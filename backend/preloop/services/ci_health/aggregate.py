"""Per-workflow and per-job CI aggregates from normalized runs.

Everything here is pure: it takes :class:`~.models.CiRun` lists and returns
JSON-ready dicts, so fixtures test it without any provider.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional, Sequence

from .models import FAILURE, SUCCESS, CiJob, CiRun

#: ``test (3/4)``, ``test (shard 2)``, ``test [py3.12, linux]``, ``test 2/4``.
_MATRIX_SUFFIX = re.compile(r"\s*(?:\([^)]*\)|\[[^\]]*\]|\d+/\d+)\s*$")
#: Shard sets whose slowest member is at least this much over the mean.
IMBALANCE_REPORT_RATIO = 1.3
TOP_WORKFLOWS = 10
TOP_JOBS = 15


def base_job_name(name: str) -> str:
    """Job name without matrix or shard suffixes.

    Args:
        name: Job name as the provider reports it.

    Returns:
        The base name (``"test (2/4)"`` becomes ``"test"``).
    """
    previous = None
    value = name.strip()
    while previous != value:
        previous = value
        value = _MATRIX_SUFFIX.sub("", value).strip()
    return value or name


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile.

    Args:
        values: Samples.
        pct: 0-100.

    Returns:
        The percentile rounded to one decimal, or None without samples.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 1)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 3) if denominator else None


def _minutes(seconds: Iterable[Optional[float]]) -> float:
    return round(sum(s for s in seconds if s) / 60, 1)


def matches_any(name: str, patterns: Sequence[str]) -> bool:
    """Case-insensitive glob match against any pattern.

    Args:
        name: Job name.
        patterns: Globs such as ``"test*"`` or ``"Build / lint"``.

    Returns:
        True when one pattern matches the name or its base name.
    """
    lowered = name.lower()
    base = base_job_name(name).lower()
    return any(
        fnmatch.fnmatchcase(lowered, p.lower()) or fnmatch.fnmatchcase(base, p.lower())
        for p in patterns
    )


def _run_wall_seconds(run: CiRun) -> Optional[float]:
    ends = [j.completed_at for j in run.jobs if j.completed_at]
    if not ends:
        return None
    return max(0.0, (max(ends) - run.created_at).total_seconds())


def _gate(run: CiRun, patterns: Sequence[str]) -> Optional[tuple[str, float]]:
    """The job that finished last (the one the run waited for)."""
    jobs = [j for j in run.jobs if j.completed_at and j.attempt == run.attempt]
    if patterns:
        jobs = [j for j in jobs if matches_any(j.name, patterns)] or jobs
    if not jobs:
        return None
    last = max(jobs, key=lambda j: j.completed_at)  # type: ignore[arg-type,return-value]
    assert last.completed_at is not None
    return base_job_name(last.name), (
        last.completed_at - run.created_at
    ).total_seconds()


def _job_stats(jobs: list[CiJob]) -> dict[str, Any]:
    finished = [j for j in jobs if j.conclusion in (SUCCESS, FAILURE)]
    failures = sum(1 for j in finished if j.conclusion == FAILURE)
    durations = [d for j in jobs if (d := j.duration_seconds) is not None]
    queues = [q for j in jobs if (q := j.queue_seconds) is not None]
    return {
        "count": len(jobs),
        "failures": failures,
        "success_rate": _rate(len(finished) - failures, len(finished)),
        "reruns": sum(1 for j in jobs if j.attempt > 1),
        "queue_p95_s": percentile(queues, 95),
        "duration_p50_s": percentile(durations, 50),
        "duration_p95_s": percentile(durations, 95),
        "runner_minutes": _minutes(durations),
    }


def flaky_jobs(runs: Sequence[CiRun]) -> dict[tuple[str, str], set[str]]:
    """Jobs that both failed and passed on the same commit.

    Args:
        runs: Runs with their jobs (all attempts).

    Returns:
        ``{(workflow, base job name): {head_sha, ...}}``.
    """
    outcomes: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for run in runs:
        for job in run.jobs:
            if job.conclusion in (SUCCESS, FAILURE):
                key = (run.workflow, base_job_name(job.name), run.head_sha)
                outcomes[key].add(job.conclusion)
    flaky: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (workflow, job_name, sha), seen in outcomes.items():
        if seen == {SUCCESS, FAILURE}:
            flaky[(workflow, job_name)].add(sha)
    return dict(flaky)


def _shards(runs: Sequence[CiRun]) -> list[dict[str, Any]]:
    ratios: dict[tuple[str, str], list[float]] = defaultdict(list)
    slowest: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    sizes: dict[tuple[str, str], int] = {}
    for run in runs:
        groups: dict[str, list[CiJob]] = defaultdict(list)
        for job in run.jobs:
            if job.attempt == run.attempt and job.duration_seconds:
                groups[base_job_name(job.name)].append(job)
        for base, members in groups.items():
            if len(members) < 2 or all(m.name == base for m in members):
                continue
            durations = [m.duration_seconds or 0.0 for m in members]
            mean = sum(durations) / len(durations)
            if mean <= 0:
                continue
            key = (run.workflow, base)
            ratios[key].append(max(durations) / mean)
            slowest[key][max(members, key=lambda m: m.duration_seconds or 0).name] += 1
            sizes[key] = len(members)
    report = []
    for key, values in ratios.items():
        median = percentile(values, 50) or 0.0
        if median >= IMBALANCE_REPORT_RATIO:
            report.append(
                {
                    "workflow": key[0],
                    "job": key[1],
                    "shards": sizes[key],
                    "imbalance_p50": round(median, 2),
                    "slowest_shard": slowest[key].most_common(1)[0][0],
                    "runs": len(values),
                }
            )
    return sorted(report, key=lambda r: -r["imbalance_p50"])


def aggregate_runs(
    runs: Sequence[CiRun], *, critical_path_jobs: Sequence[str] = ()
) -> dict[str, Any]:
    """Aggregate one repository's runs.

    Args:
        runs: Completed and running runs in the window.
        critical_path_jobs: Globs naming the jobs that gate a merge. When
            empty, the gate of a run is whichever job finished last.

    Returns:
        A dict with ``totals``, ``workflows``, ``jobs``, ``critical_path``,
        ``shard_imbalance`` and ``flaky_jobs``.
    """
    completed = [r for r in runs if r.completed]
    decided = [r for r in completed if r.conclusion in (SUCCESS, FAILURE)]
    successes = sum(1 for r in decided if r.conclusion == SUCCESS)
    all_jobs = [j for r in runs for j in r.jobs]
    walls = [w for r in completed if (w := _run_wall_seconds(r)) is not None]
    queues = [q for j in all_jobs if (q := j.queue_seconds) is not None]
    totals = {
        "runs": len(runs),
        "completed": len(completed),
        "success_rate": _rate(successes, len(decided)),
        "rerun_rate": _rate(sum(1 for r in runs if r.attempt > 1), len(runs)),
        "jobs": len(all_jobs),
        "queue_p50_s": percentile(queues, 50),
        "queue_p95_s": percentile(queues, 95),
        "run_wall_p50_s": percentile(walls, 50),
        "run_wall_p95_s": percentile(walls, 95),
        "runner_minutes": _minutes(j.duration_seconds for j in all_jobs),
    }

    by_workflow: dict[str, list[CiRun]] = defaultdict(list)
    for run in runs:
        by_workflow[run.workflow].append(run)
    workflows: list[dict[str, Any]] = []
    for name, members in by_workflow.items():
        finished = [r for r in members if r.conclusion in (SUCCESS, FAILURE)]
        wall = [w for r in members if (w := _run_wall_seconds(r)) is not None]
        workflows.append(
            {
                "workflow": name,
                "runs": len(members),
                "success_rate": _rate(
                    sum(1 for r in finished if r.conclusion == SUCCESS), len(finished)
                ),
                "rerun_rate": _rate(
                    sum(1 for r in members if r.attempt > 1), len(members)
                ),
                "run_wall_p50_s": percentile(wall, 50),
                "run_wall_p95_s": percentile(wall, 95),
                "runner_minutes": _minutes(
                    j.duration_seconds for r in members for j in r.jobs
                ),
            }
        )
    workflows.sort(key=lambda w: -w["runner_minutes"])

    by_job: dict[tuple[str, str], list[CiJob]] = defaultdict(list)
    for run in runs:
        for job in run.jobs:
            by_job[(run.workflow, base_job_name(job.name))].append(job)
    flaky = flaky_jobs(runs)
    jobs = [
        {
            "workflow": workflow,
            "job": job,
            "critical": bool(critical_path_jobs)
            and matches_any(job, critical_path_jobs),
            "flaky_commits": len(flaky.get((workflow, job), ())),
            **_job_stats(members),
        }
        for (workflow, job), members in by_job.items()
    ]
    jobs.sort(key=lambda j: -(j["duration_p95_s"] or 0))

    gates: dict[str, list[float]] = defaultdict(list)
    for run in completed:
        gate = _gate(run, critical_path_jobs)
        if gate:
            gates[gate[0]].append(gate[1])
    gate_list: list[dict[str, Any]] = [
        {"job": name, "runs": len(v), "gate_p95_s": percentile(v, 95)}
        for name, v in gates.items()
    ]
    gate_rows = sorted(
        gate_list,
        key=lambda g: (-g["runs"], -(g["gate_p95_s"] or 0)),
    )
    critical = {
        "configured": list(critical_path_jobs),
        "slowest_gate": gate_rows[0] if gate_rows else None,
        "gates": gate_rows[:5],
        "jobs": [j for j in jobs if j["critical"]][:TOP_JOBS],
    }
    flaky_rows: list[dict[str, Any]] = [
        {"workflow": w, "job": j, "commits": len(shas)}
        for (w, j), shas in flaky.items()
    ]
    return {
        "totals": totals,
        "workflows": workflows[:TOP_WORKFLOWS],
        "jobs": jobs[:TOP_JOBS],
        "critical_path": critical,
        "shard_imbalance": _shards(runs)[:5],
        "flaky_jobs": sorted(flaky_rows, key=lambda f: -int(f["commits"]))[:10],
    }
