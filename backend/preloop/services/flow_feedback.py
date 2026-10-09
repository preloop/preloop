"""Durable implementation turns: runners work, the scheduler waits for feedback."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution, crud_flow_feedback
from preloop.models.crud.flow_feedback import SESSIONLESS_RETRY_STATUSES
from preloop.services.flow_feedback_provider import (
    FeedbackProvider,
    FeedbackState,
    bounded_text,
    classify_failure,
)
from preloop.utils.bitbucket import repository_identity

logger = logging.getLogger(__name__)
FEEDBACK_TYPES = frozenset(
    {
        "comment_created",
        "comment_updated",
        "pull_request_review",
        "pull_request_review_comment",
        "check_run",
        "check_suite",
        "workflow_run",
        "status",
        "pipeline",
        "job",
        "pull_request_updated",
        "pull_request_closed",
        "pull_request_merged",
        "merge_request_updated",
        "merge_request_closed",
        "merge_request_merged",
        # Bitbucket review verdicts are participant flips, not reviews.
        "pull_request_approved",
        "pull_request_unapproved",
        "pull_request_changes_requested",
        "pull_request_changes_request_removed",
    }
)


def _repository_identity(provider: Any, repository: dict[str, Any]) -> Any:
    """The stable per-provider repository key threads are found by.

    GitHub and GitLab have numeric repository ids. Bitbucket has none, so its
    identity is ``workspace_slug/repo_uuid``, which both webhook payloads and
    manual-run payloads can produce.
    """
    if provider == "bitbucket":
        return repository_identity(repository)
    return repository.get("id")


_CODE_HOSTS = frozenset({"github", "gitlab", "bitbucket"})


def _bound_repository(
    db: Session, flow: Any, details: dict[str, Any]
) -> tuple[str, str, dict[str, Any]] | None:
    """Provider, code-host tracker and repository of a bound issue trigger.

    A Jira-triggered run publishes to the repository its Jira project is bound
    to (``repository_binding``); the trigger payload names no repository, so
    the thread must be keyed on the bound code host, which is where review
    feedback arrives from.

    Returns:
        ``(provider, tracker_id, repository)`` shaped like the code host's
        webhook ``repository`` object, or None when no binding applies.
    """
    from preloop.models.crud import crud_project
    from preloop.services.repository_binding import (
        RepositoryBindingError,
        resolve_repository_binding,
    )

    try:
        applied = resolve_repository_binding(
            db,
            account_id=str(flow.account_id),
            git_clone_config=getattr(flow, "git_clone_config", None),
            trigger_tracker_id=details.get("tracker_id"),
            trigger_source=details.get("source"),
            trigger_project_id=details.get("project_id"),
        )
    except RepositoryBindingError as exc:
        logger.warning("Cannot bind feedback: repository binding failed: %s", exc)
        return None
    if applied is None or applied.tracker_type not in _CODE_HOSTS:
        return None
    project = crud_project.get(
        db, id=str(applied.project_id), account_id=str(flow.account_id)
    )
    identifier = getattr(project, "identifier", None) if project else None
    if not identifier:
        return None
    # Bitbucket keys on workspace/uuid (full_name + uuid); GitHub and GitLab
    # on the numeric repository id the synced project stores as identifier.
    repository = {
        "full_name": applied.repository,
        "uuid": str(identifier),
        "id": str(identifier),
    }
    return applied.tracker_type, applied.tracker_id, repository


def feedback_policy(flow: Any) -> dict[str, Any] | None:
    """Existing saved flows opt in explicitly; preset updates never overwrite them."""
    config = getattr(flow, "agent_config", None)
    policy = config.get("feedback") if isinstance(config, dict) else None
    return (
        policy if isinstance(policy, dict) and policy.get("enabled") is True else None
    )


def register_thread(
    db: Session,
    execution: models.FlowExecution,
    pr_url: str,
    branch: str,
    *,
    adoption: dict[str, Any] | None = None,
    commit: bool = True,
) -> models.FlowThread | None:
    """Register after trusted publication; initial reconciliation recovers races."""
    flow = crud_flow.get(db, id=execution.flow_id)
    policy = feedback_policy(flow)
    if flow is None or not flow.account_id or policy is None:
        return None
    details = execution.trigger_event_details or {}
    if details.get("_thread_id"):
        return None
    if not details.get("_session_thread_id") and adoption is None:
        # Enabling an old flow is permission for future implementations, not
        # permission to spend repair budgets on its historical PR backlog.
        return None
    payload = details.get("payload") or {}
    repository = payload.get("repository") or payload.get("project") or {}
    tracker_id = details.get("tracker_id") or flow.trigger_event_source
    provider = details.get("source")
    trigger_source, trigger_tracker_id = provider, tracker_id
    if provider not in _CODE_HOSTS:
        bound = _bound_repository(db, flow, details)
        if bound is not None:
            provider, tracker_id, repository = bound
    repository_id = _repository_identity(provider, repository)
    parsed = urlparse(pr_url)
    parts = parsed.path.rstrip("/").split("/")
    try:
        tracker_uuid = uuid.UUID(str(tracker_id)) if tracker_id else None
        session_thread_id = (
            uuid.UUID(str(details["_session_thread_id"]))
            if details.get("_session_thread_id")
            else uuid.UUID(str(execution.id))
        )
    except (ValueError, TypeError, AttributeError):
        logger.warning("Cannot bind feedback: tracker or session id is not a UUID")
        return None
    if (
        not repository_id
        or tracker_uuid is None
        or provider not in {"github", "gitlab", "bitbucket"}
        or not parts[-1].isdigit()
    ):
        logger.warning("Cannot bind feedback: missing provider repository identity")
        return None
    now = datetime.now(UTC).replace(tzinfo=None)
    # Account and flow come from the execution's DB ownership, never webhook JSON.
    context = {
        "original_issue": payload.get("issue")
        or payload.get("object_attributes")
        or {},
        "acceptance_version": hashlib.sha256(
            json.dumps(
                payload.get("issue") or payload.get("object_attributes") or {},
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "repository": repository,
        **({"adoption": adoption} if adoption is not None else {}),
        "trigger": {
            **{
                key: details[key]
                for key in ("project_id", "project_path", "issue_id")
                if key in details
            },
            "source": trigger_source,
            "tracker_id": str(trigger_tracker_id),
            "account_id": str(flow.account_id),
        },
    }
    return crud_flow_feedback.register(
        db,
        commit=commit,
        values={
            "id": session_thread_id,
            "account_id": flow.account_id,
            "flow_id": flow.id,
            "tracker_id": tracker_uuid,
            "repository_id": str(repository_id),
            "pr_number": parts[-1],
            "pr_url": pr_url,
            "provider": provider,
            "branch": branch,
            "context": context,
            "policy": policy,
            "latest_execution_id": execution.id,
            "active_execution_id": execution.id,
            "due_at": now + timedelta(seconds=int(policy.get("debounce_seconds", 30))),
            "expires_at": now + timedelta(hours=int(policy.get("max_age_hours", 168))),
        },
    )


# A launch that dies before the agent runs. STOPPED, CANCELLED, and ABORTED
# stop the thread on purpose and are not retried here. The revival scan uses
# the same ``SESSIONLESS_RETRY_STATUSES`` constant.
_SESSIONLESS_RETRY_STATUSES = SESSIONLESS_RETRY_STATUSES


def native_session(execution: models.FlowExecution) -> dict[str, Any]:
    """Return the stored session dict, or an empty dict when none was stored."""
    session = execution.cli_session
    return session if isinstance(session, dict) else {}


def execution_has_native_session(execution: models.FlowExecution) -> bool:
    """True when the execution stored a session id or a checkpoint artifact.

    An artifact without a session id still counts. That is a broken identity,
    not permission to start a fresh conversation.
    """
    session = native_session(execution)
    return bool(session.get("session_id") or session.get("artifact_reference"))


def sessionless_retry(execution: models.FlowExecution) -> bool:
    """True when this execution died before it stored a conversation to resume."""
    return execution.status in _SESSIONLESS_RETRY_STATUSES and (
        not execution_has_native_session(execution)
    )


# Backoff after a repair that never reached an agent: base * 4**n seconds,
# capped. One review must produce one resume, not one per reconciliation.
LAUNCH_RETRY_BASE_SECONDS = 60
LAUNCH_RETRY_MAX_SECONDS = 6 * 3600


def launch_never_started(execution: models.FlowExecution) -> bool:
    """True when a failed repair ended before any agent work happened.

    A refused resume (``resume_failed: ...``) or a launch error fails during
    initialization: no session, no runtime reference and no tokens. A run
    that reached the model (an OOM 25 minutes in) spent real work and keeps
    its turn.
    """
    return (
        sessionless_retry(execution)
        and not execution.agent_session_reference
        and not (execution.total_tokens or 0)
    )


def launch_retry_delay(policy: dict[str, Any] | None, failures: int) -> int:
    """Seconds to wait before re-dispatching after ``failures`` launch failures."""
    base = max(
        int((policy or {}).get("debounce_seconds", 30) or 0),
        LAUNCH_RETRY_BASE_SECONDS,
    )
    return int(min(LAUNCH_RETRY_MAX_SECONDS, base * 4 ** max(failures, 1)))


def resolve_native_checkpoint(
    db: Session,
    *,
    account_id: uuid.UUID,
    flow_id: uuid.UUID,
    execution_id: uuid.UUID,
    resume: dict[str, Any],
) -> dict[str, Any] | None:
    """Version-one resolver: only the controller's reserved turn can resume.

    Never grant access from trigger JSON alone. The stored thread, execution
    reservation and latest checkpoint must agree even within one account/flow.
    """
    if not resume.get("thread_id"):
        return None
    thread = crud_flow_feedback.owned_thread(
        db,
        thread_id=uuid.UUID(str(resume["thread_id"])),
        account_id=account_id,
        flow_id=flow_id,
    )
    if (
        thread is None
        or thread.active_execution_id != execution_id
        or str(thread.latest_execution_id) != str(resume.get("execution_id"))
    ):
        raise ValueError("resume_failed: checkpoint binding mismatch")
    prior = crud_flow_execution.get(
        db, id=thread.latest_execution_id, account_id=account_id
    )
    if prior is None or prior.flow_id != flow_id:
        raise ValueError("resume_failed: checkpoint execution mismatch")

    def published_branch() -> dict[str, Any]:
        if (
            resume.get("pr_url") != thread.pr_url
            or resume.get("source_branch") != thread.branch
        ):
            raise ValueError("resume_failed: published branch binding mismatch")
        return {"cold_handoff_authorized": True}

    # Explicit adoption, a publisher/repair that never stored a recoverable
    # checkpoint, or Cloud with uploads off: review continues on the published
    # branch. A session id alone is not recoverable without an uploaded
    # native_session artifact. Fail closed only when an artifact was stored
    # but is expired, mismatched, or bound to the wrong execution.
    session = native_session(prior)
    reference = session.get("artifact_reference") or {}
    has_artifact_ref = bool(reference.get("artifact_id"))
    if (
        source_cold_handoff(thread, prior.id)
        or sessionless_retry(prior)
        or not has_artifact_ref
        or not settings.flow_artifact_direct_upload
    ):
        return published_branch()
    from preloop.agents.cli_session import valid_session_id

    if not valid_session_id(
        session.get("agent_type", ""), session.get("session_id", "")
    ):
        raise ValueError("resume_failed: invalid native session identity")
    from preloop.models.crud import flow_artifact
    from preloop.services.flow_artifacts import artifact_reference

    try:
        artifact = flow_artifact.get(
            db,
            artifact_id=uuid.UUID(str(reference.get("artifact_id"))),
            account_id=account_id,
            flow_id=flow_id,
            thread_id=str(thread.id),
        )
    except (ValueError, TypeError):
        artifact = None
    if (
        artifact is None
        or artifact.kind != "native_session"
        or artifact.execution_id != prior.id
        or artifact.ciphertext is None
        or artifact.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        or str(reference.get("execution_id")) != str(prior.id)
        or reference.get("manifest_sha256")
        != artifact_reference(artifact).manifest_sha256
    ):
        raise ValueError("resume_failed: native checkpoint unavailable")
    if session.get("thread_id") and session["thread_id"] != str(thread.id):
        raise ValueError("resume_failed: native session thread mismatch")
    return dict(session)


def source_cold_handoff(thread: models.FlowThread, source_id: uuid.UUID) -> bool:
    """The explicit exception belongs only to the adopted source checkpoint."""
    adoption = (thread.context or {}).get("adoption") or {}
    return adoption.get("recovery_mode") == "published_branch_handoff" and adoption.get(
        "source_execution_id"
    ) == str(source_id)


def ingest_feedback(db: Session, event: dict[str, Any]) -> bool:
    """Store delivery metadata before intake filtering; reconciliation reads content."""
    if event.get("type") not in FEEDBACK_TYPES:
        return False
    payload = event.get("payload") or {}
    repo = payload.get("repository") or payload.get("project") or {}
    provider = str(event.get("source") or "").lower()
    repo_id = _repository_identity(provider, repo)
    if not repo_id or not event.get("account_id") or not event.get("tracker_id"):
        return False
    pr = (
        payload.get("pull_request")
        or payload.get("merge_request")
        or payload.get("pullrequest")
        or payload.get("issue")
        or {}
    )
    number = pr.get("number") or pr.get("iid")
    if not number and provider == "bitbucket":
        # Bitbucket pull request payloads carry the number as ``id``.
        number = pr.get("id")
    if not number:
        # Commit-level check_run/status payloads omit a PR. Finding without a
        # number would wake every thread on the repository.
        return False
    threads = crud_flow_feedback.find(
        db,
        account_id=uuid.UUID(str(event["account_id"])),
        tracker_id=uuid.UUID(str(event["tracker_id"])),
        repository_id=str(repo_id),
        pr_number=str(number),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    delivery = event.get("delivery_id")
    identity = (
        str(delivery)
        if delivery
        else hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
    )
    for thread in threads:
        crud_flow_feedback.ingest(
            db,
            thread_id=thread.id,
            now=now,
            events=[
                {
                    "event_key": "delivery:" + identity,
                    "delivery_id": str(delivery) if delivery else None,
                    "kind": "signal",
                    "head_sha": None,
                    "payload": {"type": event["type"]},
                }
            ],
        )
    return bool(threads)


def decide(
    thread: Any, state: FeedbackState, pending: list[Any], *, now: datetime
) -> tuple[str, str | None]:
    """Pure policy decision; ready always means current-head CI and review passed."""
    if state.closed:
        return "closed", "pr_closed_or_merged"
    if now >= thread.expires_at:
        return "expired", "subscription_expired"
    requirements_unknown = state.blocked_reason == "repository_requirements_unavailable"
    if state.blocked_reason and not state.checks_pending and not requirements_unknown:
        return "blocked", state.blocked_reason
    if state.checks_pending:
        started = (thread.cursor or {}).get("ci_wait_started")
        deadline = int(thread.policy.get("ci_deadline_seconds", 3600))
        if (
            started
            and (now - datetime.fromisoformat(started)).total_seconds() >= deadline
        ):
            return "blocked", "ci_deadline_exceeded"
        if not thread.policy.get("repair_early", False):
            return "waiting", "ci_pending"
    actionable = [
        item
        for item in pending
        if item.kind != "signal"
        and (not item.head_sha or item.head_sha == state.head_sha)
    ]
    if (
        not actionable
        and state.checks_passed
        and state.reviews_passed
        and not state.blocked_reason
    ):
        return "ready", None
    if not actionable and state.infra_failures:
        # Provider infrastructure failures are not the branch's defect. Wait for
        # the platform a bounded number of reconciliations, then ask a human.
        attempts = int((thread.cursor or {}).get("ci_infra_attempts", 0))
        reason = (
            "ci_timeout"
            if any(classify_failure(item) == "timeout" for item in state.infra_failures)
            else "ci_infrastructure_failure"
        )
        if attempts > int(thread.policy.get("max_ci_infra_retries", 3)):
            return "blocked", reason
        return "waiting", f"{reason}_retry"
    if thread.turns >= int(thread.policy.get("max_turns", 5)):
        return "stopped", "turn_budget_exhausted"
    if thread.cost >= float(thread.policy.get("max_cost", 100)):
        return "stopped", "cost_budget_exhausted"
    if thread.no_progress >= int(thread.policy.get("max_no_progress", 2)):
        return "stopped", "no_progress"
    if actionable:
        return "repair", None
    if requirements_unknown:
        return "blocked", state.blocked_reason
    if state.checks_passed and state.reviews_passed and not state.blocked_reason:
        return "ready", None
    return "waiting", "review_or_ci_pending"


async def run_feedback_tick(db: Session, *, now: datetime | None = None) -> int:
    """Reconcile a bounded batch; leased execution reservation survives dispatch loss."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    for publication in crud_flow_feedback.unregistered_publications(db):
        try:
            result = publication.result or {}
            if result.get("pr_source_branch"):
                register_thread(
                    db, publication, result["pr_url"], result["pr_source_branch"]
                )
        except Exception:
            crud_flow_feedback.rollback(db)
            logger.exception(
                "Feedback registration failed for execution %s",
                getattr(publication, "id", None),
            )
    for thread in crud_flow_feedback.stopped_for_no_progress(db):
        crud_flow_feedback.revive(db, thread.id, now=now)
    claims = crud_flow_feedback.claim_due(db, now=now)
    for thread_id, token in claims:
        try:
            await _reconcile(db, thread_id, token, now=now)
        except Exception:
            crud_flow_feedback.rollback(db)
            logger.exception("Feedback reconciliation failed for thread %s", thread_id)
            crud_flow_feedback.update(
                db,
                thread_id,
                token,
                changes={
                    "state": "blocked",
                    "stop_reason": "provider_or_dispatch_unavailable",
                },
                now=now,
            )
    return len(claims)


