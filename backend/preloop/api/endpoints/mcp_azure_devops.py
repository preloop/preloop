"""Azure DevOps handlers for the pull request MCP tools.

The MCP tools ``get_pull_request``, ``add_comment``, ``update_pull_request``
and ``update_comment`` keep one signature for every provider so the reviewer
preset prompt does not fork. This module maps them onto Azure Repos pull
request threads through :class:`AzureDevOpsTracker`.

Mapping notes:

* A general comment starts a new thread. Its ``comment_id`` is the thread
  id; a reply's ``comment_id`` is ``"<thread>:<comment>"``. ``update_comment``
  accepts both forms (or a bare comment id plus ``thread_id``).
* ``in_reply_to`` names the thread (or ``"<thread>:<comment>"``) to reply in.
* Inline file-position comments are not supported in this version; their
  text is posted in the thread with the file and line prefixed.
* ``review_action`` ``approve`` and ``request_changes`` also cast the token
  owner's vote (approve, or wait for author) when the token may vote; a
  refused vote is reported as a note, never as a failed review.
* Completing (merging) or abandoning a pull request is refused. Labels,
  assignees, reviewers, draft and reactions are reported as ignored.
* Azure DevOps has no unified diff endpoint: ``changes`` lists the files of
  the latest iteration and the reviewer reads the diff from its clone.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from preloop.schemas.mcp import (
    AddCommentResponse,
    PullRequestResponse,
    UpdateCommentResponse,
    UpdatePullRequestResponse,
)
from preloop.sync.trackers.azure_devops import (
    VOTE_APPROVE,
    VOTE_NONE,
    VOTE_WAIT_FOR_AUTHOR,
    AzureDevOpsTracker,
)
from preloop.utils.azure_devops import branch_from_ref, user_name

logger = logging.getLogger(__name__)

REVIEW_ACTIONS = ("approve", "request_changes", "comment", "unapprove")
_REFUSED_STATES = (
    "closed",
    "close",
    "abandoned",
    "abandon",
    "completed",
    "complete",
    "merged",
    "merge",
)
_VOTES = {
    "approve": VOTE_APPROVE,
    "request_changes": VOTE_WAIT_FOR_AUTHOR,
    "unapprove": VOTE_NONE,
}
_STATE = {"active": "open", "completed": "merged", "abandoned": "closed"}
_SYSTEM_COMMENT_TYPES = {"system", "codeChange"}


def _pr_number(pr_number: str | int) -> int:
    """Validate and convert a pull request id.

    Raises:
        HTTPException: 400 when the value is not numeric.
    """
    try:
        return int(str(pr_number).strip().lstrip("!#"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid Azure DevOps pull request id: {pr_number}",
        ) from exc


def _comment_ref(value: str, thread_id: Optional[str] = None) -> Tuple[int, int]:
    """Parse ``"T"``, ``"T:C"`` or (``C``, thread ``T``) into ids.

    Raises:
        HTTPException: 400 for non-numeric ids.
    """
    try:
        if thread_id:
            return int(str(thread_id).strip()), int(str(value).strip())
        thread, _, comment = str(value).strip().partition(":")
        return int(thread), int(comment) if comment else 1
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Azure DevOps comment ids are '<thread>' or '<thread>:<comment>'.",
        ) from exc


def _comment_id(thread_id: int, comment_id: int) -> str:
    return str(thread_id) if comment_id == 1 else f"{thread_id}:{comment_id}"


def normalize_threads(
    threads: List[Dict[str, Any]], pr_url: str
) -> List[Dict[str, Any]]:
    """Flatten threads into the comment shape GitHub comments use.

    System threads (votes, pushes, policy) and deleted comments are dropped.
    """
    comments: List[Dict[str, Any]] = []
    for thread in threads:
        if thread.get("isDeleted"):
            continue
        thread_id = thread.get("id")
        context = thread.get("threadContext") or {}
        right = context.get("rightFileStart") or {}
        left = context.get("leftFileStart") or {}
        status = str(thread.get("status") or "").lower()
        for comment in thread.get("comments") or []:
            if comment.get("isDeleted"):
                continue
            if str(comment.get("commentType")) in _SYSTEM_COMMENT_TYPES:
                continue
            cid = comment.get("id")
            parent = comment.get("parentCommentId") or 0
            comments.append(
                {
                    "id": _comment_id(int(thread_id), int(cid)),
                    "author": user_name(comment.get("author")),
                    "body": comment.get("content") or "",
                    "created_at": comment.get("publishedDate"),
                    "updated_at": comment.get("lastUpdatedDate"),
                    "type": "review_comment"
                    if context.get("filePath")
                    else "issue_comment",
                    "path": context.get("filePath"),
                    "line": right.get("line"),
                    "old_line": left.get("line"),
                    "side": "RIGHT" if right else ("LEFT" if left else None),
                    "in_reply_to_id": (
                        _comment_id(int(thread_id), int(parent)) if parent else None
                    ),
                    "html_url": f"{pr_url}?discussionId={thread_id}",
                    "thread_id": str(thread_id),
                    "resolved": status not in ("", "active", "pending"),
                    "deleted": False,
                }
            )
    return comments


_CHANGE_STATUS = {
    "add": "added",
    "edit": "modified",
    "delete": "removed",
    "rename": "renamed",
}


def _changes(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the ``changes`` block of :class:`PullRequestResponse`."""
    files: List[Dict[str, Any]] = []
    for entry in entries:
        item = entry.get("item") or {}
        if item.get("isFolder") or item.get("gitObjectType") == "tree":
            continue
        path = str(item.get("path") or "").lstrip("/")
        if not path:
            continue
        kind = str(entry.get("changeType") or "").split(",")[0].strip().lower()
        original = str(entry.get("originalPath") or "").lstrip("/") or None
        files.append(
            {
                "filename": path,
                "previous_filename": original if original != path else None,
                "status": _CHANGE_STATUS.get(kind, kind or None),
                "additions": None,
                "deletions": None,
            }
        )
    return {
        "files_changed": len(files),
        "changed_files": files,
        "diff": None,
        "diff_note": (
            "Azure DevOps has no unified diff API. Run `git diff "
            "<target>...HEAD` in the cloned repository for line changes."
        ),
    }


