"""Read-only Bitbucket Cloud policy evaluation with explicit unknown gates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable, Protocol, Literal
from uuid import UUID, uuid4

from preloop.schemas.readiness import (
    GateEvidence,
    ReadinessObservation,
    ReadinessPolicy,
    GateState,
)
from preloop.sync.exceptions import TrackerError


class BitbucketReadClient(Protocol):
    """Bound credential-resolving tracker transport, not a parallel client."""

    async def get_pull_request(
        self, pr_id: int | str, repo_full_name: str | None = None
    ) -> dict[str, Any]: ...

    async def get_readiness_statuses(
        self, sha: str, repo_full_name: str
    ) -> list[dict[str, Any]]: ...

    async def get_readiness_tasks(
        self, pr_id: int, repo_full_name: str
    ) -> list[dict[str, Any]]: ...


class ConflictProbe(Protocol):
    """A separately isolated, credential-free immutable-commit calculation."""

    async def assess(
        self, repository: str, source_sha: str, target_sha: str
    ) -> GateEvidence: ...


def identities(pr: dict[str, Any]) -> tuple[str | None, str | None]:
    """Immutable identity pair; missing identities cannot establish readiness."""

    def commit_hash(side: str) -> str | None:
        endpoint = pr.get(side)
        commit = endpoint.get("commit") if isinstance(endpoint, dict) else None
        value = commit.get("hash") if isinstance(commit, dict) else None
        return value if isinstance(value, str) and value else None

    return commit_hash("source"), commit_hash("destination")


def _status_state(statuses: list[dict[str, Any]], key: str) -> tuple[GateState, str]:
    matches = [status for status in statuses if status.get("key") == key]
    if not matches:
        return "unknown", "missing_required_key"
    # The REST resource updates a status by key; duplicates require reliable
    # ordering, never whichever page happened to arrive last.
    if len(matches) > 1:
        from preloop.schemas.readiness import parse_jira_created

        ordered = [(parse_jira_created(s.get("updated_on")), s) for s in matches]
        if any(stamp is None for stamp, _ in ordered):
            return "unknown", "ambiguous_status"
        ordered.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC))
        if ordered[-1][0] == ordered[-2][0]:
            return "unknown", "ambiguous_status"
        status = ordered[-1][1]
    else:
        status = matches[0]
    state = status.get("state")
    if state == "SUCCESSFUL":
        return "pass", "successful"
    if state in {"FAILED", "STOPPED", "INPROGRESS", "PENDING", "QUEUED"}:
        return "fail", "pending_build" if state in {
            "INPROGRESS",
            "PENDING",
            "QUEUED",
        } else "failed_build"
    return "unknown", "unsupported_status"


async def observe_bitbucket(
    client: BitbucketReadClient,
    probe: ConflictProbe,
    *,
    account_id: UUID,
    tracker_id: UUID,
    repository: str,
    pr_id: int,
    policy: ReadinessPolicy | None,
    scope: Literal["configured_policy", "provider_enforced"] = "configured_policy",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ReadinessObservation:
    """Evaluate sampled gates and invalidate a moved commit pair."""
    started = clock()
    gates: list[GateEvidence] = []
    source: str | None = None
    target: str | None = None
    reasons: list[str] = []

    def add(name: str, state: GateState, reason: str | None, origin: str) -> None:
        gates.append(
            GateEvidence(
                name=name,
                state=state,
                reason=reason,
                source=origin,
                retrieved_at=clock(),
                source_sha=source,
                target_sha=target,
            )
        )

    def result(unsupported: bool = False) -> ReadinessObservation:
        complete = bool(gates) and all(g.state != "unknown" for g in gates)
        failed = any(g.state == "fail" for g in gates)
        invalidated = any(
            r in {"commits_changed", "identity_unavailable"} for r in reasons
        )
        return ReadinessObservation(
            observation_id=uuid4(),
            account_id=account_id,
            tracker_id=tracker_id,
            repository=repository,
            pr_id=pr_id,
            source_sha=source,
            target_sha=target,
            policy_version=policy.version if policy else None,
            scope=scope,
            started_at=started,
            completed_at=clock(),
            state="unknown"
            if invalidated
            else "not_ready"
            if failed
            else "ready"
            if complete
            else "unknown",
            coverage="unsupported"
            if unsupported
            else "complete"
            if complete and not invalidated
            else "partial",
            gates=tuple(gates),
            reasons=tuple(
                reasons + [g.reason for g in gates if g.reason and g.state != "pass"]
            ),
        )

    if scope == "provider_enforced":
        reasons.append("provider_enforced_unsupported")
        return result(True)
    if policy is None:
        reasons.append("policy_unconfigured")
        return result(True)
    try:
        pr = await client.get_pull_request(pr_id, repository)
    except (TrackerError, ValueError, KeyError):
        reasons.append("pr_unavailable")
        return result()
    source, target = identities(pr)
    if not source or not target:
        reasons.append("identity_unavailable")
        return result()
    add(
        "open",
        "pass" if pr.get("state") == "OPEN" else "fail",
        "pr_not_open" if pr.get("state") != "OPEN" else None,
        "pullrequest.state",
    )
    draft = pr.get("draft")
    add(
        "non_draft",
        "pass" if draft is False else "fail" if draft is True else "unknown",
        "draft" if draft is True else "draft_unavailable" if draft is None else None,
        "pullrequest.draft",
    )
    participants = pr.get("participants")
    if not isinstance(participants, list):
        add(
            "approvals",
            "unknown",
            "participants_unavailable",
            "pullrequest.participants",
        )
        if policy.changes_requests_block:
            add(
                "changes_requests",
                "unknown",
                "participants_unavailable",
                "pullrequest.participants",
            )
    else:
        # Each UUID contributes at most once; anonymous/malformed reviewers
        # cannot silently make the approval gate pass.
        approved: set[str] = set()
        malformed = False
        for participant in participants:
            if participant.get("approved"):
                identity = (participant.get("user") or {}).get("uuid")
                if identity:
                    approved.add(identity)
                else:
                    malformed = True
        state: GateState = (
            "pass"
            if len(approved) >= policy.minimum_approvals
            else "unknown"
            if malformed
            else "fail"
        )
        add(
            "approvals",
            state,
            "insufficient_approvals"
            if state == "fail"
            else "participants_unavailable"
            if state == "unknown"
            else None,
            "pullrequest.participants",
        )
        if policy.changes_requests_block:
            declined = any(p.get("state") == "changes_requested" for p in participants)
            add(
                "changes_requests",
                "fail" if declined else "pass",
                "changes_requested" if declined else None,
                "pullrequest.participants",
            )
    if policy.unresolved_tasks_block:
        try:
            tasks = await client.get_readiness_tasks(pr_id, repository)
            unresolved = any(t.get("state") == "UNRESOLVED" for t in tasks)
            malformed = any(
                t.get("state") not in {"RESOLVED", "UNRESOLVED"} for t in tasks
            )
            add(
                "tasks",
                "fail" if unresolved else "unknown" if malformed else "pass",
                "unresolved_task"
                if unresolved
                else "tasks_unavailable"
                if malformed
                else None,
                "pullrequest.tasks",
            )
        except (TrackerError, ValueError, KeyError):
            add("tasks", "unknown", "tasks_unavailable", "pullrequest.tasks")
    if policy.required_build_keys:
        try:
            statuses = await client.get_readiness_statuses(source, repository)
            # A status from a different commit never counts for this source.
            statuses = [
                s
                for s in statuses
                if (s.get("commit") or {}).get("hash", source) == source
            ]
            for key in policy.required_build_keys:
                state, reason = _status_state(statuses, key)
                add(f"build:{key}", state, reason, "commit.statuses")
        except (TrackerError, ValueError, KeyError):
            for key in policy.required_build_keys:
                add(
                    f"build:{key}", "unknown", "statuses_unavailable", "commit.statuses"
                )
    gates.append(await probe.assess(repository, source, target))
    try:
        last = await client.get_pull_request(pr_id, repository)
        if identities(last) != (source, target):
            reasons.append("commits_changed")
    except (TrackerError, ValueError, KeyError):
        reasons.append("identity_unavailable")
    return result()
