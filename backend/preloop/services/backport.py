"""Backport a merged pull request onto later release branches (issue #961).

A flow whose ``git_clone_config.backport`` block is enabled runs no agent.
When a pull request (GitHub) or merge request (GitLab) merges into the
configured ``source_branch``, the control plane, for each target branch in
order:

1. fetches the merge commit and cherry-picks it onto a new branch cut from the
   target tip. The branch name carries the original number and the target
   (:func:`preloop.services.backport_branches.backport_branch_name`), so a
   retry finds the same branch;
2. on a conflict, pushes nothing, records the conflicting files, and moves on
   to the next target. Conflicts are left for a person, never resolved here;
3. on a clean pick, pushes the branch and opens one pull request whose title
   and description start from the original, with a "Backport of" line and the
   original URL, then requests review. A failed review request is recorded and
   the pull request stays.

Nothing is ever merged. A re-delivered event finds the existing branch and
pull request and updates it instead of opening a duplicate. The execution
result lists a status per target under :data:`BACKPORT_RESULT_KEY`, and one
summary comment is posted on the original pull request.

Bitbucket follows issue #955; see :mod:`preloop.services.backport_hosts`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from pydantic import ValidationError

from preloop.services.backport_branches import backport_branch_name
from preloop.services.backport_git import (
    BackportGitError,
    BackportWorkspace,
    CherryPickOutcome,
)
from preloop.services.backport_hosts import BackportHost, BackportHostError

logger = logging.getLogger(__name__)

BACKPORT_RESULT_KEY = "backport"
BACKPORT_EVENT_TYPES = frozenset({"pull_request_merged", "merge_request_merged"})
SUMMARY_COMMENT_MARKER = "<!-- preloop:backport-summary -->"

STATUS_OPENED = "opened"
STATUS_UPDATED = "updated"
STATUS_EXISTS = "exists"
STATUS_ALREADY_APPLIED = "already_applied"
STATUS_CONFLICT = "conflict"
STATUS_FAILED = "failed"
FAILING_STATUSES = frozenset({STATUS_CONFLICT, STATUS_FAILED})

MAX_TITLE_LENGTH = 250
MAX_ORIGINAL_DESCRIPTION_LENGTH = 50_000
MAX_FILES_IN_COMMENT = 20
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class BackportConfigError(ValueError):
    """The flow's backport block is present but invalid."""


class BackportEventError(ValueError):
    """The trigger event cannot be backported. The message is safe to show."""


@dataclass(frozen=True)
class BackportPlan:
    """The validated backport block of a flow."""

    source_branch: str
    target_branches: Tuple[str, ...]
    reviewers: Tuple[str, ...] = ()
    comment_on_original: bool = True


@dataclass(frozen=True)
class MergedChange:
    """The merged pull request (or merge request) that triggered the run."""

    host: str
    number: int
    url: str
    title: str
    description: str
    base_branch: str
    merge_commit_sha: str
    repository_url: str


@dataclass
class TargetResult:
    """Outcome for one target branch."""

    target_branch: str
    branch: str
    status: str
    pull_request_number: Optional[int] = None
    pull_request_url: Optional[str] = None
    conflicting_files: List[str] = field(default_factory=list)
    reviewers_requested: List[str] = field(default_factory=list)
    review_request_error: Optional[str] = None
    detail: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        """JSON shape stored on the execution result."""
        return {
            "target_branch": self.target_branch,
            "branch": self.branch,
            "status": self.status,
            "pull_request_number": self.pull_request_number,
            "pull_request_url": self.pull_request_url,
            "conflicting_files": list(self.conflicting_files),
            "reviewers_requested": list(self.reviewers_requested),
            "review_request_error": self.review_request_error,
            "detail": self.detail,
        }


@dataclass
class BackportReport:
    """Outcome of one backport run."""

    change: MergedChange
    source_branch: str
    targets: List[TargetResult]
    comment_status: str = "skipped"
    comment_error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """True when no target conflicted or failed."""
        return not any(item.status in FAILING_STATUSES for item in self.targets)

    def as_dict(self) -> Dict[str, Any]:
        """JSON shape stored under :data:`BACKPORT_RESULT_KEY`."""
        return {
            "source_branch": self.source_branch,
            "original": {
                "number": self.change.number,
                "url": self.change.url,
                "merge_commit_sha": self.change.merge_commit_sha,
            },
            "targets": [item.as_dict() for item in self.targets],
            "summary_comment": {
                "status": self.comment_status,
                "error": self.comment_error,
            },
        }