async def get_pull_request(
    client: AzureDevOpsTracker,
    pr_number: str | int,
    *,
    repo_full_name: Optional[str] = None,
    include_comments: bool = True,
    include_diff: bool = True,
) -> PullRequestResponse:
    """Read a pull request with its threads and changed files.

    Args:
        client: The Azure DevOps tracker client.
        pr_number: Pull request id.
        repo_full_name: ``org/project/repo``; defaults to the client's.
        include_comments: Include thread comments.
        include_diff: Include the changed file list.

    Returns:
        The pull request in the shared MCP shape.
    """
    number = _pr_number(pr_number)
    pr = await client.get_pull_request(number, repo_full_name)
    url = client.pull_request_url(number, repo_full_name)
    comments: List[Dict[str, Any]] = []
    if include_comments:
        threads = await client.get_pull_request_threads(number, repo_full_name)
        comments = normalize_threads(threads, url)
    changes: Optional[Dict[str, Any]] = None
    if include_diff:
        entries = await client.get_pull_request_changes(number, repo_full_name)
        changes = _changes(entries)
    status = str(pr.get("status") or "").lower()
    state = _STATE.get(status, status or "open")
    reviewers = [
        name for name in (user_name(r) for r in pr.get("reviewers") or []) if name
    ]
    return PullRequestResponse(
        id=str(pr.get("pullRequestId", number)),
        number=int(pr.get("pullRequestId", number)),
        title=pr.get("title") or "",
        description=pr.get("description") or "",
        state=state,
        author=user_name(pr.get("createdBy")),
        assignees=[],
        reviewers=reviewers,
        labels=[
            str(label.get("name"))
            for label in pr.get("labels") or []
            if isinstance(label, dict) and label.get("name")
        ],
        url=url,
        source_branch=branch_from_ref(pr.get("sourceRefName")),
        target_branch=branch_from_ref(pr.get("targetRefName")),
        created_at=pr.get("creationDate"),
        updated_at=pr.get("closedDate") or pr.get("creationDate"),
        merged_at=pr.get("closedDate") if state == "merged" else None,
        head_sha=(pr.get("lastMergeSourceCommit") or {}).get("commitId"),
        merge_commit_sha=(pr.get("lastMergeCommit") or {}).get("commitId"),
        is_draft=bool(pr.get("isDraft", False)),
        comments=comments,
        changes=changes,
    )


def _inline_text(body: str, path: Optional[str], line: Optional[int]) -> str:
    """Prefix a file position, since positioned threads are not supported."""
    if not path:
        return body
    where = f"`{path}`" + (f" line {line}" if line is not None else "")
    return f"**{where}**\n\n{body}"


async def add_comment(
    client: AzureDevOpsTracker,
    pr_number: str | int,
    comment: str,
    *,
    repo_full_name: Optional[str] = None,
    path: Optional[str] = None,
    line: Optional[int] = None,
    side: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> AddCommentResponse:
    """Start a thread or reply in one.

    Args:
        client: The Azure DevOps tracker client.
        pr_number: Pull request id.
        comment: Markdown body.
        repo_full_name: ``org/project/repo``; defaults to the client's.
        path: File the comment is about (prefixed to the text).
        line: Line the comment is about (prefixed to the text).
        side: Ignored; positioned threads are not supported.
        in_reply_to: Thread (``"T"``) or comment (``"T:C"``) to reply to.

    Returns:
        The created comment.
    """
    number = _pr_number(pr_number)
    if bool(path) != (line is not None):
        raise HTTPException(
            status_code=400,
            detail="An inline comment needs both 'path' and 'line'. Omit both "
            "for a general comment.",
        )
    thread_id: Optional[int] = None
    parent: Optional[int] = None
    if in_reply_to:
        thread_id, parent = _comment_ref(in_reply_to)
    created = await client.add_pull_request_comment(
        number,
        _inline_text(comment, path, line),
        repo_full_name=repo_full_name,
        thread_id=thread_id,
        parent_comment_id=parent,
    )
    message = f"Successfully added comment to PR {number}"
    if path:
        message += (
            f" (about {path}:{line}; Azure DevOps file-position threads are not "
            "supported, so it is a general thread)"
        )
    return AddCommentResponse(
        comment_id=_comment_id(created["thread_id"], created["comment_id"]),
        status="created",
        message=message,
        url=created.get("url"),
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
                detail=f"review_comments[{idx}] must be an object",
            )
        missing = [f for f in ("path", "line", "body") if f not in rc]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"review_comments[{idx}] is missing required field(s): "
                f"{', '.join(missing)}. Each comment must have 'path', 'line', "
                "and 'body'.",
            )
        line = rc["line"]
        if isinstance(line, bool) or not str(line).strip().isdigit():
            raise HTTPException(
                status_code=400,
                detail=f"review_comments[{idx}].line must be a positive integer",
            )


