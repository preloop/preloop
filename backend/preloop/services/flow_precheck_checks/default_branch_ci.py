"""``default_branch_ci``: is the head of a branch failing (or passing)?

With ``required_only`` the checks branch protection requires are the ones
that count; a branch without required checks, or a credential that cannot
read branch protection, counts every check. One item per repository in the
requested state.
"""

from __future__ import annotations

from typing import Any, Dict, List

from preloop.models.schemas.flow_precheck import DefaultBranchCiCheck
from preloop.services.flow_precheck import CheckContext, CheckResult
from preloop.services.flow_precheck_checks.common import plural, result
from preloop.services.flow_precheck_reader import BranchCheckState

#: Failing checks listed per repository item.
MAX_FAILING_LISTED = 20


def branch_state(state: BranchCheckState) -> str:
    """``failing``, ``pending``, ``passing`` or ``none`` for a branch head."""
    checks = list(state.checks)
    if state.required:
        wanted = {name.casefold() for name in state.required}
        checks = [c for c in checks if c.name.casefold() in wanted]
    if not checks:
        return "none"
    if any(c.state == "failure" for c in checks):
        return "failing"
    if any(c.state == "pending" for c in checks):
        return "pending"
    return "passing"


async def evaluate_default_branch_ci(
    check: DefaultBranchCiCheck, ctx: CheckContext
) -> CheckResult:
    """Evaluate one ``default_branch_ci`` check."""
    found: List[Dict[str, Any]] = []
    described: List[str] = []
    for repo in ctx.repositories:
        reader = await ctx.readers(repo)
        state = await reader.default_branch_check_state(
            repo.slug, branch=check.branch, required_only=check.required_only
        )
        current = branch_state(state)
        described.append(f"{state.branch} is {_word(current)} on {repo.slug}")
        if current != check.state:
            continue
        wanted = {n.casefold() for n in state.required} if state.required else None
        failing = [
            {"name": c.name, "url": c.url}
            for c in state.checks
            if c.state == "failure" and (wanted is None or c.name.casefold() in wanted)
        ]
        found.append(
            {
                "repository": repo.slug,
                "branch": state.branch,
                "head_sha": state.head_sha,
                "state": current,
                "failing_checks": failing[:MAX_FAILING_LISTED],
            }
        )
    if len(ctx.repositories) == 1:
        summary = described[0] if described else "No branch state"
    else:
        summary = (
            f"{plural(len(found), 'repository', 'repositories')} "
            f"{'is' if len(found) == 1 else 'are'} {_word(check.state)}"
        )
    return result(
        found=found,
        min_count=check.min_count,
        max_items=check.max_items,
        more_upstream=False,
        summary=summary,
    )


def _word(state: str) -> str:
    return {
        "failing": "red",
        "passing": "green",
        "pending": "pending",
        "none": "without checks",
    }.get(state, state)
