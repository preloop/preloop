"""Durable implementation turns: runners work, the scheduler waits for feedback."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution, crud_flow_feedback
from preloop.services.flow_feedback_provider import (
    FeedbackProvider,
    FeedbackState,
    bounded_text,
)

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
    }
)


def feedback_policy(flow: Any) -> dict[str, Any] | None:
    """Existing saved flows opt in explicitly; preset updates never overwrite them."""
    config = getattr(flow, "agent_config", None)
    policy = config.get("feedback") if isinstance(config, dict) else None
    return (
        policy if isinstance(policy, dict) and policy.get("enabled") is True else None
    )


def register_thread(
    db: Session, execution: models.FlowExecution, pr_url: str, branch: str
) -> None:
    """Register after trusted publication; initial reconciliation recovers races."""
    flow = crud_flow.get(db, id=execution.flow_id)
    policy = feedback_policy(flow)
    if flow is None or not flow.account_id or policy is None:
        return
    details = execution.trigger_event_details or {}
    if details.get("_thread_id"):
        return
    payload = details.get("payload") or {}
    repository = payload.get("repository") or payload.get("project") or {}
    repository_id = repository.get("id")
    tracker_id = details.get("tracker_id") or flow.trigger_event_source
    provider = details.get("source")
    parsed = urlparse(pr_url)
    parts = parsed.path.rstrip("/").split("/")
    if (
        not repository_id
        or not tracker_id
        or provider not in {"github", "gitlab"}
        or not parts[-1].isdigit()
    ):
        logger.warning("Cannot bind feedback: missing provider repository identity")
        return
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
        "trigger": {
            **{
                key: details[key]
                for key in ("project_id", "project_path", "issue_id")
                if key in details
            },
            "source": provider,
            "tracker_id": str(tracker_id),
            "account_id": str(flow.account_id),
        },
    }
    crud_flow_feedback.register(
        db,
        values={
            "id": uuid.UUID(details["_session_thread_id"])
            if details.get("_session_thread_id")
            else uuid.uuid4(),
            "account_id": flow.account_id,
            "flow_id": flow.id,
            "tracker_id": uuid.UUID(str(tracker_id)),
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
    session = prior.cli_session
    if not isinstance(session, dict):
        return None
    if session.get("thread_id") and session["thread_id"] != str(thread.id):
        raise ValueError("resume_failed: native session thread mismatch")
    return dict(session)


def ingest_feedback(db: Session, event: dict[str, Any]) -> bool:
    """Store delivery metadata before intake filtering; reconciliation reads content."""
    if event.get("type") not in FEEDBACK_TYPES:
        return False
    payload = event.get("payload") or {}
    repo = payload.get("repository") or payload.get("project") or {}
    if not repo.get("id") or not event.get("account_id") or not event.get("tracker_id"):
        return False
    pr = (
        payload.get("pull_request")
        or payload.get("merge_request")
        or payload.get("issue")
        or {}
    )
    number = pr.get("number") or pr.get("iid")
    threads = crud_flow_feedback.find(
        db,
        account_id=uuid.UUID(str(event["account_id"])),
        tracker_id=uuid.UUID(str(event["tracker_id"])),
        repository_id=str(repo["id"]),
        pr_number=str(number) if number else None,
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
    if state.blocked_reason and not state.checks_pending:
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
    if thread.turns >= int(thread.policy.get("max_turns", 5)):
        return "stopped", "turn_budget_exhausted"
    if thread.cost >= float(thread.policy.get("max_cost", 100)):
        return "stopped", "cost_budget_exhausted"
    if thread.no_progress >= int(thread.policy.get("max_no_progress", 2)):
        return "stopped", "no_progress"
    if actionable:
        return "repair", None
    if state.checks_passed and state.reviews_passed and not state.blocked_reason:
        return "ready", None
    return "waiting", "review_or_ci_pending"


async def run_feedback_tick(db: Session, *, now: datetime | None = None) -> int:
    """Reconcile a bounded batch; leased execution reservation survives dispatch loss."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    for publication in crud_flow_feedback.unregistered_publications(db):
        result = publication.result or {}
        if result.get("pr_source_branch"):
            register_thread(
                db, publication, result["pr_url"], result["pr_source_branch"]
            )
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
    completed_repair = thread.active_execution_id is not None and thread.turns > 0
    if not crud_flow_feedback.finish_active(db, thread):
        crud_flow_feedback.update(db, thread_id, token, changes={}, now=now)
        return
    if completed_repair:
        thread.no_progress = (
            thread.no_progress + 1 if thread.head_sha == state.head_sha else 0
        )
        prior_execution = crud_flow_execution.get(db, id=thread.latest_execution_id)
        if prior_execution and prior_execution.status == "STOPPED":
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
            outcome, reason = "waiting", "feedback_debounce"
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
    if isinstance(prior.cli_session, dict) and prior.cli_session.get("session_id"):
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
            "repository"
            if thread.provider == "github"
            else "project": thread.context.get(
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
    execution = crud_flow_feedback.reserve(
        db,
        thread_id,
        token,
        event_data=event_data,
        receipt_ids=[item.id for item in pending],
        head_sha=state.head_sha,
        now=now,
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