async def update_pull_request(
    client: AzureDevOpsTracker,
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
    """Post a review (threads plus an optional vote) and metadata updates.

    Args:
        client: The Azure DevOps tracker client.
        pr_number: Pull request id.
        repo_full_name: ``org/project/repo``; defaults to the client's.
        title: New title.
        description: New description.
        state: Only ``open`` is accepted as a no-op; completing or abandoning
            is refused.
        assignees: Not supported; reported as ignored.
        reviewers: Not supported; reported as ignored.
        labels: Not supported; reported as ignored.
        draft: Not supported; reported as ignored.
        review_action: ``approve``, ``request_changes``, ``comment`` or
            ``unapprove``.
        review_body: Summary thread posted with the review.
        review_comments: File comments ``{path, line, body}``, posted as
            threads with the position in the text.
        add_reaction: Not supported; reported as ignored.
        remove_reaction: Not supported; reported as ignored.

    Returns:
        The update status; ``pull_request_id`` carries the summary thread id
        when one was posted.

    Raises:
        HTTPException: 400 for invalid input or a refused state change.
    """
    number = _pr_number(pr_number)
    if state is not None and state.strip().lower() in _REFUSED_STATES:
        raise HTTPException(
            status_code=400,
            detail="Preloop never completes or abandons Azure DevOps pull "
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

    # Content first, vote last: a failure part way never leaves a vote
    # without the comments that explain it.
    for rc in review_comments or []:
        line = int(str(rc["line"]).strip())
        await client.add_pull_request_comment(
            number,
            _inline_text(str(rc["body"]), str(rc["path"]), line),
            repo_full_name=repo_full_name,
        )
    if review_comments:
        actions.append(f"{len(review_comments)} file comment(s)")
        notes.append("file comments are posted as general threads")

    if action and review_body:
        created = await client.add_pull_request_comment(
            number, review_body, repo_full_name=repo_full_name
        )
        result_id = _comment_id(created["thread_id"], created["comment_id"])
        result_url = created.get("url")

    if action in _VOTES:
        voted = await client.set_vote(
            number, _VOTES[action], repo_full_name=repo_full_name
        )
        if not voted:
            notes.append("vote skipped: the token may not vote on this pull request")
    if action:
        actions.append(f"review ({action})")

    if title is not None or description is not None:
        await client.update_pull_request(
            number, repo_full_name=repo_full_name, title=title, description=description
        )
        actions.append("metadata update")

    ignored = [
        name
        for name, value in (
            ("assignees", assignees),
            ("reviewers", reviewers),
            ("labels", labels),
            ("draft", draft),
            ("add_reaction", add_reaction),
            ("remove_reaction", remove_reaction),
            ("state", state),
        )
        if value is not None
    ]
    if ignored:
        notes.append(
            f"ignored on Azure DevOps: {', '.join(ignored)} "
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
    client: AzureDevOpsTracker,
    pr_number: str | int,
    comment_id: str,
    *,
    repo_full_name: Optional[str] = None,
    body: Optional[str] = None,
    resolved: Optional[bool] = None,
    thread_id: Optional[str] = None,
) -> UpdateCommentResponse:
    """Edit a comment and resolve or reopen its thread.

    Args:
        client: The Azure DevOps tracker client.
        pr_number: Pull request id.
        comment_id: ``"T"``, ``"T:C"``, or a comment id with ``thread_id``.
        repo_full_name: ``org/project/repo``; defaults to the client's.
        body: New Markdown body.
        resolved: True to resolve the thread, False to reopen it.
        thread_id: Thread id when ``comment_id`` is a bare comment id.

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
    thread, comment = _comment_ref(comment_id, thread_id)
    actions: List[str] = []
    if body is not None:
        await client.update_pull_request_comment(
            number, thread, comment, body, repo_full_name=repo_full_name
        )
        actions.append("updated body")
    if resolved is not None:
        await client.set_thread_status(
            number, thread, resolved, repo_full_name=repo_full_name
        )
        actions.append("resolved" if resolved else "unresolved")
    return UpdateCommentResponse(
        comment_id=_comment_id(thread, comment),
        status="updated",
        message=f"Successfully {' and '.join(actions)} comment {comment_id}",
        url=f"{client.pull_request_url(number, repo_full_name)}?discussionId={thread}",
    )