def _config_mapping(git_clone_config: Any) -> Mapping[str, Any]:
    """Accept the stored dict or the pydantic model."""
    if git_clone_config is None:
        return {}
    if isinstance(git_clone_config, Mapping):
        return git_clone_config
    dump = getattr(git_clone_config, "model_dump", None)
    return dump() if callable(dump) else {}


def resolve_backport_plan(git_clone_config: Any) -> Optional[BackportPlan]:
    """The flow's backport plan, or None when backport is not enabled.

    Args:
        git_clone_config: ``Flow.git_clone_config`` as stored, or the model.

    Returns:
        The plan when the block is present and enabled.

    Raises:
        BackportConfigError: The block is enabled but does not validate. A
            stored flow can predate a validator; failing loudly beats running
            an agent the operator did not ask for.
    """
    from preloop.models.schemas.flow import Backport

    block = _config_mapping(git_clone_config).get("backport")
    if not isinstance(block, Mapping) or not block.get("enabled"):
        return None
    try:
        parsed = Backport.model_validate(dict(block))
    except ValidationError as error:
        raise BackportConfigError(
            f"git_clone_config.backport is invalid: {error.errors()[0]['msg']}"
        ) from error
    return BackportPlan(
        source_branch=parsed.source_branch,
        target_branches=tuple(parsed.target_branches),
        reviewers=tuple(parsed.reviewers),
        comment_on_original=parsed.comment_on_original,
    )


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def event_base_branch(event_data: Mapping[str, Any]) -> Optional[str]:
    """Branch the change merged into, for a merged-change event.

    GitHub carries it as ``pull_request.base.ref`` and GitLab as
    ``object_attributes.target_branch``.
    """
    payload = _dict(event_data.get("payload"))
    pull_request = _dict(payload.get("pull_request"))
    if pull_request:
        ref = _dict(pull_request.get("base")).get("ref")
        return str(ref) if ref else None
    attributes = _dict(payload.get("object_attributes"))
    ref = attributes.get("target_branch")
    return str(ref) if ref else None


def backport_event_matches(
    git_clone_config: Any, event_data: Mapping[str, Any]
) -> bool:
    """Whether a backport flow should start for this event.

    Flows without an enabled backport block always match (the gate does not
    apply to them). A backport flow starts only for a merged pull request or
    merge request whose base branch is the configured ``source_branch``: a
    merge into any other branch does not start the flow.

    Args:
        git_clone_config: ``Flow.git_clone_config``.
        event_data: The normalized trigger event.

    Returns:
        True when the flow may start.
    """
    try:
        plan = resolve_backport_plan(git_clone_config)
    except BackportConfigError as error:
        # Never start an agent for a broken backport block, but make the
        # "flow never fires" case visible in the logs.
        logger.warning("Backport flow not started: %s", error)
        return False
    if plan is None:
        return True
    if event_data.get("type") not in BACKPORT_EVENT_TYPES:
        return False
    return event_base_branch(event_data) == plan.source_branch


def _require_https_repository(url: Any) -> str:
    text = str(url or "")
    if not text.startswith("https://") or "@" in text.split("/", 3)[2]:
        raise BackportEventError(
            "The trigger event carries no credential-free HTTPS repository URL"
        )
    return text


