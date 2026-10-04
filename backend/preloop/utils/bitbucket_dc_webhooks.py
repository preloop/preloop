"""Bitbucket Data Center 10.2 LTS webhook contract.

Pure helpers for the ``bitbucket_dc`` webhook intake: the signature check,
the subscribed event list, payload validation against the bound instance and
repository, normalization into Preloop's review and feedback trigger
contract, and the stable delivery identity used for replay safety. Nothing
here touches the database or the network.

Contract, from the Bitbucket Data Center 10.2 documentation ("Manage
webhooks" and "Event payload"):

* A repository webhook created with a secret signs the raw request body with
  HMAC-SHA256 and sends ``X-Hub-Signature: sha256=<hex>``. Preloop always
  configures a secret, so an unsigned delivery is rejected; there is no
  unsigned fallback.
* ``X-Event-Key`` names the event and repeats the payload ``eventKey``.
* ``X-Request-Id`` identifies the delivery.
* The protocol defines no signed timestamp, so no freshness window is
  enforced. Replays are made harmless by the delivery identity instead.
* Payloads use Data Center shapes (``pullRequest`` with ``fromRef`` and
  ``toRef``, integer repository ids, ``links.self``). Bitbucket Cloud event
  names and payloads (``pullrequest:created``, ``pullrequest``,
  ``full_name``) are not accepted here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from preloop.utils.bitbucket_dc import (
    InstanceIdentity,
    looks_like_cloud_payload,
    normalize_comment,
    pull_request_web_url,
    ref_branch,
    user_name,
)

BITBUCKET_DC_SIGNATURE_HEADER = "X-Hub-Signature"
BITBUCKET_DC_EVENT_HEADER = "X-Event-Key"
BITBUCKET_DC_DELIVERY_HEADER = "X-Request-Id"

# Bodies larger than this are refused before they are read in full. Data
# Center payloads for the selected events are a few kilobytes.
BITBUCKET_DC_WEBHOOK_MAX_BYTES = 1024 * 1024

BITBUCKET_DC_PING_EVENT = "diagnostics:ping"

# Data Center event key -> normalized Preloop event type. These are the
# events the review and feedback triggers consume; the registration and the
# administrator instructions subscribe to exactly this list.
BITBUCKET_DC_EVENT_MAP: Dict[str, str] = {
    "repo:refs_changed": "push",
    "pr:opened": "pull_request_opened",
    "pr:from_ref_updated": "pull_request_updated",
    "pr:modified": "pull_request_updated",
    "pr:reviewer:approved": "pull_request_approved",
    "pr:reviewer:unapproved": "pull_request_unapproved",
    "pr:reviewer:needs_work": "pull_request_changes_requested",
    "pr:comment:added": "comment_created",
    "pr:comment:edited": "comment_updated",
    "pr:comment:deleted": "comment_deleted",
    "pr:merged": "pull_request_merged",
    "pr:declined": "pull_request_closed",
    "pr:deleted": "pull_request_closed",
}

BITBUCKET_DC_WEBHOOK_EVENTS: tuple[str, ...] = tuple(BITBUCKET_DC_EVENT_MAP)

# Name of the hook Preloop creates; registration only ever touches hooks
# that point at Preloop's callback URL.
BITBUCKET_DC_WEBHOOK_NAME = "Preloop"

_SIGNATURE_RE = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
_COMMENT_EVENTS = frozenset(
    {"pr:comment:added", "pr:comment:edited", "pr:comment:deleted"}
)
_VERDICT_EVENTS = frozenset(
    {"pr:reviewer:approved", "pr:reviewer:unapproved", "pr:reviewer:needs_work"}
)

# Reasons returned to the provider and logged. Fixed strings only: nothing
# from the payload or the secret is echoed back.
REASON_PING = "ping"
REASON_UNSUPPORTED_EVENT = "unsupported_event"
REASON_HEAD_UNCHANGED = "head_unchanged"


class BitbucketDCWebhookRejectedError(ValueError):
    """A delivery that must not be processed.

    Attributes:
        status_code: HTTP status to answer with.
        reason: Fixed, sanitized reason code.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass
