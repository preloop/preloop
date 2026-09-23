"""Bounded provider reconciliation for implementation subscriptions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple
from types import SimpleNamespace
from copy import deepcopy
from urllib.parse import quote

from preloop.models import models
from preloop.models.crud import crud_tracker, crud_flow_feedback
from preloop.models.crud.oauth_app_installation import crud_oauth_app_installation
from preloop.sync.trackers import create_tracker_client
from preloop.sync.exceptions import TrackerPermissionError, TrackerResponseError
from sqlalchemy.orm import Session

# Bounded diagnostic tail kept per failing job. Traces are untrusted task data,
# so only the end of the log (where the failing command reports) is retained.
JOB_TRACE_TAIL_BYTES = 4000
# Bytes of a job log held in memory while reading. A provider may cap traces at
# hundreds of megabytes, so the response is streamed and only this tail is kept.
JOB_TRACE_READ_BYTES = 64000
# Failing jobs whose trace is read during one reconciliation.
MAX_JOB_TRACES = 3
# Keep Actions enrichment within the adoption preflight's twelve-read budget.
MAX_ACTIONS_JOB_READS = 2
PROVIDER_INFRA_OUTCOMES = frozenset({"startup_failure"})
# GitLab job/pipeline reads stay inside one page, like notes and statuses.
PROVIDER_PAGE_SIZE = 100
# A diagnostic read of a resource that is absent or not visible to this
# installation is no evidence; every other status stays an error.
MISSING_OR_DENIED = frozenset({403, 404})
_BOT_LOGIN_SUFFIX = "[bot]"


def _login_matches(token: str, login: str) -> bool:
    """Match an app slug to its ``[bot]`` login without prefix matching.

    ``preloop`` matches ``preloop`` and ``preloop[bot]``. It does not match
    ``preloop-staging[bot]`` or a user named ``preloop-fan``.
    """
    if not token or not login:
        return False
    if token == login:
        return True
    if login.endswith(_BOT_LOGIN_SUFFIX) and token == login[: -len(_BOT_LOGIN_SUFFIX)]:
        return True
    if token.endswith(_BOT_LOGIN_SUFFIX) and login == token[: -len(_BOT_LOGIN_SUFFIX)]:
        return True
    return False


def reviewer_is_trusted(policy: Mapping[str, Any], actor: Mapping[str, Any]) -> bool:
    """Return whether a bot actor is an explicitly trusted reviewer.

    Entries may be numeric provider actor ids or user/app names. Humans are
    not decided here; callers still accept a non-bot author with an empty list.
    """
    actor_id = str(actor.get("id") or "")
    login = str(actor.get("login") or actor.get("username") or "").strip().lower()
    for raw in policy.get("trusted_reviewer_ids") or []:
        token = str(raw).strip()
        if not token:
            continue
        if token.isdigit():
            if token == actor_id:
                return True
            continue
        if _login_matches(token.lower(), login):
            return True
    return False


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
    # Terminal failures the provider attributes to its own infrastructure. They
    # never invite a code repair; the scheduler retries them a bounded number of
    # times and then escalates with an explicit reason.
    infra_failures: list[dict[str, Any]] = field(default_factory=list)


def bounded_text(value: Any) -> str:
    """Limit untrusted provider prose and redact common credential forms."""
    text = str(value or "")[:12000]
    return redact(text)


def redact(text: str) -> str:
    """Remove the common inline credential forms from provider prose."""
    return re.sub(
        r"(?i)(bearer\s+|(?:token|password|api[_-]?key)\s*[=:]\s*)[^\s]+",
        r"\1[REDACTED]",
        text,
    )


def bounded_trace(value: Any) -> str:
    """Keep a redacted tail of a job log; an oversized trace is never stored whole.

    Redaction runs before the tail is cut: cutting first would drop the
    `token=` prefix of a credential that straddles the cut and leave its value
    verbatim in the kept text. The read already bounds how much arrives here.
    """
    return redact(str(value or ""))[-JOB_TRACE_TAIL_BYTES:]


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
    payload = {
        "id": str(obj.get("id", "")),
        "body": bounded_text(
            obj.get("body") or obj.get("note") or obj.get("name") or obj.get("context")
        ),
        "url": obj.get("html_url")
        or obj.get("web_url")
        or obj.get("details_url")
        or obj.get("target_url"),
        "state": obj.get("state") or obj.get("conclusion") or obj.get("status"),
        "updated_at": obj.get("updated_at")
        or obj.get("submitted_at")
        or obj.get("created_at"),
    }
    diagnostic = check_diagnostic(obj)
    if diagnostic:
        # Bounded redacted CI evidence for the repair turn, still untrusted text.
        payload["diagnostic"] = diagnostic
    if obj.get("failure_reason"):
        payload["failure_reason"] = str(obj["failure_reason"])[:200]
    return {
        "event_key": key,
        "delivery_id": None,
        "head_sha": head_sha,
        "kind": kind,
        "payload": payload,
    }


def check_diagnostic(obj: dict[str, Any]) -> str | None:
    """Bounded failing-check evidence: a GitLab job trace or GitHub check output."""
    if obj.get("trace"):
        return bounded_trace(obj["trace"])
    output = obj.get("output")
    if isinstance(output, dict):
        parts = [
            str(output.get(key) or "")
            for key in ("title", "summary", "text")
            if output.get(key)
        ]
        if parts:
            return bounded_trace("\n".join(parts))
    return None


PENDING_OUTCOMES = frozenset(
    {
        "queued",
        "pending",
        "running",
        "in_progress",
        "created",
        "waiting_for_resource",
        "preparing",
        "scheduled",
        None,
    }
)
FAILED_OUTCOMES = frozenset({"failure", "failed", "timed_out"})
BLOCKED_OUTCOMES = frozenset(
    {
        "cancelled",
        "canceled",
        "action_required",
        "manual",
        "error",
        "stale",
    }
)
# GitLab job/pipeline failure reasons. Only a failing script is the branch's own
# defect; everything else is the platform's problem or needs a human decision.
CODE_FAILURE_REASONS = frozenset({"script_failure", "test_failure"})
TIMEOUT_FAILURE_REASONS = frozenset(
    {"stuck_or_timeout_failure", "job_execution_timeout"}
)
PERMISSION_FAILURE_REASONS = frozenset(
    {
        "archived_failure",
        "builds_disabled",
        "ci_quota_exceeded",
        "deployment_rejected",
        "forward_deployment_failure",
        "insufficient_bridge_permissions",
        "insufficient_upstream_permissions",
        "project_deleted",
        "protected_environment_failure",
        "secrets_provider_not_found",
        "user_blocked",
    }
)
INFRA_FAILURE_REASONS = frozenset(
    {
        "api_failure",
        "data_integrity_failure",
        "downstream_bridge_project_not_found",
        "environment_creation_failure",
        "image_pull_failure",
        "no_matching_runner",
        "pipeline_loop_detected",
        "reached_max_descendant_pipelines_depth",
        "runner_system_failure",
        "runner_unsupported",
        "scheduler_failure",
        "stale_schedule",
        "trace_size_exceeded",
        "upstream_bridge_project_not_found",
    }
)
# A reason we do not recognise, or a failing check with no readable evidence, is
# never guessed into a repair round.
UNCLASSIFIED_REASONS = frozenset({"unknown_failure", "missing_dependency_failure"})


class CheckClassification(NamedTuple):
    """Terminal outcome buckets for one head; a tuple for existing call sites."""

    pending: bool
    passed: bool
    blocked_reason: str | None
    failures: list[dict[str, Any]]
    infra_failures: list[dict[str, Any]]


def classify_failure(item: dict[str, Any]) -> str:
    """Bucket one failing check from provider details only, never from log prose.

    Returns ``code`` (a repair round is warranted), ``infra`` or ``timeout``
    (bounded retry then escalation), ``permission`` (a human must act) or
    ``unknown`` (no usable evidence, so an explicit blocked reason).
    """
    reason = str(item.get("failure_reason") or "").strip().lower()
    if reason in CODE_FAILURE_REASONS:
        return "code"
    if reason in TIMEOUT_FAILURE_REASONS:
        return "timeout"
    if reason in PERMISSION_FAILURE_REASONS:
        return "permission"
    if reason in INFRA_FAILURE_REASONS:
        return "infra"
    if reason and reason not in UNCLASSIFIED_REASONS:
        return "unknown"
    outcome = item.get("conclusion") or item.get("state") or item.get("status")
    if outcome in PROVIDER_INFRA_OUTCOMES:
        return "infra"
    if outcome == "timed_out":
        return "timeout"
    if reason in UNCLASSIFIED_REASONS or item.get("details_unavailable") is True:
        # The provider reported a failure it cannot explain, or its job detail
        # read was unavailable. Repairing on that evidence would be a guess.
        return "unknown"
    return "code"


def classify_checks(
    checks: list[dict[str, Any]], required: list[str], *, allow_empty: bool = False
) -> CheckClassification:
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
    failures: list[dict[str, Any]] = []
    infra_failures: list[dict[str, Any]] = []
    if not selected and not allow_empty:
        return CheckClassification(True, False, "required_checks_missing", [], [])
    for item in selected:
        conclusion = item.get("conclusion") or item.get("state") or item.get("status")
        if item.get("allow_failure") is True:
            continue
        if item.get("superseded") is True:
            # A retried attempt: the newer attempt of the same check decides.
            continue
        if conclusion in {"success", "neutral", "skipped"}:
            continue
        if conclusion in PENDING_OUTCOMES:
            pending = True
        elif conclusion in FAILED_OUTCOMES | PROVIDER_INFRA_OUTCOMES:
            category = classify_failure(item)
            if category == "code":
                failures.append(item)
            elif category in {"infra", "timeout"}:
                infra_failures.append(item)
            elif category == "permission":
                blocked = "ci_permission_required"
            else:
                blocked = "ci_failure_unclassified"
        elif conclusion in BLOCKED_OUTCOMES:
            blocked = f"ci_{conclusion}"
        else:
            blocked = "ci_unknown_outcome"
    return CheckClassification(
        pending,
        not pending and not failures and not infra_failures and not blocked,
        blocked,
        failures,
        infra_failures,
    )


async def _optional(read: Any) -> Any:
    """Await one diagnostic read; a missing or denied resource is not an error.

    Rate limits, connection failures and other provider errors still propagate:
    incomplete evidence must never look like a clean read.
    """
    try:
        return await read
    except TrackerPermissionError:
        return None
    except TrackerResponseError as error:
        status = getattr(error, "status_code", None)
        if status in MISSING_OR_DENIED:
            return None
        # Call sites that do not carry the status still report it in the
        # message; anything else (429, 5xx, connection loss) propagates.
        if status is None and re.search(r"error:\s*(404|403)\b", str(error)):
            return None
        raise


def _read_tail(response: Any) -> str:
    """Consume a streamed body chunk by chunk, holding only its tail."""
    tail = bytearray()
    with closing(response):
        for chunk in response.iter_content(chunk_size=8192):
            tail += chunk if isinstance(chunk, bytes) else str(chunk).encode()
            if len(tail) > JOB_TRACE_READ_BYTES:
                del tail[:-JOB_TRACE_READ_BYTES]
    return tail.decode("utf-8", "replace")


async def _trace_tail(trace: Any) -> str | None:
    """The end of one job log, never the whole body of an unbounded log.

    A streamed response is drained in a worker thread so the event loop keeps
    running and memory stays at the tail size regardless of the log length.
    """
    if trace is None:
        return None
    if callable(getattr(trace, "iter_content", None)):
        return await asyncio.to_thread(_read_tail, trace)
    text = getattr(trace, "text", trace)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return None
    return text[-JOB_TRACE_READ_BYTES:]


def _pipeline_only(pipeline: dict[str, Any]) -> list[dict[str, Any]]:
    """A pipeline with no readable job (config error, unavailable jobs) is evidence.

    Its own status still decides pending versus failed, but a pipeline-level
    failure has no job trace, so it can never be guessed into a code repair.
    An unreported status is no evidence at all.
    """
    if not pipeline.get("status"):
        return []
    reason = pipeline.get("failure_reason")
    if str(reason or "").strip().lower() in CODE_FAILURE_REASONS:
        # A pipeline summary cannot replace the missing job diagnostics that a
        # repair needs. Keep platform reasons for bounded retry or escalation.
        reason = None
    return [
        {
            "id": pipeline.get("id"),
            "name": f"pipeline #{pipeline.get('id')}",
            "status": pipeline.get("status"),
            "web_url": pipeline.get("web_url"),
            "details_unavailable": True,
            "failure_reason": reason,
        }
    ]


def _unexplained(
    checks: list[dict[str, Any]], jobs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Commit statuses with no readable job of the same name cannot be repaired."""
    explained = {str(job.get("name") or "") for job in jobs}
    results = []
    for item in checks:
        name = str(item.get("name") or item.get("context") or "")
        status = item.get("status") or item.get("state")
        if name not in explained and status in FAILED_OUTCOMES:
            results.append({**item, "details_unavailable": True})
        else:
            results.append(item)
    return results


