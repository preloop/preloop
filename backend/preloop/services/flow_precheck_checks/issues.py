"""``issues``: open issues matching labels, a milestone and assignment."""

from __future__ import annotations

from typing import Any, Dict, List

from preloop.models.schemas.flow_precheck import IssuesCheck
from preloop.services.flow_precheck import (
    CheckContext,
    CheckResult,
    iso,
    truncate_title,
)
from preloop.services.flow_precheck_checks.common import (
    labels_match,
    plural,
    result,
)


def _rank(labels: List[str], order: List[str]) -> int:
    have = {label.casefold() for label in labels}
    for index, name in enumerate(order):
        if name.casefold() in have:
            return index
    return len(order)


async def evaluate_issues(check: IssuesCheck, ctx: CheckContext) -> CheckResult:
    """Evaluate one ``issues`` check."""
    found: List[Dict[str, Any]] = []
    more_upstream = False
    for repo in ctx.repositories:
        reader = await ctx.readers(repo)
        page = await reader.list_issues(
            repo.slug,
            labels_all=check.labels.all,
            milestone=check.milestone,
            unassigned=check.unassigned,
        )
        more_upstream = more_upstream or page.truncated
        for issue in page.items:
            if not labels_match(issue.labels, check.labels):
                continue
            if check.unassigned and issue.assignees:
                continue
            found.append(
                {
                    "repository": issue.repository,
                    "number": issue.number,
                    "url": issue.url,
                    "title": truncate_title(issue.title),
                    "labels": list(issue.labels)[:20],
                    "milestone": issue.milestone,
                    "created_at": iso(issue.created_at),
                }
            )
    if check.order_by_labels:
        # Stable: issues of one rank stay oldest first, as the reader sent them.
        found.sort(key=lambda item: _rank(item["labels"], check.order_by_labels))
    count = len(found)
    summary = (
        f"{plural(count, 'issue')} {'matches' if count == 1 else 'match'}"
        if count
        else "No issues match"
    )
    return result(
        found=found,
        min_count=check.min_count,
        max_items=check.max_items,
        more_upstream=more_upstream,
        summary=summary,
    )
