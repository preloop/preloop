"""Review publication gate: a review flow succeeds only with a delivered review.

EE #202. A pull request review flow whose ``git_clone_config`` sets
``review_publication.required`` finishes SUCCEEDED only when the MCP
``update_pull_request`` tool recorded a receipt for a review the code host
accepted, for this execution and the triggering pull request. The agent's
exit code, success sentinel and ``review_posted`` field are claims; the
receipt is the evidence.

A deliberate no-post run (``review_action`` explicitly null, no
``blocker``) passes only when an earlier run of the account already holds a
receipt for the same pull request and head commit.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlparse

#: Key of the stored result the control plane owns.
REVIEW_PUBLICATION_RESULT_KEY = "review_publication"
#: Reason prefix written to ``error_message`` when the gate fails a run.
REVIEW_PUBLICATION_INCOMPLETE = "review_publication_incomplete"

STATUS_PUBLISHED = "published"
STATUS_ALREADY_REVIEWED = "already_reviewed"
STATUS_NOT_PUBLISHED = "not_published"

REASON_NO_RECEIPT = "no_receipt"
REASON_OTHER_TARGET = "receipt_for_other_pull_request"
REASON_OTHER_HEAD = "receipt_for_other_head"
REASON_LOOKUP_FAILED = "receipt_lookup_failed"

_GITHUB_PR = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)(?:/.*)?$")
_GITLAB_MR = re.compile(r"^/(.+?)/-/merge_requests/(\d+)(?:/.*)?$")
_BITBUCKET_PR = re.compile(r"^/([^/]+)/([^/]+)/pull-requests/(\d+)(?:/.*)?$")


def _config_block(git_clone_config: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(git_clone_config, Mapping):
        return None
    block = git_clone_config.get("review_publication")
    return block if isinstance(block, Mapping) else None


def review_publication_required(
    git_clone_config: Any, preset_git_clone_config: Any = None
) -> bool:
    """Whether the flow requires a delivered review.

    The flow's own ``review_publication`` block decides when it has one, so
    an owner can opt out with ``required: false``. A flow saved before the
    block existed inherits it from its source preset, which is how existing
    reviewer flows receive the contract without a data migration.
    """
    own = _config_block(git_clone_config)
    if own is not None:
        return own.get("required") is True
    inherited = _config_block(preset_git_clone_config)
    return inherited is not None and inherited.get("required") is True


def normalize_repository(value: Any) -> str:
    """Lower-cased repository path; matches the receipt CRUD normalization."""
    return str(value or "").strip().strip("/").lower()


def parse_pull_request_url(url: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(repository, number)`` from a GitHub, GitLab or Bitbucket PR URL."""
    if not isinstance(url, str) or not url.strip():
        return None, None
    path = urlparse(url.strip()).path.rstrip("/")
    for pattern in (_GITLAB_MR, _BITBUCKET_PR, _GITHUB_PR):
        match = pattern.match(path)
        if match:
            groups = match.groups()
            repository = "/".join(groups[:-1])
            return normalize_repository(repository), groups[-1]
    return None, None


def trigger_pull_request(
    trigger_event_data: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """The triggering pull request's ``(repository, number)``, when known."""
    if not isinstance(trigger_event_data, Mapping):
        return None, None
    payload = trigger_event_data.get("payload")
    if not isinstance(payload, Mapping):
        return None, None
    candidates = []
    attrs = payload.get("object_attributes")
    if isinstance(attrs, Mapping):
        candidates.append(attrs.get("url"))
    pull = payload.get("pull_request")
    if isinstance(pull, Mapping):
        candidates.append(pull.get("html_url"))
    for url in candidates:
        repository, number = parse_pull_request_url(url)
        if repository and number:
            return repository, number
    return None, None


def _deliberate_skip(artifact: Any) -> bool:
    """The preset's explicit no-post decision, with no reported blocker."""
    return (
        isinstance(artifact, Mapping)
        and "review_action" in artifact
        and artifact["review_action"] is None
        and not artifact.get("blocker")
        and artifact.get("review_posted") is not True
        and artifact.get("review_posted_to_github") is not True
    )


def evaluate_review_publication(
    receipts: Iterable[Any],
    *,
    repository: Optional[str],
    pull_request: Optional[str],
    head_sha: Optional[str],
    artifact: Any,
    find_prior_receipt: Callable[[str, str, str], Any],
) -> Dict[str, Any]:
    """Decide whether the run delivered its review.

    Args:
        receipts: This execution's receipts.
        repository: Triggering repository, or None when unknown.
        pull_request: Triggering PR number, or None when unknown.
        head_sha: Triggering head commit, or None when unknown.
        artifact: The agent's result document (claims only).
        find_prior_receipt: ``(repository, pull_request, head_sha) -> row``
            for a receipt any run recorded on that target.

    Returns:
        ``{"status": ..., "reason": ..., ...}`` for the stored result.
    """
    head = (head_sha or "").lower() or None
    reason = REASON_NO_RECEIPT
    for receipt in receipts:
        same_target = repository is None or (
            normalize_repository(getattr(receipt, "repository", None)) == repository
            and str(getattr(receipt, "pull_request", "")) == str(pull_request)
        )
        if not same_target:
            reason = REASON_OTHER_TARGET
            continue
        receipt_head = (getattr(receipt, "head_sha", None) or "").lower() or None
        if head and receipt_head and receipt_head != head:
            if reason == REASON_NO_RECEIPT:
                reason = REASON_OTHER_HEAD
            continue
        return {
            "status": STATUS_PUBLISHED,
            "receipt_id": str(getattr(receipt, "id", "")) or None,
            "review_action": getattr(receipt, "review_action", None),
            "provider": getattr(receipt, "provider", None),
            "head_sha": receipt_head,
        }
    if (
        reason == REASON_NO_RECEIPT
        and _deliberate_skip(artifact)
        and repository
        and pull_request
        and head
    ):
        prior = find_prior_receipt(repository, pull_request, head)
        if prior is not None:
            return {
                "status": STATUS_ALREADY_REVIEWED,
                "receipt_id": str(getattr(prior, "id", "")) or None,
                "head_sha": head,
            }
    return {"status": STATUS_NOT_PUBLISHED, "reason": reason}