def extract_merged_change(event_data: Mapping[str, Any]) -> MergedChange:
    """Read the merged change from a GitHub or GitLab merge event.

    Args:
        event_data: The normalized trigger event.

    Returns:
        The merged change.

    Raises:
        BackportEventError: The event is not a merge, or lacks what a
            cherry-pick needs (number, URL, merge commit, repository).
    """
    event_type = event_data.get("type")
    payload = _dict(event_data.get("payload"))
    if event_type == "pull_request_merged":
        pr = _dict(payload.get("pull_request"))
        repository = _dict(payload.get("repository"))
        number = pr.get("number")
        url = pr.get("html_url")
        title = pr.get("title")
        description = pr.get("body")
        base = _dict(pr.get("base")).get("ref")
        sha = pr.get("merge_commit_sha")
        repository_url = repository.get("clone_url")
        host = "github"
    elif event_type == "merge_request_merged":
        attributes = _dict(payload.get("object_attributes"))
        project = _dict(payload.get("project"))
        number = attributes.get("iid")
        url = attributes.get("url")
        title = attributes.get("title")
        description = attributes.get("description")
        base = attributes.get("target_branch")
        # merge_commit_sha is absent for a fast-forward merge of a squashed
        # merge request; squash_commit_sha is then the commit on the branch.
        sha = attributes.get("merge_commit_sha") or attributes.get("squash_commit_sha")
        repository_url = project.get("git_http_url") or project.get("http_url_to_repo")
        host = "gitlab"
    else:
        raise BackportEventError(
            f"Backports start from a merged pull request, not '{event_type}'"
        )

    try:
        number = int(number)
    except (TypeError, ValueError):
        raise BackportEventError("The merge event carries no pull request number")
    if number <= 0:
        raise BackportEventError("The merge event carries no pull request number")
    sha = str(sha or "").strip().lower()
    if not _SHA_PATTERN.match(sha):
        raise BackportEventError(
            "The merge event carries no merge commit, so there is nothing to "
            "cherry-pick"
        )
    if not base:
        raise BackportEventError("The merge event does not name its base branch")
    return MergedChange(
        host=host,
        number=number,
        url=str(url or ""),
        title=str(title or f"Change {number}"),
        description=str(description or ""),
        base_branch=str(base),
        merge_commit_sha=sha,
        repository_url=_require_https_repository(repository_url),
    )


def backport_title(change: MergedChange, target_branch: str) -> str:
    """Title of the backport pull request: the original plus the target."""
    suffix = f" (backport to {target_branch})"
    title = " ".join(change.title.split())
    room = MAX_TITLE_LENGTH - len(suffix)
    if len(title) > room:
        title = title[: max(room - 3, 0)].rstrip() + "..."
    return f"{title}{suffix}"


def backport_body(change: MergedChange, target_branch: str) -> str:
    """Description of the backport pull request.

    It starts from the original description, then adds the "Backport of"
    line with the original URL and the cherry-picked commit.
    """
    original = change.description.strip()
    if len(original) > MAX_ORIGINAL_DESCRIPTION_LENGTH:
        original = (
            original[:MAX_ORIGINAL_DESCRIPTION_LENGTH].rstrip()
            + "\n\n(original description truncated)"
        )
    reference = change.url or f"#{change.number}"
    lines = [
        original,
        "",
        "---",
        "",
        f"Backport of {reference} to `{target_branch}`.",
        "",
        f"Cherry-picked from {change.merge_commit_sha} by Preloop. This pull "
        "request is never merged automatically.",
    ]
    return "\n".join(lines).lstrip()


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


_STATUS_LABELS = {
    STATUS_OPENED: "opened",
    STATUS_UPDATED: "updated",
    STATUS_EXISTS: "already exists (closed or merged), left alone",
    STATUS_ALREADY_APPLIED: "already applied, nothing to do",
    STATUS_CONFLICT: "conflict, needs a person",
    STATUS_FAILED: "failed",
}


def render_summary_comment(report: BackportReport) -> str:
    """Markdown summary posted on the original pull request."""
    change = report.change
    lines = [
        SUMMARY_COMMENT_MARKER,
        f"**Backport summary** for {change.merge_commit_sha[:12]} from "
        f"`{report.source_branch}`",
        "",
        "| Target | Status | Pull request |",
        "| --- | --- | --- |",
    ]
    for item in report.targets:
        link = item.pull_request_url or "none"
        lines.append(
            f"| `{item.target_branch}` | {_STATUS_LABELS.get(item.status, item.status)}"
            f" | {_escape_cell(link)} |"
        )
    details: List[str] = []
    for item in report.targets:
        if item.status == STATUS_CONFLICT:
            files = item.conflicting_files[:MAX_FILES_IN_COMMENT]
            shown = ", ".join(f"`{_escape_cell(name)}`" for name in files)
            more = len(item.conflicting_files) - len(files)
            if more > 0:
                shown += f" and {more} more"
            details.append(
                f"- `{item.target_branch}`: the cherry-pick conflicts in "
                f"{shown or 'unlisted files'}. Nothing was pushed. Backport "
                f"it by hand onto `{item.branch}`."
            )
        elif item.status == STATUS_FAILED:
            details.append(
                f"- `{item.target_branch}`: {_escape_cell(item.detail or 'failed')}"
            )
        if item.review_request_error:
            details.append(
                f"- `{item.target_branch}`: the review request failed "
                f"({_escape_cell(item.review_request_error)}). The pull request "
                "stays open."
            )
    if details:
        lines += ["", *details]
    return "\n".join(lines)


