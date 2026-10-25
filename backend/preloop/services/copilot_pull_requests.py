"""Record pull requests opened by GitHub's Copilot coding agent.

A pull request whose author is the Copilot coding agent bot lands in the one
pull request record (``IssueCostPullRequest``) with ``origin`` ``copilot`` and
the bot's ``author_id``, so the Delivery Report treats it like any other
change. The author is matched by immutable forge user id and account type;
a login or display name that merely reads "Copilot" is not trusted.

Issue links come from closing keywords first; only when the pull request has
none does the branch name count (``copilot/fix-123`` and the generic branch
forms). Only issues already synced for the account are linked. No work item
and no responsible human is created.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_issue_cost
from preloop.services.issue_cost_rollup import (
    TriggerSubject,
    _dict,
    _resolve_key,
    _str,
    _trigger_tracker,
    _uuid,
    canonical_issue_key,
    closing_issue_keys,
    parse_trigger_subject,
)
from preloop.services.issue_references import KIND_BRANCH, extract_issue_references

logger = logging.getLogger(__name__)

# ``copilot-swe-agent[bot]``, shown as ``Copilot`` on github.com. The id is
# the stable identity; the login is display data and is not trusted.
COPILOT_AGENT_USER_IDS = frozenset({"198982749"})
COPILOT_ORIGIN = "copilot"

# Branches the agent creates for an assigned issue: ``copilot/fix-123``.
_COPILOT_BRANCH_RE = re.compile(r"^copilot/fix-(?P<number>\d{1,7})(?:[-_]|$)")


def copilot_author_id(event_data: dict[str, Any]) -> Optional[str]:
    """The Copilot agent's user id when it authored the event's pull request.

    Args:
        event_data: Normalized webhook event.

    Returns:
        The author id, or None for any other author (including an account
        whose login or name reads "Copilot" but whose id is not the agent's).
    """
    if _str(event_data.get("source")) != "github":
        return None
    payload = _dict(event_data.get("payload"))
    user = _dict(_dict(payload.get("pull_request")).get("user"))
    author_id = _str(user.get("id"))
    if author_id in COPILOT_AGENT_USER_IDS and _str(user.get("type")) == "Bot":
        return author_id
    if (_str(user.get("login")) or "").lower().startswith("copilot"):
        logger.info(
            "Ignoring a pull request author named Copilot with id %s", author_id
        )
    return None


def _forge_state(pull: dict[str, Any]) -> Optional[str]:
    if pull.get("merged") or pull.get("merged_at"):
        return "merged"
    state = _str(pull.get("state"))
    return state if state in {"open", "closed"} else None


def linked_issue_keys(subject: TriggerSubject) -> list[str]:
    """Issue keys a Copilot pull request names: closing keywords, else branch.

    Args:
        subject: The pull request subject.

    Returns:
        Canonical keys in order of first appearance.
    """
    keys = closing_issue_keys(subject)
    if keys:
        return keys
    branch = subject.pr_branch or ""
    match = _COPILOT_BRANCH_RE.match(branch)
    if match and subject.repo_path:
        key = canonical_issue_key(f"{subject.repo_path}#{match.group('number')}")
        return [key] if key else []
    for reference in extract_issue_references(
        branch=branch,
        repo_path=subject.repo_path,
        host=subject.host,
        platform=subject.platform,
        self_number=subject.pr_number,
    ):
        if reference.kind == KIND_BRANCH:
            key = canonical_issue_key(reference.key)
            if key and key not in keys:
                keys.append(key)
    return keys


def record_copilot_pull_request(
    db: Session, event_data: dict[str, Any]
) -> Optional[models.IssueCostPullRequest]:
    """Record a Copilot-authored pull request and its issue links.

    Replay safe: the row is upserted, links are a set, a merged state is
    never moved back.

    Args:
        db: Database session. The caller commits.
        event_data: Normalized webhook event.

    Returns:
        The pull request row, or None when the author is not the Copilot
        agent or the event names no pull request.
    """
    author_id = copilot_author_id(event_data)
    account_id = _uuid(event_data.get("account_id"))
    subject = parse_trigger_subject(event_data)
    if (
        author_id is None
        or account_id is None
        or subject is None
        or subject.kind != "pull_request"
        or not subject.url
    ):
        return None
    pull = _dict(_dict(event_data.get("payload")).get("pull_request"))
    row = crud_issue_cost.get_or_create_pull_request(
        db, account_id=account_id, pr_key=subject.url
    )
    merged = row.state == "merged"
    crud_issue_cost.record_pull_request_identity(
        db,
        row=row,
        origin=COPILOT_ORIGIN,
        author_id=author_id,
        head_sha=None if merged else _str(_dict(pull.get("head")).get("sha")),
        state=None if merged else _forge_state(pull),
    )
    tracker_id = _trigger_tracker(db, account_id=account_id, details=event_data)
    for key in linked_issue_keys(subject):
        target = _resolve_key(db, account_id=account_id, key=key, tracker_id=tracker_id)
        issue_id: Optional[uuid.UUID] = target.issue_id if target else None
        if issue_id is not None:
            crud_issue_cost.link_pull_request_issue(db, row=row, issue_id=issue_id)
    db.flush()
    return row
