"""Bitbucket Cloud handlers for the pull request MCP tools.

The MCP tools ``get_pull_request``, ``add_comment``, ``update_pull_request``
and ``update_comment`` keep one signature for every provider so the reviewer
preset prompt does not fork. This module maps those arguments onto the
Bitbucket Cloud REST API through :class:`BitbucketTracker`.

Mapping notes:

* ``side="LEFT"`` puts an inline comment on the old file line (Bitbucket
  ``inline.from``); ``"RIGHT"`` (the default) uses the new line (``inline.to``).
* ``in_reply_to`` becomes the Bitbucket ``parent`` comment id.
* ``review_action`` accepts ``approve``, ``request_changes`` and ``comment``
  like the other providers, plus ``unapprove`` and
  ``remove_request_changes`` to withdraw a verdict.
* A review comment with ``"task": true`` also opens a pull request task on
  that comment. Tasks are optional: a 403 is skipped with a note.
* Closing, declining or merging a pull request is refused. Labels,
  assignees, reviewers, draft and reactions have no Bitbucket equivalent in
  this tool and are reported as ignored.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from preloop.schemas.mcp import (
    AddCommentResponse,
    PullRequestResponse,
    UpdateCommentResponse,
    UpdatePullRequestResponse,
)
from preloop.sync.trackers.bitbucket import BitbucketTracker
from preloop.utils.bitbucket import user_name

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 200_000
REVIEW_ACTIONS = (
    "approve",
    "request_changes",
    "comment",
    "unapprove",
    "remove_request_changes",
)
_REFUSED_STATES = ("closed", "close", "declined", "decline", "merged", "merge")


def _href(obj: Dict[str, Any]) -> Optional[str]:
    """Return ``links.html.href`` from a Bitbucket object."""
    return ((obj.get("links") or {}).get("html") or {}).get("href")


def _pr_number(pr_number: str | int) -> int:
    """Validate and convert a pull request number.

    Raises:
        HTTPException: 400 when the value is not numeric.
    """
    try:
        return int(str(pr_number).strip().lstrip("#"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Bitbucket pull request number: {pr_number}",
        ) from exc


def _changes_from_diffstat(
    diffstat: List[Dict[str, Any]], diff: Optional[str]
) -> Dict[str, Any]:
    """Build the ``changes`` block of :class:`PullRequestResponse`."""
    files: List[Dict[str, Any]] = []
    additions = 0
    deletions = 0
    for entry in diffstat:
        added = int(entry.get("lines_added") or 0)
        removed = int(entry.get("lines_removed") or 0)
        additions += added
        deletions += removed
        new = entry.get("new") or {}
        old = entry.get("old") or {}
        files.append(
            {
                "filename": new.get("path") or old.get("path"),
                "previous_filename": (
                    old.get("path")
                    if old.get("path") and old.get("path") != new.get("path")
                    else None
                ),
                "status": entry.get("status"),
                "additions": added,
                "deletions": removed,
            }
        )
    changes: Dict[str, Any] = {
        "files_changed": len(files),
        "additions": additions,
        "deletions": deletions,
        "changed_files": files,
    }
    if diff is not None:
        truncated = len(diff) > MAX_DIFF_CHARS
        changes["diff"] = diff[:MAX_DIFF_CHARS]
        changes["diff_truncated"] = truncated
    return changes


async def get_pull_request(
    client: BitbucketTracker,
    pr_number: str | int,
    *,
    repo_full_name: Optional[str] = None,
    include_comments: bool = True,
    include_diff: bool = True,
) -> PullRequestResponse:
    """Read a pull request with its comments and diff.

    Args:
        client: The Bitbucket tracker client.
        pr_number: Pull request id.
        repo_full_name: ``workspace/repo``; defaults to the client's repository.
        include_comments: Include general and inline comments.
        include_diff: Include the diffstat and the unified diff.

    Returns:
        The pull request in the shared MCP shape.
    """
    number = _pr_number(pr_number)
    pr = await client.get_pull_request(number, repo_full_name)

    comments: List[Dict[str, Any]] = []
    if include_comments:
        raw_comments = await client.get_pull_request_comments(number, repo_full_name)
        comments = [
            client.normalize_comment(c) for c in raw_comments if not c.get("deleted")
        ]

    changes: Optional[Dict[str, Any]] = None
    if include_diff:
        diffstat = await client.get_pull_request_diffstat(number, repo_full_name)
        diff = await client.get_pull_request_diff(number, repo_full_name)
        changes = _changes_from_diffstat(diffstat, diff)

    reviewers = [
        name for name in (user_name(r) for r in pr.get("reviewers") or []) if name
    ]
    state = str(pr.get("state") or "").lower()
    return PullRequestResponse(
        id=str(pr.get("id", number)),
        number=int(pr.get("id", number)),
        title=pr.get("title") or "",
        description=pr.get("description") or "",
        state=state,
        author=user_name(pr.get("author")),
        assignees=[],
        reviewers=reviewers,
        labels=[],
        url=_href(pr) or client.pull_request_url(number, repo_full_name),
        source_branch=((pr.get("source") or {}).get("branch") or {}).get("name"),
        target_branch=((pr.get("destination") or {}).get("branch") or {}).get("name"),
        created_at=pr.get("created_on"),
        updated_at=pr.get("updated_on"),
        merged_at=pr.get("updated_on") if state == "merged" else None,
        is_draft=bool(pr.get("draft", False)),
        comments=comments,
        changes=changes,
    )


async def add_comment(
    client: BitbucketTracker,
    pr_number: str | int,
    comment: str,
    *,
    repo_full_name: Optional[str] = None,
    path: Optional[str] = None,
    line: Optional[int] = None,
    side: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> AddCommentResponse:
    """Post a general, inline or reply comment.

    Args:
        client: The Bitbucket tracker client.
        pr_number: Pull request id.
        comment: Markdown body.
        repo_full_name: ``workspace/repo``; defaults to the client's repository.
        path: File path for an inline comment.
        line: Line number for an inline comment.
        side: ``"LEFT"`` for the old file, ``"RIGHT"`` (default) for the new.
        in_reply_to: Comment id to reply to.

    Returns:
        The created comment.
    """
    number = _pr_number(pr_number)
    new_line: Optional[int] = None
    old_line: Optional[int] = None
    if path and line is not None:
        if (side or "RIGHT") == "LEFT":
            old_line = line
        else:
            new_line = line
    parent_id: Optional[int] = None
    if in_reply_to:
        try:
            parent_id = int(str(in_reply_to).strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"in_reply_to must be a Bitbucket comment id: {in_reply_to}",
            ) from exc

    created = await client.add_pull_request_comment(
        number,
        comment,
        repo_full_name=repo_full_name,
        path=path if line is not None else None,
        line=new_line,
        old_line=old_line,
        parent_id=parent_id,
    )
    where = f" at {path}:{line}" if path and line is not None else ""
    return AddCommentResponse(
        comment_id=str(created.get("id", "")),
        status="created",
        message=f"Successfully added comment to PR {number}{where}",
        url=_href(created),
    )


def _validate_review_comments(review_comments: List[Any]) -> None:
    """Check each review comment has ``path``, ``line`` and ``body``.

    Raises:
        HTTPException: 400 on the first malformed entry.
    """
    for idx, rc in enumerate(review_comments):
        if not isinstance(rc, dict):
            raise HTTPException(
                status_code=400,
                detail=f"review_comments[{idx}] must be an object, "
                f"got {type(rc).__name__}",
            )
        missing = [f for f in ("path", "line", "body") if f not in rc]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"review_comments[{idx}] is missing required "
                f"field(s): {', '.join(missing)}. "
                "Each comment must have 'path', 'line', and 'body'.",
            )


async def update_pull_request(
    client: BitbucketTracker,
    pr_number: str | int,
    *,
    repo_full_name: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    state: Optional[str] = None,
    assignees: Optional[List[str]] = None,
    reviewers: Optional[List[str]] = None,
    labels: Optional[List[str]] = None,
    draft: Optional[bool] = None,
    review_action: Optional[str] = None,
    review_body: Optional[str] = None,
    review_comments: Optional[List[Dict[str, Any]]] = None,
    add_reaction: Optional[str] = None,
    remove_reaction: Optional[str] = None,
) -> UpdatePullRequestResponse:
    """Apply a review verdict, review comments and metadata updates.

    Args:
        client: The Bitbucket tracker client.
        pr_number: Pull request id.
        repo_full_name: ``workspace/repo``; defaults to the client's repository.
        title: New title.
        description: New description.
        state: Only ``open`` is accepted as a no-op. Close, decline and merge
            are refused.
        assignees: Not supported; reported as ignored.
        reviewers: Not supported; reported as ignored.
        labels: Not supported; reported as ignored.
        draft: Not supported; reported as ignored.
        review_action: ``approve``, ``request_changes``, ``comment``,
            ``unapprove`` or ``remove_request_changes``.
        review_body: Summary comment posted with the verdict.
        review_comments: Inline comments ``{path, line, body, side, task}``.
        add_reaction: Not supported; reported as ignored.
        remove_reaction: Not supported; reported as ignored.

    Returns:
        The update status.

    Raises:
        HTTPException: 400 for invalid input or a refused state change.
    """
    number = _pr_number(pr_number)
    if state is not None and state.strip().lower() in _REFUSED_STATES:
        raise HTTPException(
            status_code=400,
            detail="Preloop never closes, declines or merges Bitbucket pull "
            "requests. Leave the decision to a person.",
        )
    if review_comments and not review_action:
        raise HTTPException(
            status_code=400,
            detail="review_comments requires review_action to be set. "
            "Provide review_action='comment' (or 'approve'/'request_changes') "
            "along with review_comments.",
        )
    action = review_action.lower() if review_action else None
    if action is not None and action not in REVIEW_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid review_action: {review_action}. Must be one of "
            f"{', '.join(REVIEW_ACTIONS)}.",
        )
    if action == "comment" and not review_body and not review_comments:
        raise HTTPException(
            status_code=400,
            detail="review_action='comment' requires either review_body "
            "or review_comments to be provided.",
        )
    if review_comments:
        _validate_review_comments(review_comments)

    actions: List[str] = []
    notes: List[str] = []
    result_id: Optional[str] = None
    result_url: Optional[str] = None

    if action == "approve":
        await client.set_approval(number, True, repo_full_name=repo_full_name)
        actions.append("review (approve)")
    elif action == "unapprove":
        await client.set_approval(number, False, repo_full_name=repo_full_name)
        actions.append("review (unapprove)")
    elif action == "request_changes":
        await client.set_changes_requested(number, True, repo_full_name=repo_full_name)
        actions.append("review (request_changes)")
    elif action == "remove_request_changes":
        await client.set_changes_requested(number, False, repo_full_name=repo_full_name)
        actions.append("review (remove_request_changes)")
    elif action == "comment":
        actions.append("review (comment)")

    if action and review_body:
        created = await client.add_pull_request_comment(
            number, review_body, repo_full_name=repo_full_name
        )
        result_id = str(created.get("id", "")) or None
        result_url = _href(created)

    tasks_skipped = 0
    for rc in review_comments or []:
        side = str(rc.get("side") or "RIGHT").upper()
        line = int(rc["line"])
        created = await client.add_pull_request_comment(
            number,
            rc["body"],
            repo_full_name=repo_full_name,
            path=rc["path"],
            line=None if side == "LEFT" else line,
            old_line=line if side == "LEFT" else None,
        )
        if rc.get("task"):
            task = await client.create_pull_request_task(
                number,
                rc.get("task_text") or rc["body"],
                repo_full_name=repo_full_name,
                comment_id=created.get("id"),
            )
            if task is None:
                tasks_skipped += 1
    if review_comments:
        actions.append(f"{len(review_comments)} inline comment(s)")
    if tasks_skipped:
        notes.append(
            f"{tasks_skipped} task(s) skipped: the token may not create "
            "pull request tasks"
        )

    if title is not None or description is not None:
        updated = await client.update_pull_request(
            number,
            repo_full_name=repo_full_name,
            title=title,
            description=description,
        )
        actions.append("metadata update")
        result_url = result_url or _href(updated)

    ignored = [
        name
        for name, value in (
            ("assignees", assignees),
            ("reviewers", reviewers),
            ("labels", labels),
            ("draft", draft),
            ("add_reaction", add_reaction),
            ("remove_reaction", remove_reaction),
        )
        if value is not None
    ]
    if state is not None:
        ignored.append("state")
    if ignored:
        notes.append(
            f"ignored on Bitbucket Cloud: {', '.join(ignored)} "
            "(not supported by this tool)"
        )

    if actions:
        message = f"Successfully performed {', '.join(actions)} on PR {number}"
    else:
        message = f"No actions completed on PR {number}"
    if notes:
        message += f". Note: {'; '.join(notes)}"

    return UpdatePullRequestResponse(
        pull_request_id=result_id or str(number),
        status="updated" if actions else "failed",
        message=message,
        url=result_url or client.pull_request_url(number, repo_full_name),
    )


async def update_comment(
    client: BitbucketTracker,
    pr_number: str | int,
    comment_id: str,
    *,
    repo_full_name: Optional[str] = None,
    body: Optional[str] = None,
    resolved: Optional[bool] = None,
    thread_id: Optional[str] = None,
) -> UpdateCommentResponse:
    """Edit a comment body and resolve or reopen its thread.

    Args:
        client: The Bitbucket tracker client.
        pr_number: Pull request id.
        comment_id: Comment id.
        repo_full_name: ``workspace/repo``; defaults to the client's repository.
        body: New Markdown body.
        resolved: True to resolve the thread, False to reopen it.
        thread_id: Top-level comment id of the thread; defaults to
            ``comment_id``. Bitbucket resolves threads on the top comment.

    Returns:
        The update status.
    """
    number = _pr_number(pr_number)
    if body is None and resolved is None:
        raise HTTPException(
            status_code=400,
            detail="Provide 'body' to edit the comment or 'resolved' to "
            "resolve or reopen its thread.",
        )
    try:
        int(comment_id)
        if thread_id:
            int(thread_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Bitbucket comment and thread ids are numeric.",
        ) from exc

    actions: List[str] = []
    url: Optional[str] = None
    if body is not None:
        updated = await client.update_pull_request_comment(
            number, comment_id, body, repo_full_name=repo_full_name
        )
        url = _href(updated)
        actions.append("updated body")
    if resolved is not None:
        await client.set_comment_resolved(
            number,
            thread_id or comment_id,
            resolved,
            repo_full_name=repo_full_name,
        )
        actions.append("resolved" if resolved else "unresolved")
    return UpdateCommentResponse(
        comment_id=comment_id,
        status="updated",
        message=f"Successfully {' and '.join(actions)} comment {comment_id}",
        url=url,
    )
