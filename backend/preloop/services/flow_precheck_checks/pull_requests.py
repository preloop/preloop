"""``open_pull_requests``: open pull requests that need attention.

A pull request is kept when it passes the author, label and draft filters,
has at least one reason from ``needs_any`` (or ``needs_any`` is empty), has
been idle for ``idle_hours``, and nobody owns it. It is owned when a
non-terminal Preloop execution is bound to it, or it carries a claim label
applied less than ``claim_max_age_hours`` ago. An approved and mergeable pull
request is left out unless ``exclude.approved_and_mergeable`` is false.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List

from preloop.models.schemas.flow_precheck import OpenPullRequestsCheck
from preloop.services.flow_precheck import (
    CheckContext,
    CheckResult,
    iso,
    truncate_title,
)
from preloop.services.flow_precheck_checks.common import (
    folded,
    labels_match,
    plural,
    result,
)
from preloop.services.flow_precheck_reader import PullRequestState

_FAILING_ROLLUP = {"FAILURE", "ERROR"}


def pull_request_reasons(pr: PullRequestState) -> List[str]:
    """Reason codes for why a pull request needs attention, in a fixed order."""
    reasons = []
    if (pr.checks_state or "").upper() in _FAILING_ROLLUP:
        reasons.append("failing_checks")
    if (pr.mergeable or "").upper() == "CONFLICTING":
        reasons.append("merge_conflict")
    if pr.unresolved_threads > 0:
        reasons.append("unresolved_review_threads")
    if (pr.review_decision or "").upper() == "CHANGES_REQUESTED":
        reasons.append("changes_requested")
    return reasons


def _approved_and_mergeable(pr: PullRequestState) -> bool:
    return (pr.review_decision or "").upper() == "APPROVED" and (
        pr.mergeable or ""
    ).upper() == "MERGEABLE"


def _claimed(pr: PullRequestState, check: OpenPullRequestsCheck, ctx: Any) -> bool:
    """Whether a fresh claim label marks the pull request as owned."""
    claims = folded(check.exclude.claim_labels)
    if not claims:
        return False
    max_age = check.exclude.claim_max_age_hours
    applied = {name.casefold(): when for name, when in pr.label_applied_at.items()}
    for label in pr.labels:
        if label.casefold() not in claims:
            continue
        if max_age is None:
            return True
        when = applied.get(label.casefold())
        # A label whose application is not in the recent timeline is
        # treated as fresh: leaving a pull request alone is the safe side.
        if when is None or ctx.now - when < timedelta(hours=max_age):
            return True
    return False


async def evaluate_open_pull_requests(
    check: OpenPullRequestsCheck, ctx: CheckContext
) -> CheckResult:
    """Evaluate one ``open_pull_requests`` check."""
    from preloop.services.flow_pr_binding import normalize_pr_url

    include = folded(check.authors.include)
    exclude = folded(check.authors.exclude)
    needs = set(check.needs_any)
    idle_cutoff = (
        ctx.now - timedelta(hours=check.idle_hours) if check.idle_hours else None
    )
    found: List[Dict[str, Any]] = []
    more_upstream = False
    for repo in ctx.repositories:
        reader = await ctx.readers(repo)
        page = await reader.list_open_pull_requests_with_state(
            repo.slug, include_drafts=check.include_drafts
        )
        more_upstream = more_upstream or page.truncated
        for pr in page.items:
            author = (pr.author or "").casefold()
            if include and author not in include:
                continue
            if exclude and author in exclude:
                continue
            if not labels_match(pr.labels, check.labels):
                continue
            reasons = pull_request_reasons(pr)
            if needs and not needs & set(reasons):
                continue
            if (
                idle_cutoff is not None
                and pr.updated_at
                and pr.updated_at > idle_cutoff
            ):
                continue
            if check.exclude.approved_and_mergeable and _approved_and_mergeable(pr):
                continue
            if (
                check.exclude.active_execution
                and normalize_pr_url(pr.url) in ctx.active_pull_request_urls
            ):
                continue
            if _claimed(pr, check, ctx):
                continue
            found.append(
                {
                    "repository": pr.repository,
                    "number": pr.number,
                    "url": pr.url,
                    "title": truncate_title(pr.title),
                    "author": pr.author,
                    "head_sha": pr.head_sha,
                    "base_branch": pr.base_branch,
                    "updated_at": iso(pr.updated_at),
                    "labels": list(pr.labels)[:20],
                    "reasons": reasons,
                }
            )
    count = len(found)
    summary = (
        f"{plural(count, 'open pull request')} "
        f"{'needs' if count == 1 else 'need'} attention"
        if count
        else "No open pull requests need attention"
    )
    return result(
        found=found,
        min_count=check.min_count,
        max_items=check.max_items,
        more_upstream=more_upstream,
        summary=summary,
    )
