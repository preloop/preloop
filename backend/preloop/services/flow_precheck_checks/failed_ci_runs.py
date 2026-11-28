"""``failed_ci_runs``: workflow runs that failed since a point in time.

``since: last_run`` reads from the newest execution of this flow that was not
skipped, so failures during a quiet stretch are still found by the next run.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

from preloop.models.schemas.flow_precheck import FailedCiRunsCheck
from preloop.services.flow_precheck import CheckContext, CheckResult, iso
from preloop.services.flow_precheck_checks.common import folded, plural, result
from preloop.services.flow_precheck_reader import FAILED_RUNS_SCAN


def since_instant(check: FailedCiRunsCheck, ctx: CheckContext) -> datetime:
    """The start of the window the check reads."""
    since = check.since
    if since == "last_run":
        return ctx.last_run_at
    if since == "previous_tick":
        return ctx.previous_tick_at or ctx.last_run_at
    return ctx.now - timedelta(hours=since.hours)


async def evaluate_failed_ci_runs(
    check: FailedCiRunsCheck, ctx: CheckContext
) -> CheckResult:
    """Evaluate one ``failed_ci_runs`` check."""
    start = since_instant(check, ctx)
    workflows = folded(check.workflows)
    found: List[Dict[str, Any]] = []
    more_upstream = False
    for repo in ctx.repositories:
        reader = await ctx.readers(repo)
        page = await reader.list_failed_ci_runs(
            repo.slug,
            since=start,
            branches=check.branches,
            # Scan a full page: the workflow filter below runs after the
            # read, and max_items caps only what is passed to the run.
            limit=FAILED_RUNS_SCAN,
        )
        more_upstream = more_upstream or page.truncated
        for run in page.items:
            if workflows and run.workflow.casefold() not in workflows:
                continue
            found.append(
                {
                    "repository": run.repository,
                    "id": run.id,
                    "url": run.url,
                    "workflow": run.workflow,
                    "branch": run.branch,
                    "head_sha": run.head_sha,
                    "event": run.event,
                    "created_at": iso(run.created_at),
                }
            )
    count = len(found)
    since_text = iso(start)
    summary = (
        f"{plural(count, 'failed CI run')} since {since_text}"
        if count
        else f"No failed CI runs since {since_text}"
    )
    return result(
        found=found,
        min_count=check.min_count,
        max_items=check.max_items,
        more_upstream=more_upstream,
        summary=summary,
    )
