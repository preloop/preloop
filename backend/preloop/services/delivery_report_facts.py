"""PR title, body and file list for the deterministic Delivery Report (#2020).

The deterministic report makes no model call, so everything it says about a
change has to be stored before the run reads it. Two writers fill the
detail columns of :class:`preloop.models.models.IssueCostPullRequest`:

* every pull request webhook stamps the title, the body and the author's
  login on its row (:func:`details_from_webhook`);
* before a deterministic run builds the report, a bounded forge read fills
  the file list of merged PRs in the window that have none yet
  (:func:`refresh_pull_request_details`). Webhooks never carry file lists.

The report then reads the ledger alone, through
:func:`preloop.models.crud.delivery_report_policy.pull_request_facts`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

#: Bound on the stored body; the brief and summary blocks sit well inside it.
MAX_BODY_CHARS = 16000
#: Files kept per PR; ``files_changed`` keeps the forge's own count.
MAX_STORED_FILES = 300
#: Migration files keep their patch so the downgrade rule can run.
MAX_PATCH_CHARS = 8000
#: Forge reads one deterministic run may make.
MAX_REFRESH_PER_RUN = 50
#: Length of the one-paragraph summary shown in the report and the email.
SUMMARY_CHARS = 280

_MIGRATION_RE = re.compile(r"(^|/)(alembic|migrations?)/", re.IGNORECASE)
_COMMENT_BLOCK_RE = re.compile(
    r"<!--\s*(PRELOOP_[A-Z_]+)\s*-->.*?<!--\s*/\1\s*-->", re.DOTALL
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CLOSING_LINE_RE = re.compile(
    r"^\s*(part of|closes|fixes|resolves|refs?)\b[^\n]*$", re.IGNORECASE
)
_PR_URL_RE = re.compile(
    r"^https?://[^/]+/(?P<repo>[^/]+(?:/[^/]+)+?)(?:/-)?/"
    r"(?:pull|pulls|merge_requests|pull-requests)/(?P<number>\d+)"
)


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def bounded_body(body: Optional[str]) -> Optional[str]:
    """The body as stored: at most :data:`MAX_BODY_CHARS` characters."""
    if body is None:
        return None
    return body[:MAX_BODY_CHARS]


def body_summary(body: Optional[str], *, limit: int = SUMMARY_CHARS) -> Optional[str]:
    """The first prose paragraph of a PR body, for the report and the email.

    The change brief, the review summary, other HTML comments, headings and
    closing lines ("Closes #12") are skipped: the brief is parsed on its
    own, and the rest is not a description.

    Args:
        body: The PR body.
        limit: Longest summary; a longer paragraph is cut at a word.

    Returns:
        One line, or None when the body has no prose.
    """
    if not body:
        return None
    text = _COMMENT_RE.sub("", _COMMENT_BLOCK_RE.sub("", body))
    for paragraph in re.split(r"\n\s*\n", text):
        lines = [
            ln.strip()
            for ln in paragraph.splitlines()
            if ln.strip()
            and not ln.lstrip().startswith(("#", "|", "```", "![", "<"))
            and not _CLOSING_LINE_RE.match(ln)
        ]
        if not lines:
            continue
        line = " ".join(re.sub(r"^[-*+]\s+", "", ln) for ln in lines)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if len(line) <= limit:
            return line
        cut = line[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:")
        return f"{cut}…"
    return None


def stored_files(files: Any) -> Optional[List[Dict[str, Any]]]:
    """Reduce a forge file list to what the ledger keeps.

    GitHub names a file ``filename``, GitLab ``new_path`` and Bitbucket
    ``path``. Patches are dropped except on migration files, where the
    downgrade rule reads them.

    Args:
        files: The forge's ``changes.changed_files`` list.

    Returns:
        At most :data:`MAX_STORED_FILES` entries, or None when ``files`` is
        not a list.
    """
    if not isinstance(files, list):
        return None
    kept: List[Dict[str, Any]] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        name = _text(entry.get("filename") or entry.get("new_path"))
        if name is None:
            path = entry.get("path")
            if isinstance(path, Mapping):
                path = path.get("toString")
            name = _text(path)
        if name is None:
            continue
        item: Dict[str, Any] = {"filename": name}
        status = _text(entry.get("status"))
        if status is None and entry.get("new_file"):
            status = "added"
        elif status is None and entry.get("deleted_file"):
            status = "removed"
        if status:
            item["status"] = status
        for key in ("additions", "deletions"):
            value = _int(entry.get(key))
            if value is not None:
                item[key] = value
        patch = _text(entry.get("patch") or entry.get("diff"))
        if patch and _MIGRATION_RE.search(name):
            item["patch"] = patch[:MAX_PATCH_CHARS]
        kept.append(item)
        if len(kept) >= MAX_STORED_FILES:
            break
    return kept


def details_from_forge(pr: Mapping[str, Any]) -> Dict[str, Any]:
    """The detail columns from a tracker client's PR or MR response.

    Args:
        pr: ``get_pull_request`` / ``get_merge_request`` response.

    Returns:
        Only the keys the response states.
    """
    details: Dict[str, Any] = {}
    title = _text(pr.get("title"))
    if title:
        details["title"] = title[:500]
    body = pr.get("description")
    if body is None:
        body = pr.get("body")
    if isinstance(body, str):
        details["body"] = bounded_body(body)
    author = pr.get("author")
    if isinstance(author, Mapping):
        author = author.get("login") or author.get("username")
    if _text(author):
        details["author_login"] = str(author)[:255]
    changes = pr.get("changes")
    if isinstance(changes, Mapping):
        files = stored_files(changes.get("changed_files"))
        if files is not None:
            details["changed_files"] = files
            stated = _int(changes.get("files_changed"))
            details["files_changed"] = stated if stated is not None else len(files)
    return details


def details_from_webhook(event_data: Mapping[str, Any]) -> Dict[str, Any]:
    """Title, body and author login from a pull request webhook payload.

    GitHub sends ``pull_request``, GitLab ``object_attributes``, Bitbucket
    Cloud ``pullrequest``. GitLab's top-level ``user`` is whoever acted on
    the merge request, not its author, so the author is left for the forge
    read unless the attributes name one. Webhooks carry
    no file list.

    Args:
        event_data: Normalized webhook event.

    Returns:
        Only the keys the payload states.
    """
    payload = event_data.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    github = payload.get("pull_request")
    gitlab = payload.get("object_attributes")
    bitbucket = payload.get("pullrequest")
    details: Dict[str, Any] = {}
    if isinstance(github, Mapping):
        title, body = github.get("title"), github.get("body")
        user = github.get("user")
        login = user.get("login") if isinstance(user, Mapping) else None
    elif isinstance(gitlab, Mapping):
        title, body = gitlab.get("title"), gitlab.get("description")
        user = gitlab.get("author")
        login = user.get("username") if isinstance(user, Mapping) else None
    elif isinstance(bitbucket, Mapping):
        title, body = bitbucket.get("title"), bitbucket.get("description")
        user = bitbucket.get("author")
        login = (
            (user.get("nickname") or user.get("username"))
            if isinstance(user, Mapping)
            else None
        )
    else:
        return {}
    if _text(title):
        details["title"] = str(title)[:500]
    if isinstance(body, str):
        details["body"] = bounded_body(body)
    if _text(login):
        details["author_login"] = str(login)[:255]
    return details


def parse_pr_key(pr_key: str) -> Optional[tuple[str, int]]:
    """``(owner/repo, number)`` from a normalized PR URL, or None."""
    match = _PR_URL_RE.match(pr_key or "")
    if match is None:
        return None
    return match.group("repo"), int(match.group("number"))


async def _read_forge(db: Any, *, row: Any, owner: Any) -> Optional[Dict[str, Any]]:
    """Read one PR from its repository's tracker, as the account owner."""
    from preloop.api.common import get_tracker_client
    from preloop.models.crud import crud_delivery_report_policy

    parsed = parse_pr_key(row.pr_key)
    if parsed is None:
        return None
    repository, number = parsed
    found = crud_delivery_report_policy.project_for_repository(
        db, account_id=row.account_id, repository=repository
    )
    if found is None:
        return None
    project, organization, tracker = found
    tracker_type = str(getattr(tracker.tracker_type, "value", tracker.tracker_type))
    client = await get_tracker_client(organization.id, project.id, db, owner)
    response: Any = None
    if "gitlab" in tracker_type:
        response = await client.get_merge_request(str(number))
    elif tracker_type == "github":
        response = await client.get_pull_request(str(number))
    # Bitbucket returns the raw object without a file list; the diffstat
    # endpoint is separate and not read here.
    return dict(response) if isinstance(response, Mapping) else None


