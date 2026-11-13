"""Review publication gate: a review flow succeeds only with a delivered review.

EE #202. A pull request review flow whose own ``git_clone_config`` sets
``review_publication.required`` finishes SUCCEEDED only when the MCP
``update_pull_request`` tool recorded a receipt for a review the code host
accepted, for this execution and the triggering pull request. The agent's
exit code, success sentinel and ``review_posted`` field are claims; the
receipt is the evidence.

The requirement is a per-flow opt-in and is never inherited from the flow's
source preset. Production incident 2026-11-04: the deploy-time preset sync
switched the requirement on for every flow derived from the Pull Request
Reviewer preset while the API still ran a build without the receipt-writing
tool, so each of those runs delivered its review and was then failed with
``no_receipt``. A stored catalog row must not be able to enable a fail-closed
gate for flows nobody opted in.

A deliberate no-post run (``review_action`` explicitly null, no
``blocker``) passes only when an earlier run of the account already holds a
receipt for the same pull request and the same head commit. Receipts with no
head (GitLab and Bitbucket do not report one) never satisfy a skip: they
cannot show that this head was reviewed, so on those providers the reviewer
must post again.

Version skew (#2208): the gate runs in the worker and the receipt is written
by the API. When the execution holds no receipt at all but the server-recorded
tool activity shows a succeeded ``update_pull_request`` review call for the
triggering pull request, the outcome is ``unverified`` with reason
``receipt_unavailable`` and the run is not failed. ``unverified`` is never
reported as ``published``. A receipt for another pull request or head still
fails the run.

When the trigger names no pull request the gate accepts any receipt of this
execution; it still requires a review the code host accepted.
A trigger that names a pull request must also identify its head; otherwise
the gate fails closed instead of accepting a receipt for an unknown revision.
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
#: No receipt, but server-recorded tool activity shows the review was
#: delivered. The run is not failed; the outcome is never "published".
STATUS_UNVERIFIED = "unverified"

REASON_NO_RECEIPT = "no_receipt"
REASON_RECEIPT_UNAVAILABLE = "receipt_unavailable"
REASON_OTHER_TARGET = "receipt_for_other_pull_request"
REASON_OTHER_HEAD = "receipt_for_other_head"
REASON_LOOKUP_FAILED = "receipt_lookup_failed"
REASON_TRIGGER_HEAD_MISSING = "trigger_head_missing"

_GITHUB_PR = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)(?:/.*)?$")
_GITLAB_MR = re.compile(r"^/(.+?)/-/merge_requests/(\d+)(?:/.*)?$")
_BITBUCKET_PR = re.compile(r"^/([^/]+)/([^/]+)/pull-requests/(\d+)(?:/.*)?$")


def _config_block(git_clone_config: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(git_clone_config, Mapping):
        return None
    block = git_clone_config.get("review_publication")
    return block if isinstance(block, Mapping) else None


def review_publication_required(git_clone_config: Any) -> bool:
    """Whether the flow requires a delivered review.

    Only the flow's own ``review_publication`` block decides: ``required`` must
    be exactly ``True``. A flow without the block is not gated, whatever its
    source preset says, so a preset catalog refresh cannot opt flows in.
    """
    own = _config_block(git_clone_config)
    return own is not None and own.get("required") is True


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
    # Bitbucket Cloud and Data Center send ``pullrequest``.
    bitbucket = payload.get("pullrequest")
    if isinstance(bitbucket, Mapping):
        links = bitbucket.get("links")
        html = links.get("html") if isinstance(links, Mapping) else None
        if isinstance(html, Mapping):
            candidates.append(html.get("href"))
        elif isinstance(html, list) and html and isinstance(html[0], Mapping):
            candidates.append(html[0].get("href"))
    for url in candidates:
        repository, number = parse_pull_request_url(url)
        if repository and number:
            return repository, number
    if isinstance(bitbucket, Mapping) and bitbucket.get("id") is not None:
        repo = payload.get("repository")
        full_name = repo.get("full_name") if isinstance(repo, Mapping) else None
        if isinstance(full_name, str) and full_name.strip():
            return normalize_repository(full_name), str(bitbucket["id"])
    return None, None


def delivered_review_activity(
    activities: Iterable[Any],
    *,
    repository: Optional[str],
    pull_request: Optional[str],
) -> Optional[Any]:
    """The first succeeded review call for the triggering PR, from tool activity.

    Version skew tolerance (#2208): a worker on a newer build than the API
    finds no receipt when the API's ``update_pull_request`` tool does not
    write one yet, although the code host accepted the review. The gateway's
    own activity row for that call is server-recorded evidence the agent
    cannot forge. It counts only when the call succeeded, carried a review
    action, and its classified reference names the triggering repository and
    pull request number; a bare number with no repository never matches.

    Args:
        activities: The execution's recorded tool calls (``tool_name``,
            ``status`` and ``metadata_["work_ref"]``).
        repository: Triggering repository, normalized.
        pull_request: Triggering PR number.

    Returns:
        The matching activity row, or None.
    """
    if not repository or not pull_request:
        return None
    for activity in activities:
        if getattr(activity, "tool_name", None) != "update_pull_request":
            continue
        if str(getattr(activity, "status", "") or "").lower() != "succeeded":
            continue
        metadata = getattr(activity, "metadata_", None)
        work_ref = metadata.get("work_ref") if isinstance(metadata, Mapping) else None
        if not isinstance(work_ref, Mapping) or work_ref.get("action") != "reviewed":
            continue
        ref = str(work_ref.get("ref") or "")
        number = ref[1:] if ref[:1] in {"#", "!"} else ""
        if (
            normalize_repository(work_ref.get("repo")) == repository
            and number.isdigit()
            and str(int(number)) == str(pull_request).strip()
        ):
            return activity
    return None


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
    list_review_activity: Optional[Callable[[], Iterable[Any]]] = None,
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
        list_review_activity: Lazily reads this execution's server-recorded
            tool calls. Consulted only when the execution has no receipt;
            a succeeded review call for the triggering PR then yields
            ``unverified`` / ``receipt_unavailable`` instead of a failure.

    Returns:
        ``{"status": ..., "reason": ..., ...}`` for the stored result.
    """
    head = (head_sha or "").lower() or None
    if repository and pull_request and not head:
        return {"status": STATUS_NOT_PUBLISHED, "reason": REASON_TRIGGER_HEAD_MISSING}
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
    if reason == REASON_NO_RECEIPT and list_review_activity is not None:
        # Only when this execution holds no receipt at all: a receipt for
        # another pull request or head is evidence of the wrong review and
        # still fails the run.
        activity = delivered_review_activity(
            list_review_activity(), repository=repository, pull_request=pull_request
        )
        if activity is not None:
            work_ref = (getattr(activity, "metadata_", None) or {}).get("work_ref")
            return {
                "status": STATUS_UNVERIFIED,
                "reason": REASON_RECEIPT_UNAVAILABLE,
                "activity_id": str(getattr(activity, "id", "")) or None,
                "review_state": (
                    work_ref.get("review_state")
                    if isinstance(work_ref, Mapping)
                    else None
                ),
                "head_sha": head,
            }
    return {"status": STATUS_NOT_PUBLISHED, "reason": reason}
