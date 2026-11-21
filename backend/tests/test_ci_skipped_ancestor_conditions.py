"""Guard CI job conditions against skipped jobs upstream of ``fast``.

A job ``if:`` without a status function gets an implicit ``success()``,
which is false when any job earlier in the dependency chain was skipped,
even if every direct need succeeded. ``fast`` needs path-filtered jobs, so
a pull request without backend changes would skip every job below it.
"""

from __future__ import annotations

from typing import Any

from tests.ci_workflow import load_ci_jobs


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _below_fast(jobs: dict[str, Any]) -> list[str]:
    below: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            if name in below or name == "ci":
                continue
            if any(n == "fast" or n in below for n in _needs(job)):
                below.add(name)
                changed = True
    return sorted(below)


def test_jobs_below_fast_do_not_rely_on_implicit_success() -> None:
    """Every job below ``fast`` uses ``!cancelled()`` and checks its direct needs."""
    jobs = load_ci_jobs()
    names = _below_fast(jobs)
    assert "leaf-parity" in names
    problems = []
    for name in names:
        cond = str(jobs[name].get("if", ""))
        if not cond.startswith("${{ !cancelled() && "):
            problems.append((name, "no !cancelled()"))
        for need in _needs(jobs[name]):
            if need != "changes" and f"needs.{need}.result == 'success'" not in cond:
                problems.append((name, f"does not require {need} success"))
    assert not problems, problems