def feedback_tracker_options(db: Session, tracker: Any) -> dict[str, Any]:
    """Materialize authoritative tracker authentication before network I/O."""
    options = deepcopy({"url": tracker.url, **(tracker.connection_details or {})})
    auth_type = getattr(tracker, "auth_type", None) or "api_token"
    if tracker.tracker_type == "github":
        options["auth_type"] = auth_type
        options.pop("github_installation_id", None)
        if auth_type in {"github_app", "oauth_app"}:
            installation = crud_oauth_app_installation.get_by_id_provider_and_account(
                db,
                id=tracker.oauth_installation_id,
                provider="github",
                account_id=tracker.account_id,
            )
            if installation is None or not installation.external_id:
                raise ValueError("feedback tracker installation unavailable")
            options["github_installation_id"] = installation.external_id
    return options


class FeedbackProvider:
    """Read-only APIs, scoped by the trusted tracker and bound repository ID."""

    def __init__(
        self, client: Any, thread: models.FlowThread | SimpleNamespace
    ) -> None:
        self.client = client
        self.thread = thread

    @classmethod
    async def for_thread(
        cls, db: Session, thread: models.FlowThread
    ) -> FeedbackProvider:
        tracker = crud_tracker.get(db, id=thread.tracker_id)
        if tracker is None or tracker.account_id != thread.account_id:
            raise ValueError("feedback tracker account mismatch")
        tracker_args = (
            tracker.tracker_type,
            str(tracker.id),
            tracker.resolved_api_key,
            feedback_tracker_options(db, tracker),
        )
        snapshot = SimpleNamespace(
            provider=thread.provider,
            repository_id=thread.repository_id,
            pr_number=thread.pr_number,
            policy=deepcopy(thread.policy),
        )
        crud_flow_feedback.release_read(db)
        client = await create_tracker_client(*tracker_args)
        if client is None:
            raise ValueError("feedback provider unavailable")
        return cls(client, snapshot)

    async def read(self) -> FeedbackState:
        if self.thread.provider == "github":
            return await self._github()
        if self.thread.provider == "gitlab":
            return await self._gitlab()
        raise ValueError("unsupported feedback provider")

    def _comments(
        self, items: list[dict[str, Any]], kind: str, sha: str
    ) -> list[dict[str, Any]]:
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
            ) and not reviewer_is_trusted(self.thread.policy, actor):
                continue
            # No marker can authorize a bot. Trusted sender identity is required.
            results.append(receipt(kind, item, head_sha=sha))
        return results

    async def _github_job_details(
        self,
        request: Any,
        repo: str,
        checks: list[dict[str, Any]],
        sha: str,
        required: list[str],
    ) -> list[dict[str, Any]]:
        """Enrich failing Actions checks using bounded, current-head job metadata.

        Never fetch a provider-supplied URL. Only a numeric job identity is
        extracted; the read stays on the authoritative repository endpoint.
        Missing, stale and over-budget evidence blocks speculative code repair.
        """
        enriched = []
        reads = 0
        for original in checks:
            item = dict(original)
            enriched.append(item)
            if (
                (item.get("app") or {}).get("slug") != "github-actions"
                or item.get("conclusion") not in FAILED_OUTCOMES
                or (required and item.get("name") not in required)
            ):
                continue
            item["details_unavailable"] = True
            identity = re.fullmatch(
                r"https://github\.com/[^/]+/[^/]+/actions/runs/[0-9]+/job/([0-9]+)(?:\?.*)?",
                str(item.get("details_url") or ""),
            )
            if (
                item.get("head_sha") != sha
                or identity is None
                or reads >= MAX_ACTIONS_JOB_READS
            ):
                continue
            reads += 1
            job = await _optional(
                request("GET", f"{repo}/actions/jobs/{identity.group(1)}")
            )
            if not isinstance(job, dict) or job.get("head_sha") != sha:
                continue
            if not str(job.get("check_run_url") or "").endswith(
                f"/check-runs/{item.get('id')}"
            ):
                continue
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            failed = [
                step
                for step in steps
                if isinstance(step, dict) and step.get("conclusion") == "failure"
            ]
            # Only the provider-owned first step proves a bootstrap failure.
            # A user can name an ordinary step "Set up job", so name alone is
            # insufficient. Missing steps and cleanup-only failures stay unknown.
            if failed and all(
                step.get("number") == 1 and step.get("name") == "Set up job"
                for step in failed
            ):
                item["failure_reason"] = "runner_system_failure"
            elif any(
                isinstance(step.get("number"), int)
                and step["number"] > 1
                and step.get("name") != "Complete job"
                for step in failed
            ):
                item["failure_reason"] = "script_failure"
            else:
                continue
            item.pop("details_unavailable", None)
        return enriched

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
        query = """query($id:ID!){node(id:$id){... on PullRequest{reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved isOutdated comments(first:100){pageInfo{hasNextPage} nodes{databaseId body url createdAt updatedAt author{__typename ... on User{databaseId login} ... on Bot{databaseId login}}}}}}}}}"""
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
                            "login": actor.get("login"),
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
        requirements_unknown = False
        try:
            protection = await request(
                "GET",
                f"{repo}/branches/{quote(pr['base']['ref'], safe='')}/protection",
            )
        except TrackerPermissionError:
            protection = {}
            requirements_unknown = True
        except TrackerResponseError as error:
            # GitHub distinguishes an unprotected branch from a masked or
            # unreadable resource in its response message. Only its explicit
            # absence response establishes empty classic requirements.
            if (
                error.status_code != 404
                or "branch not protected" not in str(error).lower()
            ):
                raise
            protection = {}
        required = sorted(
            set(required)
            | set((protection.get("required_status_checks") or {}).get("contexts", []))
        )
        count = max(
            count,
            int(
                (protection.get("required_pull_request_reviews") or {}).get(
                    "required_approving_review_count", 0
                )
            ),
        )
        try:
            rules = await request(
                "GET", f"{repo}/rules/branches/{quote(pr['base']['ref'], safe='')}"
            )
        except TrackerPermissionError:
            rules = []
            requirements_unknown = True
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
        check_runs = await self._github_job_details(
            request, repo, checks.get("check_runs", []), sha, required
        )
        (
            state.checks_pending,
            state.checks_passed,
            state.blocked_reason,
            failed,
            state.infra_failures,
        ) = classify_checks(check_runs + statuses.get("statuses", []), required)
        if (
            thread_page_limit
            or any(len(items) >= 100 for items in (reviews, comments, discussion))
            or checks.get("total_count", 0) > 100
            # Combined status returns latest-per-context objects. Bound that
            # page directly instead of inferring missing contexts from totals.
            or len(statuses.get("statuses", [])) >= 100
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
            [r for r in latest.values() if r.get("state") == "CHANGES_REQUESTED"]
            + [
                r
                for r in reviews
                if r.get("state") == "COMMENTED" and (r.get("body") or "").strip()
            ],
            "review",
            sha,
        )
        state.feedback += [receipt("ci", item, head_sha=sha) for item in failed]
        if requirements_unknown:
            state.checks_passed = False
            state.reviews_passed = False
            if state.blocked_reason is None:
                state.blocked_reason = "repository_requirements_unavailable"
        current = await request("GET", base)
        state.closed = current.get("state") == "closed"
        if current["head"]["sha"] != sha:
            return FeedbackState(
                current["head"]["sha"],
                closed=state.closed,
                checks_pending=True,
                blocked_reason="head_changed_during_reconciliation",
            )
        return state

    async def _gitlab(self) -> FeedbackState:
        async def get(path: str, **options: Any) -> Any:
            return await self.client._make_request(
                self.client.gl.http_get, path, **options
            )

        repo = f"/projects/{quote(self.thread.repository_id, safe='')}"
        base = f"{repo}/merge_requests/{quote(self.thread.pr_number, safe='')}"
        mr = await get(base)
        if str(mr["project_id"]) != self.thread.repository_id:
            raise ValueError("provider repository identity mismatch")
        sha = mr["sha"]
        state = FeedbackState(sha, closed=mr["state"] in {"closed", "merged"})
        if state.closed:
            return state
        page = f"per_page={PROVIDER_PAGE_SIZE}"
        checks = await get(f"{repo}/repository/commits/{sha}/statuses?{page}")
        notes = await get(f"{base}/notes?{page}&sort=desc&order_by=updated_at")
        approvals = await get(f"{base}/approvals")
        jobs, jobs_truncated = await self._gitlab_jobs(get, repo, mr, sha)
        # Job details decide first: a commit status of the same name carries no
        # failure reason, so it cannot classify its own failure.
        (
            state.checks_pending,
            state.checks_passed,
            state.blocked_reason,
            failed,
            infra,
        ) = classify_checks(
            jobs + _unexplained(checks, jobs),
            self.thread.policy.get("required_checks", []),
        )
        state.infra_failures = infra
        if (
            len(notes) >= PROVIDER_PAGE_SIZE
            or len(checks) >= PROVIDER_PAGE_SIZE
            or jobs_truncated
        ):
            state.blocked_reason = "provider_page_limit"
        approved_by = {
            str(item["user"]["id"])
            for item in approvals.get("approved_by", [])
            if (item.get("user") or {}).get("id") is not None
        }
        state.reviews_passed = (
            approvals.get("approvals_left", 1) == 0
            and len(approved_by) >= int(self.thread.policy.get("required_approvals", 0))
            and bool(mr.get("blocking_discussions_resolved", False))
        )
        await self._gitlab_traces(get, repo, failed)
        state.feedback = self._comments(notes, "comment", sha) + [
            receipt("ci", item, head_sha=sha) for item in failed
        ]
        current = await get(base)
        state.closed = current.get("state") in {"closed", "merged"}
        if current["sha"] != sha:
            return FeedbackState(
                current["sha"],
                closed=state.closed,
                checks_pending=True,
                blocked_reason="head_changed_during_reconciliation",
            )
        return state

    async def _gitlab_jobs(
        self, get: Any, repo: str, mr: dict[str, Any], sha: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Current-head pipeline jobs, deduplicated by name with retries dropped.

        Stale pipelines, other projects and superseded attempts are discarded
        before classification. An unavailable pipeline or job read marks the
        evidence unavailable instead of inventing a code failure.
        """
        pipeline = await self._gitlab_pipeline(get, repo, mr, sha)
        if pipeline is None:
            return [], False
        identity = pipeline["id"]
        jobs = await _optional(
            get(
                f"{repo}/pipelines/{identity}/jobs"
                f"?per_page={PROVIDER_PAGE_SIZE}&include_retried=false"
            )
        )
        if jobs is None:
            # Commit statuses still describe the gates; their failures simply
            # have no readable job evidence (see _unexplained).
            return _pipeline_only(pipeline), False
        truncated = len(jobs) >= PROVIDER_PAGE_SIZE
        latest: dict[str, dict[str, Any]] = {}
        for job in sorted(
            jobs, key=lambda item: int(item.get("id") or 0), reverse=True
        ):
            job_pipeline = job.get("pipeline") or {}
            if (
                job.get("retried") is True
                or (job_pipeline.get("sha") or sha) != sha
                or str(job_pipeline.get("project_id", self.thread.repository_id))
                != self.thread.repository_id
            ):
                continue
            name = str(job.get("name") or "")
            if name in latest:
                # An earlier attempt of a job the provider ran again.
                continue
            latest[name] = {
                "id": job.get("id"),
                "name": name,
                "status": job.get("status"),
                "stage": job.get("stage"),
                "allow_failure": job.get("allow_failure"),
                "failure_reason": job.get("failure_reason"),
                "web_url": job.get("web_url"),
                "created_at": job.get("created_at"),
                "pipeline_id": identity,
            }
        return list(latest.values()) or _pipeline_only(pipeline), truncated

    async def _gitlab_pipeline(
        self, get: Any, repo: str, mr: dict[str, Any], sha: str
    ) -> dict[str, Any] | None:
        """The pipeline of the current head only, never a stale or foreign run."""
        head = mr.get("head_pipeline") or {}
        if head.get("id") and head.get("sha") == sha:
            if str(head.get("project_id", self.thread.repository_id)) != str(
                self.thread.repository_id
            ):
                return None
            return dict(head)
        pipelines = await _optional(
            get(
                f"{repo}/pipelines?sha={quote(sha, safe='')}"
                "&order_by=id&sort=desc&per_page=20"
            )
        )
        for item in pipelines or []:
            if item.get("sha") == sha and item.get("id"):
                return dict(item)
        return None

    async def _gitlab_traces(
        self, get: Any, repo: str, failed: list[dict[str, Any]]
    ) -> None:
        """Attach a bounded redacted trace tail to the first failing jobs."""
        for job in failed[:MAX_JOB_TRACES]:
            if not job.get("id"):
                continue
            trace = await _optional(
                get(f"{repo}/jobs/{job['id']}/trace", streamed=True)
            )
            text = await _trace_tail(trace)
            if text and text.strip():
                job["trace"] = text