async def refresh_pull_request_details(
    db: Any,
    *,
    account_id: Any,
    start: Optional[datetime],
    end: datetime,
    limit: int = MAX_REFRESH_PER_RUN,
    now: Optional[datetime] = None,
) -> int:
    """Fill the file lists of merged PRs in the window that have none.

    Reads at most ``limit`` PRs from the forge with the account owner's
    access, and degrades: a PR whose repository is not synced, whose tracker
    refuses, or whose forge has no file list endpoint here keeps no file
    list, and the report shows it without file-based flags. Each read is
    committed on its own so one failure keeps the others.

    Args:
        db: Session.
        account_id: Owning account.
        start: Window start, or None.
        end: Window end.
        limit: Most forge reads this run.
        now: Clock for ``details_observed_at``.

    Returns:
        How many PRs gained details.
    """
    from preloop.models.crud import crud_account, crud_delivery_report_policy, crud_user

    rows = crud_delivery_report_policy.pull_requests_missing_files(
        db, account_id=account_id, start=start, end=end, limit=limit
    )
    if not rows:
        return 0
    account = crud_account.get(db, id=account_id)
    owner_id = getattr(account, "primary_user_id", None)
    owner = crud_user.get(db, id=owner_id) if owner_id else None
    if owner is None:
        return 0
    filled = 0
    for row in rows:
        try:
            pr = await _read_forge(db, row=row, owner=owner)
        except Exception:  # noqa: BLE001 - one unreadable PR keeps the rest
            logger.info("Could not read PR details for %s", row.pr_key, exc_info=True)
            pr = None
        details = details_from_forge(pr) if isinstance(pr, Mapping) else {}
        crud_delivery_report_policy.store_pull_request_details(
            db,
            pull=row,
            details=details,
            observed_at=now or datetime.now(timezone.utc),
        )
        db.commit()
        if "changed_files" in details:
            filled += 1
    return filled