def summarize(report: BackportReport) -> str:
    """One line per target for the execution's output summary."""
    lines = [
        f"Backport of {report.change.url or report.change.number} "
        f"({report.change.merge_commit_sha[:12]}) from {report.source_branch}:"
    ]
    for item in report.targets:
        line = f"- {item.target_branch}: {item.status}"
        if item.pull_request_url:
            line += f" {item.pull_request_url}"
        if item.conflicting_files:
            line += " (conflicts: " + ", ".join(item.conflicting_files[:10]) + ")"
        if item.detail and item.status == STATUS_FAILED:
            line += f" ({item.detail})"
        if item.review_request_error:
            line += f" (review request failed: {item.review_request_error})"
        lines.append(line)
    lines.append(f"Summary comment: {report.comment_status}")
    return "\n".join(lines)


async def _backport_one(
    workspace: BackportWorkspace,
    host: BackportHost,
    plan: BackportPlan,
    change: MergedChange,
    target: str,
    mainline: Optional[int],
) -> TargetResult:
    """Backport onto one target. Never raises for Git or host failures."""
    branch = backport_branch_name(change.number, target)
    result = TargetResult(target_branch=target, branch=branch, status=STATUS_FAILED)
    try:
        existing = await host.find_change(branch, target)
        if existing is not None and existing.state != "open":
            # A person closed or merged it. Reopening or re-pushing would undo
            # their decision, so the target is reported and left alone.
            result.status = STATUS_EXISTS
            result.pull_request_number = existing.number
            result.pull_request_url = existing.url
            result.detail = f"The backport pull request is {existing.state}"
            return result

        branch_sha = await asyncio.to_thread(workspace.remote_branch_sha, branch)
        if branch_sha is None:
            target_sha = await asyncio.to_thread(workspace.remote_branch_sha, target)
            if target_sha is None:
                result.detail = f"Target branch {target} does not exist"
                return result
            base_ref = await asyncio.to_thread(workspace.fetch_branch, target)
            outcome: CherryPickOutcome = await asyncio.to_thread(
                lambda: workspace.cherry_pick(
                    base_ref=base_ref,
                    branch=branch,
                    sha=change.merge_commit_sha,
                    mainline=mainline,
                )
            )
            if outcome.status == "conflict":
                result.status = STATUS_CONFLICT
                result.conflicting_files = list(outcome.conflicting_files)
                result.detail = "Cherry-pick conflicts; nothing was pushed"
                return result
            if outcome.status == "empty":
                result.status = STATUS_ALREADY_APPLIED
                result.detail = f"The change is already on {target}"
                return result
            await asyncio.to_thread(workspace.push_new_branch, branch)

        title = backport_title(change, target)
        body = backport_body(change, target)
        if existing is not None:
            updated = await host.update_change(existing.number, title, body)
            result.status = STATUS_UPDATED
            result.pull_request_number = existing.number
            result.pull_request_url = existing.url or updated.url
            return result

        opened = await host.open_change(branch, target, title, body)
        result.status = STATUS_OPENED
        result.pull_request_number = opened.number
        result.pull_request_url = opened.url
        if plan.reviewers:
            try:
                await host.request_reviewers(opened.number, list(plan.reviewers))
                result.reviewers_requested = list(plan.reviewers)
            except BackportHostError as error:
                # The pull request is the deliverable; a reviewer that cannot
                # be requested is recorded, never a reason to close it.
                result.review_request_error = str(error)
        return result
    except (BackportGitError, BackportHostError) as error:
        result.status = STATUS_FAILED
        result.detail = str(error)
        return result


