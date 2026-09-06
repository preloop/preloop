"""Bounded provider reconciliation for implementation subscriptions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from preloop.models import models
from preloop.models.crud import crud_tracker
from preloop.sync.trackers import create_tracker_client
from sqlalchemy.orm import Session


@dataclass
class FeedbackState:
    """Authoritative current head and gate state, never webhook truth alone."""

    head_sha: str
    closed: bool = False
    checks_pending: bool = False
    checks_passed: bool = False
    reviews_passed: bool = False
    blocked_reason: str | None = None
    feedback: list[dict[str, Any]] = field(default_factory=list)


def bounded_text(value: Any) -> str:
    """Limit untrusted provider prose and redact common credential forms."""
    text = str(value or "")[:12000]
    return re.sub(
        r"(?i)(bearer\s+|(?:token|password|api[_-]?key)\s*[=:]\s*)[^\s]+",
        r"\1[REDACTED]",
        text,
    )


def receipt(
    kind: str, obj: dict[str, Any], *, head_sha: str | None = None
) -> dict[str, Any]:
    """Stable semantic identity coalesces webhook retry and reconciliation."""
    identity = {
        key: obj.get(key)
        for key in (
            "id",
            "updated_at",
            "submitted_at",
            "run_attempt",
            "status",
            "conclusion",
            "state",
        )
    }
    identity["kind"] = kind
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {
        "event_key": key,
        "delivery_id": None,
        "head_sha": head_sha,
        "kind": kind,
        "payload": {
            "id": str(obj.get("id", "")),
            "body": bounded_text(
                obj.get("body")
                or obj.get("note")
                or obj.get("name")
                or obj.get("context")
            ),
            "url": obj.get("html_url")
            or obj.get("web_url")
            or obj.get("details_url")
            or obj.get("target_url"),
            "state": obj.get("state") or obj.get("conclusion") or obj.get("status"),
            "updated_at": obj.get("updated_at")
            or obj.get("submitted_at")
            or obj.get("created_at"),
        },
    }


def classify_checks(
    checks: list[dict[str, Any]], required: list[str], *, allow_empty: bool = False
) -> tuple[bool, bool, str | None, list[dict[str, Any]]]:
    """Classify every required terminal outcome; uncertain gates block readiness."""
    by_name: dict[str, dict[str, Any]] = {}
    for item in checks:
        name = str(item.get("name") or item.get("context") or "")
        # Provider endpoints return latest first; do not replace newer attempts.
        by_name.setdefault(name, item)
    selected = (
        [by_name[name] for name in required if name in by_name]
        if required
        else list(by_name.values())
    )
    pending = bool(set(required) - set(by_name))
    blocked = "required_checks_missing" if pending else None
    failures = []
    if not selected and not allow_empty:
        return True, False, "required_checks_missing", []
    for item in selected:
        conclusion = item.get("conclusion") or item.get("state") or item.get("status")
        if item.get("allow_failure") is True:
            continue
        if conclusion in {"success", "neutral", "skipped"}:
            continue
        if conclusion in {
            "queued",
            "pending",
            "running",
            "in_progress",
            "created",
            "waiting_for_resource",
            "preparing",
            "scheduled",
            None,
        }:
            pending = True
        elif conclusion in {"failure", "failed", "timed_out"}:
            failures.append(item)
        elif conclusion in {
            "cancelled",
            "canceled",
            "action_required",
            "startup_failure",
            "manual",
            "error",
            "stale",
        }:
            blocked = f"ci_{conclusion}"
        else:
            blocked = "ci_unknown_outcome"
    return pending, not pending and not failures and not blocked, blocked, failures


class FeedbackProvider:
    """Read-only APIs, scoped by the trusted tracker and bound repository ID."""

    def __init__(self, client: Any, thread: models.FlowThread) -> None:
        self.client = client
        self.thread = thread

    @classmethod
    async def for_thread(
        cls, db: Session, thread: models.FlowThread
    ) -> FeedbackProvider:
        tracker = crud_tracker.get(db, id=thread.tracker_id)
        if tracker is None or tracker.account_id != thread.account_id:
            raise ValueError("feedback tracker account mismatch")
        client = await create_tracker_client(
            tracker.tracker_type,
            str(tracker.id),
            tracker.resolved_api_key,
            {"url": tracker.url, **(tracker.connection_details or {})},
        )
        if client is None:
            raise ValueError("feedback provider unavailable")
        return cls(client, thread)

    async def read(self) -> FeedbackState:
        if self.thread.provider == "github":
            return await self._github()
        if self.thread.provider == "gitlab":
            return await self._gitlab()
        raise ValueError("unsupported feedback provider")

    def _comments(
        self, items: list[dict[str, Any]], kind: str, sha: str
    ) -> list[dict[str, Any]]:
        trusted = set(map(str, self.thread.policy.get("trusted_reviewer_ids", [])))
        self_ids = set(map(str, self.thread.policy.get("implementer_actor_ids", [])))
        results = []
        for item in items:
            actor = item.get("user") or item.get("author") or {}
            actor_id = str(actor.get("id", ""))
            if (
                actor_id in self_ids
                or item.get("system")
                or (item.get("resolvable") and item.get("resolved"))
            ):
                continue
            if (
                actor.get("type") == "Bot" or actor.get("bot")
            ) and actor_id not in trusted:
                continue
            # No marker can authorize a bot. Trusted sender identity is required.
            results.append(receipt(kind, item, head_sha=sha))
        return results

    async def _github(self) -> FeedbackState:
        request = self.client._request
        repo = f"/repositories/{quote(self.thread.repository_id, safe='')}"
        base = f"{repo}/pulls/{quote(self.thread.pr_number, safe='')}"
        pr = await request("GET", base)
        if str(pr["base"]["repo"]["id"]) != self.thread.repository_id:
            raise ValueError("provider repository identity mismatch")
        sha = pr["head"]["sha"]
        state = FeedbackState(sha, closed=pr["state"] == "closed")
        if state.closed:
            return state
        checks = await request(
            "GET", f"{repo}/commits/{sha}/check-runs?per_page=100&filter=latest"
        )
        statuses = await request("GET", f"{repo}/commits/{sha}/status?per_page=100")
        reviews = await request("GET", f"{base}/reviews?per_page=100")
        query = """query($id:ID!){node(id:$id){... on PullRequest{reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved isOutdated comments(first:100){pageInfo{hasNextPage} nodes{databaseId body url createdAt updatedAt author{__typename ... on User{databaseId} ... on Bot{databaseId}}}}}}}}}"""
        graph = await request(
            "POST", "/graphql", {"query": query, "variables": {"id": pr["node_id"]}}
        )
        if graph.get("errors") or not (graph.get("data") or {}).get("node"):
            raise ValueError("review thread reconciliation unavailable")
        threads = graph["data"]["node"]["reviewThreads"]
        comments = []
        thread_page_limit = threads["pageInfo"]["hasNextPage"]
        for discussion_thread in threads["nodes"]:
            if discussion_thread["isResolved"]:
                continue
            thread_page_limit |= discussion_thread["comments"]["pageInfo"][
                "hasNextPage"
            ]
            for comment in discussion_thread["comments"]["nodes"]:
                actor = comment.get("author") or {}
                comments.append(
                    {
                        "id": comment["databaseId"],
                        "body": comment["body"],
                        "html_url": comment["url"],
                        "created_at": comment["createdAt"],
                        "updated_at": comment["updatedAt"],
                        "user": {
                            "id": actor.get("databaseId"),
                            "type": actor.get("__typename"),
                        },
                    }
                )
        discussion = await request(
            "GET", f"{repo}/issues/{self.thread.pr_number}/comments?per_page=100"
        )
        required = self.thread.policy.get("required_checks", [])
        count = int(self.thread.policy.get("required_approvals", 1))
        # Repository policy is authoritative; absent explicit config is not proof
        # that required checks are empty. Branch protection errors fail closed.
        if "required_checks" not in self.thread.policy:
            protection = await request(
                "GET", f"{repo}/branches/{quote(pr['base']['ref'], safe='')}/protection"
            )
            required = (protection.get("required_status_checks") or {}).get(
                "contexts", []
            )
            count = int(
                (protection.get("required_pull_request_reviews") or {}).get(
                    "required_approving_review_count", count
                )
            )
        rules = await request(
            "GET", f"{repo}/rules/branches/{quote(pr['base']['ref'], safe='')}"
        )
        unsupported_gate = False
        for rule in rules:
            parameters = rule.get("parameters") or {}
            if rule.get("type") == "required_status_checks":
                required = sorted(
                    set(required)
                    | {
                        item["context"]
                        for item in parameters.get("required_status_checks", [])
                    }
                )
            elif rule.get("type") == "pull_request":
                count = max(
                    count, int(parameters.get("required_approving_review_count", 0))
                )
            elif rule.get("type") in {"workflows", "code_scanning"}:
                unsupported_gate = True
        state.checks_pending, state.checks_passed, state.blocked_reason, failed = (
            classify_checks(
                checks.get("check_runs", []) + statuses.get("statuses", []), required
            )
        )
        if (
            thread_page_limit
            or any(len(items) >= 100 for items in (reviews, comments, discussion))
            or checks.get("total_count", 0) > 100
        ):
            state.blocked_reason = "provider_page_limit"
        if unsupported_gate:
            state.blocked_reason = "repository_gate_requires_external_evidence"
        latest: dict[str, dict[str, Any]] = {}
        for review in reviews:
            if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                latest[str(review.get("user", {}).get("id"))] = review
        state.reviews_passed = sum(
            r.get("state") == "APPROVED" and r.get("commit_id") == sha
            for r in latest.values()
        ) >= count and not any(
            r.get("state") == "CHANGES_REQUESTED" for r in latest.values()
        )
        state.feedback = self._comments(
            comments, "inline_comment", sha
        ) + self._comments(discussion, "comment", sha)
        state.feedback += self._comments(
            [r for r in latest.values() if r.get("state") == "CHANGES_REQUESTED"],
            "review",
            sha,
        )
        state.feedback += [receipt("ci", item, head_sha=sha) for item in failed]
        current = await request("GET", base)
        if current["head"]["sha"] != sha:
            return FeedbackState(
                current["head"]["sha"],
                checks_pending=True,
                blocked_reason="head_changed_during_reconciliation",
            )
        return state

    async def _gitlab(self) -> FeedbackState:
        async def get(path: str) -> Any:
            return await self.client._make_request(self.client.gl.http_get, path)

        repo = f"/projects/{quote(self.thread.repository_id, safe='')}"
        base = f"{repo}/merge_requests/{quote(self.thread.pr_number, safe='')}"
        mr = await get(base)
        if str(mr["project_id"]) != self.thread.repository_id:
            raise ValueError("provider repository identity mismatch")
        sha = mr["sha"]
        state = FeedbackState(sha, closed=mr["state"] in {"closed", "merged"})
        if state.closed:
            return state
        checks = await get(f"{repo}/repository/commits/{sha}/statuses?per_page=100")
        notes = await get(f"{base}/notes?per_page=100&sort=desc&order_by=updated_at")
        approvals = await get(f"{base}/approvals")
        state.checks_pending, state.checks_passed, state.blocked_reason, failed = (
            classify_checks(checks, self.thread.policy.get("required_checks", []))
        )
        if len(notes) >= 100 or len(checks) >= 100:
            state.blocked_reason = "provider_page_limit"
        state.reviews_passed = approvals.get("approvals_left", 1) == 0 and bool(
            mr.get("blocking_discussions_resolved", False)
        )
        state.feedback = self._comments(notes, "comment", sha) + [
            receipt("ci", item, head_sha=sha) for item in failed
        ]
        current = await get(base)
        if current["sha"] != sha:
            return FeedbackState(
                current["sha"],
                checks_pending=True,
                blocked_reason="head_changed_during_reconciliation",
            )
        return state