async def _reconcile(
    db: Session, thread_id: uuid.UUID, token: uuid.UUID, *, now: datetime
) -> None:
    thread = crud_flow_feedback.leased(db, thread_id, token)
    if thread is None:
        return
    flow = crud_flow.get(db, id=thread.flow_id)
    if flow is None or flow.account_id != thread.account_id or not flow.is_enabled:
        crud_flow_feedback.update(
            db,
            thread_id,
            token,
            changes={"state": "stopped", "stop_reason": "flow_disabled"},
            now=now,
        )
        return
    if feedback_policy(flow) is None:
        # Keep budget/deadline history so an explicit re-enable can continue.
        crud_flow_feedback.update(
            db,
            thread_id,
            token,
            changes={"state": "paused", "stop_reason": "feedback_disabled"},
            now=now,
        )
        return
    policy = deepcopy(feedback_policy(flow))
    crud_flow_feedback.sync_policy(db, thread, policy)
    provider = await FeedbackProvider.for_thread(db, thread)
    state = await provider.read()
    if state.closed or now >= thread.expires_at:
        reason = "pr_closed_or_merged" if state.closed else "subscription_expired"
        active_id = crud_flow_feedback.stop_active(db, thread, reason=reason, now=now)
        if active_id:
            from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
            from preloop.sync.services.event_bus import get_nats_client

            try:
                await FlowExecutionOrchestrator.send_command(
                    str(active_id), "stop", {"reason": reason}, await get_nats_client()
                )
            except (RuntimeError, OSError):
                logger.warning(
                    "Live stop signal unavailable; cancellation is persisted for %s",
                    active_id,
                )
        crud_flow_feedback.update(
            db,
            thread_id,
            token,
            changes={
                "state": "closed" if state.closed else "expired",
                "stop_reason": reason,
            },
            now=now,
        )
        return
    completed_execution = thread.active_execution_id is not None
    completed_repair = completed_execution and thread.turns > 0
    if not crud_flow_feedback.finish_active(db, thread):
        crud_flow_feedback.update(db, thread_id, token, changes={}, now=now)
        return
    launch_failed = False
    if completed_repair:
        finished = crud_flow_execution.get(db, id=thread.latest_execution_id)
        launch_failed = finished is not None and launch_never_started(finished)
        if launch_failed:
            # No model work was spent, so the turn is given back.
            thread.turns = max(0, thread.turns - 1)
        # The agent never ran, so an unchanged head is not a failed repair.
        if finished is None or not sessionless_retry(finished):
            thread.no_progress = (
                thread.no_progress + 1 if thread.head_sha == state.head_sha else 0
            )
    # A launch that died before the agent ran already consumed its reviews.
    # Ingest will not reopen a receipt, so put those reviews back. The next
    # reservation continues from the published branch.
    if thread.active_execution_id is None:
        failed = crud_flow_execution.get(db, id=thread.latest_execution_id)
        if failed is not None and sessionless_retry(failed):
            crud_flow_feedback.release_consumed(db, thread.id, failed.id)
    if completed_execution:
        prior_execution = crud_flow_execution.get(db, id=thread.latest_execution_id)
        # Explicit adoption authorizes continuing this historical publication,
        # including one published before its source execution was cancelled.
        # That permission never extends to a later cancelled repair.
        adoption = (thread.context or {}).get("adoption") or {}
        adopted_source = thread.turns == 0 and adoption.get(
            "source_execution_id"
        ) == str(thread.latest_execution_id)
        if (
            not adopted_source
            and prior_execution
            and prior_execution.status
            in {
                "STOPPED",
                "CANCELLED",
                "ABORTED",
            }
        ):
            crud_flow_feedback.update(
                db,
                thread_id,
                token,
                changes={"state": "stopped", "stop_reason": "execution_cancelled"},
                now=now,
            )
            return
    if state.blocked_reason in {
        "provider_page_limit",
        "head_changed_during_reconciliation",
    }:
        crud_flow_feedback.update(
            db,
            thread_id,
            token,
            changes={
                "state": "blocked"
                if state.blocked_reason == "provider_page_limit"
                else "waiting",
                "stop_reason": state.blocked_reason,
                "head_sha": state.head_sha,
            },
            now=now,
        )
        return
    crud_flow_feedback.ingest(db, thread_id=thread.id, events=state.feedback, now=now)
    crud_flow_feedback.acknowledge_observed(
        db,
        thread,
        head_sha=state.head_sha,
        present_keys=[event["event_key"] for event in state.feedback],
    )
    pending = crud_flow_feedback.pending(db, thread.id)
    cursor = dict(thread.cursor or {})
    if cursor.get("head_sha") != state.head_sha or completed_repair:
        cursor.pop("ci_wait_started", None)
        cursor.pop("feedback_ready_at", None)
        cursor.pop("ci_infra_attempts", None)
    if launch_failed:
        # The repair never reached an agent (resume refused, launch error).
        # Its turn was given back above; the next attempt waits out a growing
        # backoff instead of re-dispatching the same review every tick.
        failures = int(cursor.get("launch_failures", 0)) + 1
        cursor["launch_failures"] = failures
        cursor["feedback_ready_at"] = (
            now + timedelta(seconds=launch_retry_delay(thread.policy, failures))
        ).isoformat()
    elif completed_execution:
        cursor.pop("launch_failures", None)
    if state.infra_failures:
        # Counted per head: a new head starts a fresh infrastructure allowance.
        cursor["ci_infra_attempts"] = int(cursor.get("ci_infra_attempts", 0)) + 1
    else:
        cursor.pop("ci_infra_attempts", None)
    thread.cursor = cursor
    outcome, reason = decide(thread, state, pending, now=now)
    if outcome == "repair":
        ready_at = cursor.setdefault(
            "feedback_ready_at",
            (
                now + timedelta(seconds=int(thread.policy.get("debounce_seconds", 30)))
            ).isoformat(),
        )
        if now < datetime.fromisoformat(ready_at):
            outcome, reason = (
                "waiting",
                (
                    "resume_launch_retry"
                    if cursor.get("launch_failures")
                    else "feedback_debounce"
                ),
            )
    if state.checks_pending:
        if cursor.get("head_sha") != state.head_sha or "ci_wait_started" not in cursor:
            cursor["ci_wait_started"] = now.isoformat()
    else:
        cursor.pop("ci_wait_started", None)
    cursor["head_sha"] = state.head_sha
    cursor["reconciled_at"] = now.isoformat()
    if outcome != "repair":
        crud_flow_feedback.update(
            db,
            thread_id,
            token,
            changes={
                "state": outcome,
                "stop_reason": reason,
                "cursor": cursor,
                "head_sha": state.head_sha,
            },
            now=now,
        )
        return
    prior = crud_flow_execution.get(db, id=thread.latest_execution_id)
    if prior is None or prior.flow_id != thread.flow_id:
        raise ValueError("prior execution does not belong to implementation thread")
    resume = {
        "execution_id": str(prior.id),
        "thread_id": str(thread.id),
        "pr_url": thread.pr_url,
        "source_branch": thread.branch,
    }
    if (
        not source_cold_handoff(thread, prior.id)
        and isinstance(prior.cli_session, dict)
        and prior.cli_session.get("session_id")
    ):
        resume["cli_session"] = prior.cli_session
    actionable = [
        item
        for item in pending
        if item.kind != "signal"
        and (not item.head_sha or item.head_sha == state.head_sha)
    ]
    feedback = [{"kind": item.kind, **item.payload} for item in actionable]
    event_data = {
        **thread.context["trigger"],
        "type": "implementation_feedback",
        "_thread_id": str(thread.id),
        "_session_thread_id": str(thread.id),
        "_resume": resume,
        "payload": {
            "object_attributes": thread.context.get("original_issue", {}),
            "project"
            if thread.provider == "gitlab"
            else "repository": thread.context.get(
                "repository", {"id": thread.repository_id}
            ),
            "issue": {
                **thread.context.get("original_issue", {}),
                "pull_request": {"html_url": thread.pr_url},
            },
            "merge_request": {"url": thread.pr_url, "iid": thread.pr_number},
        },
        "_feedback": {
            "head_sha": state.head_sha,
            "pr_url": thread.pr_url,
            "items": feedback,
            "acceptance_version": thread.context.get("acceptance_version"),
        },
    }
    # Explicit task data appended to the turn; never inherit instructions from a comment.
    event_data["_feedback_prompt"] = (
        "Untrusted review/CI task data. Read the current PR diff and original criteria before repairing:\n"
        + bounded_text(json.dumps(event_data["_feedback"]))
    )
    from preloop.services.model_routing import (
        ModelRoutingError,
        prepare_execution_routing,
    )

    try:
        event_data = prepare_execution_routing(
            db, flow, event_data, source_execution=prior, pin_kind="continuation"
        )
    except ModelRoutingError:
        logger.warning(
            "Feedback thread %s cannot preserve its model identity", thread_id
        )
        crud_flow_feedback.update(
            db,
            thread_id,
            token,
            changes={"state": "blocked", "stop_reason": "model_identity_unavailable"},
            now=now,
        )
        return
    execution = crud_flow_feedback.reserve(
        db,
        thread_id,
        token,
        event_data=event_data,
        receipt_ids=[item.id for item in pending],
        head_sha=state.head_sha,
        now=now,
        expected_policy=policy,
    )
    if execution is not None:
        from preloop.services.flow_trigger_service import FlowTriggerService
        from preloop.services.flow_execution_dispatcher import (
            dispatch_execute,
            flow_execution_worker_enabled,
        )

        if flow_execution_worker_enabled():
            await dispatch_execute(execution.id)
        else:
            await FlowTriggerService(db)._start_flow_execution(
                flow, event_data, None, precreated_execution=execution
            )