async def run_backport(
    plan: BackportPlan,
    change: MergedChange,
    host: BackportHost,
    *,
    token: Optional[str],
    committer_name: str,
    committer_email: str,
    allow_file_protocol: bool = False,
) -> BackportReport:
    """Backport ``change`` onto every target of ``plan``, in order.

    Args:
        plan: The flow's backport plan.
        change: The merged change from the trigger event.
        host: Code host adapter for the repository.
        token: Git credential for the repository, or None.
        committer_name: Committer of the cherry-picked commits.
        committer_email: Committer email.
        allow_file_protocol: Tests only. Permits a local ``file://`` remote.

    Returns:
        The report. Per-target failures are recorded in it, never raised.
    """
    report = BackportReport(change=change, source_branch=plan.source_branch, targets=[])
    # Cleanup must never replace the real outcome: after a cancellation the
    # worker thread may still be unwinding inside the directory.
    with tempfile.TemporaryDirectory(
        prefix="preloop-backport-", ignore_cleanup_errors=True
    ) as directory:
        opened: Optional[BackportWorkspace] = None
        try:
            try:
                workspace = BackportWorkspace(
                    Path(directory),
                    repository_url=change.repository_url,
                    token=token,
                    auth_username=host.git_auth_username,
                    committer_name=committer_name,
                    committer_email=committer_email,
                    allow_file_protocol=allow_file_protocol,
                )
                opened = workspace
                await asyncio.to_thread(workspace.init)
                await asyncio.to_thread(
                    lambda: workspace.fetch_commit(
                        change.merge_commit_sha, fallback_branch=change.base_branch
                    )
                )
                parents = await asyncio.to_thread(
                    workspace.parent_count, change.merge_commit_sha
                )
            except BackportGitError as error:
                report.targets = [
                    TargetResult(
                        target_branch=target,
                        branch=backport_branch_name(change.number, target),
                        status=STATUS_FAILED,
                        detail=str(error),
                    )
                    for target in plan.target_branches
                ]
            else:
                # A merge commit is replayed against its first parent, the
                # branch it merged into: exactly the change the PR made.
                mainline = 1 if parents > 1 else None
                for target in plan.target_branches:
                    report.targets.append(
                        await _backport_one(
                            workspace, host, plan, change, target, mainline
                        )
                    )
        finally:
            # On cancellation (the flow's timeout budget) the worker thread
            # keeps running; stop its Git child so nothing is pushed late.
            if opened is not None:
                opened.close()

    if plan.comment_on_original:
        try:
            await host.comment_on_original(
                change.number, render_summary_comment(report)
            )
            report.comment_status = "posted"
        except BackportHostError as error:
            report.comment_status = "failed"
            report.comment_error = str(error)
    return report


def agent_result_for(report: BackportReport) -> Dict[str, Any]:
    """Shape a report like an agent result for the orchestrator's terminal path.

    Args:
        report: The finished backport report.

    Returns:
        A dict with ``status``, ``output_summary``, ``error_message``,
        ``result`` and ``failure_category``.
    """
    from preloop.services.flow_failure_category import FAILURE_CATEGORY_TOOL_ERROR

    failing = [item for item in report.targets if item.status in FAILING_STATUSES]
    error_message = None
    failure_category = None
    if failing:
        parts = []
        for item in failing:
            if item.status == STATUS_CONFLICT:
                parts.append(
                    f"{item.target_branch} needs a person: cherry-pick conflicts in "
                    + ", ".join(item.conflicting_files[:10])
                )
            else:
                parts.append(f"{item.target_branch}: {item.detail or 'failed'}")
        error_message = "Backport incomplete. " + "; ".join(parts)
        if any(item.status == STATUS_FAILED for item in failing):
            failure_category = FAILURE_CATEGORY_TOOL_ERROR
    return {
        "status": "SUCCEEDED" if report.succeeded else "FAILED",
        "output_summary": summarize(report),
        "error_message": error_message,
        "failure_category": failure_category,
        "result": {BACKPORT_RESULT_KEY: report.as_dict()},
    }


def agent_result_for_error(message: str) -> Dict[str, Any]:
    """A failed agent-like result for a run that could not start backporting."""
    from preloop.services.flow_failure_category import FAILURE_CATEGORY_TOOL_ERROR

    return {
        "status": "FAILED",
        "output_summary": f"Backport did not run: {message}",
        "error_message": f"Backport did not run: {message}",
        "failure_category": FAILURE_CATEGORY_TOOL_ERROR,
        "result": {BACKPORT_RESULT_KEY: {"error": message, "targets": []}},
    }