class BitbucketDCDelivery:
    """Outcome of normalizing one authenticated delivery.

    Attributes:
        accepted: True when the delivery should be dispatched.
        reason: Sanitized reason when it is acknowledged without dispatch.
        event_key: The Data Center event key.
        event_type: Normalized Preloop event type, when mapped.
        payload: Normalized payload handed to the trigger pipeline.
        repository_id: Immutable id of the repository the event is about.
    """

    accepted: bool
    event_key: str
    reason: Optional[str] = None
    event_type: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    repository_id: Optional[int] = None


def generate_webhook_secret() -> str:
    """Return a new random per-tracker webhook secret."""
    return secrets.token_urlsafe(32)


def compute_webhook_signature(secret: str, body: bytes) -> str:
    """Return the ``X-Hub-Signature`` value Data Center sends for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(
    secret: Optional[str], body: bytes, header_value: Optional[str]
) -> bool:
    """Check a Data Center webhook signature over the raw body.

    The comparison is constant time and runs before the body is parsed.

    Args:
        secret: The tracker's current webhook secret.
        body: The raw request bytes, exactly as received.
        header_value: The ``X-Hub-Signature`` header.

    Returns:
        False when the secret or header is missing, malformed or wrong.
    """
    if not secret or not header_value:
        return False
    match = _SIGNATURE_RE.match(header_value.strip())
    if not match:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1).lower())


def webhook_callback_path(tracker_id: Any) -> str:
    """Path of the intake endpoint for one tracker, below ``/api/v1``."""
    return f"/private/webhooks/bitbucket_dc/{tracker_id}"


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _self_links(obj: Any) -> List[str]:
    links = _dig(obj, "links", "self")
    hrefs: List[str] = []
    if isinstance(links, list):
        for link in links:
            href = link.get("href") if isinstance(link, Mapping) else None
            if isinstance(href, str) and href:
                hrefs.append(href)
    return hrefs


def _actor(user: Any) -> Dict[str, Any]:
    """Public identity of a user, without the e-mail address."""
    if not isinstance(user, Mapping):
        return {}
    return {
        key: user.get(key)
        for key in ("id", "name", "slug", "displayName", "type")
        if user.get(key) is not None
    }


def _repository(repo: Mapping[str, Any]) -> Dict[str, Any]:
    project = repo.get("project") if isinstance(repo.get("project"), Mapping) else {}
    key = str(project.get("key") or "")
    slug = str(repo.get("slug") or "")
    return {
        "id": repo.get("id"),
        "slug": slug,
        "name": repo.get("name"),
        "path": f"{key}/{slug}" if key and slug else None,
        "project": {"key": key or None, "id": project.get("id")},
    }


def _check_instance(instance: InstanceIdentity, *objects: Any) -> None:
    for obj in objects:
        for href in _self_links(obj):
            if not instance.owns_url(href):
                raise BitbucketDCWebhookRejectedError(403, "instance_mismatch")


def normalize_delivery(
    event_key: Optional[str],
    payload: Any,
    *,
    instance: InstanceIdentity,
    bound_repository_id: Optional[int] = None,
    bound_project_key: Optional[str] = None,
    repository_known: Optional[Callable[[int], bool]] = None,
    self_user_slug: Optional[str] = None,
) -> BitbucketDCDelivery:
    """Validate an authenticated delivery and map it to the trigger contract.

    Args:
        event_key: ``X-Event-Key`` header.
        payload: Parsed JSON body.
        instance: The tracker's approved instance.
        bound_repository_id: Immutable repository id the tracker is bound to.
        bound_project_key: Project key the tracker is bound to.
        repository_known: Answers whether a repository id was imported for
            this tracker. Required for trackers not bound to one repository;
            a delivery for an unknown repository is rejected.
        self_user_slug: User slug Preloop acts as on this instance.

    Returns:
        The delivery outcome.

    Raises:
        BitbucketDCWebhookRejectedError: For malformed payloads, Cloud payloads and
            deliveries about another instance or repository.
    """
    key = (event_key or "").strip()
    if not key:
        raise BitbucketDCWebhookRejectedError(400, "missing_event_key")
    if not isinstance(payload, Mapping):
        raise BitbucketDCWebhookRejectedError(400, "invalid_payload")
    body_key = payload.get("eventKey")
    if body_key is not None and body_key != key:
        raise BitbucketDCWebhookRejectedError(400, "event_key_mismatch")
    if key == BITBUCKET_DC_PING_EVENT:
        return BitbucketDCDelivery(accepted=False, event_key=key, reason=REASON_PING)
    event_type = BITBUCKET_DC_EVENT_MAP.get(key)
    if event_type is None:
        return BitbucketDCDelivery(
            accepted=False, event_key=key, reason=REASON_UNSUPPORTED_EVENT
        )
    if "pullrequest" in payload or looks_like_cloud_payload(payload.get("repository")):
        raise BitbucketDCWebhookRejectedError(400, "cloud_payload")

    pr = payload.get("pullRequest")
    if key.startswith("pr:"):
        if not isinstance(pr, Mapping) or _int(pr.get("id")) is None:
            raise BitbucketDCWebhookRejectedError(400, "missing_pull_request")
        if looks_like_cloud_payload(pr):
            raise BitbucketDCWebhookRejectedError(400, "cloud_payload")
        repo = _dig(pr, "toRef", "repository")
    else:
        pr = None
        repo = payload.get("repository")
    if not isinstance(repo, Mapping) or _int(repo.get("id")) is None:
        raise BitbucketDCWebhookRejectedError(400, "missing_repository")
    repo_id = int(repo["id"])
    project_key = str(_dig(repo, "project", "key") or "")

    if bound_repository_id is not None and repo_id != int(bound_repository_id):
        raise BitbucketDCWebhookRejectedError(403, "repository_mismatch")
    if bound_project_key and project_key.casefold() != bound_project_key.casefold():
        raise BitbucketDCWebhookRejectedError(403, "repository_mismatch")
    if bound_repository_id is None and (
        repository_known is None or not repository_known(repo_id)
    ):
        raise BitbucketDCWebhookRejectedError(403, "repository_mismatch")
    _check_instance(instance, repo, pr, payload.get("repository"))

    actor = _actor(payload.get("actor"))
    actor_slug = user_name(actor)
    self_generated = bool(
        self_user_slug
        and actor_slug
        and str(actor_slug).casefold() == self_user_slug.casefold()
    )
    repository = _repository(repo)
    dc: Dict[str, Any] = {
        "event_key": key,
        "instance": instance.base_url,
        "project_key": project_key or None,
        "repository_id": repo_id,
        "repository_slug": repository["slug"] or None,
        "actor": actor_slug,
        "self_generated": self_generated,
    }
    normalized: Dict[str, Any] = {
        "event_key": key,
        "date": payload.get("date"),
        "actor": actor,
        "repository": repository,
        "bitbucket_dc": dc,
    }

    if pr is not None:
        from_ref = pr.get("fromRef") if isinstance(pr.get("fromRef"), Mapping) else {}
        to_ref = pr.get("toRef") if isinstance(pr.get("toRef"), Mapping) else {}
        pr_id = int(pr["id"])
        head_sha = from_ref.get("latestCommit")
        previous_head = payload.get("previousFromHash")
        head_changed = (
            bool(
                key == "pr:from_ref_updated"
                and head_sha
                and previous_head
                and previous_head != head_sha
            )
            or key == "pr:opened"
        )
        if key == "pr:from_ref_updated" and not head_changed:
            # A redelivered or no-op ref update: same head as before.
            return BitbucketDCDelivery(
                accepted=False,
                event_key=key,
                reason=REASON_HEAD_UNCHANGED,
                event_type=event_type,
                repository_id=repo_id,
            )
        source_repo_id = _int(_dig(from_ref, "repository", "id"))
        reviewers = [
            user_name(_dig(item, "user"))
            for item in pr.get("reviewers") or []
            if isinstance(item, Mapping)
        ]
        normalized["pull_request"] = {
            "id": pr_id,
            "number": pr_id,
            "title": pr.get("title"),
            "description": pr.get("description") or "",
            "state": str(pr.get("state") or "").lower() or None,
            "draft": bool(pr.get("draft", False)),
            "version": _int(pr.get("version")),
            "html_url": pull_request_web_url(
                instance, project_key, repository["slug"], pr_id
            ),
            "author": user_name(_dig(pr, "author", "user")),
            "reviewers": [name for name in reviewers if name],
            "head": {
                "sha": head_sha,
                "ref": ref_branch(from_ref),
                "full_ref": from_ref.get("id"),
                "repository_id": source_repo_id,
            },
            "base": {
                "sha": to_ref.get("latestCommit"),
                "ref": ref_branch(to_ref),
                "full_ref": to_ref.get("id"),
                "repository_id": repo_id,
            },
        }
        dc.update(
            {
                "pull_request_id": pr_id,
                "pull_request_version": _int(pr.get("version")),
                "source_ref": from_ref.get("id"),
                "target_ref": to_ref.get("id"),
                "source_repository_id": source_repo_id,
                "head_sha": head_sha,
                "previous_head_sha": previous_head,
                "head_changed": head_changed,
            }
        )
        if key in _VERDICT_EVENTS:
            participant = payload.get("participant")
            dc["participant"] = user_name(_dig(participant, "user"))
            dc["participant_status"] = _dig(participant, "status")
            dc["previous_status"] = payload.get("previousStatus")
        if key in _COMMENT_EVENTS:
            comment = payload.get("comment")
            if not isinstance(comment, Mapping) or _int(comment.get("id")) is None:
                raise BitbucketDCWebhookRejectedError(400, "missing_comment")
            if looks_like_cloud_payload(comment):
                raise BitbucketDCWebhookRejectedError(400, "cloud_payload")
            parent_id = _int(payload.get("commentParentId"))
            mapped = normalize_comment(comment, parent_id=parent_id)
            mapped["html_url"] = (
                f"{normalized['pull_request']['html_url']}"
                f"/overview?commentId={int(comment['id'])}"
            )
            normalized["comment"] = mapped
            dc["comment_id"] = int(comment["id"])
            dc["parent_comment_id"] = parent_id
    else:
        changes: List[Dict[str, Any]] = []
        for change in payload.get("changes") or []:
            if not isinstance(change, Mapping):
                continue
            ref = change.get("ref") if isinstance(change.get("ref"), Mapping) else {}
            changes.append(
                {
                    "ref": change.get("refId") or ref.get("id"),
                    "branch": ref_branch(ref) if ref else None,
                    "type": change.get("type"),
                    "from_hash": change.get("fromHash"),
                    "to_hash": change.get("toHash"),
                }
            )
        normalized["push"] = {"changes": changes}
        last = changes[-1] if changes else {}
        if last.get("to_hash") and set(str(last["to_hash"])) != {"0"}:
            normalized["after"] = last["to_hash"]
        normalized["ref"] = last.get("ref")
        dc["changes"] = changes
        dc["head_sha"] = normalized.get("after")

    return BitbucketDCDelivery(
        accepted=True,
        event_key=key,
        event_type=event_type,
        payload=normalized,
        repository_id=repo_id,
    )


def delivery_identity(
    tracker_id: Any,
    request_id: Optional[str],
    event_key: str,
    payload: Mapping[str, Any],
    raw_body: bytes,
) -> str:
    """Stable, tracker-scoped identity of one delivery.

    Uses the provider's ``X-Request-Id`` when present. Otherwise derives the
    identity from documented immutable identifiers (repository, pull request,
    head commit, comment and ref change hashes) plus a digest of the raw
    body, so the same physical delivery always maps to the same identity and
    two different events never share one.
    """
    scope = f"bitbucket_dc:{tracker_id}"
    request = (request_id or "").strip()
    if request and len(request) <= 200 and request.isprintable():
        return f"{scope}:{request}"
    dc = payload.get("bitbucket_dc") if isinstance(payload, Mapping) else None
    dc = dc if isinstance(dc, Mapping) else {}
    material = {
        "event_key": event_key,
        "repository_id": dc.get("repository_id"),
        "pull_request_id": dc.get("pull_request_id"),
        "head_sha": dc.get("head_sha"),
        "comment_id": dc.get("comment_id"),
        "changes": dc.get("changes"),
        "body": hashlib.sha256(raw_body).hexdigest(),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:40]
    return f"{scope}:derived:{digest}"


__all__ = [
    "BITBUCKET_DC_DELIVERY_HEADER",
    "BITBUCKET_DC_EVENT_HEADER",
    "BITBUCKET_DC_EVENT_MAP",
    "BITBUCKET_DC_PING_EVENT",
    "BITBUCKET_DC_SIGNATURE_HEADER",
    "BITBUCKET_DC_WEBHOOK_EVENTS",
    "BITBUCKET_DC_WEBHOOK_MAX_BYTES",
    "BITBUCKET_DC_WEBHOOK_NAME",
    "BitbucketDCDelivery",
    "BitbucketDCWebhookRejectedError",
    "compute_webhook_signature",
    "delivery_identity",
    "generate_webhook_secret",
    "normalize_delivery",
    "verify_webhook_signature",
    "webhook_callback_path",
]
